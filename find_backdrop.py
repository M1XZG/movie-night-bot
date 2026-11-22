#!/usr/bin/env python3
"""Fetch and cache a validated landscape backdrop using the official TMDB API."""

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PIL import Image, ImageOps, UnidentifiedImageError

from ratings import (
    IMDB_ID, _lookup_deadline, _normal_title, _read_response, _remaining_timeout, _retry_delay,
    _search_titles, _wait_for_retry,
)

API = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w1280"
TMP = Path(os.environ.get("MOVIE_BOT_TMP", Path(__file__).resolve().parent / "tmp"))
MAX_JSON_BYTES = 1_000_000
MAX_IMAGE_BYTES = 8_000_000
TIMEOUT = 8
MAX_SEARCH_PAGES = 5
LOOKUP_TIMEOUT = 50
MAX_IMAGE_CANDIDATES = 5
CACHE_VERSION = 2
CACHE_MAX_AGE = 30 * 24 * 60 * 60
IMAGE_PATH = re.compile(r"/[A-Za-z0-9_-]+\.(?:jpg|png)\Z")


class BackdropError(Exception):
    """A safe-to-display image failure, without credentials or request URLs."""


class BackdropIdentityError(BackdropError):
    """A ratings-inferred identity failed the independent TMDB check."""


@dataclass(frozen=True)
class Credential:
    value: str
    bearer: bool = True


def load_credential() -> Credential:
    for name, bearer in (("TMDB_READ_ACCESS_TOKEN", True), ("TMDB_API_KEY", False)):
        value = os.environ.get(name, "").strip()
        if value:
            return _credential(value, bearer)
    for name, bearer in (("TMDB_TOKEN_FILE", True), ("TMDB_API_KEY_FILE", False)):
        filename = os.environ.get(name)
        if filename:
            try:
                value = Path(filename).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError, ValueError):
                raise BackdropError("The TMDB credential file could not be read.") from None
            return _credential(value, bearer)
    entry = os.environ.get("TMDB_PASS_ENTRY", "api/tmdb")
    if not entry.strip():
        raise BackdropError("The TMDB pass entry name is empty.")
    env = dict(os.environ)
    env["PASSWORD_STORE_GPG_OPTS"] = (
        env.get("PASSWORD_STORE_GPG_OPTS", "") + " --batch --pinentry-mode error"
    ).strip()
    try:
        result = subprocess.run(
            ["pass", "show", "--", entry], capture_output=True, text=True,
            timeout=10, env=env,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise BackdropError("The TMDB credential could not be read from pass.") from None
    if result.returncode:
        raise BackdropError(
            "The TMDB credential is unavailable; configure api/tmdb in pass."
        )
    lines = result.stdout.splitlines()
    value = lines[0].strip() if lines else ""
    return _credential(value, not bool(re.fullmatch(r"[0-9a-fA-F]{32}", value)))


def _credential(value: str, bearer: bool) -> Credential:
    if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise BackdropError("TMDB needs one non-empty API credential.")
    return Credential(value, bearer)


def _read(request: Request, limit: int, *, provider: str) -> bytes:
    for attempt in range(2):
        try:
            with urlopen(request, timeout=_remaining_timeout(TIMEOUT)) as response:
                data = _read_response(response, limit, TIMEOUT)
            if len(data) > limit:
                raise BackdropError(f"{provider} returned an oversized response.")
            return data
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            code, delay = exc.code, _retry_delay(exc)
            exc.close()
            if not retryable or attempt:
                raise BackdropError(f"{provider} returned HTTP {code}.") from None
        except (URLError, TimeoutError, OSError):
            delay = 0.5
            if attempt:
                raise BackdropError(f"{provider} could not be reached.") from None
        try:
            _wait_for_retry(delay, LOOKUP_TIMEOUT)
        except TimeoutError:
            raise BackdropError(f"{provider} lookup timed out.") from None
    raise BackdropError(f"{provider} could not be reached.")


def _request(credential: Credential, path: str, **params) -> dict:
    headers = {"User-Agent": "MovieNightBot/1.0", "Accept": "application/json"}
    if credential.bearer:
        headers["Authorization"] = f"Bearer {credential.value}"
    else:
        params["api_key"] = credential.value
    raw = _read(
        Request(API + path + "?" + urlencode(params), headers=headers),
        MAX_JSON_BYTES, provider="TMDB",
    )
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BackdropError("TMDB returned invalid JSON.") from None
    if not isinstance(data, dict) or data.get("success") is False:
        raise BackdropError("TMDB did not return a successful lookup.")
    return data


def find_title(
    title: str, year: str | None, show_type: str, credential: Credential,
    imdb_id: str | None = None, *, imdb_id_explicit: bool = True,
) -> tuple[str, int]:
    _validate_lookup(title, year, show_type, imdb_id)
    kinds = {"movie": ("movie",), "tv": ("tv",)}.get(show_type, ("tv", "movie"))
    titles = _search_titles(title, show_type, year)
    if imdb_id:
        if not IMDB_ID.fullmatch(imdb_id):
            raise BackdropError("Use an IMDb identifier such as tt31349844.")
        data = _request(credential, f"/find/{imdb_id}", external_source="imdb_id")
        matches = set()
        for kind in kinds:
            results = data.get(f"{kind}_results")
            if not isinstance(results, list):
                raise BackdropError("TMDB returned invalid IMDb lookup results.")
            for item in results:
                if not isinstance(item, dict):
                    raise BackdropError("TMDB returned an invalid title.")
                identifier = item.get("id")
                if (
                    isinstance(identifier, bool)
                    or not isinstance(identifier, int)
                    or identifier <= 0
                ):
                    raise BackdropError("TMDB returned an invalid title identifier.")
                if not imdb_id_explicit and _matching_id(item, titles, year, kind) is None:
                    raise BackdropIdentityError(
                        "TMDB's inferred IMDb identity does not match the requested title/year; supply an explicit IMDb identifier."
                    )
                matches.add((kind, identifier))
        if len(matches) == 1:
            return next(iter(matches))
        error_type = BackdropError if imdb_id_explicit else BackdropIdentityError
        raise error_type(
            "TMDB could not uniquely match this IMDb identifier to the selected media type."
        )
    for search_title in titles:
        matches = {}
        for kind in kinds:
            params = {"query": search_title, "include_adult": "false"}
            if year:
                params["year" if kind == "movie" else "first_air_date_year"] = year
            data = _request(credential, f"/search/{kind}", **params)
            pages = data.get("total_pages", 1)
            if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
                raise BackdropError("TMDB returned an invalid search page count.")
            if pages > MAX_SEARCH_PAGES:
                raise BackdropError(
                    "TMDB returned too many candidates; use `/movie schedule imdb:` or a manual backdrop."
                )
            seen_pages = set()
            for page in range(1, max(pages, 1) + 1):
                if page > 1:
                    data = _request(credential, f"/search/{kind}", **params, page=page)
                if (data.get("page", page) != page
                        or data.get("total_pages", pages) != pages):
                    raise BackdropError("TMDB search pages changed; retry or supply an IMDb identifier.")
                results = data.get("results")
                if not isinstance(results, list):
                    raise BackdropError("TMDB returned invalid search results.")
                if pages == 0 and results:
                    raise BackdropError("TMDB returned invalid search results.")
                fingerprint = json.dumps(sorted(json.dumps(item, sort_keys=True) for item in results))
                if fingerprint in seen_pages:
                    raise BackdropError("TMDB repeated a search page; supply an IMDb identifier.")
                seen_pages.add(fingerprint)
                for item in results:
                    identifier = _matching_id(item, [search_title], year, kind)
                    if identifier is not None:
                        matches[kind, identifier] = None
        if len(matches) == 1:
            return next(iter(matches))
        if matches:
            raise BackdropError(
                "Multiple TMDB films/shows match this title and year; use `/movie schedule imdb:` or a manual backdrop."
            )
    raise BackdropError("No exact title/year match was found on TMDB; use a manual backdrop.")


def _matching_id(item, titles: list[str], year: str | None, kind: str) -> int | None:
    if not isinstance(item, dict):
        raise BackdropError("TMDB returned an invalid title.")
    names = (item.get("title"), item.get("original_title")) if kind == "movie" else (
        item.get("name"), item.get("original_name"),
    )
    if not any(isinstance(name, str) and _normal_title(name) == _normal_title(title)
               for name in names for title in titles):
        return None
    date = item.get("release_date" if kind == "movie" else "first_air_date")
    if (not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date)
            or (year is not None and date[:4] != year)):
        return None
    identifier = item.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        raise BackdropError("TMDB returned an invalid title identifier.")
    return identifier


def _backdrop_paths(kind: str, identifier: int, credential: Credential) -> list[str]:
    try:
        return _image_paths(_request(credential, f"/{kind}/{identifier}/images"))
    except BackdropError as image_error:
        # The canonical record can retain its cover when the images listing fails.
        try:
            data = _request(credential, f"/{kind}/{identifier}")
            path = data.get("backdrop_path")
            if (type(data.get("id")) is int and data["id"] == identifier
                    and isinstance(path, str) and IMAGE_PATH.fullmatch(path)):
                return [path]
        except BackdropError:
            pass
        raise image_error


def _image_paths(data: dict) -> list[str]:
    images = data.get("backdrops")
    if not isinstance(images, list):
        raise BackdropError("TMDB returned invalid backdrop information.")
    candidates = []
    for item in images:
        if not isinstance(item, dict):
            continue
        path, width, height = item.get("file_path"), item.get("width"), item.get("height")
        if not isinstance(path, str) or not IMAGE_PATH.fullmatch(path):
            continue
        if (isinstance(width, bool) or isinstance(height, bool)
                or not isinstance(width, int) or not isinstance(height, int)
                or width < 320 or height < 180 or not 1.5 <= width / height <= 2):
            continue
        candidates.append((
            item.get("iso_639_1") not in (None, "en"),
            abs(width / height - 16 / 9), -width, path,
        ))
    paths = list(dict.fromkeys(item[3] for item in sorted(candidates)))
    if not paths:
        raise BackdropError("TMDB has no suitable landscape backdrop for this title.")
    return paths


def repair_candidates(query: str, year: str | None, show_type: str) -> list[tuple[str, int, str]]:
    """Offer choices rather than treating the first fuzzy search result as authoritative."""
    with _lookup_deadline(LOOKUP_TIMEOUT):
        return _repair_candidates(query, year, show_type)


def _repair_candidates(query: str, year: str | None, show_type: str) -> list[tuple[str, int, str]]:
    _validate_lookup(query, year, show_type, None)
    credential = load_credential()
    kinds = {"movie": ("movie",), "tv": ("tv",)}.get(show_type, ("tv", "movie"))
    choices = []
    found = (_request(credential, f"/find/{query}", external_source="imdb_id")
             if IMDB_ID.fullmatch(query) else None)
    for kind in kinds:
        if found is not None:
            rows = found.get(f"{kind}_results", [])
        else:
            params = {"query": query, "include_adult": "false"}
            if year:
                params["year" if kind == "movie" else "first_air_date_year"] = year
            rows = _request(credential, f"/search/{kind}", **params).get("results", [])
        if not isinstance(rows, list):
            raise BackdropError("TMDB returned invalid repair candidates.")
        count = 0
        seen = set()
        for item in rows:
            if not isinstance(item, dict):
                raise BackdropError("TMDB returned an invalid repair candidate.")
            key = item.get("id")
            name = item.get("title" if kind == "movie" else "name")
            date = item.get("release_date" if kind == "movie" else "first_air_date") or ""
            if (type(key) is int and key > 0 and isinstance(name, str) and _normal_title(name)
                    and isinstance(date, str) and (not year or date[:4] == year)
                    and key not in seen):
                choices.append((kind, key, f"{name} ({date[:4] or '?'}, {kind})"))
                seen.add(key)
                count += 1
                if count == 8:
                    break
    return choices


def repair_details(kind: str, identifier: int) -> dict:
    if kind not in ("movie", "tv") or type(identifier) is not int or identifier <= 0:
        raise BackdropError("Select a valid TMDB movie or series.")
    with _lookup_deadline(LOOKUP_TIMEOUT):
        data = _request(load_credential(), f"/{kind}/{identifier}", append_to_response="credits,external_ids")
    if type(data.get("id")) is not int or data["id"] != identifier:
        raise BackdropError("TMDB did not confirm the selected title.")
    title = data.get("title" if kind == "movie" else "name")
    date = data.get("release_date" if kind == "movie" else "first_air_date")
    credits, external_ids = data.get("credits"), data.get("external_ids")
    if (not isinstance(title, str) or not _normal_title(title)
            or not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date)
            or not isinstance(credits, dict) or not isinstance(external_ids, dict)):
        raise BackdropError("TMDB returned invalid title details.")
    cast, crew = credits.get("cast", []), credits.get("crew", [])
    if not isinstance(cast, list) or not isinstance(crew, list):
        raise BackdropError("TMDB returned invalid credits.")
    imdb_id = external_ids.get("imdb_id")
    if imdb_id is not None and (not isinstance(imdb_id, str) or not IMDB_ID.fullmatch(imdb_id)):
        raise BackdropError("TMDB returned an invalid IMDb identifier.")
    overview, runtime = data.get("overview") or "", data.get("runtime")
    if (not isinstance(overview, str) or (runtime is not None
            and (type(runtime) is not int or runtime < 0))):
        raise BackdropError("TMDB returned invalid title details.")
    directors = [p["name"] for p in crew if isinstance(p, dict)
                 and p.get("job") == "Director" and isinstance(p.get("name"), str)]
    actors = [p["name"] for p in cast if isinstance(p, dict) and isinstance(p.get("name"), str)]
    return dict(title=title, year=date[:4], imdb_id=imdb_id,
                overview=overview, runtime=runtime,
                people=", ".join(directors[:1] + actors[:3]))


def repair_artwork(kind: str, identifier: int) -> list[str]:
    if kind not in ("movie", "tv") or type(identifier) is not int or identifier <= 0:
        raise BackdropError("Select a valid TMDB movie or series.")
    with _lookup_deadline(LOOKUP_TIMEOUT):
        return _backdrop_paths(kind, identifier, load_credential())[:3]


def repair_cover(path: str) -> bytes:
    if not isinstance(path, str) or not IMAGE_PATH.fullmatch(path):
        raise BackdropError("TMDB returned an invalid image path.")
    with _lookup_deadline(LOOKUP_TIMEOUT):
        return validated_image(_read(
            Request(IMAGE_BASE + path, headers={"User-Agent": "MovieNightBot/1.0"}),
            MAX_IMAGE_BYTES, provider="TMDB image CDN",
        ))


def validated_image(data: bytes) -> bytes:
    """Decode fully, reject non-landscape input, and produce a bounded 16:9 JPEG."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise BackdropError("The backdrop must be an image smaller than 8 MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP"):
                raise BackdropError("Use a JPEG, PNG or WebP backdrop.")
            if image.width * image.height > 40_000_000:
                raise BackdropError("The backdrop has too many pixels.")
            image.load()
            image = ImageOps.exif_transpose(image)
            if image.width < 320 or image.height < 180 or not 1.5 <= image.width / image.height <= 2:
                raise BackdropError("Use a landscape backdrop, ideally 16:9 and at least 320 x 180.")
            image = ImageOps.fit(image.convert("RGB"), (1280, 720), method=Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90)
            return output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise BackdropError("The backdrop could not be decoded as a valid image.") from None


def _atomic_write(encoded: bytes, dest: Path) -> Path:
    temporary = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".jpg", delete=False) as file:
            temporary = Path(file.name)
            file.write(encoded)
        temporary.replace(dest)
        return dest
    except OSError:
        raise BackdropError("The backdrop cache could not be written.") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_image(data: bytes, dest: Path) -> Path:
    return _atomic_write(validated_image(data), dest)


def manual_backdrop(data: bytes, directory: Path = TMP) -> Path:
    return save_image(data, directory / "backdrops" / f"manual-{hashlib.sha256(data).hexdigest()}.jpg")


def _validate_lookup(title, year, show_type, imdb_id):
    if (not isinstance(title, str) or not _normal_title(title)
            or (year is not None and (not isinstance(year, str) or not re.fullmatch(r"[0-9]{4}", year)))):
        raise BackdropError("Provide a movie title and, optionally, a four-digit release year.")
    if show_type not in ("movie", "tv", "anime", "other"):
        raise BackdropError("Select movie, TV, anime or other as the media type.")
    if imdb_id is not None and (not isinstance(imdb_id, str) or not IMDB_ID.fullmatch(imdb_id)):
        raise BackdropError("Use an IMDb identifier such as tt31349844.")


def _cached_backdrop(manifest: Path, identity: list) -> Path | None:
    try:
        with manifest.open("rb") as file:
            raw = file.read(4097)
        if len(raw) > 4096:
            raise ValueError
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("identity") != identity
                or data.get("version") != CACHE_VERSION
                or data.get("kind") not in ("movie", "tv")
                or type(data.get("tmdb_id")) is not int or data["tmdb_id"] <= 0
                or not isinstance(data.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", data["sha256"])):
            raise ValueError
        created = data.get("created")
        if (type(created) not in (int, float) or not math.isfinite(created)
                or not 0 <= time.time() - created <= CACHE_MAX_AGE):
            raise ValueError
        path = manifest.parent / f"{manifest.stem}-{data['sha256']}.jpg"
        with path.open("rb") as file:
            image_data = file.read(MAX_IMAGE_BYTES + 1)
        if hashlib.sha256(image_data).hexdigest() != data["sha256"]:
            raise ValueError
        validated_image(image_data)
        return path
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError, BackdropError):
        print("WARN: cached backdrop is invalid or expired; fetching a fresh image.", file=sys.stderr)
        return None


def _fetch_backdrop(
    title: str, year: str | None = None, show_type: str = "movie",
    *, directory: Path = TMP, imdb_id: str | None = None, imdb_id_explicit: bool = True,
) -> Path:
    _validate_lookup(title, year, show_type, imdb_id)
    identity = [CACHE_VERSION, title.strip().casefold(), year, show_type, imdb_id,
                bool(imdb_id and imdb_id_explicit)]
    cache_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    manifest = directory / "backdrops" / f"tmdb-v{CACHE_VERSION}-{cache_key}.json"
    cached = _cached_backdrop(manifest, identity)
    if cached is not None:
        return cached
    credential = load_credential()
    kind, identifier = find_title(
        title, year, show_type, credential, imdb_id, imdb_id_explicit=imdb_id_explicit,
    )
    paths = _backdrop_paths(kind, identifier, credential)
    failures = []
    for path in paths[:MAX_IMAGE_CANDIDATES]:
        try:
            raw = _read(
                Request(IMAGE_BASE + path, headers={"User-Agent": "MovieNightBot/1.0"}),
                MAX_IMAGE_BYTES, provider="TMDB image CDN",
            )
            encoded = validated_image(raw)
            digest = hashlib.sha256(encoded).hexdigest()
            dest = manifest.parent / f"{manifest.stem}-{digest}.jpg"
            _atomic_write(encoded, dest)
            # The atomic pointer references immutable content, so concurrent writers
            # cannot mix one image with another writer's identity/digest.
            _atomic_write(json.dumps(dict(
                version=CACHE_VERSION, identity=identity, kind=kind,
                tmdb_id=identifier, sha256=digest, created=time.time(),
            )).encode(), manifest)
            return dest
        except BackdropError as exc:
            failures.append(str(exc))
    raise BackdropError("No backdrop could be downloaded: " + "; ".join(dict.fromkeys(failures)))


def fetch_backdrop(
    title: str, year: str | None = None, show_type: str = "movie",
    *, directory: Path = TMP, imdb_id: str | None = None, imdb_id_explicit: bool = True,
) -> Path:
    """Cache only validated identities; inferred IDs must also match title/year."""
    with _lookup_deadline(LOOKUP_TIMEOUT):
        return _fetch_backdrop(
            title, year, show_type, directory=directory,
            imdb_id=imdb_id, imdb_id_explicit=imdb_id_explicit,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title")
    parser.add_argument("year", nargs="?")
    parser.add_argument("--show-type", choices=("movie", "tv", "anime", "other"), default="movie")
    identifiers = parser.add_mutually_exclusive_group()
    identifiers.add_argument("--imdb-id")
    identifiers.add_argument("--inferred-imdb-id")
    args = parser.parse_args()
    try:
        print(fetch_backdrop(
            args.title, args.year, args.show_type,
            imdb_id=args.imdb_id or args.inferred_imdb_id,
            imdb_id_explicit=args.inferred_imdb_id is None,
        ))
        return 0
    except BackdropError as exc:
        if isinstance(exc, BackdropIdentityError):
            print("ERROR_CODE: inferred_identity_mismatch", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        print("NONE")
        return 1


if __name__ == "__main__":
    sys.exit(main())
