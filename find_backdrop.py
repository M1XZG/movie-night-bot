#!/usr/bin/env python3
"""Fetch and cache a validated landscape backdrop using the official TMDB API."""

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
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

from ratings import _normal_title, _search_titles

API = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w1280"
TMP = Path(os.environ.get("MOVIE_BOT_TMP", Path(__file__).resolve().parent / "tmp"))
MAX_JSON_BYTES = 1_000_000
MAX_IMAGE_BYTES = 8_000_000
TIMEOUT = 8
IMAGE_PATH = re.compile(r"/[A-Za-z0-9_-]+\.(?:jpg|png)\Z")


class BackdropError(Exception):
    """A safe-to-display image failure, without credentials or request URLs."""


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
            with urlopen(request, timeout=TIMEOUT) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise BackdropError(f"{provider} returned an oversized response.")
            return data
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt:
                raise BackdropError(f"{provider} returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError):
            if attempt:
                raise BackdropError(f"{provider} could not be reached.") from None
        time.sleep(0.5)
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
) -> tuple[str, int]:
    kinds = {"movie": ("movie",), "tv": ("tv",)}.get(show_type, ("tv", "movie"))
    for search_title in _search_titles(title, show_type):
        matches = {}
        for kind in kinds:
            params = {"query": search_title, "include_adult": "false"}
            if year:
                params["year" if kind == "movie" else "first_air_date_year"] = year
            data = _request(credential, f"/search/{kind}", **params)
            results = data.get("results")
            if not isinstance(results, list):
                raise BackdropError("TMDB returned invalid search results.")
            pages = data.get("total_pages", 1)
            if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
                raise BackdropError("TMDB returned an invalid search page count.")
            if pages > 1:
                raise BackdropError(
                    "TMDB returned too many candidates; use a manual backdrop."
                )
            for item in results:
                if not isinstance(item, dict):
                    raise BackdropError("TMDB returned an invalid title.")
                identifier = item.get("id")
                names = (item.get("title"), item.get("original_title")) if kind == "movie" else (
                    item.get("name"), item.get("original_name"),
                )
                date = item.get("release_date" if kind == "movie" else "first_air_date")
                if not any(isinstance(name, str) and _normal_title(name) == _normal_title(search_title)
                           for name in names):
                    continue
                if year and (not isinstance(date, str) or date[:4] != year):
                    continue
                if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
                    raise BackdropError("TMDB returned an invalid title identifier.")
                matches[kind, identifier] = None
        if len(matches) == 1:
            return next(iter(matches))
        if matches:
            raise BackdropError("The title is ambiguous on TMDB; specify its year or a manual backdrop.")
    raise BackdropError("No exact title/year match was found on TMDB; use a manual backdrop.")


def _backdrop_paths(kind: str, identifier: int, credential: Credential) -> list[str]:
    data = _request(credential, f"/{kind}/{identifier}/images")
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


def save_image(data: bytes, dest: Path) -> Path:
    encoded = validated_image(data)
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


def manual_backdrop(data: bytes, directory: Path = TMP) -> Path:
    return save_image(data, directory / "backdrops" / f"manual-{hashlib.sha256(data).hexdigest()}.jpg")


def fetch_backdrop(
    title: str, year: str | None = None, show_type: str = "movie",
    *, directory: Path = TMP,
) -> Path:
    if not title.strip() or (year and not re.fullmatch(r"\d{4}", year)):
        raise BackdropError("Provide a movie title and, optionally, a four-digit release year.")
    cache_key = hashlib.sha256(json.dumps([title.strip().casefold(), year, show_type]).encode()).hexdigest()
    dest = directory / "backdrops" / f"tmdb-{cache_key}.jpg"
    if dest.exists():
        try:
            with dest.open("rb") as file:
                validated_image(file.read(MAX_IMAGE_BYTES + 1))
            return dest
        except (OSError, BackdropError):
            print("WARN: cached backdrop is invalid; fetching a fresh image.", file=sys.stderr)
    credential = load_credential()
    kind, identifier = find_title(title, year, show_type, credential)
    paths = _backdrop_paths(kind, identifier, credential)
    failures = []
    for path in paths[:3]:
        try:
            raw = _read(
                Request(IMAGE_BASE + path, headers={"User-Agent": "MovieNightBot/1.0"}),
                MAX_IMAGE_BYTES, provider="TMDB image CDN",
            )
            return save_image(raw, dest)
        except BackdropError as exc:
            failures.append(str(exc))
    raise BackdropError("No backdrop could be downloaded: " + "; ".join(dict.fromkeys(failures)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title")
    parser.add_argument("year", nargs="?")
    parser.add_argument("--show-type", choices=("movie", "tv", "anime", "other"), default="movie")
    args = parser.parse_args()
    try:
        print(fetch_backdrop(args.title, args.year, args.show_type))
        return 0
    except BackdropError as exc:
        print(str(exc), file=sys.stderr)
        print("NONE")
        return 1


if __name__ == "__main__":
    sys.exit(main())
