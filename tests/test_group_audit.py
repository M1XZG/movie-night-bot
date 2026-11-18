import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import group_audit as audit


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-11-18T00:00:00+00:00"
CONFIG = {
    "group_id": "grp_test",
    "expected_user_id": "usr_owner",
    "vrchat_guild_id": "source-guild",
    "discord_guild_id": "guild",
    "category_id": "category",
    "channel_name": "audit",
}


def event(identifier="aud_1"):
    return {
        "id": identifier, "created_at": STAMP, "groupId": "grp_test",
        "eventType": "group.member.join", "actorId": "usr_1",
        "actorDisplayName": "@everyone *name*", "targetId": "usr_2",
        "description": "Joined", "data": {"note": "Kept in full"},
    }


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temporary.name)
        self.database = audit.open_database(self.root / "audit.sqlite3")

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def test_backfill_summarizes_and_preserves_full_records_without_duplicate_posts(self):
        first = event()
        first["data"] = {"unrecognized_future_field": "x" * 4000}
        self.assertEqual(audit.archive(self.database, [first], "Test", STAMP), (1, 1))
        raw = self.database.execute("SELECT raw_json FROM events").fetchone()[0]
        self.assertEqual(json.loads(raw), first)
        self.assertEqual(self.database.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
        audit.archive(self.database, [first, event("aud_2")], "Test", STAMP)
        audit.archive(self.database, [first, event("aud_2")], "Test", STAMP)
        keys = [row[0] for row in self.database.execute("SELECT key FROM outbox ORDER BY sequence")]
        self.assertEqual(keys, ["check:2026-11-18", "event:aud_2"])
        self.assertEqual((self.root / "audit.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_empty_backfill_still_allows_future_events_and_daily_heartbeat(self):
        audit.archive(self.database, [], "Test", STAMP)
        tomorrow = "2026-11-19T00:00:00+00:00"
        audit.archive(self.database, [event()], "Test", tomorrow)
        self.assertEqual(self.database.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.database.execute("SELECT count(*) FROM outbox").fetchone()[0], 3)

    def test_untrusted_audit_text_cannot_ping_and_embeds_are_bounded(self):
        record = event()
        record["description"] = "@everyone " * 2000
        record["data"] = {"big": "z" * 10000}
        payload = audit.event_payload(record)
        embed = payload["embeds"][0]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("@everyone", json.dumps(embed, ensure_ascii=False))
        self.assertLessEqual(len(embed["description"]), 1500)
        self.assertTrue(all(len(field["value"]) <= 1024 for field in embed["fields"]))


class FetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_pagination_freezes_end_date_and_deduplicates_overlap(self):
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            source = Path(directory) / "session.json"
            source.write_text(json.dumps({"source-guild": {"auth_cookie": "test-only"}}))
            config = dict(CONFIG, vrchat_sessions_file=str(source))
            request = AsyncMock(side_effect=[
                {"id": "usr_owner"}, {"name": "Test"},
                {"results": [event()], "hasNext": True},
                {"results": [event(), event("aud_2")], "hasNext": False},
            ])
            with patch.object(audit, "vrchat_get", request):
                records, name, _ = await audit.fetch_events(config)
            self.assertEqual(len(records), 2)
            self.assertEqual(name, "Test")
            paths = [call.args[0] for call in request.await_args_list]
            self.assertIn("offset=0", paths[2])
            self.assertIn("offset=1", paths[3])
            self.assertEqual(paths[2].split("endDate=")[1], paths[3].split("endDate=")[1])

    async def test_pagination_failure_does_not_return_partial_backfill(self):
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            source = Path(directory) / "session.json"
            source.write_text(json.dumps({"source-guild": {"auth_cookie": "test-only"}}))
            config = dict(CONFIG, vrchat_sessions_file=str(source))
            request = AsyncMock(side_effect=[
                {"id": "usr_owner"}, {"name": "Test"},
                {"results": [event()], "hasNext": True},
                {"results": [], "hasNext": True},
            ])
            with patch.object(audit, "vrchat_get", request):
                with self.assertRaises(audit.AuditError):
                    await audit.fetch_events(config)


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_webhook_delivery_is_reconciled_without_reposting(self):
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            database = audit.open_database(root / "audit.sqlite3")
            self.addCleanup(database.close)
            hook_file = root / "webhook"
            hook_file.write_text("https://discord.com/api/webhooks/123/test-only")
            config = dict(CONFIG, webhook_file=str(hook_file))
            audit.archive(database, [], "Test", STAMP)
            messages = []
            posts = []
            overwrites = [{"id": "guild", "type": 0, "allow": "0", "deny": "1024"}]

            async def request(method, path, **kwargs):
                if method == "POST":
                    posts.append(kwargs["payload"])
                    messages.append({
                        "id": "456", "timestamp": STAMP, "webhook_id": "123",
                        "embeds": kwargs["payload"]["embeds"],
                    })
                    raise audit.AuditError("Simulated response lost after successful send")
                if "/messages" in path:
                    return [] if "before=" in path else messages
                if path == "/channels/channel":
                    return {
                        "name": "audit", "parent_id": "category",
                        "permission_overwrites": overwrites,
                    }
                if path == "/channels/category":
                    return {"permission_overwrites": overwrites}
                return {"id": "123", "guild_id": "guild", "channel_id": "channel"}

            discord = AsyncMock()
            discord.request.side_effect = request
            with patch.object(asyncio, "sleep", AsyncMock()):
                with self.assertRaises(audit.AuditError):
                    await audit.deliver(database, config, discord)
                self.assertEqual(await audit.deliver(database, config, discord), 1)
                self.assertEqual(await audit.deliver(database, config, discord), 0)
            self.assertEqual(len(posts), 1)
            self.assertEqual(database.execute("SELECT message_id FROM outbox").fetchone()[0], "456")

    async def test_public_or_moved_channel_blocks_delivery(self):
        discord = AsyncMock()
        discord.request.side_effect = [
            {"id": "123", "guild_id": "guild", "channel_id": "channel"},
            {"name": "audit", "parent_id": "elsewhere"},
        ]
        with patch.object(audit, "webhook_url", return_value="https://discord.com/api/webhooks/123/test"):
            with self.assertRaises(audit.AuditError):
                await audit.deliver(None, CONFIG, discord)
        self.assertTrue(all(call.args[0] == "GET" for call in discord.request.await_args_list))

    async def test_channel_with_changed_permissions_blocks_delivery(self):
        private = [{"id": "guild", "type": 0, "allow": "0", "deny": "1024"}]
        discord = AsyncMock()
        discord.request.side_effect = [
            {"id": "123", "guild_id": "guild", "channel_id": "channel"},
            {"name": "audit", "parent_id": "category", "permission_overwrites": []},
            {"permission_overwrites": private},
        ]
        with patch.object(audit, "webhook_url", return_value="https://discord.com/api/webhooks/123/test"):
            with self.assertRaises(audit.AuditError):
                await audit.deliver(None, CONFIG, discord)
        self.assertTrue(all(call.args[0] == "GET" for call in discord.request.await_args_list))


if __name__ == "__main__":
    unittest.main()
