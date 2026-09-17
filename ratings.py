"""Fetch MDBList scores and format them for Discord, independently of AI copy."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import subprocess
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


MDBLIST_URL = "https://api.mdblist.com"
REQUEST_TIMEOUT = 10
SEARCH_LIMIT = 30
MAX_RESPONSE_BYTES = 256_000
IMDB_ID = re.compile(r"tt[0-9]{7,12}\Z")


class RatingsError(Exception):
    """A safe-to-display lookup failure; never contains a key or request URL."""


@dataclass(frozen=True)
class MovieRatings:
    imdb: str | None = None
    rotten_tomatoes: str | None = None
    imdb_id: str | None = None
    rotten_tomatoes_url: str | None = None

    def missing_message(self) -> str:
        missing = []
        if self.imdb is None:
            missing.append("IMDb")
        if self.rotten_tomatoes is None:
            missing.append("Rotten Tomatoes")
        return f"MDBList did not return {' and '.join(missing)} scores." if missing else ""


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


def _validated_key(key: str) -> str:
    if not key or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in key):
        raise RatingsError("MDBList needs one non-empty API key.")
    return key


def _request(key: str, path: str, **params) -> dict:
    query = urlencode({"apikey": key, **params})
    request = Request(
        MDBLIST_URL + path + "?" + query,
        headers={"User-Agent": "MovieNightBot/1.0", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        messages = {
            401: "The MDBList API key was rejected.",
            403: "MDBList denied this lookup.",
            404: "MDBList could not find this title.",
            429: "The MDBList request limit was reached.",
        }
        raise RatingsError(messages.get(exc.code, f"MDBList returned HTTP {exc.code}.")) from None
    except (URLError, TimeoutError, OSError):
        raise RatingsError("MDBList could not be reached.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RatingsError("MDBList returned an oversized response.")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RatingsError("MDBList returned invalid JSON.") from None
    if not isinstance(data, dict):
        raise RatingsError("MDBList returned an invalid response.")
    if data.get("error") or data.get("detail"):
        raise RatingsError("MDBList did not return a successful lookup.")
    return data


def _normal_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title).casefold()
    title = re.sub(r"^(?:the|a|an)\s+", "", title.strip())
    return "".join(character for character in title if character.isalnum())


def _score(value, *, imdb: bool) -> str | None:
    if value is None or value == "N/A":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RatingsError("MDBList returned an invalid rating.")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise RatingsError("MDBList returned an invalid rating.") from None
    maximum = 10 if imdb else 100
    if not number.is_finite() or not 0 <= number <= maximum:
        raise RatingsError("MDBList returned an out-of-range rating.")
    if imdb:
        return f"{number:.1f}/10"
    if number != number.to_integral_value():
        raise RatingsError("MDBList returned an invalid Rotten Tomatoes percentage.")
    return f"{int(number)}%"


def _identifier(item: dict, provider: str) -> str | None:
    ids = item.get("ids", {})
    if not isinstance(ids, dict):
        raise RatingsError("MDBList returned invalid title identifiers.")
    if provider == "imdb":
        value = ids.get("imdb") or ids.get("imdbid") or item.get("imdb_id")
        return value if isinstance(value, str) and IMDB_ID.fullmatch(value) else None
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


def fetch_ratings(
    title: str, year: str | None = None, show_type: str = "movie",
    *, api_key: str | None = None,
) -> MovieRatings:
    """Resolve a title/year, refusing ambiguous or mismatched provider results."""
    title = title.strip()
    if not _normal_title(title):
        raise RatingsError("Enter a movie or series title to look up ratings.")
    if year is not None and not re.fullmatch(r"[0-9]{4}", year):
        raise RatingsError("Ratings lookup needs a four-digit release year.")
    key = load_api_key() if api_key is None else _validated_key(api_key)
    kind = {"movie": "movie", "tv": "show"}.get(show_type, "any")
    params = {"query": title, "limit": SEARCH_LIMIT}
    if year:
        params["year"] = int(year)
    search = _request(key, f"/search/{kind}", **params)
    matches = search.get("search")
    total = search.get("total")
    if not isinstance(matches, list) or isinstance(total, bool) or not isinstance(total, int) or total < len(matches):
        raise RatingsError("MDBList returned invalid search results.")
    if total > len(matches):
        raise RatingsError("Search results are incomplete; use a more specific title and release year.")
    # MDBList's year filter is a +/-1-year hint, so enforce the requested year here.
    exact = [
        item for item in matches
        if isinstance(item, dict) and isinstance(item.get("title"), str)
        and _normal_title(item["title"]) == _normal_title(title)
        and isinstance(item.get("year"), int) and not isinstance(item["year"], bool)
        and item.get("type") in ("movie", "show")
        and (kind == "any" or item["type"] == kind)
        and (year is None or item["year"] == int(year))
    ]
    if len(exact) != 1:
        raise RatingsError("No unique title match in MDBList; check the title and release year.")
    selected = exact[0]
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
        or _normal_title(returned_title) != _normal_title(title)
        or isinstance(returned_year, bool) or not isinstance(returned_year, int)
        or returned_year != selected["year"]
        or data.get("type") != selected["type"]
        or _identifier(data, provider) != expected_id
    ):
        raise RatingsError("MDBList returned a different or unverified title/year.")

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
        scores.get("imdb"), scores.get("tomatoes"), _identifier(data, "imdb"), rotten_url,
    )


def _runtime(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    return f"about {hours}h {remainder:02d}m"


def _rating_lines(ratings: MovieRatings, *, markdown: bool) -> str:
    imdb = ratings.imdb or "unavailable"
    rotten = ratings.rotten_tomatoes or "unavailable"
    if markdown:
        if ratings.imdb and ratings.imdb_id:
            imdb = f"[{imdb}](https://www.imdb.com/title/{ratings.imdb_id}/)"
        if ratings.rotten_tomatoes and ratings.rotten_tomatoes_url:
            rotten = f"[{rotten}]({ratings.rotten_tomatoes_url})"
        lines = f":star: **IMDb:** {imdb}\n:tomato: **Rotten Tomatoes (critics):** {rotten}"
        if ratings.imdb or ratings.rotten_tomatoes:
            lines += "\n_Ratings via [MDBList](https://mdblist.com/)._"
        return lines
    lines = f"IMDb: {imdb}\nRotten Tomatoes (critics): {rotten}"
    if ratings.imdb or ratings.rotten_tomatoes:
        lines += "\nRatings via MDBList: https://mdblist.com/"
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
