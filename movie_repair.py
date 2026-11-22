"""Private, confirmed repairs for existing Movie Bot bookings."""

import asyncio
import copy
import datetime as dt
import io
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import discord

import find_backdrop as art
import vrchat
from ratings import MovieRatings, RatingsError, discord_announcement, discord_event_description, fetch_ratings


ACTIVE = set()


def consistent_identity(text, imdb_id):
    """A correct identifier does not excuse another film's identifier in the same copy."""
    return set(re.findall(r"tt\d{7,12}(?!\d)", text)) <= {imdb_id}


def interpret(problem, replacement):
    p = problem.casefold()
    if re.search(r"\b(wrong|different|incorrect|mistaken)\s+(movie|film|show|title|series)\b", p):
        return "identity"
    if re.search(r"\b(poster|image|artwork|backdrop|cover|picture|thumbnail)s?\b", p):
        return "artwork"
    if replacement.strip() or re.search(r"\b(meant|misidentified)\b", p):
        return "identity"
    return None


def search_text(problem, replacement, record, mode):
    imdb = re.search(r"\btt[0-9]{7,12}\b", replacement or problem)
    if imdb:
        return imdb[0], None
    title = replacement.strip()
    if not title and mode == "identity":
        match = re.search(r'''["“]([^"”]{1,140})["”]|\b(?:meant|should be)\s+(.+?)(?:\s+with\b|[.!?]?$)''',
                          problem.strip(), re.I)
        if match:
            title = (match[1] or match[2]).strip()
    if not title:
        return record["title"], record.get("year")
    match = re.fullmatch(r"(.+?)\s*[\s(]((?:19|20)\d{2})\)?", title)
    return (match[1].strip(), match[2]) if match else (title, None)


class RepairSession:
    def __init__(self, bot, interaction, attachment):
        self.bot = bot
        self.guild_id = interaction.guild_id
        self.user_id = interaction.user.id
        self.attachment = attachment
        self.event_id = None
        self.existing = None

    def records(self):
        return sorted((r for r in self.bot.EVENTS.get(str(self.guild_id), [])
                       if r.get("unix", 0) > time.time() and not r.get("cancel_requested")), key=lambda r: r["unix"])

    def record(self):
        return next((r for r in self.records() if r["event_id"] == self.event_id), None)

    async def allowed(self, interaction):
        if (interaction.user.id == self.user_id and interaction.guild_id == self.guild_id
                and interaction.permissions.manage_events
                and self.bot.gconf(self.guild_id).get("mod_channel_id") == interaction.channel_id):
            return True
        await interaction.response.send_message(
            "Only the requesting mod can repair events in the configured mod channel.", ephemeral=True)
        return False


class RepairView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=600)
        self.session = session

    async def interaction_check(self, interaction):
        return await self.session.allowed(interaction)

    def button(self, label, callback, *, style=discord.ButtonStyle.secondary, disabled=False):
        item = discord.ui.Button(label=label, style=style, disabled=disabled)
        item.callback = callback
        self.add_item(item)

    async def cancel(self, interaction):
        await interaction.response.edit_message(content="Closed. No further changes.", embed=None,
                                                attachments=[], view=None)
        self.stop()

    async def on_error(self, interaction, error, item):
        self.session.bot.log(f"Repair interaction failed ({type(error).__name__})")
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Couldn't finish that step. Check the event before retrying.", ephemeral=True)


class Picker(RepairView):
    def __init__(self, session, page=0):
        super().__init__(session)
        records = session.records()
        page = min(page, max(0, (len(records)-1)//25))
        tz = ZoneInfo(session.bot.gconf(session.guild_id).get("timezone") or session.bot.CFG["default_timezone"])
        options = [discord.SelectOption(label=r["title"][:100], value=str(r["event_id"]),
                   description=dt.datetime.fromtimestamp(r["unix"], tz).strftime("%a %d %b %Y, %H:%M %Z"))
                   for r in records[page*25:(page+1)*25]]
        if options:
            select = discord.ui.Select(placeholder="Which movie night needs fixing?", options=options)

            async def choose(interaction):
                # Each selection owns its own modal and preview, even if the original picker is reused.
                session = RepairSession(self.session.bot, interaction, self.session.attachment)
                session.event_id = int(select.values[0])
                if not session.record():
                    return await interaction.response.send_message("That booking is no longer upcoming.", ephemeral=True)
                if session.record().get("repair_pending"):
                    await interaction.response.defer(ephemeral=True, thinking=True)
                    try:
                        await restore_repair(session)
                        await Confirm(session).show(interaction)
                    except (art.BackdropError, OSError, ValueError, KeyError):
                        await interaction.edit_original_response(
                            content="The saved repair preview could not be restored. Review the booking before a new repair.",
                            view=RepairShortcut(session, session.event_id))
                    return
                await interaction.response.send_modal(Problem(session))

            select.callback = choose
            self.add_item(select)
        if len(records) > 25:
            self.button("Previous", lambda i: self.turn(i, page-1), disabled=page == 0)
            self.button("Next", lambda i: self.turn(i, page+1), disabled=(page+1)*25 >= len(records))
        self.button("Cancel", self.cancel)

    async def turn(self, interaction, page):
        await interaction.response.edit_message(view=Picker(self.session, page))
        self.stop()


class RepairShortcut(RepairView):
    def __init__(self, session, event_id):
        super().__init__(session)
        session.event_id = event_id
        self.button("Repair this event", self.open)

    async def open(self, interaction):
        session = copy.copy(self.session)
        if not session.record():
            return await interaction.response.send_message("This booking is no longer upcoming.", ephemeral=True)
        if session.record().get("repair_pending"):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await restore_repair(session)
                await Confirm(session).show(interaction)
            except (art.BackdropError, OSError, ValueError, KeyError):
                await interaction.edit_original_response(
                    content="The unfinished repair needs review; its saved image could not be restored.", view=None)
            return
        await interaction.response.send_modal(Problem(session))


class Problem(discord.ui.Modal, title="Repair this booking"):
    problem = discord.ui.TextInput(label="What went wrong?", placeholder="Wrong movie, missing poster, wrong cover…",
                                   max_length=400)
    replacement = discord.ui.TextInput(label="Correct title and year, or IMDb ID (optional)",
                                       placeholder="The Dark Knight (2008) or tt0468569",
                                       required=False, max_length=140)

    def __init__(self, session):
        super().__init__(timeout=600)
        self.session = session

    async def on_submit(self, interaction):
        session = self.session
        if not await session.allowed(interaction):
            return
        rec = session.record()
        mode = interpret(self.problem.value, self.replacement.value)
        if not rec or not mode:
            return await interaction.response.send_message(
                "That booking is no longer upcoming." if not rec else
                "I can fix a wrong movie or its artwork. Pick the booking again and describe one of those problems.",
                ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        session.mode = mode
        session.text_only = mode == "identity" and bool(re.search(r"\btext[\s-]+only\b", self.problem.value, re.I))
        session.existing = None
        session.upload_id = None
        session.snapshot = copy.deepcopy(rec)
        session.missing_only = (mode == "artwork" and not session.attachment and
                                bool(re.search(r"\b(missing|no|blank|absent)\b|not found", self.problem.value, re.I)))
        try:
            if mode == "artwork" and session.attachment:
                await preview(interaction, session, None, [])
                return
            if session.missing_only and not self.replacement.value:
                # Reuse an existing platform's cover even when TMDB cannot match the event label.
                event = await interaction.guild.fetch_scheduled_event(session.event_id)
                if event.cover_image:
                    session.existing = await event.cover_image.with_size(2048).read()
                elif rec.get("vrc_event_id") and rec.get("vrc_group_id"):
                    try:
                        _, v = await read_vrc(session, rec)
                        if v.get("imageId") and v.get("imageUrl"):
                            session.existing = await asyncio.to_thread(read_vrc_image, v["imageUrl"])
                    except (art.BackdropError, vrchat.VRChatError, ValueError):
                        pass
                if getattr(session, "existing", None):
                    await preview(interaction, session, None, [])
                    return
            query, year = search_text(self.problem.value, self.replacement.value, rec, mode)
            if mode == "artwork" and not self.replacement.value.strip() and rec.get("imdb_id") and \
                    rec.get("identity_provenance") in ("user_explicit", "tmdb_verified", "repair_tmdb_verified"):
                query, year = rec["imdb_id"], None
            choices = await asyncio.to_thread(art.repair_candidates, query, year, rec.get("show_type", "movie"))
            if not choices:
                raise art.BackdropError("No matching title. Try the official title or IMDb ID in the second field.")
            await interaction.edit_original_response(content="Pick the intended title. Nothing has been changed.",
                                                     view=Titles(session, choices))
        except (art.BackdropError, discord.HTTPException, ValueError) as exc:
            await interaction.edit_original_response(content=safe_error(exc),
                view=RepairShortcut(session, session.event_id))


def safe_error(exc):
    if isinstance(exc, (art.BackdropError, ValueError)) and not isinstance(exc, UnicodeError):
        return str(exc)[:400]
    return f"{type(exc).__name__}: check the bot's permissions, linkage and connection."


class Titles(RepairView):
    def __init__(self, session, choices):
        super().__init__(session)
        select = discord.ui.Select(placeholder="Which film or series?", options=[
            discord.SelectOption(label=name[:100], value=str(i)) for i, (_, _, name) in enumerate(choices)])

        async def choose(interaction):
            await interaction.response.defer()
            try:
                kind, identifier, _ = choices[int(select.values[0])]
                details = await asyncio.to_thread(art.repair_details, kind, identifier)
                covers = [] if session.attachment or session.text_only else \
                    await asyncio.to_thread(art.repair_artwork, kind, identifier)
                await preview(interaction, copy.copy(session), details, covers)
                self.stop()
            except (art.BackdropError, discord.HTTPException, ValueError) as exc:
                await interaction.edit_original_response(content=safe_error(exc), view=self)
        select.callback = choose
        self.add_item(select)
        self.button("Change search", self.change_search)
        self.button("Cancel", self.cancel)

    async def change_search(self, interaction):
        await interaction.response.send_modal(Problem(copy.copy(self.session)))


def read_vrc_image(url):
    from urllib.request import HTTPRedirectHandler, Request, build_opener
    from urllib.error import URLError

    def trusted(link):
        parsed = urlsplit(link)
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.hostname in {"api.vrchat.cloud", "assets.vrchat.com", "api.vrchat.com",
                                        "assets.vrchat.cloud", "files.vrchat.cloud"})

    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not trusted(newurl):
                raise art.BackdropError("Unsupported VRChat cover source.")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    if not trusted(url):
        raise art.BackdropError("Unsupported VRChat cover source.")
    try:
        with build_opener(Redirect()).open(Request(url, headers={"User-Agent":vrchat.USER_AGENT}), timeout=8) as r:
            return art.validated_image(r.read(art.MAX_IMAGE_BYTES+1))
    except (URLError, OSError):
        raise art.BackdropError("The existing VRChat cover could not be read.") from None


async def read_vrc(session, rec):
    conf = session.bot.vconf(session.guild_id)
    if conf.get("group_id") != rec["vrc_group_id"] or not conf.get("auth_cookie"):
        raise ValueError("VRChat group or login changed; check the link before retrying.")
    cookies = session.bot.vrc_cookies(conf)
    event = await vrchat.get_event(cookies, rec["vrc_group_id"], rec["vrc_event_id"])
    if event.get("id") != rec["vrc_event_id"]:
        raise ValueError("VRChat returned a different calendar event.")
    return cookies, event


async def preview(interaction, session, details, covers):
    rec = session.record()
    if not rec or rec != session.snapshot:
        raise ValueError("The booking changed. Open the picker again.")
    event = await interaction.guild.fetch_scheduled_event(session.event_id)
    if event.status != discord.EventStatus.scheduled or event.start_time.timestamp() <= time.time():
        raise ValueError("This Discord event is no longer upcoming.")
    session.booking = (event.start_time, event.end_time)
    session.details = details or {}
    session.covers = covers
    session.index = 0
    session.targets = []
    if not session.missing_only or not event.cover_image:
        session.targets.append("Discord event")
    if rec.get("vrc_event_id") and rec.get("vrc_group_id"):
        available = False
        try:
            _, current = await read_vrc(session, rec)
            available = bool(current.get("imageId"))
        except (ValueError, vrchat.VRChatError):
            pass  # Still offer the linked target; apply reports it separately if unavailable.
        if not session.missing_only or not available:
            session.targets.append("VRChat event")
    if rec.get("channel_id") and rec.get("message_id"):
        session.targets.append("Discord announcement")
    if not session.targets:
        await interaction.edit_original_response(content="Both checked covers are already in place. "
                                                "For a replacement, describe it as a wrong cover.", view=None)
        return
    if getattr(session, "text_only", False):
        session.image = b""
    elif session.attachment:
        image_path = await session.bot.fetch_backdrop(rec["title"], rec.get("year"), image=session.attachment)
        session.image = await asyncio.to_thread(session.bot.read_backdrop, image_path)
    elif getattr(session, "existing", None):
        session.image = art.validated_image(session.existing)
    else:
        if not covers:
            raise art.BackdropError("No replacement cover is available. Supply a landscape image.")
        session.image = await asyncio.to_thread(art.repair_cover, covers[0])
    if session.mode == "identity":
        title, year, imdb = details["title"], details["year"], details["imdb_id"]
        if not imdb or not art.IMDB_ID.fullmatch(imdb):
            raise ValueError("TMDB did not provide an IMDb ID for this identity repair. Pick another match.")
        duration = details.get("runtime")
        booked = round((event.end_time-event.start_time).total_seconds()/60) if event.end_time else None
        runtime = duration or booked or session.bot.CFG["default_runtime_minutes"]
        try:
            ratings = await asyncio.to_thread(fetch_ratings, title, year or None, rec.get("show_type","movie"),
                api_key=session.bot.RATINGS_API_KEY, omdb_api_key=session.bot.OMDB_API_KEY or None, imdb_id=imdb)
        except RatingsError:
            ratings = MovieRatings()
        if ratings.imdb_id != imdb:
            ratings = MovieRatings()
        when = event.start_time.astimezone(ZoneInfo(session.bot.gconf(session.guild_id).get("timezone")
                                                   or session.bot.CFG["default_timezone"]))
        label, emoji = session.bot.label_for(rec.get("show_type","movie"), when)
        session.name = f"{emoji} {label}: {title}"[:100]
        session.vname = f"{label}: {title}"[:64]
        anchor = f"\nIMDb: https://www.imdb.com/title/{imdb}/"
        session.synopsis = (details["overview"] or f"{title} ({year})")[:900-len(anchor)] + anchor
        session.description = discord_event_description(session.synopsis, runtime, ratings)
        body = session.bot.template_announcement(title, year, int(event.start_time.timestamp()), runtime, label, emoji)
        footer = f"\n\n:calendar_spiral: **Event:** https://discord.com/events/{session.guild_id}/{session.event_id}"
        session.post = discord_announcement(body, runtime, ratings, limit=2000-len(footer)) + footer
        if not consistent_identity(session.description + session.synopsis + session.post, imdb):
            raise ValueError("Replacement text contains conflicting IMDb identifiers. Choose another source.")
        session.runtime_note = (f"Film runtime: {duration} min; booking: {booked} min. Times will not move."
                                if duration and booked and duration != booked else "")
    await Confirm(session).show(interaction)


PLAN_FIELDS = ("mode", "text_only", "missing_only", "details", "targets", "name", "vname", "synopsis",
               "description", "post", "runtime_note", "upload_id")


async def restore_repair(session):
    rec = session.record()
    plan = copy.deepcopy(rec["repair_pending"])
    for key in PLAN_FIELDS:
        if key in plan:
            setattr(session, key, plan[key])
    session.image = b"" if getattr(session, "text_only", False) else \
        await asyncio.to_thread(session.bot.read_backdrop, Path(plan["image_path"]))
    session.booking = tuple(dt.datetime.fromisoformat(value) if value else None for value in plan["booking"])
    session.covers, session.index = [], 0
    session.snapshot = copy.deepcopy(rec)


async def persist_plan(session, rec):
    path = None if getattr(session, "text_only", False) else \
        await asyncio.to_thread(art.manual_backdrop, session.image, session.bot.TMP_DIR)
    plan = {key: copy.deepcopy(getattr(session, key)) for key in PLAN_FIELDS if hasattr(session, key)}
    plan["image_path"] = str(path) if path else None
    plan["booking"] = [value.isoformat() if value else None for value in session.booking]
    previous = rec.get("repair_pending", {}).get("upload_id")
    if previous and previous != plan.get("upload_id"):
        retained = rec.setdefault("retained_vrc_files", [])
        if previous not in retained:
            retained.append(previous)
    rec["repair_pending"] = plan
    if path:
        rec["backdrop_path"] = str(path)
    session.bot.save_events(strict=True)
    return plan


class Confirm(RepairView):
    def __init__(self, session):
        super().__init__(session)
        self.done = False
        if len(session.covers) > 1:
            self.button("Next cover", self.next_cover)
        self.button("Apply repair", self.apply, style=discord.ButtonStyle.success)
        self.button("Change search", lambda i: i.response.send_modal(Problem(copy.copy(session))))
        self.button("Cancel", self.cancel)

    async def show(self, interaction):
        s = self.session
        rec = s.snapshot
        d = s.details
        embed = discord.Embed(title=f"Repair: {rec['title']}"[:256], color=0x5865F2)
        embed.description = ("Update title, synopsis and ratings; keep existing artwork." if getattr(s, "text_only", False) else
                             "Update title, synopsis, ratings and artwork." if s.mode == "identity" else
                             "Update artwork only.") + "\n" + ", ".join(s.targets) + ".\nNo time changes or new pings."
        if d:
            identity = f"{d['title']} ({d['year']})"
            if d.get("imdb_id"):
                identity += f" · [IMDb](https://www.imdb.com/title/{d['imdb_id']}/)"
            embed.add_field(name="Selected title", value=identity + "\n" + d["people"], inline=False)
        if s.mode == "identity":
            embed.add_field(name="New Discord description", value=s.description[:1024], inline=False)
            if s.runtime_note:
                embed.add_field(name="Runtime", value=s.runtime_note)
        if s.image:
            embed.set_image(url="attachment://cover.jpg")
        content = "Resume the unfinished repair steps." if s.snapshot.get("repair_pending") else "Nothing has changed yet."
        await interaction.edit_original_response(content=content, embed=embed,
            attachments=[discord.File(io.BytesIO(s.image), filename="cover.jpg")] if s.image else [], view=self)

    async def cancel(self, interaction):
        if self.done:
            return await interaction.response.send_message("This repair is already closed or running.", ephemeral=True)
        self.done = True
        await super().cancel(interaction)

    async def next_cover(self, interaction):
        await interaction.response.defer()
        idx = (self.session.index+1) % len(self.session.covers)
        image = await asyncio.to_thread(art.repair_cover, self.session.covers[idx])
        if self.done:
            return
        self.session.index, self.session.image = idx, image
        await self.show(interaction)

    async def apply(self, interaction):
        s = self.session
        key = s.guild_id, s.event_id
        if self.done or key in ACTIVE or not s.record() or s.record() != s.snapshot:
            return await interaction.response.send_message("This repair is stale or already running. Reopen the picker.",
                                                           ephemeral=True)
        self.done = True
        ACTIVE.add(key)
        try:
            await interaction.response.defer()
            rec = s.record()
            if not rec or rec != s.snapshot:
                await interaction.edit_original_response(content="The booking changed. Reopen the picker.", view=None)
                return
            live = await interaction.guild.fetch_scheduled_event(s.event_id)
            if (live.status != discord.EventStatus.scheduled or live.start_time.timestamp() <= time.time()
                    or (live.start_time, live.end_time) != s.booking):
                await interaction.edit_original_response(content="Booking changed; no further edits. Reopen the picker.",
                                                         view=None)
                return
            notes, pending = [], []
            plan = await persist_plan(s, rec)
            for target in list(s.targets):
                try:
                    await apply_target(s, interaction.guild, rec, target)
                    plan["targets"].remove(target)
                    s.bot.save_events(strict=True)
                    notes.append(f"{target}: updated or already correct.")
                except OSError:
                    pending.append(target)
                    pending.extend(t for t in s.targets[s.targets.index(target)+1:] if t not in pending)
                    plan["targets"] = pending
                    notes.append(f"{target}: booking state could not be saved; no further edits.")
                    break
                except (ValueError, discord.HTTPException, vrchat.VRChatError) as exc:
                    pending.append(target)
                    notes.append(f"{target}: {safe_error(exc)}")
            plan["targets"] = pending
            if not pending:
                rec.pop("repair_pending", None)
            s.bot.save_events(strict=True)
            s.targets, s.snapshot = pending, copy.deepcopy(rec)
            retry = Confirm(s) if pending else None
            if retry:
                retry.clear_items()
                label = f"Retry {pending[0]}" if len(pending) == 1 else "Retry failed steps"
                retry.button(label, retry.apply, style=discord.ButtonStyle.primary)
                retry.button("Close", retry.cancel)
            await interaction.edit_original_response(content="\n".join(notes), embed=None, attachments=[], view=retry)
        finally:
            ACTIVE.discard(key)
            self.stop()


async def apply_target(s, guild, rec, target):
    if s.record() is not rec:
        raise ValueError("Booking is no longer available.")
    identity = s.mode == "identity"
    text_only = getattr(s, "text_only", False)
    if identity and not consistent_identity(s.description + s.synopsis + getattr(s, "post", ""), s.details["imdb_id"]):
        raise ValueError("Replacement text contains conflicting IMDb identifiers.")
    if target == "Discord event":
        ev = await guild.fetch_scheduled_event(s.event_id)
        if (ev.status != discord.EventStatus.scheduled or ev.start_time.timestamp() <= time.time()
                or (ev.start_time, ev.end_time) != s.booking):
            raise ValueError("The booking time or status changed. Reopen the picker.")
        if not s.missing_only or not ev.cover_image:
            fields = dict(name=s.name, description=s.description) if identity else {}
            if not text_only:
                fields["image"] = s.image
            updated = await ev.edit(**fields)
            if (not text_only and not updated.cover_image) or (
                identity and (updated.name != s.name or updated.description != s.description)):
                raise ValueError("Discord did not confirm the event update.")
        if identity:
            rec.update(title=s.details["title"], year=s.details["year"], imdb_id=s.details["imdb_id"],
                       identity_provenance="repair_tmdb_verified")
            # An auth-expired calendar booking must recover the repaired identity, not the stale film.
            if rec.get("vrc_payload") and not rec.get("vrc_event_id"):
                rec["vrc_payload"].update(title=s.vname, description=s.synopsis)
            s.bot.save_events(strict=True)
    elif target == "VRChat event":
        cookies, ev = await read_vrc(s, rec)
        if s.missing_only and ev.get("imageId") and ev["imageId"] != getattr(s, "upload_id", None):
            return
        if not text_only and not getattr(s, "upload_id", None):
            png = await asyncio.to_thread(s.bot._to_vrchat_png, s.image)
            if not png:
                raise ValueError("Couldn't convert the selected cover.")
            s.upload_id = await vrchat.upload_image(cookies, png, filename=f"movie-night-{s.event_id}.png")
            if rec.get("repair_pending") is not None:
                rec["repair_pending"]["upload_id"] = s.upload_id
                s.bot.save_events(strict=True)
        fields = {"sendCreationNotification": False}
        if not text_only:
            fields["imageId"] = s.upload_id
        if identity:
            fields.update(title=s.vname, description=s.synopsis)
        if ev.get("imageId") == getattr(s, "upload_id", None) and not identity:
            updated = ev  # The previous write may have succeeded before its response was lost.
        else:
            updated = await vrchat.update_event(cookies, rec["vrc_group_id"], rec["vrc_event_id"], fields)
        if not text_only and updated.get("imageId") != s.upload_id:
            raise ValueError("VRChat did not confirm the new cover.")
        if identity and (vrchat._normalise_event_description(updated.get("description"))
                         != vrchat._normalise_event_description(s.synopsis) or
                         vrchat._normalise_event_title(updated.get("title","").replace("˸",":"))
                         != vrchat._normalise_event_title(s.vname)):
            raise ValueError("VRChat did not confirm the updated film details.")
        previous = rec.get("vrc_file_id")
        if not text_only:
            rec["vrc_file_id"] = s.upload_id
        if rec.get("vrc_payload"):
            if not text_only:
                rec["vrc_payload"].update(imageId=s.upload_id)
            if identity:
                rec["vrc_payload"].update(title=s.vname, description=s.synopsis)
        s.bot.save_events(strict=True)
        if not text_only and previous and previous != s.upload_id and previous == ev.get("imageId"):
            retained = rec.setdefault("retained_vrc_files", [])
            if previous not in retained:
                retained.append(previous)
            s.bot.save_events(strict=True)
            try:
                await vrchat.delete_file(cookies, previous)
                retained.remove(previous)
                s.bot.save_events(strict=True)
            except vrchat.VRChatError:
                s.bot.log(f"Old repair cover could not be removed for event {s.event_id}.")
    else:
        channel = guild.get_channel(rec["channel_id"])
        if not channel:
            raise ValueError("Announcement channel unavailable.")
        msg = await channel.fetch_message(rec["message_id"])
        if msg.author.id != s.bot.client.user.id:
            raise ValueError("The announcement is not owned by Movie Bot.")
        keep = [a for a in msg.attachments if a.filename != "backdrop.jpg"]
        if s.missing_only and len(keep) != len(msg.attachments):
            return
        fields = {"content": s.post} if identity else {}
        if text_only:
            updated = await msg.edit(allowed_mentions=discord.AllowedMentions.none(), **fields)
        else:
            image = discord.File(io.BytesIO(s.image), filename="backdrop.jpg")
            try:
                updated = await msg.edit(attachments=[*keep, image],
                                         allowed_mentions=discord.AllowedMentions.none(), **fields)
            finally:
                image.close()
        if not text_only and not any(a.filename == "backdrop.jpg" for a in updated.attachments):
            raise ValueError("Discord did not confirm the announcement cover.")
        if identity and updated.content != s.post:
            raise ValueError("Discord did not confirm the announcement details.")
