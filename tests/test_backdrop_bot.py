import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
from find_backdrop import BackdropError


class BackdropBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_subprocess_failure_is_logged_and_reported(self):
        process = SimpleNamespace(
            returncode=1, communicate=AsyncMock(return_value=(b"NONE\n", b"ERROR: TMDB returned HTTP 403.\n")),
        )
        with patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
                patch.object(bot, "log") as log:
            with self.assertRaisesRegex(BackdropError, "TMDB returned HTTP 403"):
                await bot.fetch_backdrop("A Movie", "2003")
        self.assertIn("HTTP 403", log.call_args.args[0])

    async def test_timeout_kills_and_reaps_subprocess(self):
        process = SimpleNamespace(returncode=None, communicate=AsyncMock(), kill=Mock())
        async def timeout(awaitable, *, timeout):
            awaitable.close()
            raise asyncio.TimeoutError
        with patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
                patch.object(bot.asyncio, "wait_for", timeout):
            with self.assertRaisesRegex(BackdropError, "timed out"):
                await bot.fetch_backdrop("A Movie", None)
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 1)

    async def test_manual_attachment_bypasses_tmdb(self):
        attachment = SimpleNamespace(size=100, read=AsyncMock(return_value=b"image"))
        with patch.object(bot, "manual_backdrop", return_value=bot.TMP_DIR / "manual.jpg") as save, \
                patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock()) as process:
            result = await bot.fetch_backdrop("A Movie", None, image=attachment)
        self.assertEqual(result, bot.TMP_DIR / "manual.jpg")
        save.assert_called_once_with(b"image", bot.TMP_DIR)
        process.assert_not_awaited()

    async def test_oversized_attachment_fails_before_download(self):
        attachment = SimpleNamespace(size=bot.MAX_IMAGE_BYTES + 1, read=AsyncMock())
        with self.assertRaisesRegex(BackdropError, "smaller than 8 MB"):
            await bot.fetch_backdrop("A Movie", None, image=attachment)
        attachment.read.assert_not_awaited()

    async def test_movie_attachment_option_reaches_modal(self):
        attachment = SimpleNamespace(size=100)
        modal = bot.MovieModal({}, image=attachment)
        self.assertIs(modal.image, attachment)
        parameter = next(item for item in bot.movie.parameters if item.name == "image")
        self.assertEqual(parameter.type, bot.discord.AppCommandOptionType.attachment)
        self.assertFalse(parameter.required)

    async def test_help_shows_tmdb_credit_and_logo(self):
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await bot.movie_help.callback(interaction)
        sent = interaction.response.send_message.await_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(sent["embed"].thumbnail.url, "attachment://tmdb-logo.png")
        self.assertTrue(any("not endorsed or certified" in field.value
                            for field in sent["embed"].fields))
        sent["file"].close()

    async def test_imdb_option_is_available_and_retained_by_modal(self):
        modal = bot.MovieModal({}, imdb_id="tt31349844")
        self.assertEqual(modal.imdb_id, "tt31349844")
        parameter = next(item for item in bot.movie.parameters if item.name == "imdb")
        self.assertFalse(parameter.required)
        self.assertEqual(parameter.type, bot.discord.AppCommandOptionType.string)

    async def test_invalid_imdb_option_rejected_before_modal(self):
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()))
        await bot.movie.callback(interaction, imdb="not-an-id")
        interaction.response.send_modal.assert_not_awaited()
        self.assertIn("IMDb identifier", interaction.response.send_message.await_args.args[0])

    async def test_unstructured_traceback_not_shown_to_scheduling_mod(self):
        process = SimpleNamespace(
            returncode=1, communicate=AsyncMock(return_value=(b"", b"Traceback\nsecret URL\n")),
        )
        with patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
                patch.object(bot, "log"):
            with self.assertRaisesRegex(BackdropError, "Check bot.log") as error:
                await bot.fetch_backdrop("A Movie", None)
        self.assertNotIn("secret URL", str(error.exception))

    async def test_helper_cannot_return_a_file_outside_its_cache(self):
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(
            return_value=(str(bot.HERE / "bot.py").encode(), b"")))
        with patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(BackdropError, "invalid image location"):
                await bot.fetch_backdrop("A Movie", None)

    async def test_manual_attachment_download_is_bounded(self):
        attachment = SimpleNamespace(size=100, read=AsyncMock())
        async def timeout(awaitable, *, timeout):
            awaitable.close()
            raise asyncio.TimeoutError
        with patch.object(bot.asyncio, "wait_for", timeout):
            with self.assertRaisesRegex(BackdropError, "download timed out"):
                await bot.fetch_backdrop("A Movie", None, image=attachment)

    async def test_inferred_imdb_uses_validating_helper_option(self):
        process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(
            b"NONE", b"ERROR_CODE: inferred_identity_mismatch\nERROR: Wrong inferred identity.\n")))
        with patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn, \
             patch.object(bot, "log"):
            with self.assertRaises(bot.BackdropIdentityError):
                await bot.fetch_backdrop("A Movie", "2008", imdb_id="tt1234567", imdb_id_explicit=False)
        self.assertIn("--inferred-imdb-id", spawn.await_args.args)
        self.assertNotIn("--imdb-id", spawn.await_args.args)


class CopilotReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(
            b'{"announcement":"Bring snacks and settle in.","event_description":"See you there!"}', b"")))
        self.metadata = {"title": "The Correct Film", "year": "2008", "imdb_id": "tt0468569",
                         "overview": "A verified synopsis.", "people": "Verified people."}
        import find_backdrop
        patches = (
            patch.object(bot, "find_copilot", return_value="/bin/copilot"),
            patch.object(find_backdrop, "load_credential"),
            patch.object(find_backdrop, "find_title", return_value=("movie", 1)),
            patch.object(find_backdrop, "repair_details", return_value=self.metadata),
            patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=self.process)),
            patch.object(bot, "log"),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_verified_metadata_controls_title_synopsis_and_runtime(self):
        result = await bot.enrich_with_copilot("Wrong input label", "2008", 123, 120, imdb_id="tt0468569")
        self.assertIn("The Correct Film", result["announcement"])
        self.assertNotIn("Wrong input label", result["announcement"])
        self.assertIn("2h 00m", result["announcement"])
        self.assertEqual(result["event_description"], self.metadata["overview"])
        args = bot.asyncio.create_subprocess_exec.await_args.args
        self.assertIn("--available-tools", args)
        self.assertNotIn("*", args)

    async def test_metadata_with_wrong_imdb_prevents_ai_launch(self):
        self.metadata["imdb_id"] = "tt1234567"
        result = await bot.enrich_with_copilot("A Movie", "2008", 123, 120, imdb_id="tt0468569")
        self.assertIsNone(result)
        bot.asyncio.create_subprocess_exec.assert_not_awaited()

    async def test_nonzero_exit_with_json_is_not_published(self):
        self.process.returncode = 1
        result = await bot.enrich_with_copilot("A Movie", "2008", 123, 120)
        self.assertEqual(result["event_description"], self.metadata["overview"])
        self.assertNotIn("Bring snacks and settle in.", result["announcement"])

    async def test_timeout_kills_and_reaps_ai_subprocess(self):
        self.process.returncode = None
        self.process.kill = Mock()
        async def timeout(awaitable, *, timeout):
            awaitable.close()
            raise asyncio.TimeoutError
        with patch.object(bot.asyncio, "wait_for", timeout):
            result = await bot.enrich_with_copilot("A Movie", "2008", 123, 120)
            self.assertEqual(result["event_description"], self.metadata["overview"])
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.communicate.await_count, 1)

    async def test_cancellation_kills_and_reaps_ai_subprocess(self):
        self.process.returncode = None
        self.process.kill = Mock()
        started = asyncio.Event()
        async def communicate():
            if not started.is_set():
                started.set()
                await asyncio.Future()
            return b"", b""
        self.process.communicate.side_effect = communicate
        task = asyncio.create_task(bot.enrich_with_copilot("A Movie", "2008", 123, 120))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.communicate.await_count, 2)

    async def test_bad_ai_output_never_becomes_public_raw_text(self):
        for text in ("runtime failure transcript", "[]", "42",
                     '{"announcement":123,"event_description":"ok"}',
                     '{"announcement":"@everyone","event_description":"ok"}'):
            with self.subTest(text=text):
                self.assertIsNone(bot._parse_enrichment(text))


if __name__ == "__main__":
    unittest.main()
