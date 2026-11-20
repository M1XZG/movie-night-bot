import datetime as dt
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import discord

import bot
import movie_repair as repair


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rec = dict(event_id=11, unix=int(time.time())+86400, title="Bad label",
                        imdb_id="tt2098632", year="2008", show_type="movie",
                        vrc_group_id="grp-1", vrc_event_id="cal-1")
        self.i = NS(user=NS(id=1), guild_id=7, channel_id=3, permissions=NS(manage_events=True),
                    response=NS(send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock()),
                    edit_original_response=AsyncMock(), guild=NS(id=7, fetch_scheduled_event=AsyncMock()))
        self.b = NS(EVENTS={"7":[self.rec], "8":[dict(self.rec, event_id=88)]},
                    CFG={"default_timezone":"UTC", "default_runtime_minutes":120},
                    gconf=lambda _: {"mod_channel_id":3}, save_events=Mock(),
                    RATINGS_API_KEY=None, OMDB_API_KEY=None, label_for=bot.label_for,
                    template_announcement=bot.template_announcement, _to_vrchat_png=Mock(return_value=b"png"))
        self.s = repair.RepairSession(self.b, self.i, None)
        self.s.event_id = 11
        self.s.snapshot = self.rec.copy()
        self.ev = NS(name="Bad label", description="Wrong film", cover_image=None,
                     status=discord.EventStatus.scheduled, edit=AsyncMock(),
                     start_time=dt.datetime.fromtimestamp(self.rec["unix"], dt.timezone.utc))
        self.ev.end_time = self.ev.start_time + dt.timedelta(hours=2)
        self.s.booking = (self.ev.start_time, self.ev.end_time)
        self.i.guild.fetch_scheduled_event.return_value = self.ev

    async def test_event_picker_is_scoped_paged_and_uses_ids_not_titles(self):
        self.b.EVENTS["7"].extend(dict(self.rec,event_id=n) for n in range(12,40))
        self.b.EVENTS["7"].append(dict(self.rec,event_id=91,unix=1))
        a = repair.Picker(self.s).children[0]
        b = repair.Picker(self.s,1).children[0]
        self.assertEqual(len(a.options),25)
        self.assertEqual({int(o.value) for o in a.options+b.options},set(range(11,40)))
        self.assertTrue(all(o.description.endswith("UTC") for o in a.options))
        self.assertEqual(bot.movie_group.get_command("schedule"), bot.movie)
        self.assertEqual(bot.movie_group.get_command("unfuck"), bot.movie_unfuck)
        for attr, value in [("guild_id",8), ("channel_id",9), ("user",NS(id=2)),
                            ("permissions",NS(manage_events=False))]:
            with patch.object(self.i,attr,value):
                self.assertFalse(await self.s.allowed(self.i))
        self.assertTrue(await self.s.allowed(self.i))

    async def test_request_classification_never_uses_stale_imdb_as_search_hint(self):
        text = "Wrong movie and no poster. I meant The Dark Knight with Heath Ledger."
        self.assertEqual(repair.interpret(text,""),"identity")
        self.assertEqual(repair.search_text(text,"",self.rec,"identity"),("The Dark Knight", None))
        self.assertEqual(repair.interpret("wrong cover","The Dark Knight (2008)"),"artwork")
        self.assertEqual(repair.search_text("wrong cover","The Dark Knight (2008)",self.rec,"artwork"),
                         ("The Dark Knight","2008"))
        self.assertEqual(repair.search_text("wrong movie","tt0468569",self.rec,"identity"),("tt0468569",None))
        self.assertIsNone(repair.interpret("cancel everything",""))

    async def test_identity_preview_has_no_public_writes_and_apply_preserves_booking(self):
        self.s.mode, self.s.missing_only = "identity",False
        details=dict(title="The Dark Knight", year="2008", imdb_id="tt0468569", runtime=152,
                     people="Christopher Nolan",overview="The correct synopsis.")
        vread=AsyncMock(return_value=({},{"imageId":None}))
        vwrite=AsyncMock(side_effect=lambda cookies, group, event, fields: dict(fields))
        async def edit(**kwargs):
            return NS(cover_image=True,name=kwargs["name"],description=kwargs["description"])
        self.ev.edit.side_effect=edit
        with patch.object(repair,"read_vrc",vread), patch.object(repair.art,"repair_cover",return_value=b"jpg"), \
             patch.object(repair,"fetch_ratings",return_value=repair.MovieRatings("7/10",None,"tt2098632")), \
             patch.object(repair.vrchat,"upload_image",AsyncMock(return_value="file-1")), \
             patch.object(repair.vrchat,"update_event",vwrite):
            await repair.preview(self.i,self.s,details,["/cover.jpg"])
            msg=self.i.edit_original_response.await_args.kwargs
            msg["attachments"][0].close()
            self.ev.edit.assert_not_awaited()
            vwrite.assert_not_awaited()
            self.assertIn("120 min", self.s.runtime_note)
            self.assertNotIn("7/10", self.s.description)
            self.assertEqual(self.rec["imdb_id"],"tt2098632")
            await msg["view"].apply(self.i)
        self.assertEqual(set(self.ev.edit.await_args.kwargs), {"image","name","description"})
        self.assertEqual(vwrite.await_args.args[-1],{"imageId":"file-1","sendCreationNotification":False,
                         "title":self.s.vname,"description":details["overview"]})
        self.assertEqual(self.rec["vrc_file_id"],"file-1")
        self.assertEqual(self.rec["imdb_id"],"tt0468569")
        self.assertIsNone(self.i.edit_original_response.await_args.kwargs["view"])

    async def test_partial_failure_only_retries_failed_target_and_does_not_change_film(self):
        self.s.mode,self.s.missing_only,self.s.image="artwork",False,b"cover"
        self.s.targets=["Discord event","VRChat event"]
        self.ev.edit.return_value=NS(cover_image=True)
        self.s.covers=[]
        upload=AsyncMock(return_value="file-repair")
        with patch.object(repair,"read_vrc",AsyncMock(return_value=({},{}))), \
             patch.object(repair.vrchat,"upload_image",upload), \
             patch.object(repair.vrchat,"update_event",AsyncMock(side_effect=[
                 repair.vrchat.VRChatError("temporary"),{"imageId":"file-repair"}])) as vwrite:
            await repair.Confirm(self.s).apply(self.i)
            retry=self.i.edit_original_response.await_args.kwargs["view"]
            self.assertEqual(self.s.targets,["VRChat event"])
            self.assertEqual(retry.children[0].label,"Retry VRChat event")
            await retry.apply(self.i)
        self.ev.edit.assert_awaited_once_with(image=b"cover")
        upload.assert_awaited_once()
        self.assertEqual(vwrite.await_args.args[-1],{"imageId":"file-repair","sendCreationNotification":False})
        self.assertEqual(self.rec["title"],"Bad label")

    async def test_stale_booking_cannot_apply(self):
        self.s.covers=[]
        self.rec["unix"]+=300
        await repair.Confirm(self.s).apply(self.i)
        self.i.response.send_message.assert_awaited_once()
        self.i.guild.fetch_scheduled_event.assert_not_awaited()

    async def test_artwork_announcement_edits_in_place_without_mentions_or_content(self):
        self.s.mode,self.s.missing_only,self.s.image="artwork",False,b"jpg"
        self.b.client=NS(user=NS(id=4))
        keep=NS(filename="calendar.ics")
        msg=NS(author=NS(id=4),attachments=[keep,NS(filename="backdrop.jpg")],
               edit=AsyncMock(return_value=NS(attachments=[NS(filename="backdrop.jpg")])))
        self.i.guild.get_channel=lambda _:NS(fetch_message=AsyncMock(return_value=msg))
        self.rec.update(message_id=12,channel_id=3)
        await repair.apply_target(self.s,self.i.guild,self.rec,"Discord announcement")
        fields=msg.edit.await_args.kwargs
        self.assertNotIn("content",fields)
        self.assertEqual(fields["attachments"][0],keep)
        self.assertEqual(fields["allowed_mentions"].to_dict(),{"parse":[]})
        fields["attachments"][1].close()
