#!/usr/bin/env python3
"""
Minimal VRChat API client for the Movie Night bot.

Scope: just enough of the (unofficial) VRChat API to log a server owner in and
create / delete **group calendar events**. Stdlib only (urllib); all blocking
calls are run via ``asyncio.to_thread`` so they never stall the discord.py
event loop.

Auth model (VRChat has no OAuth):
  1. ``GET /auth/user`` with HTTP Basic auth (username/password URL-encoded).
     -> sets an ``auth`` cookie; may return ``requiresTwoFactorAuth``.
  2. If 2FA: ``POST /auth/twofactorauth/{totp|emailotp|otp}/verify`` with the
     code and the ``auth`` cookie -> sets a ``twoFactorAuth`` cookie.
  3. Reuse the ``auth`` (+ ``twoFactorAuth``) cookies for subsequent calls.

We persist ONLY the resulting session cookies, never the password.

VRChat asks API consumers to send a descriptive User-Agent identifying the app
and a contact, and to respect rate limits. Do not hammer the API.
"""
import asyncio
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import http.client
import http.cookies
import json
import re
import secrets
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.vrchat.cloud/api/1"
USER_AGENT = "MovieNightBot/0.1 (+https://github.com/M1XZG/movie-night-bot)"

# Map the method names VRChat returns -> the verify endpoint segment.
TWOFA_ENDPOINT = {
    "totp": "totp",
    "otp": "otp",
    "emailotp": "emailotp",
}

VALID_CATEGORIES = {
    "arts", "avatars", "dance", "education", "exploration", "film_media",
    "gaming", "hangout", "music", "other", "performance", "roleplaying",
    "wellness",
}

# VRChat client platforms an event can target. Members only receive the
# in-game "event starting" notification if their client matches one of these,
# so we default to all of them (matching the website's create-event form).
VALID_PLATFORMS = {"standalonewindows", "android", "ios"}
DEFAULT_PLATFORMS = ["standalonewindows", "android", "ios"]
MAX_REQUEST_ATTEMPTS = 3
MAX_RETRY_DELAY = 10
CALENDAR_PAGE_SIZE = 100
MAX_CALENDAR_PAGES = 10
MAX_CALENDAR_SECONDS = 60


class VRChatError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.message = message
        self.status = status


class VRChatAuthError(VRChatError):
    """Raised on 401 / expired session -> the guild must re-link."""


class VRChatWriteUncertainError(VRChatError):
    """A write may have succeeded. Preserve artwork and never replay its POST.

    For calendar creation, persist ``existing_ids`` with the submitted payload
    to use ``find_created_event`` after the session/network has recovered.
    ``calendar_id`` identifies an accepted event whose fields need review.
    """

    preserve_image = True

    def __init__(self, message, status=None, *, operation="create",
                 calendar_id=None, existing_ids=(), validation_problems=()):
        super().__init__(message, status)
        self.operation = operation
        self.calendar_id = calendar_id
        self.existing_ids = tuple(existing_ids)
        self.validation_problems = tuple(validation_problems)


# --------------------------------------------------------------------------- #
# Low-level (blocking) request
# --------------------------------------------------------------------------- #
def _parse_set_cookies(lines) -> dict:
    out = {}
    for line in lines or []:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(line)
        except http.cookies.CookieError:
            continue
        for k, morsel in jar.items():
            out[k] = morsel.value
    return out


def _err_message(raw: str):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        message = err.get("message")
        return message if isinstance(message, str) else None
    if isinstance(err, str):
        return err
    message = data.get("message")
    return message if isinstance(message, str) else None


def _json_object(raw, operation, status=200):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise VRChatError(f"{operation} returned invalid JSON.", status) from None
    if not isinstance(data, dict):
        raise VRChatError(f"{operation} returned an invalid response.", status)
    if "error" in data:
        raise VRChatError(_err_message(raw) or f"{operation} returned an error.", status)
    return data


def _retry_delay(headers, attempt):
    value = headers.get("Retry-After") if headers else None
    delay = 2 ** attempt
    if value:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                delay = (parsedate_to_datetime(value) -
                         datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    # Do not retry earlier than a long Retry-After, or sleep indefinitely.
    return max(0, delay) if delay <= MAX_RETRY_DELAY else None


def _calendar_time_left(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VRChatError("VRChat calendar enumeration deadline exceeded.")
    return remaining


def _wait_to_retry(delay, deadline):
    if deadline is not None and delay >= _calendar_time_left(deadline):
        raise VRChatError("VRChat calendar enumeration deadline exceeded.")
    time.sleep(delay)


def _send_request(req, timeout, deadline=None):
    retry_safe = req.get_method() in ("GET", "DELETE")
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        request_timeout = timeout
        if deadline is not None:
            request_timeout = min(timeout, _calendar_time_left(deadline))
        try:
            try:
                with urllib.request.urlopen(req, timeout=request_timeout) as response:
                    status = response.status
                    raw = response.read().decode("utf-8", "replace")
                    headers = response.headers
            except urllib.error.HTTPError as error:
                status = error.code
                raw = error.read().decode("utf-8", "replace")
                headers = error.headers
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException) as error:
            if retry_safe and attempt < MAX_REQUEST_ATTEMPTS - 1:
                _wait_to_retry(2 ** attempt, deadline)
                continue
            raise VRChatError(f"network error contacting VRChat: {error}") from error
        if deadline is not None:
            _calendar_time_left(deadline)
        delay = _retry_delay(headers, attempt)
        if (retry_safe and (status == 429 or 500 <= status < 600)
                and attempt < MAX_REQUEST_ATTEMPTS - 1 and delay is not None):
            _wait_to_retry(delay, deadline)
            continue
        return status, raw, _parse_set_cookies(
            headers.get_all("Set-Cookie") if headers else [])


def _request(method, path, *, cookies=None, basic=None, body=None, timeout=30,
             deadline=None):
    url = API + path
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if basic is not None:
        user = urllib.parse.quote(basic[0], safe="")
        pw = urllib.parse.quote(basic[1], safe="")
        tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
        headers["Authorization"] = "Basic " + tok
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{k}={v}" for k, v in cookies.items() if v)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return _send_request(req, timeout, deadline)


def _request_multipart(method, path, *, cookies, fields, file_field,
                       filename, file_bytes, content_type="image/png",
                       timeout=60):
    """Send a multipart/form-data request (used for image uploads)."""
    url = API + path
    boundary = "----MovieNightBot" + secrets.token_hex(16)
    crlf = b"\r\n"
    parts = []
    for name, value in (fields or {}).items():
        if value is None:
            continue
        parts.append(b"--" + boundary.encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode())
    parts.append(b"--" + boundary.encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"'.encode())
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(b"")
    parts.append(file_bytes)
    parts.append(b"--" + boundary.encode() + b"--")
    parts.append(b"")
    data = crlf.join(parts)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    status, raw, _ = _send_request(req, timeout)
    return status, raw


# --------------------------------------------------------------------------- #
# Sync operations
# --------------------------------------------------------------------------- #
def _login_sync(username, password):
    status, raw, ck = _request("GET", "/auth/user", basic=(username, password))
    if status == 401:
        raise VRChatAuthError(
            _err_message(raw) or "Invalid VRChat username or password.", 401)
    if status != 200:
        raise VRChatError(_err_message(raw) or f"login failed ({status})", status)
    data = _json_object(raw, "login", status)
    auth = ck.get("auth")
    if not auth:
        raise VRChatError("VRChat login returned no session cookie.", status)
    methods = data.get("requiresTwoFactorAuth")
    if methods:
        if (not isinstance(methods, list) or
                any(not isinstance(method, str) for method in methods)):
            raise VRChatError("VRChat returned invalid 2FA methods.", status)
        methods = [method for method in methods if method in TWOFA_ENDPOINT]
        if not methods:
            raise VRChatError("VRChat requires an unsupported 2FA method.", status)
        return {"state": "2fa", "auth": auth, "methods": methods}
    _validate_id(data.get("id"), "usr")
    return {
        "state": "ok", "auth": auth, "twofa": ck.get("twoFactorAuth"),
        "display_name": data.get("displayName"), "user_id": data.get("id"),
    }


def _verify_sync(auth, method, code):
    seg = TWOFA_ENDPOINT.get(method.lower()) if isinstance(method, str) else None
    if not seg:
        raise VRChatError(f"unsupported 2FA method: {method}")
    status, raw, ck = _request(
        "POST", f"/auth/twofactorauth/{seg}/verify",
        cookies={"auth": auth}, body={"code": code})
    if status == 401:
        raise VRChatAuthError("VRChat session expired during 2FA.", 401)
    if status != 200:
        raise VRChatError(
            _err_message(raw) or "Incorrect or expired 2FA code.", status)
    data = _json_object(raw, "2FA verification", status)
    if data.get("verified") is not True:
        raise VRChatError("2FA code was not accepted.", status)
    if not ck.get("twoFactorAuth"):
        raise VRChatError("2FA verification returned no session cookie.", status)
    return {"auth": auth, "twofa": ck.get("twoFactorAuth")}


def _current_user_sync(cookies):
    status, raw, _ = _request("GET", "/auth/user", cookies=cookies)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status != 200:
        raise VRChatError(_err_message(raw) or f"auth/user failed ({status})", status)
    data = _json_object(raw, "auth/user", status)
    if data.get("requiresTwoFactorAuth"):
        raise VRChatAuthError("VRChat session needs re-authentication.", 401)
    _validate_id(data.get("id"), "usr")
    return data


def _validate_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(
            rf"{prefix}_[A-Za-z0-9-]+", value):
        raise VRChatError(f"VRChat returned an invalid {prefix} id.")
    return value


def _normalise_event_title(value):
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKD", _normalise_event_description(value)).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W_]+", value))


def _normalise_event_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    return None


def _normalise_event_description(value):
    """VRChat stores these punctuation substitutions in calendar text."""
    if not isinstance(value, str):
        return None
    return value.translate(str.maketrans({
        "\u2024": ".", "\u201a": ",", "\u02f8": ":", "\u2044": "/", "\u01c3": "!",
    }))


def _list_events_sync(cookies, group_id):
    """Enumerate one calendar snapshot within a shared page/retry time budget."""
    _validate_id(group_id, "grp")
    deadline = time.monotonic() + MAX_CALENDAR_SECONDS
    events, seen = [], set()
    offset = 0
    for _ in range(MAX_CALENDAR_PAGES):
        _calendar_time_left(deadline)
        status, raw, _ = _request(
            "GET", f"/calendar/{group_id}?n={CALENDAR_PAGE_SIZE}&offset={offset}",
            cookies=cookies, deadline=deadline)
        _calendar_time_left(deadline)
        if status == 401:
            raise VRChatAuthError("VRChat session expired.", 401)
        if status != 200:
            raise VRChatError(
                _err_message(raw) or f"could not read calendar ({status})", status)
        data = _json_object(raw, "calendar listing", status)
        results = data.get("results")
        if not isinstance(results, list):
            raise VRChatError("VRChat calendar returned invalid results.", status)
        for event in results:
            if not isinstance(event, dict):
                raise VRChatError("VRChat calendar returned an invalid event.", status)
            event_id = _validate_id(event.get("id"), "cal")
            if event_id in seen:
                raise VRChatError("VRChat calendar pagination repeated an event.", status)
            seen.add(event_id)
            events.append(event)
        has_next = data.get("hasNext", len(results) == CALENDAR_PAGE_SIZE)
        if not isinstance(has_next, bool):
            raise VRChatError("VRChat calendar returned invalid pagination.", status)
        if not has_next:
            _calendar_time_left(deadline)
            return events
        if not results:
            raise VRChatError("VRChat calendar pagination made no progress.", status)
        offset += len(results)
    raise VRChatError("VRChat calendar exceeded the pagination limit.")


def _event_problems(event, payload):
    problems = []
    try:
        _validate_id(event.get("id"), "cal")
    except VRChatError:
        problems.append("VRChat returned no valid calendar id")
    if event.get("isDraft") is not False:
        problems.append("VRChat left the event as a draft or did not confirm publication")
    actual_platforms = event.get("platforms")
    expected_platforms = payload.get("platforms", [])
    if (not isinstance(actual_platforms, list) or not actual_platforms
            or any(not isinstance(platform, str) or platform not in VALID_PLATFORMS
                   for platform in actual_platforms)
            or not set(expected_platforms).issubset(actual_platforms)):
        problems.append("VRChat returned no valid target platforms or omitted requested platforms")
    for field in ("title", "startsAt", "endsAt", "description",
                  "category", "accessType", "imageId"):
        if field not in payload:
            continue
        expected, actual = payload[field], event.get(field)
        if field == "title":
            matches = (bool(_normalise_event_title(expected)) and
                       _normalise_event_title(expected) == _normalise_event_title(actual))
        elif field in ("startsAt", "endsAt"):
            matches = (_normalise_event_time(expected) is not None and
                       _normalise_event_time(expected) == _normalise_event_time(actual))
        elif field == "description":
            matches = (isinstance(expected, str) and
                       _normalise_event_description(expected) == _normalise_event_description(actual))
        else:
            matches = actual == expected
        if not matches:
            problems.append(f"VRChat did not confirm the requested {field}")
    return problems


def _find_created_event_sync(cookies, group_id, payload, existing_ids):
    """Return one new, published event matching every submitted calendar field."""
    payload = _validate_calendar_payload(payload, creation=True)
    if not isinstance(existing_ids, (list, tuple, set)):
        raise VRChatError("Reconciliation requires the before-write ID baseline.")
    for event_id in existing_ids:
        _validate_id(event_id, "cal")
    excluded = set(existing_ids)
    matches = [
        event for event in _list_events_sync(cookies, group_id)
        if event["id"] not in excluded and not _event_problems(event, payload)
    ]
    return matches[0] if len(matches) == 1 else None


def _get_event_sync(cookies, group_id, calendar_id):
    _validate_id(group_id, "grp")
    _validate_id(calendar_id, "cal")
    status, raw, _ = _request(
        "GET", f"/calendar/{group_id}/{calendar_id}", cookies=cookies)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status != 200:
        raise VRChatError(
            _err_message(raw) or f"could not read event ({status})", status)
    event = _json_object(raw, "calendar event", status)
    if event.get("id") != calendar_id:
        raise VRChatError("VRChat returned a different calendar event.", status)
    return event


def _reconcile_create_sync(cookies, group_id, payload, existing_ids, error):
    for attempt in range(3):
        try:
            event = _find_created_event_sync(cookies, group_id, payload, existing_ids)
            if event:
                return event
        except VRChatAuthError as auth_error:
            error = VRChatError(f"{error.message} {auth_error.message}", 401)
            break
        except VRChatError:
            pass
        if attempt < 2:
            time.sleep(1)
    raise VRChatWriteUncertainError(
        f"{error.message} Calendar creation could not be confirmed; "
        "retain its image and reconcile before retrying.",
        error.status, existing_ids=existing_ids) from error


def _create_event_sync(cookies, group_id, payload, existing_ids=None):
    _validate_id(group_id, "grp")
    payload = _validate_calendar_payload(payload, creation=True)
    # Without a before-write baseline, an older identical booking could be
    # mistaken for this POST after an ambiguous response.
    if existing_ids is None:
        existing_ids = tuple(event["id"] for event in _list_events_sync(cookies, group_id))
    else:
        if not isinstance(existing_ids, (list, tuple, set)):
            raise VRChatError("Calendar creation requires a before-write ID baseline.")
        existing_ids = tuple(_validate_id(event_id, "cal") for event_id in existing_ids)
    try:
        status, raw, _ = _request(
            "POST", f"/calendar/{group_id}/event", cookies=cookies, body=payload)
    except VRChatError as error:
        return _reconcile_create_sync(cookies, group_id, payload, existing_ids, error)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status not in (200, 201):
        error = VRChatError(
            _err_message(raw) or f"could not create event ({status})", status)
        if 200 <= status < 300 or status == 408 or status >= 500:
            return _reconcile_create_sync(cookies, group_id, payload, existing_ids, error)
        raise error
    try:
        event = _json_object(raw, "calendar creation", status)
        _validate_id(event.get("id"), "cal")
    except VRChatError as error:
        return _reconcile_create_sync(cookies, group_id, payload, existing_ids, error)
    if event["id"] in existing_ids:
        return _reconcile_create_sync(
            cookies, group_id, payload, existing_ids,
            VRChatError("VRChat returned a pre-existing calendar event.", status))
    problems = _event_problems(event, payload)
    if problems:
        # A partial response does not prove the stored event is wrong.
        try:
            stored = _get_event_sync(cookies, group_id, event["id"])
            problems = _event_problems(stored, payload)
            if not problems:
                return stored
        except VRChatError as error:
            raise VRChatWriteUncertainError(
                f"VRChat creation response could not be verified: {error.message}",
                error.status, calendar_id=event["id"], existing_ids=existing_ids) from error
        # Creation notifications cannot be undone. Retain the accepted ID so
        # another unknown text transformation cannot cause delete/recreate loops.
        raise VRChatWriteUncertainError(
            "; ".join(problems) + "; created event retained for review.",
            status, calendar_id=event["id"], existing_ids=existing_ids,
            validation_problems=problems)
    return event


def _update_event_sync(cookies, group_id, calendar_id, payload):
    _validate_id(group_id, "grp")
    _validate_id(calendar_id, "cal")
    payload = _validate_calendar_payload(payload)
    try:
        status, raw, _ = _request(
            "PUT", f"/calendar/{group_id}/{calendar_id}/event",
            cookies=cookies, body=payload)
    except VRChatError as error:
        return _reconcile_update_sync(cookies, group_id, calendar_id, payload, error)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status == 404:
        raise VRChatError("VRChat calendar event no longer exists.", 404)
    if status != 200:
        error = VRChatError(
            _err_message(raw) or f"could not update event ({status})", status)
        if 200 <= status < 300 or status == 408 or status >= 500:
            return _reconcile_update_sync(cookies, group_id, calendar_id, payload, error)
        raise error
    try:
        event = _json_object(raw, "calendar update", status)
        if event.get("id") != calendar_id:
            raise VRChatError("VRChat returned a different calendar event.", status)
        problems = _event_problems(event, payload)
        if problems:
            raise VRChatError("; ".join(problems), status)
        return event
    except VRChatError as error:
        return _reconcile_update_sync(cookies, group_id, calendar_id, payload, error)


def _reconcile_update_sync(cookies, group_id, calendar_id, payload, error):
    for attempt in range(3):
        try:
            event = _get_event_sync(cookies, group_id, calendar_id)
            if not _event_problems(event, payload):
                return event
        except VRChatAuthError as auth_error:
            error = VRChatError(f"{error.message} {auth_error.message}", 401)
            break
        except VRChatError:
            pass
        if attempt < 2:
            time.sleep(1)
    raise VRChatWriteUncertainError(
        f"{error.message} Calendar update could not be confirmed; retain both images.",
        error.status, operation="update", calendar_id=calendar_id) from error


def _validate_calendar_payload(payload, *, creation=False):
    if not isinstance(payload, dict) or not payload:
        raise VRChatError("Calendar writes require an event payload.")
    if "isDraft" in payload and payload["isDraft"] is not False:
        raise VRChatError("Movie calendar events must be published.")
    platforms = payload.get("platforms", [])
    if (not isinstance(platforms, list) or
            any(not isinstance(platform, str) or platform not in VALID_PLATFORMS
                for platform in platforms) or
            ("platforms" in payload and not platforms)):
        raise VRChatError("Movie calendar events require valid target platforms.")
    for field in ("title", "description", "category", "accessType"):
        if field in payload and not isinstance(payload[field], str):
            raise VRChatError(f"Calendar {field} must be text.")
    for field in ("startsAt", "endsAt"):
        if field in payload and _normalise_event_time(payload[field]) is None:
            raise VRChatError(f"Calendar {field} requires a timezone-aware timestamp.")
    if payload.get("imageId"):
        _validate_id(payload["imageId"], "file")
    result = payload.copy()
    if creation:
        start = _normalise_event_time(result.get("startsAt"))
        end = _normalise_event_time(result.get("endsAt"))
        if (not start or not end or start >= end or
                not _normalise_event_title(result.get("title"))):
            raise VRChatError("Calendar creation requires a title and valid start/end times.")
        result.setdefault("isDraft", False)
        result.setdefault("platforms", list(DEFAULT_PLATFORMS))
    return result


def _delete_event_sync(cookies, group_id, calendar_id):
    _validate_id(group_id, "grp")
    _validate_id(calendar_id, "cal")
    status, raw, _ = _request(
        "DELETE", f"/calendar/{group_id}/{calendar_id}", cookies=cookies)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status == 404:
        return False
    if status not in (200, 204):
        raise VRChatError(
            _err_message(raw) or f"could not delete event ({status})", status)
    if raw:
        _json_object(raw, "calendar deletion", status)
    return True


def _upload_image_sync(cookies, png_bytes, tag="gallery", filename="movie.png"):
    """Upload a PNG to the linked account; return its File id (file_...)."""
    if not isinstance(png_bytes, bytes) or not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VRChatError("VRChat image upload requires PNG bytes.")
    try:
        status, raw = _request_multipart(
            "POST", "/file/image", cookies=cookies,
            fields={"tag": tag}, file_field="file", filename=filename,
            file_bytes=png_bytes, content_type="image/png")
    except VRChatError as error:
        raise VRChatWriteUncertainError(
            "Image upload may have succeeded; do not automatically upload it again.",
            operation="image_upload") from error
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status not in (200, 201):
        if 200 <= status < 300 or status == 408 or status >= 500:
            raise VRChatWriteUncertainError(
                _err_message(raw) or f"image upload outcome is unknown ({status})",
                status, operation="image_upload")
        raise VRChatError(
            _err_message(raw) or f"image upload failed ({status})", status)
    try:
        data = _json_object(raw, "image upload", status)
        return _validate_id(data.get("id"), "file")
    except VRChatError as error:
        raise VRChatWriteUncertainError(
            f"{error.message} Image upload outcome is unknown.",
            status, operation="image_upload") from error


def _delete_file_sync(cookies, file_id):
    _validate_id(file_id, "file")
    status, raw, _ = _request("DELETE", f"/file/{file_id}", cookies=cookies, timeout=10)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status not in (200, 204, 404):
        raise VRChatError(
            _err_message(raw) or f"could not delete file ({status})", status)
    if raw and status != 404:
        _json_object(raw, "file deletion", status)
    verified_status, _, _ = _request("GET", f"/file/{file_id}", cookies=cookies, timeout=10)
    if verified_status == 401:
        raise VRChatAuthError("VRChat session expired while verifying artwork deletion.", 401)
    if verified_status != 404:
        raise VRChatError("VRChat artwork deletion could not be confirmed.", verified_status)
    return status != 404


# --------------------------------------------------------------------------- #
# Async wrappers
# --------------------------------------------------------------------------- #
async def login(username, password):
    return await asyncio.to_thread(_login_sync, username, password)


async def verify_2fa(auth, method, code):
    return await asyncio.to_thread(_verify_sync, auth, method, code)


async def current_user(cookies):
    return await asyncio.to_thread(_current_user_sync, cookies)


async def create_event(cookies, group_id, payload, *, existing_ids=None):
    """Create once; an uncertain error requires reconciliation, not image cleanup.

    For crash recovery, save a ``list_events`` ID baseline with the pending
    payload/artwork before calling this function and pass it as ``existing_ids``.
    If omitted, the client reads its own baseline before submitting the POST.
    """
    return await asyncio.to_thread(
        _create_event_sync, cookies, group_id, payload, existing_ids)


async def list_events(cookies, group_id):
    return await asyncio.to_thread(_list_events_sync, cookies, group_id)


async def get_event(cookies, group_id, calendar_id):
    return await asyncio.to_thread(_get_event_sync, cookies, group_id, calendar_id)


async def find_created_event(cookies, group_id, payload, existing_ids):
    return await asyncio.to_thread(
        _find_created_event_sync, cookies, group_id, payload, existing_ids)


async def update_event(cookies, group_id, calendar_id, payload):
    return await asyncio.to_thread(
        _update_event_sync, cookies, group_id, calendar_id, payload)


async def delete_event(cookies, group_id, calendar_id):
    return await asyncio.to_thread(
        _delete_event_sync, cookies, group_id, calendar_id)


async def upload_image(cookies, png_bytes, tag="gallery", filename="movie.png"):
    return await asyncio.to_thread(
        _upload_image_sync, cookies, png_bytes, tag, filename)


async def delete_file(cookies, file_id):
    return await asyncio.to_thread(_delete_file_sync, cookies, file_id)


def build_event_payload(title, description, starts_at_utc, ends_at_utc, *,
                        category="film_media", access_type="group",
                        send_notification=True, image_id=None, platforms=None):
    """Assemble the create-calendar-event body. Times are ISO-8601 UTC 'Z'."""
    if category not in VALID_CATEGORIES:
        category = "film_media"
    requested = [platforms] if isinstance(platforms, str) else platforms
    if not isinstance(requested, (list, tuple, set)):
        requested = DEFAULT_PLATFORMS
    plats = list(dict.fromkeys(
        p for p in (requested or DEFAULT_PLATFORMS)
        if isinstance(p, str) and p in VALID_PLATFORMS))
    if not plats:
        plats = list(DEFAULT_PLATFORMS)
    payload = {
        "title": (title or "Movie Night")[:64],
        "description": (description or "")[:1500] or "Movie night!",
        "startsAt": starts_at_utc,
        "endsAt": ends_at_utc,
        "category": category,
        "accessType": access_type if access_type in ("group", "public") else "group",
        "sendCreationNotification": bool(send_notification),
        "isDraft": False,
        "platforms": plats,
    }
    if image_id:
        payload["imageId"] = image_id
    return payload
