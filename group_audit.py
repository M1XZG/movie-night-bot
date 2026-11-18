#!/usr/bin/env python3
"""Archive VRChat group audit entries and deliver them through a Discord webhook."""

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlencode, urlsplit

import aiohttp

import vrchat


DISCORD = "https://discord.com/api/v10"


class AuditError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
    temporary.chmod(0o600)
    temporary.replace(path)


def text(value, limit=900):
    value = str(value or "Not specified").replace("@", "@\u200b")
    return re.sub(r"([\\`*_{}\[\]()|>~])", r"\\\1", value)[:limit]


def permission_overwrites(channel):
    return sorted(
        channel.get("permission_overwrites", []),
        key=lambda row: (row["type"], row["id"]),
    )


def event_payload(event):
    fields = [
        {"name": "Actor", "value": text(event.get("actorDisplayName")), "inline": True},
        {"name": "Target", "value": text(event.get("targetId")), "inline": True},
    ]
    if event.get("data"):
        fields.append({
            "name": "Details",
            "value": text(json.dumps(event["data"], ensure_ascii=False), 1000),
        })
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": text(event.get("eventType"), 200),
            "description": text(event.get("description"), 1500),
            "color": 0xC08BFF,
            "timestamp": event["created_at"],
            "fields": fields,
            "footer": {"text": "vrchat-audit:" + event["id"]},
        }],
    }


def open_database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = sqlite3.connect(path)
    path.chmod(0o600)
    database.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            queued_at TEXT NOT NULL,
            message_id TEXT
        );
    """)
    return database


def archive(database, events, group_name, captured_at):
    initialized = database.execute(
        "SELECT 1 FROM metadata WHERE key='initialized'"
    ).fetchone() is not None
    added = 0

    def enqueue(key, payload):
        database.execute(
            "INSERT OR IGNORE INTO outbox(key,payload,queued_at) VALUES (?,?,?)",
            (key, json.dumps(payload, ensure_ascii=False), captured_at),
        )

    with database:
        for event in sorted(events, key=lambda row: (row["created_at"], row["id"])):
            cursor = database.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?)",
                (event["id"], event["created_at"], json.dumps(event, ensure_ascii=False)),
            )
            if cursor.rowcount:
                added += 1
                if initialized:
                    enqueue("event:" + event["id"], event_payload(event))
        total, oldest, newest = database.execute(
            "SELECT count(*),min(created_at),max(created_at) FROM events"
        ).fetchone()
        description = (
            f"Archived {added} available entries. Historical entries are stored locally, "
            "not posted individually."
            if not initialized else
            f"Archived {added} new entries. New events are queued above this summary."
        )
        description += f"\nTotal archived: {total}."
        if oldest:
            description += f"\nArchive range: {oldest} to {newest}."
        key = "check:" + captured_at[:10]
        enqueue(key, {
            "allowed_mentions": {"parse": []},
            "embeds": [{
                "title": text(group_name, 180) + (
                    " | Archive initialized" if not initialized else " | Daily audit check"
                ),
                "description": description,
                "color": 0x70E7FF,
                "timestamp": captured_at,
                "footer": {"text": "vrchat-audit:" + key},
            }],
        })
        database.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('initialized',?)", (captured_at,)
        )
        database.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('last_fetch',?)", (captured_at,)
        )
    return added, total


async def vrchat_get(path, cookies):
    for attempt in range(5):
        try:
            status, body, _ = await asyncio.to_thread(
                vrchat._request, "GET", path, cookies=cookies
            )
        except vrchat.VRChatError:
            raise AuditError("VRChat network request failed") from None
        await asyncio.sleep(1.1)
        if status == 429 or status >= 500:
            await asyncio.sleep(2 ** attempt)
            continue
        if status != 200:
            raise AuditError(f"VRChat request failed (HTTP {status}); check the linked session and group permissions")
        try:
            return json.loads(body)
        except ValueError:
            raise AuditError("VRChat returned invalid JSON") from None
    raise AuditError("VRChat remained rate-limited or unavailable")


async def fetch_events(config):
    try:
        saved = json.loads(Path(config["vrchat_sessions_file"]).read_text())
        session = saved[config["vrchat_guild_id"]]
        cookies = {"auth": session["auth_cookie"], "twoFactorAuth": session.get("twofa_cookie")}
    except (OSError, ValueError, KeyError):
        raise AuditError("Cannot load the configured VRChat session") from None
    user = await vrchat_get("/auth/user", cookies)
    if user.get("id") != config["expected_user_id"]:
        raise AuditError("VRChat session does not match the configured account")
    group_id = config["group_id"]
    group = await vrchat_get(f"/groups/{group_id}", cookies)
    end = now()
    events = {}
    offset = 0
    while True:
        query = urlencode({
            "n": 100, "offset": offset,
            "startDate": "1970-01-01T00:00:00Z", "endDate": end,
        })
        page = await vrchat_get(f"/groups/{group_id}/auditLogs?{query}", cookies)
        rows = page.get("results")
        if not isinstance(rows, list) or not isinstance(page.get("hasNext"), bool):
            raise AuditError("Unexpected VRChat audit pagination response")
        previous_count = len(events)
        for event in rows:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("id"), str)
                or not isinstance(event.get("created_at"), str)
                or event.get("groupId") != group_id
            ):
                raise AuditError("Unexpected VRChat audit entry")
            events[event["id"]] = event
        if not page["hasNext"]:
            break
        if not rows or len(events) == previous_count:
            raise AuditError("VRChat audit pagination made no progress")
        offset += len(rows)
    return list(events.values()), group["name"], end


class Discord:
    def __init__(self, session, token):
        self.session = session
        self.token = token

    async def request(self, method, path, *, payload=None, authenticated=True):
        headers = {"Authorization": "Bot " + self.token} if authenticated else {}
        url = DISCORD + path if path.startswith("/") else path
        for _ in range(5):
            try:
                async with self.session.request(
                    method, url, json=payload, headers=headers, allow_redirects=False
                ) as response:
                    raw = await response.text()
                    try:
                        data = json.loads(raw) if raw else None
                    except ValueError:
                        data = None
                    if response.status == 429:
                        delay = data.get("retry_after", 5) if isinstance(data, dict) else 5
                        await asyncio.sleep(min(max(float(delay), 1), 300))
                        continue
                    if not 200 <= response.status < 300:
                        raise AuditError(f"Discord request failed (HTTP {response.status})")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # A POST may have succeeded. Leave it queued and reconcile on the next run.
                raise AuditError("Discord connection failed; delivery remains queued") from None
        raise AuditError("Discord remained rate-limited")


def webhook_url(config):
    try:
        url = Path(config["webhook_file"]).read_text().strip()
    except OSError:
        raise AuditError("Cannot load the Discord webhook file; run setup first") from None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.netloc != "discord.com"
        or not re.fullmatch(r"/api(?:/v10)?/webhooks/\d+/[\w.-]+", parsed.path)
        or parsed.query or parsed.fragment
    ):
        raise AuditError("Unexpected Discord webhook URL")
    return url


async def setup(config, discord):
    channels = await discord.request("GET", f'/guilds/{config["discord_guild_id"]}/channels')
    category = next((row for row in channels if row["id"] == config["category_id"]), None)
    if not category or category["type"] != 4:
        raise AuditError("Configured admin category does not exist")
    overwrites = permission_overwrites(category)
    if not any(
        row["id"] == config["discord_guild_id"] and int(row["deny"]) & 1024
        for row in overwrites
    ):
        raise AuditError("Admin category must deny View Channel to @everyone")
    matches = [
        row for row in channels
        if row["name"] == config["channel_name"] and row.get("parent_id") == category["id"]
    ]
    if len(matches) > 1:
        raise AuditError("More than one matching audit channel exists")
    if matches:
        channel = matches[0]
        if permission_overwrites(channel) != overwrites:
            raise AuditError("Existing channel permissions differ from the admin category")
    else:
        channel = await discord.request(
            "POST", f'/guilds/{config["discord_guild_id"]}/channels',
            payload={
                "name": config["channel_name"], "type": 0, "parent_id": category["id"],
                "permission_overwrites": overwrites,
                "topic": "VRChat group audit archive. Daily at 00:00 UTC; full records retained locally.",
            },
        )
    user = await discord.request("GET", "/users/@me")
    hooks = await discord.request("GET", f'/channels/{channel["id"]}/webhooks')
    hook = next((
        row for row in hooks
        if row["name"] == "VRChat Group Audit" and row.get("user", {}).get("id") == user["id"]
    ), None)
    if hook is None:
        hook = await discord.request(
            "POST", f'/channels/{channel["id"]}/webhooks',
            payload={"name": "VRChat Group Audit"},
        )
    if not hook.get("token"):
        raise AuditError("Cannot retrieve the audit webhook token")
    url = f'https://discord.com/api/webhooks/{hook["id"]}/{hook["token"]}'
    private_write(config["webhook_file"], url + "\n")
    print(f'Audit channel ready: {channel["id"]}')


async def deliver(database, config, discord):
    url = webhook_url(config)
    hook = await discord.request("GET", url, authenticated=False)
    if hook.get("guild_id") != config["discord_guild_id"]:
        raise AuditError("Webhook belongs to a different Discord server")
    channel = await discord.request("GET", f'/channels/{hook["channel_id"]}')
    if channel.get("parent_id") != config["category_id"] or channel["name"] != config["channel_name"]:
        raise AuditError("Webhook channel is not the configured admin audit channel")
    category = await discord.request("GET", f'/channels/{config["category_id"]}')
    overwrites = permission_overwrites(category)
    if (
        permission_overwrites(channel) != overwrites
        or not any(
            row["id"] == config["discord_guild_id"] and int(row["deny"]) & 1024
            for row in overwrites
        )
    ):
        raise AuditError("Audit channel no longer inherits private admin permissions")
    pending = database.execute(
        "SELECT key,payload,queued_at FROM outbox WHERE message_id IS NULL ORDER BY sequence"
    ).fetchall()
    if not pending:
        return 0
    # Reconcile a successful webhook POST followed by a crash before its SQLite commit.
    markers = {}
    before = None
    earliest = min(row[2] for row in pending)
    for _ in range(100):
        query = "?limit=100" + ("&before=" + before if before else "")
        messages = await discord.request("GET", f'/channels/{hook["channel_id"]}/messages{query}')
        for message in messages:
            if message.get("webhook_id") == hook["id"]:
                for embed in message.get("embeds", []):
                    marker = embed.get("footer", {}).get("text", "")
                    markers[marker] = message["id"]
        if not messages or min(row["timestamp"] for row in messages) < earliest:
            break
        before = min((row["id"] for row in messages), key=int)
    else:
        raise AuditError("Discord reconciliation history limit reached; delivery remains queued")
    delivered = 0
    for key, raw, _ in pending:
        payload = json.loads(raw)
        marker = payload["embeds"][0]["footer"]["text"]
        message_id = markers.get(marker)
        if message_id is None:
            message = await discord.request("POST", url + "?wait=true", payload=payload, authenticated=False)
            message_id = message["id"]
        with database:
            database.execute("UPDATE outbox SET message_id=? WHERE key=?", (message_id, key))
        delivered += 1
        await asyncio.sleep(1)
    return delivered


async def run(config, setup_only=False):
    token = Path(config["discord_token_file"]).read_text().strip()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        discord = Discord(session, token)
        if setup_only:
            await setup(config, discord)
            return
        database = open_database(config["database"])
        try:
            events, name, captured = await fetch_events(config)
            added, total = archive(database, events, name, captured)
            delivered = await deliver(database, config, discord)
            print(f"Archived {added} new entries; {total} total; delivered {delivered} queued messages.")
        except AuditError as error:
            stamp = now()
            key = "error:" + stamp[:10]
            payload = {
                "allowed_mentions": {"parse": []},
                "embeds": [{
                    "title": "VRChat audit monitor needs attention",
                    "description": text(error, 1500) + (
                        "\nExisting records remain archived. Queued deliveries are retained for retry. "
                        "A failed or expired VRChat session may need re-linking in the movie bot."
                    ),
                    "color": 0xFF879C,
                    "timestamp": stamp,
                    "footer": {"text": "vrchat-audit:" + key},
                }],
            }
            with database:
                database.execute(
                    "INSERT OR IGNORE INTO outbox(key,payload,queued_at) VALUES (?,?,?)",
                    (key, json.dumps(payload), stamp),
                )
            try:
                await deliver(database, config, discord)
            except AuditError:
                pass
            raise
        finally:
            database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--setup", action="store_true", help="Create/reuse the private audit channel and webhook.")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = json.loads(args.config.read_text())
        lock_path = Path(config["database"]).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(run(config, args.setup))
    except AuditError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("Audit monitor failed; check configuration, permissions and connectivity.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
