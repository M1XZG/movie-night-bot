from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
from ratings import MovieRatings, RatingsError


class DiscordRatingsTests(unittest.IsolatedAsyncioTestCase):
    async def run_submission(self, *, lookup_error=False, announce_enabled=True, enriched=True):
        voice = SimpleNamespace(id=11, mention="<#11>")
        announce = SimpleNamespace(id=12, mention="<#12>", send=AsyncMock(return_value=SimpleNamespace(id=99)))
        guild = SimpleNamespace(
            id=42, get_channel=lambda identifier: voice if identifier == 11 else announce,
            create_scheduled_event=AsyncMock(return_value=SimpleNamespace(id=100)),
        )
        interaction = SimpleNamespace(
            guild=guild, user="test-user",
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        modal = SimpleNamespace(
            conf={"timezone": "UTC", "voice_channel_id": 11, "announce_channel_id": 12,
                  "announce_enabled": announce_enabled},
            show_type="movie", make_vrchat=True,
            date=SimpleNamespace(value="2099-01-07"), start=SimpleNamespace(value="19:00"),
            movie=SimpleNamespace(value="The Matrix"), year=SimpleNamespace(value="1999"),
            runtime=SimpleNamespace(value="136"),
        )
        description = "Original synopsis.\nFun facts:\n- Original fact."
        enrichment = {
            "announcement": "**Film:** The Matrix\n**Runtime:** about 2h 16m\n\nSynopsis.",
            "event_description": description,
        } if enriched else None
        vrc_create = AsyncMock(return_value={"id": "cal-test"})
        record = Mock()
        log = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, "RATINGS_API_KEY", "testapikey"))
            stack.enter_context(patch.object(bot, "fetch_backdrop", AsyncMock(return_value=None)))
            stack.enter_context(patch.object(bot, "enrich_with_copilot", AsyncMock(return_value=enrichment)))
            lookup = Mock(return_value=MovieRatings("8.7/10", "83%", "tt0133093"))
            if lookup_error:
                lookup.side_effect = RatingsError("MDBList could not be reached.")
            stack.enter_context(patch.object(bot, "fetch_ratings", lookup))
            stack.enter_context(patch.object(bot, "vconf", return_value={
                "group_id": "grp-test", "auth_cookie": "test-cookie",
            }))
            stack.enter_context(patch.object(bot, "vrc_cookies", return_value={}))
            stack.enter_context(patch.object(bot.vrchat, "create_event", vrc_create))
            stack.enter_context(patch.object(bot, "add_event_record", record))
            stack.enter_context(patch.object(bot, "log", log))
            await bot.MovieModal.on_submit(modal, interaction)
        return guild, announce, vrc_create, interaction, record, log, description

    async def test_scores_are_in_discord_but_never_added_to_vrchat(self):
        guild, announce, vrc, interaction, record, _, original = await self.run_submission()
        discord_description = guild.create_scheduled_event.await_args.kwargs["description"]
        self.assertIn("Runtime: about 2h 16m\nIMDb: 8.7/10", discord_description)
        self.assertIn("Rotten Tomatoes (critics): 83%", discord_description)
        post = announce.send.await_args.kwargs["content"]
        self.assertIn("**Runtime:** about 2h 16m\n:star: **IMDb:**", post)
        self.assertIn("83%", post)
        self.assertEqual(vrc.await_args.args[2]["description"], original)
        self.assertNotIn("MDBList", vrc.await_args.args[2]["description"])
        record.assert_called_once()

    async def test_provider_failure_is_reported_without_losing_events(self):
        guild, announce, vrc, interaction, record, log, original = await self.run_submission(lookup_error=True)
        guild.create_scheduled_event.assert_awaited_once()
        announce.send.assert_awaited_once()
        self.assertIn("IMDb: unavailable", guild.create_scheduled_event.await_args.kwargs["description"])
        self.assertEqual(vrc.await_args.args[2]["description"], original)
        self.assertIn("MDBList could not be reached", interaction.followup.send.await_args.args[0])
        self.assertTrue(any("ratings unavailable" in call.args[0] for call in log.call_args_list))
        record.assert_called_once()

    async def test_event_only_mode_still_includes_scores(self):
        guild, announce, vrc, _, _, _, original = await self.run_submission(announce_enabled=False)
        self.assertIn("IMDb: 8.7/10", guild.create_scheduled_event.await_args.kwargs["description"])
        announce.send.assert_not_awaited()
        self.assertEqual(vrc.await_args.args[2]["description"], original)

    async def test_no_ai_fallback_still_gets_scores_only_in_discord(self):
        guild, announce, vrc, _, _, _, _ = await self.run_submission(enriched=False)
        self.assertIn("IMDb: 8.7/10", guild.create_scheduled_event.await_args.kwargs["description"])
        self.assertIn("83%", announce.send.await_args.kwargs["content"])
        self.assertNotIn("IMDb", vrc.await_args.args[2]["description"])
        self.assertNotIn("Rotten Tomatoes", vrc.await_args.args[2]["description"])


if __name__ == "__main__":
    unittest.main()
