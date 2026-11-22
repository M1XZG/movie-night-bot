"""Fetch OMDb/MDBList scores and format them independently of AI copy."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


MDBLIST_URL = "https://api.mdblist.com"
OMDB_URL = "https://www.omdbapi.com/"
REQUEST_TIMEOUT = 10
SEARCH_LIMIT = 30
MAX_SEARCH_PAGES = 5
LOOKUP_TIMEOUT = 45
RETRY_DELAY = 0.5
MAX_RESPONSE_BYTES = 256_000
IMDB_ID = re.compile(r"tt[0-9]{7,12}\Z")
_DEADLINE = ContextVar("media_lookup_deadline", default=None)


class RatingsError(Exception):
    """A safe-to-display lookup failure; never contains a key or request URL."""


class RatingsNotFoundError(RatingsError):
    """Raised when a provider has no record for a valid lookup."""


@dataclass(frozen=True)
class MovieRatings:
    imdb: str | None = None
    rotten_tomatoes: str | None = None
    imdb_id: str | None = None
    rotten_tomatoes_url: str | None = None
    provider: str = "MDBList"
    canonical_title: str | None = field(default=None, compare=False)
    canonical_year: str | None = field(default=None, compare=False)
    canonical_type: str | None = field(default=None, compare=False)

    def missing_message(self) -> str:
        missing = []
        if self.imdb is None:
            missing.append("IMDb")
        if self.rotten_tomatoes is None:
            missing.append("Rotten Tomatoes")
        return f"No {' or '.join(missing)} score is available." if missing else ""


def load_api_key() -> str:
    """Read a credential without prompting or including it in error messages."""
    key = os.environ.get("MDBLIST_API_KEY", "").strip()
    if key:
        return _validated_key(key)
    key_file = os.environ.get("MDBLIST_API_KEY_FILE")
    if key_file:
        try:
            key = Path(key_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, ValueError):
            raise RatingsError("The MDBList key file could not be read.") from None
        return _validated_key(key)

    entry = os.environ.get("MDBLIST_PASS_ENTRY", "api/mdblist")
    if not entry.strip():
        raise RatingsError("The MDBList pass entry name is empty.")
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
        raise RatingsError("The MDBList key could not be read from pass.") from None
    if result.returncode:
        raise RatingsError("The MDBList pass entry is unavailable; unlock GPG or configure an API key.")
    lines = result.stdout.splitlines()
    return _validated_key(lines[0].strip() if lines else "")


def load_omdb_api_key() -> str:
    """Read the OMDb credential without exposing it in errors or logs."""
    key = os.environ.get("OMDB_API_KEY", "").strip()
    if key:
        return _validated_key(key, "OMDb")
    key_file = os.environ.get("OMDB_API_KEY_FILE")
    if key_file:
        try:
            key = Path(key_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, ValueError):
            raise RatingsError("The OMDb key file could not be read.") from None
        return _validated_key(key, "OMDb")

    entry = os.environ.get("OMDB_PASS_ENTRY", "api/omdb")
    if not entry.strip():
        raise RatingsError("The OMDb pass entry name is empty.")
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
        raise RatingsError("The OMDb key could not be read from pass.") from None
    if result.returncode:
        raise RatingsError(
            "The OMDb pass entry is unavailable; unlock GPG or configure an API key."
        )
    lines = result.stdout.splitlines()
    return _validated_key(lines[0].strip() if lines else "", "OMDb")


def _validated_key(key: str, provider: str = "MDBList") -> str:
    if not key or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in key):
        raise RatingsError(f"{provider} needs one non-empty API key.")
    return key


@contextmanager
def _lookup_deadline(seconds: float):
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _remaining_timeout(maximum: float) -> float:
    deadline = _DEADLINE.get()
    remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _retry_delay(error=None) -> float:
    if not isinstance(error, HTTPError) or not error.headers:
        return RETRY_DELAY
    value = error.headers.get("Retry-After")
    if value is None:
        return RETRY_DELAY
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0, date.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return RETRY_DELAY
    if not math.isfinite(delay) or delay < 0:
        return RETRY_DELAY
    return max(RETRY_DELAY, delay)


def _wait_for_retry(delay: float, budget: float) -> None:
    if delay >= _remaining_timeout(budget):
        raise TimeoutError
    time.sleep(delay)


def _read_response(response, limit: int, timeout: float) -> bytes:
    read = getattr(response, "read1", None)
    if not callable(read):
        return response.read(limit + 1)
    deadline = time.monotonic() + timeout
    chunks = bytearray()
    while len(chunks) <= limit:
        _remaining_timeout(timeout)
        if time.monotonic() >= deadline:
            raise TimeoutError
        chunk = read(min(65536, limit + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _read_json(request: Request, provider: str) -> dict:
    for attempt in range(2):
        try:
            with urlopen(request, timeout=_remaining_timeout(REQUEST_TIMEOUT)) as response:
                raw = _read_response(response, MAX_RESPONSE_BYTES, REQUEST_TIMEOUT)
            break
        except HTTPError as exc:
            delay = _retry_delay(exc)
            code = exc.code
            exc.close()
            if not attempt and (code == 429 or 500 <= code < 600):
                pass
            elif provider == "MDBList" and code == 404:
                raise RatingsNotFoundError("MDBList could not find this title.") from None
            else:
                raise RatingsError(f"{provider} returned HTTP {code}.") from None
        except (URLError, TimeoutError, OSError):
            delay = RETRY_DELAY
            if attempt:
                raise RatingsError(f"{provider} could not be reached.") from None
        try:
            _wait_for_retry(delay, LOOKUP_TIMEOUT)
        except TimeoutError:
            raise RatingsError(f"{provider} lookup timed out.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RatingsError(f"{provider} returned an oversized response.")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RatingsError(f"{provider} returned invalid JSON.") from None
    if not isinstance(data, dict):
        raise RatingsError(f"{provider} returned an invalid response.")
    return data


def _request(key: str, path: str, **params) -> dict:
    query = urlencode({"apikey": key, **params})
    data = _read_json(Request(
        MDBLIST_URL + path + "?" + query,
        headers={"User-Agent": "MovieNightBot/1.0", "Accept": "application/json"},
    ), "MDBList")
    if data.get("error") or data.get("detail"):
        raise RatingsError("MDBList did not return a successful lookup.")
    return data


def _omdb_request(
    key: str, title: str, year: str | None, show_type: str,
    imdb_id: str | None = None, *, search: bool = False, page: int = 1,
) -> dict | None:
    params = {
        "apikey": key,
        "r": "json",
        "plot": "short",
    }
    if imdb_id:
        params["i"] = imdb_id
    else:
        params["s" if search else "t"] = title
        if year:
            params["y"] = year
        if search:
            params["page"] = page
    media_type = {"movie": "movie", "tv": "series"}.get(show_type)
    if media_type:
        params["type"] = media_type
    request = Request(
        OMDB_URL + "?" + urlencode(params),
        headers={"User-Agent": "MovieNightBot/1.0", "Accept": "application/json"},
    )
    data = _read_json(request, "OMDb")
    if data.get("Response") == "False":
        error = data.get("Error")
        if isinstance(error, str) and "not found" in error.casefold():
            return None
        raise RatingsError("OMDb did not return a successful lookup.")
    if data.get("Response") != "True":
        raise RatingsError("OMDb returned an invalid response.")
    return data


def _normal_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title).casefold()
    title = re.sub(r"^(?:the|a|an)\s+", "", title.strip())
    return "".join(character for character in title if character.isalnum())


EPISODE_SUFFIX = re.compile(
    r"""
    \s*(?:[-–—:]\s*)?
    (?:
        s(?:eason)?\s*\d{1,3}\s*e(?:p(?:isode)?)?\s*\d{1,4}
        |
        e(?:p(?:isode)?)?\.?\s*\d{1,4}
        |
        episode\s*\d{1,4}
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _search_titles(title: str, show_type: str, year: str | None = None) -> list[str]:
    """Return exact catalogue titles worth trying, without fuzzy guessing."""
    titles = [title]
    # This abbreviation was confirmed against IMDb tt0099700 / TMDB movie 928.
    if (show_type == "movie" and year in (None, "1990")
            and _normal_title(title) == "gremlins2"):
        titles.append("Gremlins 2: The New Batch")
    if show_type not in ("tv", "anime"):
        return titles
    parent = EPISODE_SUFFIX.sub("", title).rstrip(" -–—:")
    if parent and _normal_title(parent) != _normal_title(title):
        titles.append(parent)
    return titles


def _score(value, *, imdb: bool, provider: str = "MDBList") -> str | None:
    if value is None or value == "N/A":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RatingsError(f"{provider} returned an invalid rating.")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise RatingsError(f"{provider} returned an invalid rating.") from None
    maximum = 10 if imdb else 100
    if not number.is_finite() or not 0 <= number <= maximum:
        raise RatingsError(f"{provider} returned an out-of-range rating.")
    if imdb:
        return f"{number:.1f}/10"
    if number != number.to_integral_value():
        raise RatingsError(f"{provider} returned an invalid Rotten Tomatoes percentage.")
    return f"{int(number)}%"


def _identifier(item: dict, provider: str) -> str | None:
    ids = item.get("ids", {})
    if not isinstance(ids, dict):
        raise RatingsError("MDBList returned invalid title identifiers.")
    if provider == "imdb":
        values = {value for value in (ids.get("imdb"), ids.get("imdbid"), item.get("imdb_id"))
                  if isinstance(value, str) and IMDB_ID.fullmatch(value)}
        if len(values) > 1:
            raise RatingsError("MDBList returned conflicting IMDb identifiers.")
        return next(iter(values), None)
    value = ids.get("mdblist")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{1,32}", value) else None


def _rotten_url(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 400:
        raise RatingsError("MDBList returned an invalid Rotten Tomatoes link.")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise RatingsError("MDBList returned an invalid Rotten Tomatoes link.") from None
    if parsed.netloc and (
        parsed.scheme != "https"
        or parsed.netloc not in ("www.rottentomatoes.com", "rottentomatoes.com")
    ):
        raise RatingsError("MDBList returned an invalid Rotten Tomatoes link.")
    if parsed.scheme and not parsed.netloc:
        raise RatingsError("MDBList returned an invalid Rotten Tomatoes link.")
    if re.fullmatch(r"/(?:m|tv)/[A-Za-z0-9_-]+(?:/s[0-9]{1,2})?/?", parsed.path):
        return "https://www.rottentomatoes.com" + parsed.path
    raise RatingsError("MDBList returned an invalid Rotten Tomatoes link.")


def _fetch_mdblist_ratings(
    title: str, year: str | None = None, show_type: str = "movie",
    *, api_key: str | None = None, imdb_id: str | None = None,
) -> MovieRatings:
    """Resolve a title/year, refusing ambiguous or mismatched provider results."""
    title = title.strip()
    if not _normal_title(title):
        raise RatingsError("Enter a movie or series title to look up ratings.")
    if year is not None and not re.fullmatch(r"[0-9]{4}", year):
        raise RatingsError("Ratings lookup needs a four-digit release year.")
    if imdb_id is not None and not IMDB_ID.fullmatch(imdb_id):
        raise RatingsError("Use an IMDb identifier such as tt31349844.")
    key = load_api_key() if api_key is None else _validated_key(api_key)
    if imdb_id:
        kinds = {
            "movie": ("movie",),
            "tv": ("show",),
            "anime": ("show", "movie"),
        }.get(show_type, ("movie", "show"))
        for current_kind in kinds:
            try:
                data = _request(key, f"/imdb/{current_kind}/{imdb_id}")
            except RatingsNotFoundError:
                continue
            if (
                data.get("type") != current_kind
                or _identifier(data, "imdb") != imdb_id
                or not isinstance(data.get("title"), str)
                or not _normal_title(data["title"])
                or isinstance(data.get("year"), bool)
                or not isinstance(data.get("year"), int)
                or not 1000 <= data["year"] <= 9999
            ):
                raise RatingsError(
                    "MDBList returned a different or unverified IMDb identifier."
                )
            return _mdblist_scores(data)
        raise RatingsError("MDBList could not find this IMDb identifier.")

    kind = {"movie": "movie", "tv": "show"}.get(show_type, "any")
    selected = None
    resolved_title = title
    for candidate in _search_titles(title, show_type, year):
        params = {"query": candidate, "limit": SEARCH_LIMIT}
        if year:
            params["year"] = int(year)
        search = _request(key, f"/search/{kind}", **params)
        matches = search.get("search")
        total = search.get("total")
        if (
            not isinstance(matches, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < len(matches)
        ):
            raise RatingsError("MDBList returned invalid search results.")
        if total > len(matches):
            raise RatingsError(
                "Search results are incomplete; use a more specific title and release year."
            )
        # MDBList's year filter is a +/-1-year hint, so enforce it here.
        exact = [
            item for item in matches
            if isinstance(item, dict) and isinstance(item.get("title"), str)
            and _normal_title(item["title"]) == _normal_title(candidate)
            and isinstance(item.get("year"), int) and not isinstance(item["year"], bool)
            and item.get("type") in ("movie", "show")
            and (kind == "any" or item["type"] == kind)
            and (year is None or item["year"] == int(year))
        ]
        if len(exact) > 1:
            raise RatingsError(
                "No unique title match in MDBList; check the title and release year."
            )
        if exact:
            selected = exact[0]
            resolved_title = candidate
            break
    if selected is None:
        raise RatingsError("No unique title match in MDBList; check the title and release year.")
    provider = "imdb"
    expected_id = _identifier(selected, provider)
    if expected_id is None:
        provider = "mdblist"
        expected_id = _identifier(selected, provider)
    if expected_id is None:
        raise RatingsError("MDBList returned no usable title identifier.")
    data = _request(key, f"/{provider}/{selected['type']}/{expected_id}")
    returned_title = data.get("title")
    returned_year = data.get("year")
    if (
        not isinstance(returned_title, str)
        or _normal_title(returned_title) != _normal_title(resolved_title)
        or isinstance(returned_year, bool) or not isinstance(returned_year, int)
        or returned_year != selected["year"]
        or data.get("type") != selected["type"]
        or _identifier(data, provider) != expected_id
    ):
        raise RatingsError("MDBList returned a different or unverified title/year.")

    return _mdblist_scores(data)


def _mdblist_scores(data: dict) -> MovieRatings:
    entries = data.get("ratings", [])
    if not isinstance(entries, list):
        raise RatingsError("MDBList returned an invalid ratings list.")
    scores = {}
    rotten_url = None
    for entry in entries:
        if not isinstance(entry, dict):
            raise RatingsError("MDBList returned an invalid ratings entry.")
        source = entry.get("source")
        if source not in ("imdb", "tomatoes"):
            continue
        value = _score(entry.get("value"), imdb=source == "imdb")
        if source in scores and scores[source] != value:
            raise RatingsError("MDBList returned conflicting ratings.")
        scores[source] = value
        if source == "tomatoes":
            rotten_url = _rotten_url(entry.get("url"))
    return MovieRatings(
        scores.get("imdb"), scores.get("tomatoes"), _identifier(data, "imdb"),
        rotten_url, "MDBList", data.get("title"), str(data.get("year")),
        {"show": "tv", "movie": "movie"}.get(data.get("type")),
    )


def _omdb_unique_identity(
    key: str, title: str, year: str | None, show_type: str, expected_id: str,
) -> None:
    """The OMDb `t` endpoint picks one result, even for ambiguous titles."""
    ids = set()
    seen_ids = set()
    seen_pages = set()
    total = None
    received = 0
    pages = 1
    for page in range(1, MAX_SEARCH_PAGES + 1):
        data = _omdb_request(key, title, year, show_type, search=True, page=page)
        if data is None:
            raise RatingsError("No unique title match in OMDb; supply an IMDb identifier.")
        rows = data.get("Search")
        count = data.get("totalResults")
        if (not isinstance(rows, list) or not rows or not isinstance(count, str)
                or not re.fullmatch(r"[0-9]{1,6}", count) or int(count) < len(rows)):
            raise RatingsError("OMDb returned invalid search results.")
        count = int(count)
        if total is None:
            total = count
            pages = (total + 9) // 10
            if pages > MAX_SEARCH_PAGES:
                raise RatingsError("OMDb search is incomplete; supply a release year or IMDb identifier.")
        elif count != total:
            raise RatingsError("OMDb search results changed; retry or supply an IMDb identifier.")
        fingerprint = json.dumps(sorted(json.dumps(item, sort_keys=True) for item in rows))
        if fingerprint in seen_pages:
            raise RatingsError("OMDb repeated a search page; supply an IMDb identifier.")
        seen_pages.add(fingerprint)
        received += len(rows)
        for item in rows:
            if not isinstance(item, dict):
                raise RatingsError("OMDb returned an invalid search result.")
            name, date = item.get("Title"), item.get("Year")
            item_type, identifier = item.get("Type"), item.get("imdbID")
            if isinstance(identifier, str) and IMDB_ID.fullmatch(identifier):
                if identifier in seen_ids:
                    raise RatingsError("OMDb repeated a title identifier in search results; supply an IMDb identifier.")
                seen_ids.add(identifier)
            if (not isinstance(name, str) or _normal_title(name) != _normal_title(title)
                    or item_type not in ("movie", "series")
                    or (show_type in ("movie", "tv") and item_type != (
                        "movie" if show_type == "movie" else "series"))
                    or not isinstance(date, str)
                    or not re.fullmatch(r"[0-9]{4}(?:[–-][0-9]{0,4})?", date)
                    or (year is not None and date[:4] != year)):
                continue
            if not isinstance(identifier, str) or not IMDB_ID.fullmatch(identifier):
                raise RatingsError("OMDb returned an invalid title identifier.")
            ids.add(identifier)
        if page >= pages:
            break
    if received != total:
        raise RatingsError("OMDb search is incomplete; supply an IMDb identifier.")
    if ids != {expected_id}:
        raise RatingsError("No unique title match in OMDb; supply a release year or IMDb identifier.")


def _fetch_omdb_ratings(
    title: str,
    year: str | None,
    show_type: str,
    api_key: str,
    requested_imdb_id: str | None = None,
) -> MovieRatings | None:
    key = _validated_key(api_key, "OMDb")
    candidates = [title] if requested_imdb_id else _search_titles(title, show_type, year)
    for candidate in candidates:
        data = _omdb_request(
            key, candidate, year, show_type, requested_imdb_id)
        if data is None:
            continue
        returned_title = data.get("Title")
        returned_year = data.get("Year")
        returned_type = data.get("Type")
        imdb_id = data.get("imdbID")
        year_match = (re.fullmatch(r"([0-9]{4})(?:[–-][0-9]{0,4})?", returned_year)
                      if isinstance(returned_year, str) else None)
        expected_type = {"movie": "movie", "tv": "series"}.get(show_type)
        if requested_imdb_id:
            if (
                requested_imdb_id != imdb_id
                or not isinstance(returned_title, str) or not _normal_title(returned_title)
                or not year_match
                or returned_type not in ("movie", "series")
                or (expected_type is not None and returned_type != expected_type)
            ):
                raise RatingsError(
                    "OMDb returned a different or unverified IMDb identifier."
                )
        elif (
            not isinstance(returned_title, str)
            or not any(_normal_title(returned_title) == _normal_title(name) for name in candidates)
            or not year_match
            or (year is not None and year_match.group(1) != year)
            or returned_type not in ("movie", "series")
            or (expected_type is not None and returned_type != expected_type)
            or not isinstance(imdb_id, str)
            or not IMDB_ID.fullmatch(imdb_id)
        ):
            raise RatingsError("OMDb returned a different or unverified title/year.")
        if not requested_imdb_id:
            _omdb_unique_identity(key, returned_title, year, show_type, imdb_id)

        imdb = None
        rotten = None
        entries = data.get("Ratings", [])
        if not isinstance(entries, list):
            raise RatingsError("OMDb returned an invalid ratings list.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RatingsError("OMDb returned an invalid ratings entry.")
            source = entry.get("Source")
            value = entry.get("Value")
            if source == "Internet Movie Database":
                match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)/10", str(value))
                if not match:
                    raise RatingsError("OMDb returned an invalid IMDb rating.")
                score = _score(match.group(1), imdb=True, provider="OMDb")
                if imdb is not None and imdb != score:
                    raise RatingsError("OMDb returned conflicting ratings.")
                imdb = score
            elif source == "Rotten Tomatoes":
                match = re.fullmatch(r"([0-9]{1,3})%", str(value))
                if not match:
                    raise RatingsError("OMDb returned an invalid Rotten Tomatoes rating.")
                score = _score(match.group(1), imdb=False, provider="OMDb")
                if rotten is not None and rotten != score:
                    raise RatingsError("OMDb returned conflicting ratings.")
                rotten = score
        raw_imdb = data.get("imdbRating")
        if raw_imdb not in (None, "", "N/A"):
            score = _score(raw_imdb, imdb=True, provider="OMDb")
            if imdb is not None and imdb != score:
                raise RatingsError("OMDb returned conflicting ratings.")
            imdb = score
        return MovieRatings(
            imdb, rotten, imdb_id, None, "OMDb",
            returned_title, year_match.group(1),
            "movie" if returned_type == "movie" else "tv",
        )
    return None


def _fetch_ratings(
    title: str,
    year: str | None = None,
    show_type: str = "movie",
    *,
    api_key: str | None = None,
    omdb_api_key: str | None = None,
    imdb_id: str | None = None,
) -> MovieRatings:
    """Prefer exact OMDb scores, then use MDBList when they are unavailable."""
    if not isinstance(title, str):
        raise RatingsError("Enter a movie or series title to look up ratings.")
    title = title.strip()
    if not _normal_title(title):
        raise RatingsError("Enter a movie or series title to look up ratings.")
    if year is not None and (not isinstance(year, str) or not re.fullmatch(r"[0-9]{4}", year)):
        raise RatingsError("Ratings lookup needs a four-digit release year.")
    if show_type not in ("movie", "tv", "anime", "other"):
        raise RatingsError("Select movie, TV, anime or other as the media type.")
    if imdb_id is not None and (not isinstance(imdb_id, str) or not IMDB_ID.fullmatch(imdb_id)):
        raise RatingsError("Use an IMDb identifier such as tt31349844.")

    omdb_error = None
    omdb = None
    if omdb_api_key:
        try:
            omdb = _fetch_omdb_ratings(
                title, year, show_type, omdb_api_key, imdb_id)
            if omdb is not None and (omdb.imdb is not None or omdb.rotten_tomatoes is not None):
                return omdb
        except RatingsError as exc:
            omdb_error = exc

    try:
        mdblist = _fetch_mdblist_ratings(
            title, year, show_type, api_key=api_key, imdb_id=imdb_id)
    except RatingsError:
        if omdb is not None:
            return omdb
        if omdb_error is not None:
            raise omdb_error
        raise
    if (omdb is not None and omdb.imdb_id and mdblist.imdb_id
            and omdb.imdb_id != mdblist.imdb_id):
        raise RatingsError("Ratings providers disagree on this title's IMDb identity; supply an IMDb identifier.")
    if omdb is not None and mdblist.imdb is None and mdblist.rotten_tomatoes is None:
        return omdb
    return mdblist


def fetch_ratings(
    title: str, year: str | None = None, show_type: str = "movie", *,
    api_key: str | None = None, omdb_api_key: str | None = None,
    imdb_id: str | None = None,
) -> MovieRatings:
    """Prefer verified OMDb scores, with bounded retries and an MDBList fallback."""
    with _lookup_deadline(LOOKUP_TIMEOUT):
        return _fetch_ratings(
            title, year, show_type, api_key=api_key,
            omdb_api_key=omdb_api_key, imdb_id=imdb_id,
        )


def _runtime(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    return f"about {hours}h {remainder:02d}m"


def _rating_lines(ratings: MovieRatings, *, markdown: bool) -> str:
    imdb = ratings.imdb or "unavailable"
    rotten = ratings.rotten_tomatoes or "unavailable"
    provider_name = ratings.provider if ratings.provider in ("OMDb", "MDBList") else "MDBList"
    provider_url = (
        "https://www.omdbapi.com/"
        if provider_name == "OMDb"
        else "https://mdblist.com/"
    )
    if markdown:
        if ratings.imdb and ratings.imdb_id:
            imdb = f"[{imdb}](https://www.imdb.com/title/{ratings.imdb_id}/)"
        if ratings.rotten_tomatoes and ratings.rotten_tomatoes_url:
            rotten = f"[{rotten}]({ratings.rotten_tomatoes_url})"
        lines = f":star: **IMDb:** {imdb}\n:tomato: **Rotten Tomatoes (critics):** {rotten}"
        if ratings.imdb or ratings.rotten_tomatoes:
            lines += f"\n_Ratings via [{provider_name}]({provider_url})._"
        return lines
    lines = f"IMDb: {imdb}\nRotten Tomatoes (critics): {rotten}"
    if ratings.imdb or ratings.rotten_tomatoes:
        lines += f"\nRatings via {provider_name}: {provider_url}"
    return lines


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit < 3:
        return "." * max(0, limit)
    return text[:limit - 3].rstrip() + "..."


def _runtime_parts(body: str, runtime_min: int) -> tuple[str, str, str]:
    lines = body.splitlines()
    runtime_index = next(
        (index for index, line in enumerate(lines)
         if re.search(r"\bruntime\b.*:", line, re.IGNORECASE)),
        None,
    )
    if runtime_index is None:
        before = body.rstrip()
        runtime_line = f":hourglass_flowing_sand: **Runtime:** {_runtime(runtime_min)}"
        after = ""
    else:
        before = "\n".join(lines[:runtime_index]).rstrip()
        runtime_line = lines[runtime_index]
        if len(runtime_line) > 160:
            runtime_line = f":hourglass_flowing_sand: **Runtime:** {_runtime(runtime_min)}"
        after = "\n".join(lines[runtime_index + 1:])
    return before, runtime_line, after


def discord_event_description(
    description: str, runtime_min: int, ratings: MovieRatings, *, announcement: str = "",
) -> str:
    _, runtime_line, _ = _runtime_parts(announcement, runtime_min)
    plain = re.sub(r":[A-Za-z0-9_]+:", "", runtime_line).replace("*", "").replace("`", "")
    match = re.search(r"((?:episode/season )?runtime)\s*:\s*(.+)", plain, re.IGNORECASE)
    runtime = f"{match[1].capitalize()}: {match[2].strip()}" if match else f"Runtime: {_runtime(runtime_min)}"
    header = runtime + "\n" + _rating_lines(ratings, markdown=False)
    return header + "\n\n" + _shorten(description, 1000 - len(header) - 2)


def discord_announcement(
    body: str, runtime_min: int, ratings: MovieRatings, *, limit: int,
) -> str:
    """Keep the runtime and ratings together, reserving room for the caller's footer."""
    before, runtime_line, after = _runtime_parts(body, runtime_min)
    block = runtime_line + "\n" + _rating_lines(ratings, markdown=True)
    available = limit - len(block) - 2
    if available < 0:
        raise ValueError("Announcement budget is too small for runtime and ratings")
    before = _shorten(before, available)
    after = _shorten(after, available - len(before))
    return "\n".join(part for part in (before, block, after) if part).rstrip()
