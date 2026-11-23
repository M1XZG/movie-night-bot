import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

import bot


class MovieCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_registered_commands_share_one_permission_gated_root(self):
        roots = bot.tree.get_commands()
        self.assertEqual([command.name for command in roots], ["movie"])
        root = roots[0]
        self.assertTrue(root.guild_only)
        self.assertEqual(root.default_permissions, discord.Permissions(manage_events=True))
        self.assertEqual(
            {command.name for command in root.commands},
            {"schedule", "unfuck", "help", "test", "cancel", "config", "vrchat"},
        )
        self.assertEqual(root.get_command("config"), bot.config_group)
        self.assertEqual(root.get_command("vrchat"), bot.vrc_group)
        self.assertEqual(root.get_command("cancel").callback, bot.movie_cancel.callback)
        self.assertIsNotNone(root.get_command("cancel").get_parameter("movie").autocomplete)
        self.assertIsNotNone(bot.vrc_group.get_command("recover").get_parameter("movie").autocomplete)

    async def test_help_lists_every_command_with_valid_embed_fields(self):
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await bot.movie_help.callback(interaction)
        sent = interaction.response.send_message.await_args.kwargs
        embed = sent["embed"]
        self.addCleanup(sent["file"].close)
        self.assertTrue(sent["ephemeral"])
        self.assertLessEqual(len(embed), 6000)
        self.assertNotRegex(str(embed.to_dict()), r"/movie-")
        for field in embed.fields:
            self.assertFalse(field.inline)
            self.assertLessEqual(len(field.value), 1024)
        documented = set(re.findall(r"`(/movie [^`]+)`", "\n".join(
            field.value for field in embed.fields)))
        for command in bot.movie_group.walk_commands():
            if not isinstance(command, discord.app_commands.Group):
                self.assertIn(f"/{command.qualified_name}", documented)
        self.assertIn("not endorsed or certified by TMDB", str(embed.to_dict()))
