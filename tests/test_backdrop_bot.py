import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
from find_backdrop import BackdropError


class BackdropBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_subprocess_failure_is_logged_and_reported(self):
        process = SimpleNamespace(
            returncode=1, communicate=AsyncMock(return_value=(b"NONE\n", b"TMDB returned HTTP 403.\n")),
        )
        with patch.object(bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
                patch.object(bot, "log") as log:
            with self.assertRaisesRegex(BackdropError, "Check bot.log"):
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


if __name__ == "__main__":
    unittest.main()
