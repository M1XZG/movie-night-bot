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
import http.cookies
import json
import secrets
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


class VRChatError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.message = message
        self.status = status


class VRChatAuthError(VRChatError):
    """Raised on 401 / expired session -> the guild must re-link."""


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
    err = data.get("error")
    if isinstance(err, dict):
        return err.get("message")
    if isinstance(err, str):
        return err
    return data.get("message")


def _request(method, path, *, cookies=None, basic=None, body=None, timeout=30):
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw, _parse_set_cookies(r.headers.get_all("Set-Cookie"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, raw, _parse_set_cookies(e.headers.get_all("Set-Cookie"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VRChatError(f"network error contacting VRChat: {e}")


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VRChatError(f"network error contacting VRChat: {e}")


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
    data = json.loads(raw) if raw else {}
    auth = ck.get("auth")
    methods = data.get("requiresTwoFactorAuth")
    if methods:
        return {"state": "2fa", "auth": auth, "methods": methods}
    return {
        "state": "ok", "auth": auth, "twofa": ck.get("twoFactorAuth"),
        "display_name": data.get("displayName"), "user_id": data.get("id"),
    }


def _verify_sync(auth, method, code):
    seg = TWOFA_ENDPOINT.get(method.lower())
    if not seg:
        raise VRChatError(f"unsupported 2FA method: {method}")
    status, raw, ck = _request(
        "POST", f"/auth/twofactorauth/{seg}/verify",
        cookies={"auth": auth}, body={"code": code})
    if status != 200:
        raise VRChatError(
            _err_message(raw) or "Incorrect or expired 2FA code.", status)
    data = json.loads(raw) if raw else {}
    if not data.get("verified", False):
        raise VRChatError("2FA code was not accepted.", status)
    return {"auth": auth, "twofa": ck.get("twoFactorAuth")}


def _current_user_sync(cookies):
    status, raw, _ = _request("GET", "/auth/user", cookies=cookies)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status != 200:
        raise VRChatError(_err_message(raw) or f"auth/user failed ({status})", status)
    data = json.loads(raw) if raw else {}
    if data.get("requiresTwoFactorAuth"):
        raise VRChatAuthError("VRChat session needs re-authentication.", 401)
    return data


def _create_event_sync(cookies, group_id, payload):
    status, raw, _ = _request(
        "POST", f"/calendar/{group_id}/event", cookies=cookies, body=payload)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status not in (200, 201):
        raise VRChatError(
            _err_message(raw) or f"could not create event ({status})", status)
    return json.loads(raw) if raw else {}


def _delete_event_sync(cookies, group_id, calendar_id):
    status, raw, _ = _request(
        "DELETE", f"/calendar/{group_id}/{calendar_id}", cookies=cookies)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status == 404:
        return False
    if status not in (200, 204):
        raise VRChatError(
            _err_message(raw) or f"could not delete event ({status})", status)
    return True


def _upload_image_sync(cookies, png_bytes, tag="gallery", filename="movie.png"):
    """Upload a PNG to the linked account; return its File id (file_...)."""
    status, raw = _request_multipart(
        "POST", "/file/image", cookies=cookies,
        fields={"tag": tag}, file_field="file", filename=filename,
        file_bytes=png_bytes, content_type="image/png")
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status not in (200, 201):
        raise VRChatError(
            _err_message(raw) or f"image upload failed ({status})", status)
    data = json.loads(raw) if raw else {}
    file_id = data.get("id")
    if not file_id:
        raise VRChatError("image upload returned no file id", status)
    return file_id


def _delete_file_sync(cookies, file_id):
    status, raw, _ = _request("DELETE", f"/file/{file_id}", cookies=cookies)
    if status == 401:
        raise VRChatAuthError("VRChat session expired.", 401)
    if status == 404:
        return False
    # A file that's still referenced can return 4xx; treat as best-effort.
    if status not in (200, 204):
        raise VRChatError(
            _err_message(raw) or f"could not delete file ({status})", status)
    return True


# --------------------------------------------------------------------------- #
# Async wrappers
# --------------------------------------------------------------------------- #
async def login(username, password):
    return await asyncio.to_thread(_login_sync, username, password)


async def verify_2fa(auth, method, code):
    return await asyncio.to_thread(_verify_sync, auth, method, code)


async def current_user(cookies):
    return await asyncio.to_thread(_current_user_sync, cookies)


async def create_event(cookies, group_id, payload):
    return await asyncio.to_thread(_create_event_sync, cookies, group_id, payload)


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
                        send_notification=True, image_id=None):
    """Assemble the create-calendar-event body. Times are ISO-8601 UTC 'Z'."""
    if category not in VALID_CATEGORIES:
        category = "film_media"
    payload = {
        "title": (title or "Movie Night")[:64],
        "description": (description or "")[:1500] or "Movie night!",
        "startsAt": starts_at_utc,
        "endsAt": ends_at_utc,
        "category": category,
        "accessType": access_type if access_type in ("group", "public") else "group",
        "sendCreationNotification": bool(send_notification),
    }
    if image_id:
        payload["imageId"] = image_id
    return payload
