#!/usr/bin/env python3
"""
Movie Night Bot
---------------
A focused, least-privilege Discord bot that lets server mods schedule movie
nights. It creates a native Discord **scheduled event** in the guild's movie
voice channel and posts a whimsical announcement in the guild's announcements
channel.

Slash commands (all guild-only, gated to mods via default_member_permissions =
Manage Events):

  /movie                Opens a form (title / year / date / time / runtime),
                        then creates the event + posts the announcement.
                        Only works in the guild's configured mod channel.
  /movie-cancel         Autocompletes upcoming movie nights; deletes the
                        scheduled event AND the announcement message.

  /movie-config set     Set this guild's mod_channel, announce_channel,
                        voice_channel, ping_role and timezone (any subset).
  /movie-config show    Show this guild's current configuration.
  /movie-config clear   Wipe this guild's configuration to start clean.

Announcement and event content are enriched with AI: the bot shells out to the
GitHub Copilot CLI (`copilot -p ... --silent`) to generate a whimsical post
plus a synopsis and fun facts. If Copilot is unavailable it falls back to a
plain template. Set use_copilot=false in config.json to disable.

Per-guild config is stored in guilds.json keyed by guild id, so each server's
mods configure their own channels, ping role and timezone.

Required Discord permissions (NOT administrator):
  View Channels, Send Messages, Embed Links, Attach Files, Manage Events
Invite scopes: bot + applications.commands
Gateway intents: guilds only (no privileged intents).
"""
import asyncio
import datetime as dt
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands

import vrchat

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "token"
CONFIG_FILE = HERE / "config.json"
GUILDS_FILE = HERE / "guilds.json"
EVENTS_FILE = HERE / "events.json"
VRC_FILE = HERE / "vrchat.json"
LOG_FILE = HERE / "bot.log"

# Bundled landscape-backdrop fetcher (ships alongside this file).
BACKDROP_SCRIPT = HERE / "find_backdrop.py"
# Scratch dir for downloaded images; overridable with MOVIE_BOT_TMP.
TMP_DIR = Path(os.environ.get(
    "MOVIE_BOT_TMP", Path(tempfile.gettempdir()) / "movie-night-bot"))

DEFAULTS = {
    "default_timezone": "Europe/London",
    "use_copilot": True,            # enrich announcement text via copilot CLI
    "copilot_timeout": 90,
    "default_runtime_minutes": 120,
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f"WARN: could not read {path.name}: {e}")
    return default


def save_guilds(data: dict) -> None:
    try:
        GUILDS_FILE.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        log(f"WARN: could not write guilds.json: {e}")


CFG = {**DEFAULTS, **load_json(CONFIG_FILE, {})}
GUILDS = load_json(GUILDS_FILE, {})
# Per-guild list of scheduled movie nights the bot created, so /movie-cancel can
# remove both the event and the announcement message:
#   { guild_id: [ {event_id, message_id, channel_id, title, unix,
#                  vrc_group_id?, vrc_event_id?}, ... ] }
EVENTS = load_json(EVENTS_FILE, {})
# Per-guild VRChat linkage (chmod 600 — session cookies, never the password):
#   { guild_id: {group_id, auth_cookie, twofa_cookie, display_name,
#                category, access_type, send_notification, linked_by, linked_at} }
VRC = load_json(VRC_FILE, {})


def save_events() -> None:
    try:
        EVENTS_FILE.write_text(json.dumps(EVENTS, indent=2) + "\n")
    except OSError as e:
        log(f"WARN: could not write events.json: {e}")


def add_event_record(guild_id: int, rec: dict) -> None:
    EVENTS.setdefault(str(guild_id), []).append(rec)
    save_events()


def remove_event_record(guild_id: int, event_id: int) -> None:
    lst = EVENTS.get(str(guild_id), [])
    EVENTS[str(guild_id)] = [r for r in lst if r.get("event_id") != event_id]
    save_events()


def upcoming_records(guild_id: int) -> list:
    """Return this guild's records, pruning any whose showtime has passed."""
    now = int(time.time())
    lst = EVENTS.get(str(guild_id), [])
    kept = [r for r in lst if r.get("unix", 0) >= now - 6 * 3600]
    if len(kept) != len(lst):
        EVENTS[str(guild_id)] = kept
        save_events()
    return sorted(kept, key=lambda r: r.get("unix", 0))


def next_slot_prefill(guild_id: int, tz: "ZoneInfo",
                      interval_days: int = 7) -> tuple[str | None, str | None]:
    """Suggest the next (date, time) one interval after the latest upcoming event.

    Returns (YYYY-MM-DD, HH:MM) strings to pre-fill the schedule form, or
    (None, None) when there are no upcoming events to extrapolate from. The date
    is advanced on the calendar (date + N days) so the suggested local start time
    is preserved across daylight-saving changes.
    """
    recs = upcoming_records(guild_id)
    if not recs:
        return None, None
    last = dt.datetime.fromtimestamp(recs[-1].get("unix", 0), tz)
    next_date = last.date() + dt.timedelta(days=interval_days)
    return next_date.strftime("%Y-%m-%d"), last.strftime("%H:%M")


def save_vrc() -> None:
    try:
        VRC_FILE.write_text(json.dumps(VRC, indent=2) + "\n")
        try:
            os.chmod(VRC_FILE, 0o600)
        except OSError:
            pass
    except OSError as e:
        log(f"WARN: could not write vrchat.json: {e}")


def vconf(guild_id: int) -> dict:
    return VRC.get(str(guild_id), {})


def set_vconf(guild_id: int, data: dict) -> None:
    VRC.setdefault(str(guild_id), {}).update(data)
    save_vrc()


def clear_vconf(guild_id: int) -> bool:
    if str(guild_id) in VRC:
        del VRC[str(guild_id)]
        save_vrc()
        return True
    return False


def vrc_cookies(conf: dict) -> dict:
    return {"auth": conf.get("auth_cookie"),
            "twoFactorAuth": conf.get("twofa_cookie")}


def gconf(guild_id: int) -> dict:
    return GUILDS.get(str(guild_id), {})


def set_gconf(guild_id: int, **kv) -> dict:
    g = GUILDS.setdefault(str(guild_id), {})
    for k, v in kv.items():
        if v is not None:
            g[k] = v
    save_guilds(GUILDS)
    return g


def clear_gconf(guild_id: int) -> bool:
    if str(guild_id) in GUILDS:
        del GUILDS[str(guild_id)]
        save_guilds(GUILDS)
        return True
    return False


# --------------------------------------------------------------------------- #
# Helpers: backdrop, copilot enrichment, announcement template
# --------------------------------------------------------------------------- #
async def fetch_backdrop(title: str, year: str | None) -> Path | None:
    cmd = ["python3", str(BACKDROP_SCRIPT), title]
    if year:
        cmd.append(str(year))
    env = dict(os.environ, MOVIE_BOT_TMP=str(TMP_DIR))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        path = (out or b"").decode().strip().splitlines()[-1] if out else "NONE"
        if path and path != "NONE" and Path(path).exists():
            return Path(path)
    except (asyncio.TimeoutError, OSError, IndexError) as e:
        log(f"backdrop fetch failed: {e}")
    return None


def _to_vrchat_png(img_bytes: bytes) -> bytes | None:
    """Convert backdrop bytes to a VRChat-friendly PNG (<=1920px, RGB).

    Best-effort: returns None if Pillow is unavailable or conversion fails.
    """
    try:
        import io
        from PIL import Image
    except ImportError:
        log("Pillow not installed — skipping VRChat event image.")
        return None
    try:
        im = Image.open(io.BytesIO(img_bytes))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        max_w = 1920
        if im.width > max_w:
            ratio = max_w / im.width
            im = im.resize((max_w, int(im.height * ratio)), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) > 9_500_000:  # stay well under VRChat's limit
            out = io.BytesIO()
            im.convert("RGB").save(out, format="PNG")
            data = out.getvalue()
        return data
    except Exception as e:  # noqa: BLE001 - best-effort image path
        log(f"VRChat image conversion failed: {e}")
        return None


# --------------------------------------------------------------------------- #
# Show-type + daypart aware wording
# --------------------------------------------------------------------------- #
# Per-type emoji used on the event name / announcement header.
SHOW_TYPE_EMOJI = {
    "movie": "🎬",
    "tv": "📺",
    "anime": "🌸",
    "other": "🍿",
}
# Event/announcement label by (type, daypart). "other" is daypart-agnostic.
LABEL_MATRIX = {
    "movie": {"morning": "Movie Morning", "afternoon": "Movie Matinée",
              "night": "Movie Night"},
    "tv":    {"morning": "Morning Binge", "afternoon": "Series Matinée",
              "night": "Series Night"},
    "anime": {"morning": "Anime Morning", "afternoon": "Anime Matinée",
              "night": "Anime Night"},
    "other": {"morning": "Watch Party", "afternoon": "Watch Party",
              "night": "Watch Party"},
}
# Noun used when asking the AI to research the title.
SHOW_TYPE_NOUN = {
    "movie": "film",
    "tv": "TV series",
    "anime": "anime series",
    "other": "show",
}


def daypart(when: dt.datetime) -> str:
    """Bucket a datetime into morning / afternoon / night.

    Morning 05:00–11:59, Afternoon 12:00–17:59, Night 18:00–04:59.
    """
    h = when.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 18:
        return "afternoon"
    return "night"


def label_for(show_type: str, when: dt.datetime) -> tuple[str, str]:
    """Return (label, emoji) for a show type at a given start time."""
    st = show_type if show_type in LABEL_MATRIX else "movie"
    return LABEL_MATRIX[st][daypart(when)], SHOW_TYPE_EMOJI[st]


def find_copilot() -> str | None:
    import shutil
    found = shutil.which("copilot")
    if found:
        return found
    for p in Path.home().glob(".nvm/versions/node/*/bin/copilot"):
        return str(p)
    return None


async def enrich_with_copilot(title, year, unix, runtime_min,
                              show_type="movie", label="Movie Night") -> dict | None:
    """Ask Copilot CLI for the announcement body AND a short event blurb.

    Returns {"announcement": str, "event_description": str} or None on failure.
    Text only, no tools. ``show_type`` adapts the research/wording; ``label`` is
    the type/daypart-aware header (e.g. "Anime Matinée").
    """
    if not CFG.get("use_copilot", True):
        return None
    bin = find_copilot()
    if not bin:
        return None
    yr = f" ({year})" if year else ""
    noun = SHOW_TYPE_NOUN.get(show_type, "film")
    runtime_label = ("episode/season runtime" if show_type in ("tv", "anime")
                     else "Runtime")
    prompt = (
        "You are writing content for a Discord watch-party bot. Research the "
        f"{noun} \"{title}\"{yr} from your own knowledge and produce fun, "
        "accurate content. Keep facts accurate; if unsure about a detail, omit "
        "it. Do not invent a runtime if unknown.\n\n"
        "Output ONLY a JSON object (no markdown fences, no commentary) with "
        "exactly these two string keys:\n\n"
        "1. \"announcement\": a whimsical Discord announcement (max 1800 chars) "
        "using :shortcode: emoji (NOT unicode) and this EXACT structure:\n"
        f":clapper: **{label}: <TITLE>** <themed-emoji>\n\n"
        "<one whimsical intro line>\n\n"
        f"Join us at <t:{unix}:F> for **{title}**, <one-line premise>.\n\n"
        f":clapper: **{'Series' if show_type in ('tv', 'anime') else 'Film'}:** <title and year>\n"
        f":alarm_clock: **When:** <t:{unix}:F>\n"
        ":performing_arts: **Starring:** <top 3-5 cast or voice cast>\n"
        f":clapper: **{'Created by' if show_type in ('tv', 'anime') else 'Directed by'}:** <creator or director>\n"
        f":hourglass_flowing_sand: **{runtime_label}:** about <Xh Ym>\n\n"
        "**What's it about?**\n<short paragraph>\n\n"
        "**Fun bits:**\n• <bullet>\n• <bullet>\n• <bullet>\n\n"
        "<whimsical closing line>\n\n"
        "2. \"event_description\": a plain-text blurb for the Discord scheduled "
        "event (max 900 chars, NO emoji shortcodes, NO markdown headers): a "
        "2-3 sentence synopsis, then a line 'Fun facts:' followed by 2-3 short "
        "fun facts each on its own line prefixed with '- '."
    )
    cmd = [bin, "-p", prompt, "--silent"]
    # systemd's minimal PATH can resolve an old /usr/bin/node that breaks the
    # copilot loader. Ensure the bin dir holding *this* copilot (and its node)
    # is first on PATH for the subprocess.
    env = dict(os.environ)
    bin_dir = str(Path(bin).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(Path.home()), env=env)
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=int(CFG.get("copilot_timeout", 120)))
        text = (out or b"").decode("utf-8", "replace").strip()
        if err:
            log(f"copilot stderr: {err.decode('utf-8','replace')[:300]}")
        if not text:
            return None
        return _parse_enrichment(text)
    except (asyncio.TimeoutError, OSError) as e:
        log(f"copilot enrich failed: {e}")
    return None


def _parse_enrichment(text: str) -> dict | None:
    """Extract the JSON object from copilot output (tolerant of code fences)."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if "```" in s:
            s = s[: s.rfind("```")]
        s = s.strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j != -1 and j > i:
            s = s[i : j + 1]
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"copilot enrich: JSON parse failed ({e}); using raw text as body")
        return {"announcement": text.strip()[:1990], "event_description": ""}
    ann = (data.get("announcement") or "").strip()
    desc = (data.get("event_description") or "").strip()
    if not ann:
        return None
    return {"announcement": ann[:1990], "event_description": desc[:900]}


def template_announcement(title, year, unix, runtime_min,
                          label="Movie Night", emoji="🍿") -> str:
    yr = f" ({year})" if year else ""
    rt = ""
    if runtime_min:
        h, m = divmod(int(runtime_min), 60)
        rt = f"\n:hourglass_flowing_sand: **Runtime:** about {h}h {m:02d}m"
    return (
        f":clapper: **{label}: {title}** :popcorn:\n\n"
        f"Grab your snacks and a comfy seat — it's {label.lower()}!\n\n"
        f"Join us at <t:{unix}:F> for **{title}**{yr}.\n\n"
        f":clapper: **Showing:** {title}{yr}\n"
        f":alarm_clock: **When:** <t:{unix}:F>"
        f"{rt}\n\n"
        "React if you're in. See you there! :tada:"
    )


# --------------------------------------------------------------------------- #
# Discord client + command tree
# --------------------------------------------------------------------------- #
intents = discord.Intents.none()
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


class MovieModal(discord.ui.Modal, title="Schedule a Watch Party"):
    def __init__(self, conf: dict, show_type: str = "movie",
                 make_vrchat: bool = True,
                 prefill_date: str | None = None,
                 prefill_time: str | None = None):
        super().__init__(timeout=600)
        self.conf = conf
        self.show_type = show_type if show_type in LABEL_MATRIX else "movie"
        self.make_vrchat = make_vrchat

        # Fields are built here (not as class attributes) so the date/time can be
        # pre-filled per invocation with the next suggested slot. Defaults stay
        # fully editable; they just save typing and avoid mis-clicked dates.
        self.movie = discord.ui.TextInput(
            label="Title", placeholder="e.g. The Matrix", max_length=100)
        self.year = discord.ui.TextInput(
            label="Year (optional, helps for remakes)", required=False,
            placeholder="e.g. 1999", max_length=4)
        self.date = discord.ui.TextInput(
            label="Date (YYYY-MM-DD)", placeholder="2026-07-01", max_length=10,
            default=prefill_date)
        self.start = discord.ui.TextInput(
            label="Start time (24h, HH:MM)", placeholder="19:00", max_length=5,
            default=prefill_time)
        self.runtime = discord.ui.TextInput(
            label="Runtime in minutes (optional)", required=False,
            placeholder="e.g. 136", max_length=4)
        for item in (self.movie, self.year, self.date, self.start, self.runtime):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        conf = self.conf
        tzname = conf.get("timezone", CFG["default_timezone"])
        try:
            tz = ZoneInfo(tzname)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo(CFG["default_timezone"])

        # Parse date/time
        try:
            d = dt.datetime.strptime(self.date.value.strip(), "%Y-%m-%d").date()
            t = dt.datetime.strptime(self.start.value.strip(), "%H:%M").time()
            start_dt = dt.datetime.combine(d, t, tzinfo=tz)
        except ValueError:
            await interaction.followup.send(
                "⚠️ Couldn't parse the date/time. Use **YYYY-MM-DD** and "
                "**HH:MM** (24-hour), e.g. `2026-07-01` and `19:00`.",
                ephemeral=True)
            return

        if start_dt <= dt.datetime.now(tz):
            await interaction.followup.send(
                "⚠️ That date/time is in the past. Pick a future showtime.",
                ephemeral=True)
            return

        runtime_min = None
        if self.runtime.value.strip():
            try:
                runtime_min = int(self.runtime.value.strip())
            except ValueError:
                runtime_min = None
        if not runtime_min:
            runtime_min = CFG.get("default_runtime_minutes", 120)

        end_dt = start_dt + dt.timedelta(minutes=runtime_min)
        unix = int(start_dt.timestamp())
        title = self.movie.value.strip()
        yr = self.year.value.strip() or None

        guild = interaction.guild
        voice = guild.get_channel(conf["voice_channel_id"])
        announce = guild.get_channel(conf["announce_channel_id"])
        if voice is None or announce is None:
            await interaction.followup.send(
                "⚠️ Configured channels are missing. A mod should re-run "
                "`/movie-config set`.", ephemeral=True)
            return

        # Type + daypart aware wording (e.g. "Anime Matinée", "Movie Night").
        label, emoji = label_for(self.show_type, start_dt)

        # Backdrop image (best-effort)
        img_path = await fetch_backdrop(title, yr)
        img_bytes = img_path.read_bytes() if img_path else None

        # Generate rich content once (announcement body + event blurb).
        enriched = await enrich_with_copilot(
            title, yr, unix, runtime_min, self.show_type, label)
        if enriched:
            body = enriched["announcement"]
            event_desc = enriched.get("event_description") or ""
        else:
            body = template_announcement(
                title, yr, unix, runtime_min, label, emoji)
            event_desc = ""
        if not event_desc:
            event_desc = (f"{label} — {title}{f' ({yr})' if yr else ''}. "
                          "Grab snacks and join us!")
        event_desc = event_desc[:1000]

        # Create the scheduled event
        try:
            ev_kwargs = dict(
                name=f"{emoji} {label}: {title}",
                description=event_desc,
                start_time=start_dt,
                end_time=end_dt,
                channel=voice,
                privacy_level=discord.PrivacyLevel.guild_only,
                entity_type=discord.EntityType.voice,
            )
            if img_bytes:
                ev_kwargs["image"] = img_bytes
            event = await guild.create_scheduled_event(**ev_kwargs)
        except discord.Forbidden as e:
            await interaction.followup.send(
                "⚠️ I couldn't create the event. For a **voice** event I need "
                "the **Create Events** permission, plus **View Channel** and "
                "**Connect** on "
                f"{voice.mention if hasattr(voice, 'mention') else 'the voice channel'}. "
                f"Run `/movie-test` to see what's missing.\n`{e.text or e}`",
                ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Failed to create the event: {e}", ephemeral=True)
            return

        event_url = f"https://discord.com/events/{guild.id}/{event.id}"

        record = {
            "event_id": event.id,
            "message_id": None,
            "channel_id": None,
            "title": title,
            "unix": unix,
        }

        # Post the announcement, unless it's been toggled off for this server.
        announce_note = ""
        if conf.get("announce_enabled", True):
            post = f"{body}\n\n:calendar_spiral: **Event:** {event_url}"
            allowed = discord.AllowedMentions.none()
            ping_role = (guild.get_role(conf["ping_role_id"])
                         if conf.get("ping_role_id") else None)
            if ping_role:
                if ping_role.is_default():
                    post = f"@everyone\n{post}"
                    allowed = discord.AllowedMentions(everyone=True)
                else:
                    post = f"{ping_role.mention}\n{post}"
                    allowed = discord.AllowedMentions(roles=[ping_role])
            if len(post) > 2000:
                post = post[:1990]
            try:
                kwargs = {"allowed_mentions": allowed}
                if img_bytes:
                    kwargs["file"] = discord.File(
                        str(img_path), filename="backdrop.jpg")
                msg = await announce.send(content=post, **kwargs)
                record["message_id"] = msg.id
                record["channel_id"] = announce.id
                announce_note = f" and announced it in {announce.mention}"
            except discord.Forbidden:
                announce_note = (f", but I couldn't post in {announce.mention} "
                                 "(missing Send Messages/Embed/Attach)")
        else:
            announce_note = " (announcement skipped — disabled for this server)"

        # Optional: also create a VRChat group calendar event (best-effort).
        vrc_note = ""
        vconf_g = vconf(guild.id)
        vrc_linked = bool(vconf_g.get("group_id") and vconf_g.get("auth_cookie"))
        if vrc_linked and not self.make_vrchat:
            vrc_note = "\n:globe_with_meridians: VRChat event skipped (Discord-only watch party)."
        elif vrc_linked:
            cookies = vrc_cookies(vconf_g)
            # Optionally bring the VRChat event's start forward (end unchanged) so
            # VRChat's own "event starting soon" announcement lands before the
            # movie actually begins. End time is left at the real finish.
            early_min = int(vconf_g.get("early_start_minutes", 0) or 0)
            vrc_start_dt = start_dt - dt.timedelta(minutes=early_min)
            starts = vrc_start_dt.astimezone(dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            ends = end_dt.astimezone(dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

            # Upload the movie backdrop to the linked account (best-effort) so
            # the event carries it instead of the group's banner.
            vrc_image_id = None
            if img_bytes:
                png = _to_vrchat_png(img_bytes)
                if png:
                    try:
                        vrc_image_id = await vrchat.upload_image(
                            cookies, png, tag="gallery",
                            filename=f"movie-night-{event.id}.png")
                        log(f"VRChat image uploaded for guild {guild.id}: "
                            f"{vrc_image_id}")
                    except vrchat.VRChatAuthError:
                        log(f"VRChat image upload: session expired (guild {guild.id})")
                    except vrchat.VRChatError as e:
                        log(f"VRChat image upload failed (guild {guild.id}): "
                            f"{e.message}")

            payload = vrchat.build_event_payload(
                f"{label}: {title}", event_desc, starts, ends,
                category=vconf_g.get("category", "film_media"),
                access_type=vconf_g.get("access_type", "group"),
                send_notification=vconf_g.get("send_notification", True),
                image_id=vrc_image_id)
            try:
                ev = await vrchat.create_event(
                    cookies, vconf_g["group_id"], payload)
                cal_id = ev.get("id")
                record["vrc_group_id"] = vconf_g["group_id"]
                record["vrc_event_id"] = cal_id
                if vrc_image_id:
                    record["vrc_file_id"] = vrc_image_id
                vrc_note = "\n:globe_with_meridians: Also posted to the VRChat group calendar."
                if vrc_image_id:
                    vrc_note += " (with the movie image)"
                log(f"VRChat event created for guild {guild.id}: {cal_id}")
            except vrchat.VRChatAuthError:
                vrc_note = ("\n:warning: VRChat session expired — run "
                            "`/movie-vrchat link` to re-link. (Discord event still created.)")
                log(f"VRChat auth expired for guild {guild.id}")
                # Don't leave the just-uploaded image orphaned.
                if vrc_image_id:
                    try:
                        await vrchat.delete_file(cookies, vrc_image_id)
                    except vrchat.VRChatError:
                        pass
            except vrchat.VRChatError as e:
                vrc_note = f"\n:warning: VRChat calendar event failed: {e.message}"
                log(f"VRChat event failed for guild {guild.id}: {e.message}")
                if vrc_image_id:
                    try:
                        await vrchat.delete_file(cookies, vrc_image_id)
                    except vrchat.VRChatError:
                        pass

        add_event_record(guild.id, record)

        await interaction.followup.send(
            f"✅ Scheduled **{title}** for <t:{unix}:F>, created the event"
            f"{announce_note}.\n{event_url}{vrc_note}",
            ephemeral=True)
        log(f"/movie by {interaction.user} in guild {guild.id}: "
            f"'{title}' @ {start_dt.isoformat()} event={event.id}")


@tree.command(name="movie",
              description="Schedule a watch party (creates an event + announcement).")
@app_commands.guild_only()
@app_commands.default_permissions(manage_events=True)
@app_commands.describe(
    type="What you're showing — adapts the wording (default: Movie).",
    vrchat="Also create a VRChat group event? (default: yes when linked).")
@app_commands.choices(type=[
    app_commands.Choice(name="🎬 Movie", value="movie"),
    app_commands.Choice(name="📺 TV Series", value="tv"),
    app_commands.Choice(name="🌸 Anime", value="anime"),
    app_commands.Choice(name="🍿 Other", value="other"),
])
async def movie(interaction: discord.Interaction,
                type: app_commands.Choice[str] | None = None,
                vrchat: bool | None = None):
    conf = gconf(interaction.guild_id)
    needed = ("mod_channel_id", "announce_channel_id", "voice_channel_id")
    missing = [k for k in needed if k not in conf]
    if missing:
        await interaction.response.send_message(
            "⚠️ This server isn't configured yet. A mod must run "
            "`/movie-config set` with a mod channel, announcements channel and "
            "movie voice channel first.", ephemeral=True)
        return
    if interaction.channel_id != conf["mod_channel_id"]:
        ch = interaction.guild.get_channel(conf["mod_channel_id"])
        where = ch.mention if ch else "the configured mod channel"
        await interaction.response.send_message(
            f"⚠️ `/movie` can only be used in {where}.", ephemeral=True)
        return
    show_type = type.value if type else "movie"
    make_vrchat = True if vrchat is None else vrchat
    tzname = conf.get("timezone", CFG["default_timezone"])
    try:
        tz = ZoneInfo(tzname)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo(CFG["default_timezone"])
    prefill_date, prefill_time = next_slot_prefill(interaction.guild_id, tz)
    await interaction.response.send_modal(
        MovieModal(conf, show_type, make_vrchat, prefill_date, prefill_time))


@tree.command(name="movie-help",
              description="List all movie-night commands and what they do.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_events=True)
async def movie_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎬 Movie Night — command guide",
        description="All commands need the **Manage Events** permission and are "
                    "used in your server.",
        color=0x5865F2)
    embed.add_field(
        name="Scheduling",
        value=("**/movie** — open a form (title, year, date, time, runtime) to "
               "create a scheduled event + announcement. Pick a **type** "
               "(Movie / TV Series / Anime / Other) and the wording adapts to "
               "the show and time of day (e.g. *Anime Matinée*). Set "
               "**vrchat:False** for a Discord-only watch party (skips the "
               "VRChat event even when linked). *Only works in the configured "
               "mod channel.*\n"
               "**/movie-test** — dry-run that checks config & permissions "
               "without posting anything. Run this first.\n"
               "**/movie-cancel** — pick an upcoming movie night to delete its "
               "event **and** announcement (and the VRChat event if linked).\n"
               "**/movie-help** — show this guide."),
        inline=False)
    embed.add_field(
        name="Configuration  ·  /movie-config",
        value=("**set** — set the mod channel, announcements channel, movie "
               "voice channel, optional ping role, timezone, and the "
               "`announcements` toggle (off = create the event only, no post).\n"
               "**show** — show this server's current configuration.\n"
               "**clear** — wipe the configuration to start fresh."),
        inline=False)
    embed.add_field(
        name="VRChat (optional)  ·  /movie-vrchat",
        value=("**link** — link a VRChat account + group (one-time) so movie "
               "nights also post to its calendar.\n"
               "**status** — show the linked account/group and check the "
               "session.\n"
               "**unlink** — remove the VRChat link and stored session."),
        inline=False)
    embed.add_field(
        name="First-time setup",
        value=("1. `/movie-config set` your channels.\n"
               "2. *(optional)* `/movie-vrchat link` your VRChat group.\n"
               "3. `/movie-test` to confirm I have the access I need.\n"
               "4. `/movie` in the mod channel to schedule a night."),
        inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="movie-test",
              description="Dry-run: check the bot's config & permissions without posting anything.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_events=True)
async def movie_test(interaction: discord.Interaction):
    guild = interaction.guild
    me = guild.me
    conf = gconf(guild.id)
    lines = []
    ok_all = True

    def mark(ok, text):
        nonlocal ok_all
        if not ok:
            ok_all = False
        return f"{'✅' if ok else '❌'} {text}"

    # 1. Configuration present
    needed = {
        "mod_channel_id": "Mod channel",
        "announce_channel_id": "Announcements channel",
        "voice_channel_id": "Movie voice channel",
    }
    missing = [label for key, label in needed.items() if key not in conf]
    if missing:
        lines.append(mark(False, "Configuration: missing " + ", ".join(missing)
                          + " — run `/movie-config set`."))
        embed = discord.Embed(
            title="🎬 Movie Night — setup test",
            description="\n".join(lines), color=0xED4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    lines.append(mark(True, "Configuration is set."))

    # 2. Guild-level: Create Events (to create) + Manage Events (to delete any)
    gp = me.guild_permissions
    lines.append(mark(gp.create_events,
                      "**Create Events** permission (required to create scheduled events)."))
    lines.append(mark(gp.manage_events,
                      "**Manage Events** permission (to edit/delete events)."))

    # 3. Announcements channel — view + post (skipped if announcements are off)
    announce_on = conf.get("announce_enabled", True)
    announce = guild.get_channel(conf["announce_channel_id"])
    if not announce_on:
        lines.append("ℹ️ Announcements are **off** — `/movie` will create the "
                     "event (and VRChat event) but won't post a message.")
    if announce is None:
        lines.append(mark(False, "Announcements channel not found (re-run `/movie-config set`)."))
    else:
        p = announce.permissions_for(me)
        sub = []
        sub.append(("View Channel", p.view_channel))
        sub.append(("Send Messages", p.send_messages))
        sub.append(("Embed Links", p.embed_links))
        sub.append(("Attach Files", p.attach_files))
        bad = [n for n, v in sub if not v]
        prefix = "Announcements" if announce_on else "Announce channel (currently off)"
        if announce_on:
            lines.append(mark(not bad,
                              f"{prefix} {announce.mention}: "
                              + ("all good." if not bad else "missing " + ", ".join(bad) + ".")))
        else:
            # Don't fail the test for a channel we're not posting to.
            lines.append(f"ℹ️ {prefix} {announce.mention}: "
                         + ("ready when re-enabled." if not bad
                            else "missing " + ", ".join(bad) + " (fix before re-enabling)."))

    # 4. Voice channel — view + connect (both needed to create a voice event)
    voice = guild.get_channel(conf["voice_channel_id"])
    if voice is None:
        lines.append(mark(False, "Movie voice channel not found (re-run `/movie-config set`)."))
    else:
        p = voice.permissions_for(me)
        vbad = [n for n, v in (("View Channel", p.view_channel),
                               ("Connect", p.connect)) if not v]
        lines.append(mark(not vbad,
                          f"Movie voice channel **{voice.name}**: "
                          + ("good." if not vbad
                             else "missing " + ", ".join(vbad)
                             + " (Discord needs both to schedule a voice event).")))

    # 5. Mod channel — view (where /movie is used)
    mod = guild.get_channel(conf["mod_channel_id"])
    if mod is None:
        lines.append(mark(False, "Mod channel not found (re-run `/movie-config set`)."))
    else:
        p = mod.permissions_for(me)
        lines.append(mark(p.view_channel,
                          f"Mod channel {mod.mention}: "
                          + ("visible." if p.view_channel else "I can't see it (need View Channel).")))

    # 6. Ping role (optional)
    if conf.get("ping_role_id"):
        role = guild.get_role(conf["ping_role_id"])
        if role is None:
            lines.append(mark(False, "Configured ping role no longer exists — update with `/movie-config set`."))
        elif role.is_default():
            lines.append(mark(True, "Ping role: @everyone (handled specially)."))
        else:
            can_ping = role.mentionable or me.guild_permissions.mention_everyone
            lines.append(mark(can_ping,
                              f"Ping role {role.mention}: "
                              + ("can be pinged." if can_ping
                                 else "may not ping (role isn't mentionable and I lack Mention @everyone).")))
    else:
        lines.append("ℹ️ No ping role configured (announcements won't @-mention).")

    # 7. VRChat (optional)
    vconf_g = vconf(guild.id)
    if vconf_g.get("group_id") and vconf_g.get("auth_cookie"):
        gname = vconf_g.get("display_name") or vconf_g.get("group_id")
        lines.append(f"ℹ️ VRChat linked ({gname}) — movie nights will also post to its calendar.")
    else:
        lines.append("ℹ️ VRChat not linked (optional).")

    title = "🎬 Movie Night — setup test"
    if ok_all:
        lines.append("\n**All required checks passed — you're ready to run `/movie`.** 🎉")
        color = 0x57F287
    else:
        lines.append("\n**Some checks failed.** Fix the ❌ items above, then run `/movie-test` again.")
        color = 0xED4245
    embed = discord.Embed(title=title, description="\n".join(lines), color=color)
    embed.set_footer(text="This is a dry run — nothing was posted and no event was created.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _cancel_autocomplete(interaction: discord.Interaction, current: str):
    recs = upcoming_records(interaction.guild_id)
    cur = current.lower()
    choices = []
    for r in recs:
        when = dt.datetime.fromtimestamp(r.get("unix", 0)).strftime("%d %b %H:%M")
        label = f"{r['title']} — {when}"[:100]
        if cur in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(r["event_id"])))
    return choices[:25]


@tree.command(name="movie-cancel",
              description="Cancel a scheduled movie night (deletes the event + announcement).")
@app_commands.guild_only()
@app_commands.default_permissions(manage_events=True)
@app_commands.describe(movie="Which scheduled movie night to cancel")
@app_commands.autocomplete(movie=_cancel_autocomplete)
async def movie_cancel(interaction: discord.Interaction, movie: str):
    conf = gconf(interaction.guild_id)
    # Keep cancellation in the mod channel too, when one is configured.
    if conf.get("mod_channel_id") and interaction.channel_id != conf["mod_channel_id"]:
        ch = interaction.guild.get_channel(conf["mod_channel_id"])
        where = ch.mention if ch else "the configured mod channel"
        await interaction.response.send_message(
            f"⚠️ `/movie-cancel` can only be used in {where}.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        event_id = int(movie)
    except ValueError:
        await interaction.followup.send(
            "⚠️ Pick a movie from the list.", ephemeral=True)
        return

    rec = next((r for r in EVENTS.get(str(interaction.guild_id), [])
                if r.get("event_id") == event_id), None)
    if not rec:
        await interaction.followup.send(
            "⚠️ I couldn't find that movie night (it may already be cancelled "
            "or finished).", ephemeral=True)
        return

    guild = interaction.guild
    notes = []

    # Delete the scheduled event
    try:
        event = guild.get_scheduled_event(event_id) or \
            await guild.fetch_scheduled_event(event_id)
        await event.delete()
        notes.append("event deleted")
    except discord.NotFound:
        notes.append("event already gone")
    except discord.Forbidden:
        notes.append("⚠️ no permission to delete the event")
    except discord.HTTPException as e:
        notes.append(f"⚠️ event delete failed: {e}")

    # Delete the announcement message
    try:
        ch = guild.get_channel(rec["channel_id"])
        if ch:
            msg = await ch.fetch_message(rec["message_id"])
            await msg.delete()
            notes.append("announcement deleted")
        else:
            notes.append("announcement channel missing")
    except discord.NotFound:
        notes.append("announcement already gone")
    except discord.Forbidden:
        notes.append("⚠️ no permission to delete the announcement")
    except discord.HTTPException as e:
        notes.append(f"⚠️ announcement delete failed: {e}")

    # Delete the linked VRChat calendar event, if any
    if rec.get("vrc_event_id") and rec.get("vrc_group_id"):
        vconf_g = vconf(interaction.guild_id)
        if vconf_g.get("auth_cookie"):
            cookies = vrc_cookies(vconf_g)
            try:
                ok = await vrchat.delete_event(
                    cookies, rec["vrc_group_id"], rec["vrc_event_id"])
                notes.append("VRChat event deleted" if ok
                             else "VRChat event already gone")
            except vrchat.VRChatAuthError:
                notes.append("⚠️ VRChat session expired (event not deleted)")
            except vrchat.VRChatError as e:
                notes.append(f"⚠️ VRChat delete failed: {e.message}")
            # Also remove the uploaded image from the linked account.
            if rec.get("vrc_file_id"):
                try:
                    ok = await vrchat.delete_file(cookies, rec["vrc_file_id"])
                    notes.append("VRChat image deleted" if ok
                                 else "VRChat image already gone")
                except vrchat.VRChatAuthError:
                    notes.append("⚠️ VRChat session expired (image not deleted)")
                except vrchat.VRChatError as e:
                    notes.append(f"⚠️ VRChat image delete failed: {e.message}")

    remove_event_record(interaction.guild_id, event_id)
    log(f"/movie-cancel by {interaction.user} in guild {guild.id}: "
        f"'{rec['title']}' event={event_id} ({'; '.join(notes)})")
    await interaction.followup.send(
        f"🗑️ Cancelled **{rec['title']}** — {', '.join(notes)}.", ephemeral=True)


config_group = app_commands.Group(
    name="movie-config", description="Configure the movie-night bot for this server.",
    guild_only=True, default_permissions=discord.Permissions(manage_events=True))


@config_group.command(name="set", description="Set this server's channels, timezone and options.")
@app_commands.describe(
    mod_channel="Channel where mods may run /movie",
    announce_channel="Channel where announcements are posted",
    voice_channel="Voice channel the movie event points to",
    ping_role="Role to @mention with each announcement (optional)",
    timezone="IANA timezone, e.g. Europe/London (default if unset)",
    announcements="Post an announcement in the announce channel? (off = event-only)")
async def config_set(
    interaction: discord.Interaction,
    mod_channel: discord.TextChannel | None = None,
    announce_channel: discord.TextChannel | None = None,
    voice_channel: discord.VoiceChannel | None = None,
    ping_role: discord.Role | None = None,
    timezone: str | None = None,
    announcements: bool | None = None,
):
    if timezone:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            await interaction.response.send_message(
                f"⚠️ `{timezone}` isn't a valid IANA timezone (e.g. "
                "`Europe/London`, `America/New_York`).", ephemeral=True)
            return
    provided = [mod_channel, announce_channel, voice_channel, ping_role,
                timezone, announcements]
    if all(v is None for v in provided):
        await interaction.response.send_message(
            "Nothing to set — provide at least one option.", ephemeral=True)
        return
    conf = set_gconf(
        interaction.guild_id,
        mod_channel_id=mod_channel.id if mod_channel else None,
        announce_channel_id=announce_channel.id if announce_channel else None,
        voice_channel_id=voice_channel.id if voice_channel else None,
        ping_role_id=ping_role.id if ping_role else None,
        timezone=timezone,
        announce_enabled=announcements)
    log(f"/movie-config set by {interaction.user} in guild "
        f"{interaction.guild_id}: {conf}")
    await interaction.response.send_message(
        "✅ Saved. Current config:\n" + _format_conf(interaction.guild, conf),
        ephemeral=True)


@config_group.command(name="show", description="Show this server's current config.")
async def config_show(interaction: discord.Interaction):
    conf = gconf(interaction.guild_id)
    if not conf:
        await interaction.response.send_message(
            "No configuration yet. Run `/movie-config set`.", ephemeral=True)
        return
    await interaction.response.send_message(
        _format_conf(interaction.guild, conf), ephemeral=True)


@config_group.command(name="clear",
                      description="Wipe this server's movie-bot config to start clean.")
async def config_clear(interaction: discord.Interaction):
    cleared = clear_gconf(interaction.guild_id)
    log(f"/movie-config clear by {interaction.user} in guild "
        f"{interaction.guild_id}: cleared={cleared}")
    if cleared:
        await interaction.response.send_message(
            "🧹 Config cleared. The bot is now unconfigured for this server — "
            "run `/movie-config set` to set it up again.\n"
            "_(Scheduled events and announcements already posted are untouched; "
            "use `/movie-cancel` for those.)_", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Nothing to clear — this server has no saved config.", ephemeral=True)


def _format_conf(guild: discord.Guild, conf: dict) -> str:
    def chan(cid):
        c = guild.get_channel(cid) if cid else None
        return c.mention if c else (f"`{cid}` (missing)" if cid else "_not set_")

    def role(rid):
        r = guild.get_role(rid) if rid else None
        if not r:
            return f"`{rid}` (missing)" if rid else "_not set_"
        return "@everyone" if r.is_default() else r.mention

    return (
        f"• **Mod channel:** {chan(conf.get('mod_channel_id'))}\n"
        f"• **Announcements:** {chan(conf.get('announce_channel_id'))}\n"
        f"• **Post announcement:** "
        f"{'on' if conf.get('announce_enabled', True) else 'off (event-only)'}\n"
        f"• **Movie voice channel:** {chan(conf.get('voice_channel_id'))}\n"
        f"• **Ping role:** {role(conf.get('ping_role_id'))}\n"
        f"• **Timezone:** `{conf.get('timezone', CFG['default_timezone'])}`")


tree.add_command(config_group)


# --------------------------------------------------------------------------- #
# VRChat group-calendar linking (/movie-vrchat)
# --------------------------------------------------------------------------- #
# In-memory pending logins between the credentials modal and the 2FA modal.
# Keyed by (guild_id, user_id) -> {auth, methods, group_id, category, expires}.
PENDING_VRC: dict = {}
PENDING_TTL = 600  # seconds


def _prune_pending() -> None:
    now = time.time()
    for k in [k for k, v in PENDING_VRC.items() if v.get("expires", 0) < now]:
        PENDING_VRC.pop(k, None)


async def _store_vrc_session(interaction, group_id, category, auth, twofa):
    """Validate the freshly-minted session and persist it for the guild."""
    cookies = {"auth": auth, "twoFactorAuth": twofa}
    try:
        me = await vrchat.current_user(cookies)
    except vrchat.VRChatError as e:
        await interaction.followup.send(
            f"⚠️ Linked but couldn't confirm the session: {e.message}",
            ephemeral=True)
        return
    set_vconf(interaction.guild_id, {
        "group_id": group_id,
        "auth_cookie": auth,
        "twofa_cookie": twofa,
        "display_name": me.get("displayName"),
        "category": category,
        "access_type": "group",
        "send_notification": True,
        "linked_by": interaction.user.id,
        "linked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    log(f"/movie-vrchat link by {interaction.user} in guild "
        f"{interaction.guild_id}: {me.get('displayName')} -> {group_id}")
    await interaction.followup.send(
        f"✅ Linked VRChat account **{me.get('displayName')}** to group "
        f"`{group_id}`. Movie nights will now also post to its calendar.\n"
        "_Only the session token was stored — never your password._",
        ephemeral=True)


class VRC2FAModal(discord.ui.Modal, title="VRChat 2FA"):
    code = discord.ui.TextInput(
        label="2FA code", placeholder="6-digit code from your app or email",
        min_length=6, max_length=8)

    def __init__(self, pending_key):
        super().__init__(timeout=300)
        self.pending_key = pending_key

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        _prune_pending()
        pending = PENDING_VRC.get(self.pending_key)
        if not pending:
            await interaction.followup.send(
                "⚠️ That login attempt expired. Run `/movie-vrchat link` again.",
                ephemeral=True)
            return
        method = pending["methods"][0]
        try:
            res = await vrchat.verify_2fa(
                pending["auth"], method, self.code.value.strip())
        except vrchat.VRChatError as e:
            await interaction.followup.send(
                f"⚠️ {e.message}", ephemeral=True)
            return
        PENDING_VRC.pop(self.pending_key, None)
        await _store_vrc_session(
            interaction, pending["group_id"], pending["category"],
            res["auth"], res.get("twofa"))


class VRC2FAView(discord.ui.View):
    def __init__(self, pending_key, user_id):
        super().__init__(timeout=300)
        self.pending_key = pending_key
        self.user_id = user_id

    @discord.ui.button(label="Enter 2FA code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction: discord.Interaction,
                         button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This button isn't for you.", ephemeral=True)
            return
        await interaction.response.send_modal(VRC2FAModal(self.pending_key))


class VRCLinkModal(discord.ui.Modal, title="Link VRChat account"):
    username = discord.ui.TextInput(
        label="VRChat username or email", max_length=100)
    password = discord.ui.TextInput(
        label="VRChat password (used once, not stored)", max_length=128)
    group_id = discord.ui.TextInput(
        label="VRChat group ID", placeholder="grp_xxxxxxxx-...", max_length=64)
    category = discord.ui.TextInput(
        label="Event category (optional)", required=False,
        placeholder="film_media", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid = self.group_id.value.strip()
        if not gid.startswith("grp_"):
            await interaction.followup.send(
                "⚠️ That doesn't look like a VRChat group ID (should start with "
                "`grp_`).", ephemeral=True)
            return
        cat = (self.category.value or "").strip().lower() or "film_media"
        if cat not in vrchat.VALID_CATEGORIES:
            cat = "film_media"
        try:
            res = await vrchat.login(
                self.username.value.strip(), self.password.value)
        except vrchat.VRChatError as e:
            await interaction.followup.send(f"⚠️ {e.message}", ephemeral=True)
            return

        if res["state"] == "ok":
            await _store_vrc_session(
                interaction, gid, cat, res["auth"], res.get("twofa"))
            return

        # 2FA required: stash the partial session and offer a button.
        key = (interaction.guild_id, interaction.user.id)
        PENDING_VRC[key] = {
            "auth": res["auth"],
            "methods": [m.lower() for m in res["methods"]],
            "group_id": gid,
            "category": cat,
            "expires": time.time() + PENDING_TTL,
        }
        method = PENDING_VRC[key]["methods"][0]
        how = ("your email" if method == "emailotp"
               else "your authenticator app")
        await interaction.followup.send(
            f"🔐 VRChat needs a 2FA code from {how}. Click below to enter it.",
            view=VRC2FAView(key, interaction.user.id), ephemeral=True)


vrc_group = app_commands.Group(
    name="movie-vrchat",
    description="Link a VRChat group so movie nights post to its calendar.",
    guild_only=True, default_permissions=discord.Permissions(manage_events=True))


@vrc_group.command(name="link",
                   description="Link this server's VRChat account + group (one-time).")
async def vrc_link(interaction: discord.Interaction):
    await interaction.response.send_modal(VRCLinkModal())


@vrc_group.command(name="status",
                   description="Show the linked VRChat account and check the session.")
async def vrc_status(interaction: discord.Interaction):
    conf = vconf(interaction.guild_id)
    if not conf.get("group_id"):
        await interaction.response.send_message(
            "No VRChat group linked. Run `/movie-vrchat link`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    valid = "unknown"
    try:
        me = await vrchat.current_user(vrc_cookies(conf))
        valid = f"✅ active (as **{me.get('displayName')}**)"
    except vrchat.VRChatAuthError:
        valid = "⚠️ expired — run `/movie-vrchat link` to refresh"
    except vrchat.VRChatError as e:
        valid = f"⚠️ check failed: {e.message}"
    early = int(conf.get("early_start_minutes", 0) or 0)
    early_label = f"{early} min early" if early else "off (starts at showtime)"
    await interaction.followup.send(
        f"• **VRChat account:** {conf.get('display_name', '—')}\n"
        f"• **Group:** `{conf['group_id']}`\n"
        f"• **Category:** `{conf.get('category', 'film_media')}`\n"
        f"• **Early start:** {early_label}\n"
        f"• **Session:** {valid}", ephemeral=True)


@vrc_group.command(name="unlink",
                   description="Remove this server's VRChat link and stored session.")
async def vrc_unlink(interaction: discord.Interaction):
    cleared = clear_vconf(interaction.guild_id)
    log(f"/movie-vrchat unlink by {interaction.user} in guild "
        f"{interaction.guild_id}: cleared={cleared}")
    await interaction.response.send_message(
        "🧹 VRChat link removed and stored session deleted." if cleared
        else "Nothing to unlink — no VRChat group is linked.", ephemeral=True)


@vrc_group.command(
    name="early-start",
    description="Start the VRChat event N minutes early (end time unchanged).")
@app_commands.describe(
    minutes="Minutes to bring the VRChat event start forward (0 disables, max 60).")
async def vrc_early_start(interaction: discord.Interaction, minutes: int):
    if not vconf(interaction.guild_id).get("group_id"):
        await interaction.response.send_message(
            "No VRChat group linked. Run `/movie-vrchat link` first.", ephemeral=True)
        return
    minutes = max(0, min(60, minutes))
    set_vconf(interaction.guild_id, {"early_start_minutes": minutes})
    log(f"/movie-vrchat early-start by {interaction.user} in guild "
        f"{interaction.guild_id}: {minutes} min")
    if minutes:
        msg = (f"⏪ VRChat events will now start **{minutes} min early** so VRChat's "
               f"announcement goes out before showtime. The end time is unchanged "
               f"(e.g. a 19:00 movie → VRChat event starts 18:{60 - minutes:02d}).")
    else:
        msg = "VRChat early start disabled — events start at the movie's real time."
    await interaction.response.send_message(msg, ephemeral=True)


tree.add_command(vrc_group)


@client.event
async def on_ready():
    # Per-guild sync = instant command availability.
    for g in client.guilds:
        try:
            tree.copy_global_to(guild=g)
            await tree.sync(guild=g)
        except discord.HTTPException as e:
            log(f"sync failed for guild {g.id}: {e}")
    log(f"Logged in as {client.user} (id={client.user.id}). "
        f"Guilds={[g.name for g in client.guilds]}")


@client.event
async def on_guild_join(guild: discord.Guild):
    try:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        log(f"Joined guild {guild.name} ({guild.id}); commands synced.")
    except discord.HTTPException as e:
        log(f"sync failed on join for guild {guild.id}: {e}")


def main():
    token = os.environ.get("MOVIE_BOT_TOKEN", "").strip()
    if not token and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
    if not token:
        log(f"FATAL: no token. Set MOVIE_BOT_TOKEN or create {TOKEN_FILE}")
        raise SystemExit(1)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    log("Starting Movie Night bot.")
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
