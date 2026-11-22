import asyncio
import copy
import datetime as dt
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import discord

import bot
from find_backdrop import BackdropError
from ratings import MovieRatings


class SchedulingReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def schedule(self, *, announcement_error=None, vrc_error=None, runtime="120",
                       date="2099-01-07", start="19:00", timezone="UTC", save_error=False):
        announce = NS(id=12, mention="<#12>", send=AsyncMock(return_value=NS(id=99)))
        announce.send.side_effect = announcement_error
        voice = NS(id=11)
        guild = NS(id=42, get_channel=lambda ident: voice if ident == 11 else announce,
                   create_scheduled_event=AsyncMock(return_value=NS(id=100)))
        interaction = NS(guild=guild, guild_id=42, user=NS(id=17),
                         response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
        modal = NS(conf={"timezone": timezone, "voice_channel_id": 11, "announce_channel_id": 12},
                   show_type="movie", make_vrchat=True, image=None, imdb_id=None,
                   date=NS(value=date), start=NS(value=start), runtime=NS(value=runtime),
                   movie=NS(value="A Film"), year=NS(value="1999"))
        create_vrc = AsyncMock(return_value={"id": "cal-test"}, side_effect=vrc_error)
        records, snapshots = [], []

        def record(_, rec):
            records.append(rec)
            snapshots.append(copy.deepcopy(rec))

        def save(**kwargs):
            if save_error:
                raise OSError("Storage unavailable")
            if records:
                snapshots.append(copy.deepcopy(records[0]))

        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, "save_events", side_effect=save))
            stack.enter_context(patch.object(bot, "add_event_record", side_effect=record))
            stack.enter_context(patch.object(bot, "RATINGS_API_KEY", "test"))
            stack.enter_context(patch.object(bot, "fetch_ratings", return_value=MovieRatings()))
            stack.enter_context(patch.object(bot, "fetch_backdrop", AsyncMock(side_effect=BackdropError("No cover"))))
            stack.enter_context(patch.object(bot, "enrich_with_copilot", AsyncMock(return_value=None)))
            stack.enter_context(patch.object(bot, "vconf", return_value={"group_id": "grp-test", "auth_cookie": "test"}))
            stack.enter_context(patch.object(bot.vrchat, "create_event", create_vrc))
            stack.enter_context(patch.object(bot.vrchat, "list_events", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(bot, "log"))
            await bot.MovieModal.on_submit(modal, interaction)
        return guild, announce, create_vrc, interaction, records, snapshots

    async def test_partial_success_saved_before_announcement_and_vrchat(self):
        guild, _, _, _, records, snapshots = await self.schedule()
        guild.create_scheduled_event.assert_awaited_once()
        self.assertEqual(len(records), 1)
        self.assertIsNone(snapshots[0]["message_id"])
        self.assertEqual(snapshots[0]["vrc_state"], "pending")
        self.assertTrue(any(s["message_id"] == 99 and s["vrc_state"] == "pending" for s in snapshots))
        self.assertTrue(any(s["vrc_state"] == "creating" for s in snapshots))
        self.assertEqual(snapshots[-1]["vrc_state"], "created")

    async def test_announcement_transport_failure_does_not_lose_booking_or_vrchat(self):
        _, _, create_vrc, interaction, records, _ = await self.schedule(announcement_error=asyncio.TimeoutError())
        create_vrc.assert_awaited_once()
        self.assertEqual(records[0]["announcement_state"], "unknown")
        self.assertEqual(records[0]["vrc_state"], "created")
        self.assertIn("uncertain", interaction.followup.send.await_args.args[0])

    async def test_expired_login_keeps_recoverable_creation_payload(self):
        _, _, _, interaction, records, _ = await self.schedule(vrc_error=bot.vrchat.VRChatAuthError("expired", 401))
        rec = records[0]
        self.assertEqual(rec["vrc_state"], "failed")
        self.assertNotIn("vrc_event_id", rec)
        self.assertEqual(rec["vrc_payload"]["title"], "Movie Night: A Film")
        self.assertIn("/movie-vrchat recover", interaction.followup.send.await_args.args[0])

    async def test_unwritable_index_prevents_external_writes(self):
        guild, announce, create_vrc, interaction, records, _ = await self.schedule(save_error=True)
        guild.create_scheduled_event.assert_not_awaited()
        announce.send.assert_not_awaited()
        create_vrc.assert_not_awaited()
        self.assertEqual(records, [])
        self.assertIn("state could not be saved", interaction.followup.send.await_args.args[0])

    async def test_invalid_runtime_rejected_without_external_writes(self):
        for runtime in ("0", "-1", "nope", "1441"):
            with self.subTest(runtime=runtime):
                guild, _, _, interaction, records, _ = await self.schedule(runtime=runtime)
                guild.create_scheduled_event.assert_not_awaited()
                self.assertEqual(records, [])
                self.assertIn("Runtime must", interaction.followup.send.await_args.args[0])

    async def test_clock_change_ambiguous_or_nonexistent_time_rejected(self):
        for date, start in (("2099-03-29", "01:30"), ("2099-10-25", "01:30")):
            with self.subTest(date=date):
                guild, _, _, interaction, _, _ = await self.schedule(date=date, start=start, timezone="Europe/London")
                guild.create_scheduled_event.assert_not_awaited()
                self.assertIn("clocks change", interaction.followup.send.await_args.args[0])


class CancellationAndRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rec = dict(event_id=11, title="A Film", unix=int(time.time()) + 86400,
                        message_id=None, channel_id=None, announcement_state="disabled",
                        vrc_group_id="grp-test", vrc_event_id="cal-test", vrc_file_id="file-test")
        self.event = NS(delete=AsyncMock(), status=discord.EventStatus.scheduled,
                        start_time=dt.datetime.fromtimestamp(self.rec["unix"], dt.timezone.utc))
        self.event.end_time = self.event.start_time + dt.timedelta(hours=2)
        self.guild = NS(id=7, get_scheduled_event=lambda _: self.event,
                        fetch_scheduled_event=AsyncMock(return_value=self.event))
        self.i = NS(guild=self.guild, guild_id=7, followup=NS(send=AsyncMock()))
        self.conf = {"group_id": "grp-test", "auth_cookie": "test"}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(bot, "EVENTS", {"7": [self.rec]}))
        self.save = self.stack.enter_context(patch.object(bot, "save_events"))
        self.stack.enter_context(patch.object(bot, "vconf", return_value=self.conf))
        self.stack.enter_context(patch.object(bot, "log"))
        self.delete_event = self.stack.enter_context(patch.object(bot.vrchat, "delete_event", AsyncMock(return_value=True)))
        self.delete_file = self.stack.enter_context(patch.object(bot.vrchat, "delete_file", AsyncMock(return_value=True)))
        self.create = self.stack.enter_context(patch.object(bot.vrchat, "create_event", AsyncMock(return_value={"id": "cal-new"})))
        self.list = self.stack.enter_context(patch.object(bot.vrchat, "list_events", AsyncMock(return_value=[])))
        self.find = self.stack.enter_context(patch.object(bot.vrchat, "find_created_event", AsyncMock(return_value=None)))

    async def test_failed_calendar_delete_retains_cover_and_retry_record(self):
        self.delete_event.side_effect = bot.vrchat.VRChatAuthError("expired", 401)
        await bot.cancel_record(self.i, self.rec)
        self.delete_file.assert_not_awaited()
        self.assertEqual(bot.EVENTS["7"], [self.rec])
        self.assertTrue(self.rec["cancel_progress"]["event"])
        self.delete_event.side_effect = None
        await bot.cancel_record(self.i, self.rec)
        self.event.delete.assert_awaited_once()
        self.delete_file.assert_awaited_once()
        self.assertEqual(bot.EVENTS["7"], [])

    async def test_no_announcement_does_not_crash_cancellation(self):
        await bot.cancel_record(self.i, self.rec)
        self.assertEqual(bot.EVENTS["7"], [])
        self.assertIn("Cancelled", self.i.followup.send.await_args.args[0])

    async def test_calendar_with_unlinked_account_retains_cleanup(self):
        self.conf.clear()
        await bot.cancel_record(self.i, self.rec)
        self.delete_event.assert_not_awaited()
        self.delete_file.assert_not_awaited()
        self.assertEqual(bot.EVENTS["7"], [self.rec])

    async def test_missing_message_after_uncertain_send_keeps_record(self):
        self.rec["announcement_state"] = "unknown"
        await bot.cancel_record(self.i, self.rec)
        self.assertEqual(bot.EVENTS["7"], [self.rec])
        self.assertFalse(self.rec["cancel_progress"].get("announcement"))

    async def test_reading_upcoming_records_never_discards_failed_cleanup(self):
        self.rec.update(unix=1, cancel_requested=True)
        self.assertEqual(bot.upcoming_records(7), [self.rec])
        self.rec.pop("cancel_requested")
        self.assertEqual(bot.upcoming_records(7), [])
        self.assertEqual(bot.EVENTS["7"], [self.rec])
        self.save.assert_not_called()

    def pending(self, state):
        self.rec.pop("vrc_event_id")
        self.rec.update(vrc_state=state, vrc_payload=bot.vrchat.build_event_payload(
            "A Film", "Synopsis", self.event.start_time.isoformat(), self.event.end_time.isoformat(),
            image_id="file-test"), end_unix=int(self.event.end_time.timestamp()))

    async def test_relink_backfill_reuses_upload_and_updates_same_booking(self):
        self.pending("failed")
        self.stack.enter_context(patch.object(bot.vrchat, "upload_image", AsyncMock())) \
            .assert_not_awaited()
        result = await bot.recover_record_vrc(self.i, self.rec)
        self.create.assert_awaited_once()
        self.assertEqual(self.create.await_args.args[2]["imageId"], "file-test")
        self.assertEqual(self.rec["vrc_event_id"], "cal-new")
        self.assertEqual(self.rec["event_id"], 11)
        self.assertIn("Also posted", result)

    async def test_uncertain_creation_reconciles_without_another_post(self):
        self.pending("unknown")
        self.rec["vrc_existing_ids"] = []
        self.find.return_value = {"id": "cal-existing"}
        result = await bot.recover_record_vrc(self.i, self.rec)
        self.create.assert_not_awaited()
        self.assertEqual(self.rec["vrc_event_id"], "cal-existing")
        self.assertIn("No new event", result)

    async def test_matching_event_without_baseline_is_not_claimed_or_duplicated(self):
        self.pending("failed")
        self.list.return_value = [dict(self.rec["vrc_payload"], id="cal_existing")]
        result = await bot.recover_record_vrc(self.i, self.rec)
        self.assertIn("no saved before-write baseline", result)
        self.assertNotIn("vrc_event_id", self.rec)
        self.create.assert_not_awaited()

    async def test_unknown_or_cancelled_creation_never_blindly_reposts(self):
        for state, cancelled in (("unknown", False), ("creating", False), ("failed", True)):
            self.rec.pop("vrc_event_id", None)
            self.rec.update(vrc_state=state, cancel_requested=cancelled,
                            vrc_payload=bot.vrchat.build_event_payload("A Film", "Synopsis",
                                self.event.start_time.isoformat(), self.event.end_time.isoformat()))
            await bot.recover_record_vrc(self.i, self.rec)
        self.create.assert_not_awaited()

    async def test_calendar_creation_failure_never_deletes_uploaded_cover(self):
        self.pending("pending")
        self.create.side_effect = bot.vrchat.VRChatWriteUncertainError("Lost response", 500)
        await bot.create_record_vrc(self.rec, self.conf)
        self.assertEqual(self.rec["vrc_state"], "unknown")
        self.assertEqual(self.rec["vrc_file_id"], "file-test")
        self.delete_file.assert_not_awaited()

    async def test_uncertain_error_saves_baseline_even_if_http_status_looks_definitive(self):
        self.pending("pending")
        self.list.return_value = [{"id": "cal-before"}]
        self.create.side_effect = bot.vrchat.VRChatWriteUncertainError(
            "Rollback unclear", 403, existing_ids=["cal-before"], calendar_id="cal-uncertain")
        await bot.create_record_vrc(self.rec, self.conf)
        self.assertEqual(self.rec["vrc_state"], "unknown")
        self.assertEqual(self.rec["vrc_existing_ids"], ["cal-before"])
        self.assertEqual(self.rec["vrc_event_id"], "cal-uncertain")
        self.assertEqual(self.create.await_args.kwargs["existing_ids"], ["cal-before"])
        self.delete_file.assert_not_awaited()

    async def test_index_is_saved_with_baseline_before_calendar_write(self):
        self.pending("pending")
        self.list.return_value = [{"id": "cal-before"}]
        captured = []
        self.save.side_effect = lambda **kwargs: captured.append(copy.deepcopy(self.rec))
        await bot.create_record_vrc(self.rec, self.conf)
        self.assertTrue(any(r["vrc_state"] == "creating" and r["vrc_existing_ids"] == ["cal-before"]
                            for r in captured))
        self.assertEqual(self.create.await_args.kwargs["existing_ids"], ["cal-before"])


class EventStateTests(unittest.TestCase):
    def setUp(self):
        bot.TMP_DIR.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=bot.TMP_DIR)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "events.json"

    def test_corrupt_index_fails_closed_and_is_not_replaced(self):
        self.path.write_text("{broken")
        with patch.object(bot, "EVENTS_FILE", self.path):
            with self.assertRaises(json.JSONDecodeError):
                bot.load_events()
        self.assertEqual(self.path.read_text(), "{broken")

    def test_atomic_failure_preserves_original_index(self):
        self.path.write_text('{"old": []}')
        with patch.object(bot, "EVENTS_FILE", self.path), patch.object(bot, "EVENTS", {"new": []}), \
             patch.object(Path, "replace", side_effect=OSError("Disk failure")), patch.object(bot, "log"):
            with self.assertRaises(OSError):
                bot.save_events()
        self.assertEqual(json.loads(self.path.read_text()), {"old": []})
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_vrchat_login_save_failure_preserves_previous_link(self):
        original = {"7": {"group_id": "grp-original", "auth_cookie": "test-original"}}
        with patch.object(bot, "VRC", copy.deepcopy(original)), \
             patch.object(bot, "save_vrc", side_effect=OSError("Storage unavailable")):
            with self.assertRaises(OSError):
                bot.set_vconf(7, {"group_id": "grp-new", "auth_cookie": "test-new"})
            self.assertEqual(bot.VRC, original)
