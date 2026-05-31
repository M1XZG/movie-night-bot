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

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "token"
CONFIG_FILE = HERE / "config.json"
GUILDS_FILE = HERE / "guilds.json"
EVENTS_FILE = HERE / "events.json"
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
#   { guild_id: [ {event_id, message_id, channel_id, title, unix}, ... ] }
EVENTS = load_json(EVENTS_FILE, {})


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


def find_copilot() -> str | None:
    import shutil
    found = shutil.which("copilot")
    if found:
        return found
    for p in Path.home().glob(".nvm/versions/node/*/bin/copilot"):
        return str(p)
    return None


async def enrich_with_copilot(title, year, unix, runtime_min) -> dict | None:
    """Ask Copilot CLI for the announcement body AND a short event blurb.

    Returns {"announcement": str, "event_description": str} or None on failure.
    Text only, no tools.
    """
    if not CFG.get("use_copilot", True):
        return None
    bin = find_copilot()
    if not bin:
        return None
    yr = f" ({year})" if year else ""
    prompt = (
        "You are writing content for a Discord movie-night bot. Research the "
        f"film \"{title}\"{yr} from your own knowledge and produce fun, "
        "accurate content. Keep facts accurate; if unsure about a detail, omit "
        "it. Do not invent a runtime if unknown.\n\n"
        "Output ONLY a JSON object (no markdown fences, no commentary) with "
        "exactly these two string keys:\n\n"
        "1. \"announcement\": a whimsical Discord announcement (max 1800 chars) "
        "using :shortcode: emoji (NOT unicode) and this EXACT structure:\n"
        ":clapper: **Movie Night: <TITLE>** <themed-emoji>\n\n"
        "<one whimsical intro line>\n\n"
        f"Join us at <t:{unix}:F> for **{title}**, <one-line premise>.\n\n"
        ":clapper: **Film:** <title and year>\n"
        f":alarm_clock: **When:** <t:{unix}:F>\n"
        ":performing_arts: **Starring:** <top 3-5 cast>\n"
        ":clapper: **Directed by:** <director>\n"
        ":hourglass_flowing_sand: **Runtime:** about <Xh Ym>\n\n"
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


def template_announcement(title, year, unix, runtime_min) -> str:
    yr = f" ({year})" if year else ""
    rt = ""
    if runtime_min:
        h, m = divmod(int(runtime_min), 60)
        rt = f"\n:hourglass_flowing_sand: **Runtime:** about {h}h {m:02d}m"
    return (
        f":clapper: **Movie Night: {title}** :popcorn:\n\n"
        "Grab your snacks and a comfy seat — it's movie night!\n\n"
        f"Join us at <t:{unix}:F> for **{title}**{yr}.\n\n"
        f":clapper: **Film:** {title}{yr}\n"
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


class MovieModal(discord.ui.Modal, title="Schedule a Movie Night"):
    def __init__(self, conf: dict):
        super().__init__(timeout=600)
        self.conf = conf

    movie = discord.ui.TextInput(
        label="Movie title", placeholder="e.g. The Matrix", max_length=100)
    year = discord.ui.TextInput(
        label="Year (optional, helps for remakes)", required=False,
        placeholder="e.g. 1999", max_length=4)
    date = discord.ui.TextInput(
        label="Date (YYYY-MM-DD)", placeholder="2026-07-01", max_length=10)
    start = discord.ui.TextInput(
        label="Start time (24h, HH:MM)", placeholder="19:00", max_length=5)
    runtime = discord.ui.TextInput(
        label="Runtime in minutes (optional)", required=False,
        placeholder="e.g. 136", max_length=4)

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

        # Backdrop image (best-effort)
        img_path = await fetch_backdrop(title, yr)
        img_bytes = img_path.read_bytes() if img_path else None

        # Generate rich content once (announcement body + event blurb).
        enriched = await enrich_with_copilot(title, yr, unix, runtime_min)
        if enriched:
            body = enriched["announcement"]
            event_desc = enriched.get("event_description") or ""
        else:
            body = template_announcement(title, yr, unix, runtime_min)
            event_desc = ""
        if not event_desc:
            event_desc = (f"Movie night — {title}{f' ({yr})' if yr else ''}. "
                          "Grab snacks and join us!")
        event_desc = event_desc[:1000]

        # Create the scheduled event
        try:
            ev_kwargs = dict(
                name=f"🎬 Movie Night: {title}",
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
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ I lack **Manage Events** permission (or can't see the "
                "voice channel). Ask an admin to grant it.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Failed to create the event: {e}", ephemeral=True)
            return

        event_url = f"https://discord.com/events/{guild.id}/{event.id}"

        body = f"{body}\n\n:calendar_spiral: **Event:** {event_url}"

        # Optional role ping at the top of the announcement.
        allowed = discord.AllowedMentions.none()
        ping_role = None
        if conf.get("ping_role_id"):
            ping_role = guild.get_role(conf["ping_role_id"])
        if ping_role:
            if ping_role.is_default():
                body = f"@everyone\n{body}"
                allowed = discord.AllowedMentions(everyone=True)
            else:
                body = f"{ping_role.mention}\n{body}"
                allowed = discord.AllowedMentions(roles=[ping_role])
        if len(body) > 2000:
            body = body[:1990]

        # Post the announcement
        try:
            kwargs = {"allowed_mentions": allowed}
            if img_bytes:
                kwargs["file"] = discord.File(
                    str(img_path), filename="backdrop.jpg")
            msg = await announce.send(content=body, **kwargs)
        except discord.Forbidden:
            await interaction.followup.send(
                f"✅ Event created ({event_url}) but I can't post in "
                f"{announce.mention} (missing Send Messages/Embed/Attach).",
                ephemeral=True)
            return

        add_event_record(guild.id, {
            "event_id": event.id,
            "message_id": msg.id,
            "channel_id": announce.id,
            "title": title,
            "unix": unix,
        })

        await interaction.followup.send(
            f"✅ Scheduled **{title}** for <t:{unix}:F>, created the event, and "
            f"announced it in {announce.mention}.\n{event_url}", ephemeral=True)
        log(f"/movie by {interaction.user} in guild {guild.id}: "
            f"'{title}' @ {start_dt.isoformat()} event={event.id}")


@tree.command(name="movie",
              description="Schedule a movie night (creates an event + announcement).")
@app_commands.guild_only()
@app_commands.default_permissions(manage_events=True)
async def movie(interaction: discord.Interaction):
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
    await interaction.response.send_modal(MovieModal(conf))


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

    remove_event_record(interaction.guild_id, event_id)
    log(f"/movie-cancel by {interaction.user} in guild {guild.id}: "
        f"'{rec['title']}' event={event_id} ({'; '.join(notes)})")
    await interaction.followup.send(
        f"🗑️ Cancelled **{rec['title']}** — {', '.join(notes)}.", ephemeral=True)


config_group = app_commands.Group(
    name="movie-config", description="Configure the movie-night bot for this server.",
    guild_only=True, default_permissions=discord.Permissions(manage_events=True))


@config_group.command(name="set", description="Set this server's channels and timezone.")
@app_commands.describe(
    mod_channel="Channel where mods may run /movie",
    announce_channel="Channel where announcements are posted",
    voice_channel="Voice channel the movie event points to",
    ping_role="Role to @mention with each announcement (optional)",
    timezone="IANA timezone, e.g. Europe/London (default if unset)")
async def config_set(
    interaction: discord.Interaction,
    mod_channel: discord.TextChannel | None = None,
    announce_channel: discord.TextChannel | None = None,
    voice_channel: discord.VoiceChannel | None = None,
    ping_role: discord.Role | None = None,
    timezone: str | None = None,
):
    if timezone:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            await interaction.response.send_message(
                f"⚠️ `{timezone}` isn't a valid IANA timezone (e.g. "
                "`Europe/London`, `America/New_York`).", ephemeral=True)
            return
    if not any([mod_channel, announce_channel, voice_channel, ping_role, timezone]):
        await interaction.response.send_message(
            "Nothing to set — provide at least one option.", ephemeral=True)
        return
    conf = set_gconf(
        interaction.guild_id,
        mod_channel_id=mod_channel.id if mod_channel else None,
        announce_channel_id=announce_channel.id if announce_channel else None,
        voice_channel_id=voice_channel.id if voice_channel else None,
        ping_role_id=ping_role.id if ping_role else None,
        timezone=timezone)
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
        f"• **Movie voice channel:** {chan(conf.get('voice_channel_id'))}\n"
        f"• **Ping role:** {role(conf.get('ping_role_id'))}\n"
        f"• **Timezone:** `{conf.get('timezone', CFG['default_timezone'])}`")


tree.add_command(config_group)


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
