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

  /movie schedule                Opens a form (title / year / date / time / runtime),
                        then creates the event + posts the announcement.
                        Only works in the guild's configured mod channel.
  /movie-cancel         Autocompletes upcoming movie nights; deletes the
                        scheduled event AND the announcement message.

  /movie-config set     Set this guild's mod_channel, announce_channel,
                        voice_channel, ping_role and timezone (any subset).
  /movie-config show    Show this guild's current configuration.
  /movie-config clear   Wipe this guild's configuration to start clean.

Film facts come from verified TMDB metadata. The optional GitHub Copilot CLI
adds a generic invitation, never a film synopsis or ratings. Failed metadata
lookups fall back to a plain template without invented facts.

Per-guild config is stored in guilds.json keyed by guild id, so each server's
mods configure their own channels, ping role and timezone.

Required Discord permissions (NOT administrator):
  View Channels, Send Messages, Embed Links, Attach Files, Manage Events
Invite scopes: bot + applications.commands
Gateway intents: guilds only (no privileged intents).
"""
import asyncio
import copy
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands

import vrchat
from movie_repair import ACTIVE, Picker, RepairSession, RepairShortcut
from movie_repair import consistent_identity
from find_backdrop import BackdropError, BackdropIdentityError, MAX_IMAGE_BYTES, manual_backdrop, validated_image
from ratings import (
    IMDB_ID, MovieRatings, RatingsError, discord_announcement,
    discord_event_description, fetch_ratings, load_api_key,
    load_omdb_api_key,
)

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
    "MOVIE_BOT_TMP", HERE / "tmp"))

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
RATINGS_API_KEY = None
OMDB_API_KEY = ""
GUILDS = load_json(GUILDS_FILE, {})
# Per-guild list of scheduled movie nights the bot created, so /movie-cancel can
# remove both the event and the announcement message:
#   { guild_id: [ {event_id, message_id, channel_id, title, unix,
#                  vrc_group_id?, vrc_event_id?}, ... ] }
def load_events() -> dict:
    if not EVENTS_FILE.exists():
        return {}
    # Losing this index loses the only safe way to cancel or recover partial writes.
    data = json.loads(EVENTS_FILE.read_text())
    if not isinstance(data, dict) or any(
        not isinstance(records, list) or any(
            not isinstance(record, dict)
            or type(record.get("event_id")) is not int
            or type(record.get("unix")) is not int
            for record in records
        ) for records in data.values()
    ):
        raise ValueError("Invalid events.json; restore the booking index before starting.")
    return data


EVENTS = load_events()
# Per-guild VRChat linkage (chmod 600 — session cookies, never the password):
#   { guild_id: {group_id, auth_cookie, twofa_cookie, display_name,
#                category, access_type, send_notification, linked_by, linked_at} }
VRC = load_json(VRC_FILE, {})


def save_events(*, strict=True) -> None:
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=EVENTS_FILE.parent, delete=False) as f:
            temp = Path(f.name)
            json.dump(EVENTS, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        temp.replace(EVENTS_FILE)
        directory = os.open(EVENTS_FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as e:
        log(f"WARN: could not write events.json: {e}")
        if strict:
            raise
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def add_event_record(guild_id: int, rec: dict) -> None:
    records = EVENTS.setdefault(str(guild_id), [])
    if any(r.get("event_id") == rec["event_id"] for r in records):
        raise ValueError("This Discord booking is already tracked.")
    records.append(rec)
    save_events()


def remove_event_record(guild_id: int, event_id: int) -> None:
    lst = EVENTS.get(str(guild_id), [])
    EVENTS[str(guild_id)] = [r for r in lst if r.get("event_id") != event_id]
    try:
        save_events()
    except OSError:
        EVENTS[str(guild_id)] = lst
        raise


def upcoming_records(guild_id: int) -> list:
    """Return recent bookings and unfinished cancellations without deleting history."""
    now = int(time.time())
    lst = EVENTS.get(str(guild_id), [])
    kept = [r for r in lst if r.get("unix", 0) >= now - 6 * 3600
            or r.get("cancel_requested")]
    return sorted(kept, key=lambda r: r.get("unix", 0))


def next_slot_prefill(guild_id: int, tz: "ZoneInfo",
                      interval_days: int = 7) -> tuple[str | None, str | None]:
    """Suggest the next (date, time) one interval after the latest upcoming event.

    Returns (YYYY-MM-DD, HH:MM) strings to pre-fill the schedule form, or
    (None, None) when there are no upcoming events to extrapolate from. The date
    is advanced on the calendar (date + N days) so the suggested local start time
    is preserved across daylight-saving changes.
    """
    recs = [r for r in upcoming_records(guild_id)
            if not r.get("cancel_requested") and r.get("unix", 0) > time.time()]
    if not recs:
        return None, None
    last = dt.datetime.fromtimestamp(recs[-1].get("unix", 0), tz)
    next_date = last.date() + dt.timedelta(days=interval_days)
    return next_date.strftime("%Y-%m-%d"), last.strftime("%H:%M")


def save_vrc() -> None:
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=VRC_FILE.parent, delete=False) as f:
            temp = Path(f.name)
            os.fchmod(f.fileno(), 0o600)
            json.dump(VRC, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        temp.replace(VRC_FILE)
        directory = os.open(VRC_FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as e:
        log(f"WARN: could not write vrchat.json: {e}")
        raise
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def vconf(guild_id: int) -> dict:
    return VRC.get(str(guild_id), {})


def set_vconf(guild_id: int, data: dict) -> None:
    previous = copy.deepcopy(VRC)
    VRC.setdefault(str(guild_id), {}).update(data)
    try:
        save_vrc()
    except OSError:
        VRC.clear()
        VRC.update(previous)
        raise


def clear_vconf(guild_id: int) -> bool:
    if str(guild_id) in VRC:
        previous = VRC.pop(str(guild_id))
        try:
            save_vrc()
        except OSError:
            VRC[str(guild_id)] = previous
            raise
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
async def fetch_backdrop(title: str, year: str | None,
                         show_type: str = "movie",
                         image: discord.Attachment | None = None,
                         *, imdb_id: str | None = None, imdb_id_explicit: bool = True) -> Path:
    if image is not None:
        if image.size > MAX_IMAGE_BYTES:
            raise BackdropError("The manual backdrop must be smaller than 8 MB.")
        try:
            data = await asyncio.wait_for(image.read(), timeout=30)
        except asyncio.TimeoutError:
            raise BackdropError("The manual backdrop download timed out.") from None
        except discord.HTTPException:
            raise BackdropError("The manual backdrop attachment could not be downloaded.") from None
        return await asyncio.to_thread(manual_backdrop, data, TMP_DIR)
    cmd = [sys.executable, str(BACKDROP_SCRIPT), title]
    if year:
        cmd.append(str(year))
    cmd.extend(["--show-type", show_type])
    if imdb_id:
        cmd.extend(["--imdb-id" if imdb_id_explicit else "--inferred-imdb-id", imdb_id])
    env = dict(os.environ, MOVIE_BOT_TMP=str(TMP_DIR))
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        if err:
            log(f"backdrop lookup: {err.decode(errors='replace').strip()}")
        if proc.returncode:
            reasons = [
                line.removeprefix("ERROR: ") for line in err.decode(errors="replace").splitlines()
                if line.startswith("ERROR: ")
            ]
            if reasons:
                error_type = BackdropIdentityError if b"ERROR_CODE: inferred_identity_mismatch" in err else BackdropError
                raise error_type(reasons[-1][:500])
            raise BackdropError(
                "TMDB backdrop lookup failed. Check bot.log or supply an image with `/movie schedule image:`."
            )
        path = (out or b"").decode().strip().splitlines()[-1] if out else "NONE"
        if path and path != "NONE":
            result = Path(path).resolve()
            if not result.is_relative_to(TMP_DIR.resolve()) or not result.is_file():
                raise BackdropError("The backdrop helper returned an invalid image location.")
            await asyncio.to_thread(read_backdrop, result)
            return result
        raise BackdropError("The backdrop lookup did not produce an image.")
    except asyncio.TimeoutError:
        raise BackdropError("The backdrop lookup timed out; supply an image with `/movie schedule image:`.") from None
    except (OSError, IndexError, UnicodeError):
        raise BackdropError("The backdrop lookup could not be run; check bot.log.") from None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # The child exited before the timeout cleanup.
            await proc.communicate()


def read_backdrop(path: Path) -> bytes:
    """Only reuse bounded, validated images in our managed cache."""
    path = Path(path).resolve()
    if not path.is_relative_to(TMP_DIR.resolve()):
        raise BackdropError("The backdrop is outside the managed image cache.")
    with path.open("rb") as source:
        return validated_image(source.read(MAX_IMAGE_BYTES + 1))


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
        return data if len(data) <= 9_500_000 else None
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
                              show_type="movie", label="Movie Night",
                              imdb_id: str | None = None, *, imdb_id_explicit: bool = True) -> dict | None:
    """Use verified movie metadata for facts; ask Copilot only for a snack invitation."""
    try:
        from find_backdrop import find_title, load_credential, repair_details

        def details():
            kind, identifier = find_title(title, year, show_type, load_credential(), imdb_id,
                                          imdb_id_explicit=imdb_id_explicit)
            data = repair_details(kind, identifier)
            if imdb_id and data.get("imdb_id") != imdb_id:
                raise BackdropError("Movie metadata did not confirm the selected IMDb identifier.")
            return data

        metadata = await asyncio.to_thread(details)
    except (BackdropError, ValueError, KeyError, TypeError):
        log("Movie metadata unavailable; using the plain announcement without invented facts.")
        return None
    display_title = title if show_type in ("tv", "anime") else metadata["title"]
    body = template_announcement(display_title, metadata.get("year") or year, unix, runtime_min, label)
    synopsis = metadata.get("overview") or ""
    people = metadata.get("people") or ""
    if synopsis:
        body += "\n\n**What's it about?**\n" + synopsis[:800]
    if people:
        body += "\n\n:performing_arts: **People:** " + people[:250]
    verified = {"announcement": body, "event_description": synopsis[:900], "metadata": metadata}
    if not CFG.get("use_copilot", True):
        return verified
    bin = find_copilot()
    if not bin:
        return verified
    prompt = (
        "Write a whimsical, generic watch-party snack invitation. Do not research "
        "a film or supply any facts, titles, names, dates, times, runtimes, links, "
        "ratings, mentions or instructions. Use no tools. Output only JSON with "
        "two short string keys: announcement (one invitation sentence, max 180 "
        "characters) and event_description (one closing sentence, max 100 characters)."
    )
    cmd = [bin, "-p", prompt, "--silent", "--available-tools", "--no-custom-instructions"]
    # systemd's minimal PATH can resolve an old /usr/bin/node that breaks the
    # copilot loader. Ensure the bin dir holding *this* copilot (and its node)
    # is first on PATH for the subprocess.
    env = dict(os.environ)
    bin_dir = str(Path(bin).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(HERE), env=env)
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=int(CFG.get("copilot_timeout", 120)))
        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode or not text:
            log(f"Copilot returned no usable invitation (exit_code={proc.returncode}); using the plain template.")
            return verified
        invitation = _parse_enrichment(text)
        if not invitation:
            return verified
        verified["announcement"] += "\n\n" + invitation["announcement"][:180]
        return verified
    except (asyncio.TimeoutError, OSError, ValueError) as e:
        log(f"copilot enrich failed ({type(e).__name__}); using the plain template.")
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
    return verified


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
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) for key in ("announcement", "event_description")
    ):
        return None
    ann = data["announcement"].strip().replace("\u2014", ",")
    desc = data["event_description"].strip().replace("\u2014", ",")
    if not ann or len(ann) > 180 or len(desc) > 100 or re.search(r"\btt\d{7,12}\b", ann + desc) or any(
        token in ann + desc for token in ("@", "http", "<t:", "\n")
    ):
        return None
    return {"announcement": ann, "event_description": desc}


def template_announcement(title, year, unix, runtime_min,
                          label="Movie Night", emoji="🍿") -> str:
    yr = f" ({year})" if year else ""
    rt = ""
    if runtime_min:
        h, m = divmod(int(runtime_min), 60)
        rt = f"\n:hourglass_flowing_sand: **Runtime:** about {h}h {m:02d}m"
    return (
        f":clapper: **{label}: {title}** :popcorn:\n\n"
        f"Grab your snacks and a comfy seat, it's {label.lower()}!\n\n"
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
                 prefill_time: str | None = None,
                 image: discord.Attachment | None = None,
                 imdb_id: str | None = None):
        super().__init__(timeout=600)
        self.conf = dict(conf)
        self.show_type = show_type if show_type in LABEL_MATRIX else "movie"
        self.make_vrchat = make_vrchat
        self.image = image
        self.imdb_id = imdb_id

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
        if getattr(self, "_submitted", False):
            await interaction.response.send_message(
                "This scheduling request was already submitted. Check the booking before retrying.",
                ephemeral=True)
            return
        self._submitted = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        started = time.monotonic()
        outcome = "error"
        try:
            await MovieModal._schedule(self, interaction)
            outcome = "returned"
        except OSError:
            await interaction.followup.send(
                "⚠️ Booking state could not be saved. No further actions were attempted. "
                "An event may already exist; don't schedule it again. Restore events.json "
                "storage and check the existing booking before retrying.",
                ephemeral=True)
        except (discord.HTTPException, asyncio.TimeoutError):
            await interaction.followup.send(
                "⚠️ A Discord request failed. A booking may already exist; check it "
                "before retrying. Any confirmed steps remain in events.json.",
                ephemeral=True)
        finally:
            log(f"movie_request outcome={outcome} elapsed_seconds={time.monotonic() - started:.2f}")

    async def _schedule(self, interaction: discord.Interaction):
        global RATINGS_API_KEY, OMDB_API_KEY
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

        utc_start = start_dt.astimezone(dt.timezone.utc)
        if utc_start.astimezone(tz).replace(tzinfo=None) != start_dt.replace(tzinfo=None):
            await interaction.followup.send(
                "⚠️ That local time does not exist because the clocks change. Choose another time.",
                ephemeral=True)
            return
        if start_dt.utcoffset() != start_dt.replace(fold=1).utcoffset():
            await interaction.followup.send(
                "⚠️ That local time occurs twice when the clocks change. Choose a time outside that hour.",
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
                runtime_min = -1
        if runtime_min is None:
            runtime_min = CFG.get("default_runtime_minutes", 120)
        if type(runtime_min) is not int or not 1 <= runtime_min <= 1440:
            await interaction.followup.send(
                "⚠️ Runtime must be a whole number between 1 and 1440 minutes.",
                ephemeral=True)
            return

        end_dt = (utc_start + dt.timedelta(minutes=runtime_min)).astimezone(tz)
        unix = int(start_dt.timestamp())
        title = self.movie.value.strip()
        yr = self.year.value.strip() or None
        if not title or (yr and (not yr.isdecimal() or not 1800 <= int(yr) <= 2200)):
            await interaction.followup.send(
                "⚠️ Enter a title and, if supplied, a four-digit release year.", ephemeral=True)
            return

        guild = interaction.guild
        voice = guild.get_channel(conf["voice_channel_id"])
        announce = guild.get_channel(conf["announce_channel_id"])
        if voice is None or (conf.get("announce_enabled", True) and announce is None):
            await interaction.followup.send(
                "⚠️ Configured channels are missing. A mod should re-run "
                "`/movie-config set`.", ephemeral=True)
            return

        # Type + daypart aware wording (e.g. "Anime Matinée", "Movie Night").
        label, emoji = label_for(self.show_type, start_dt)

        ratings = MovieRatings()
        try:
            ratings = await asyncio.to_thread(
                fetch_ratings, title, yr, self.show_type,
                api_key=RATINGS_API_KEY,
                omdb_api_key=OMDB_API_KEY or None,
                imdb_id=self.imdb_id)
            if self.imdb_id and ratings.imdb_id != self.imdb_id:
                ratings = MovieRatings()
                ratings_warning = "Ratings did not match the supplied IMDb identifier; scores omitted."
            else:
                ratings_warning = ratings.missing_message()
        except RatingsError as exc:
            ratings_warning = str(exc)
        ratings_note = ""
        if ratings_warning:
            log(f"WARN: movie ratings unavailable: {ratings_warning}")
            ratings_note = f"\n:warning: Ratings: {ratings_warning}"
        imdb_id = self.imdb_id or ratings.imdb_id
        identity_verified = False
        if imdb_id and not self.imdb_id and self.image is not None:
            # Manual images bypass the helper, but must not bypass inferred-ID validation.
            def validate_inferred():
                from find_backdrop import find_title, load_credential
                find_title(title, yr, self.show_type, load_credential(), imdb_id, imdb_id_explicit=False)
            try:
                await asyncio.to_thread(validate_inferred)
                identity_verified = True
            except BackdropError:
                imdb_id, ratings = None, MovieRatings()
                ratings_note += "\n:warning: The inferred IMDb identity could not be verified; identifier and scores omitted."

        image_note = ""
        img_path = None
        img_bytes = None
        try:
            img_path = await fetch_backdrop(
                title, yr, self.show_type, self.image, imdb_id=imdb_id,
                imdb_id_explicit=bool(self.imdb_id))
            img_bytes = img_path.read_bytes()
            identity_verified = self.image is None or identity_verified
        except BackdropIdentityError:
            imdb_id, ratings = None, MovieRatings()
            ratings_note += "\n:warning: The inferred IMDb identity did not match this title/year; identifier and scores omitted."
            try:
                img_path = await fetch_backdrop(title, yr, self.show_type, self.image)
                img_bytes = img_path.read_bytes()
            except (BackdropError, OSError):
                image_note = "\n:warning: No verified event image was found. Supply a manual backdrop."
        except (BackdropError, OSError) as exc:
            warning = str(exc) if isinstance(exc, BackdropError) else "The downloaded backdrop could not be read."
            log(f"WARN: backdrop unavailable for '{title}': {warning}")
            image_note = f"\n:warning: No event image: {warning}"

        # Generate rich content once (announcement body + event blurb).
        enriched = await enrich_with_copilot(
            title, yr, unix, runtime_min, self.show_type, label, imdb_id=imdb_id,
            imdb_id_explicit=bool(self.imdb_id))
        if enriched:
            body = enriched["announcement"]
            event_desc = enriched.get("event_description") or ""
            metadata = enriched.get("metadata")
            if metadata:
                canonical_id = metadata.get("imdb_id")
                if canonical_id and IMDB_ID.fullmatch(canonical_id):
                    if ratings.imdb_id and ratings.imdb_id != canonical_id:
                        ratings = MovieRatings()
                        ratings_note += "\n:warning: Ratings identity differed from verified metadata; scores omitted."
                    imdb_id = canonical_id
                    identity_verified = True
                if self.show_type not in ("tv", "anime"):
                    title = metadata["title"]
                yr = metadata.get("year") or yr
            if not consistent_identity(body + "\n" + event_desc, imdb_id):
                body = template_announcement(title, yr, unix, runtime_min, label, emoji)
                event_desc = ""
                ratings_note += "\n:warning: Conflicting IMDb identifiers in generated text were discarded."
        else:
            body = template_announcement(
                title, yr, unix, runtime_min, label, emoji)
            event_desc = ""
        if not event_desc:
            event_desc = (f"{label}: {title}{f' ({yr})' if yr else ''}. "
                          "Grab snacks and join us!")
        event_desc = event_desc[:1000]

        discord_desc = discord_event_description(
            event_desc, runtime_min, ratings, announcement=body)

        if utc_start <= dt.datetime.now(dt.timezone.utc):
            await interaction.followup.send(
                "⚠️ The showtime passed while preparing the booking. Choose a later time; no event was created.",
                ephemeral=True)
            return
        vconf_g = dict(vconf(guild.id))
        vrc_linked = bool(vconf_g.get("group_id"))
        vrc_payload = None
        if vrc_linked and self.make_vrchat:
            early_min = max(0, min(60, int(vconf_g.get("early_start_minutes", 0) or 0)))
            starts = (utc_start - dt.timedelta(minutes=early_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
            ends = end_dt.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            vrc_payload = vrchat.build_event_payload(
                f"{label}: {title}", event_desc, starts, ends,
                category=vconf_g.get("category", "film_media"),
                access_type=vconf_g.get("access_type", "group"),
                send_notification=vconf_g.get("send_notification", True))
        # Refuse external writes if the booking index cannot be persisted.
        save_events(strict=True)
        # Create the scheduled event
        try:
            ev_kwargs = dict(
                name=f"{emoji} {label}: {title}"[:100],
                description=discord_desc,
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
            "year": yr,
            "show_type": self.show_type,
            "imdb_id": imdb_id,
            "identity_provenance": ("user_explicit" if self.imdb_id else
                                    "tmdb_verified" if imdb_id and identity_verified else
                                    "ratings_inferred" if imdb_id else "unverified"),
            "unix": unix,
            "announcement_state": "pending" if conf.get("announce_enabled", True) else "disabled",
            "backdrop_path": str(img_path) if img_path else None,
            "end_unix": int(end_dt.timestamp()),
        }
        if vrc_payload is not None:
            record.update(
                vrc_group_id=vconf_g["group_id"], vrc_state="pending",
                vrc_payload=vrc_payload)
        # Save the Discord success before any independently failing operation.
        add_event_record(guild.id, record)
        key = guild.id, event.id
        ACTIVE.add(key)
        try:
            await MovieModal._finish_schedule(
                self, interaction, record, announce, body, runtime_min, ratings,
                img_path, img_bytes, vconf_g, vrc_linked, event_url, ratings_note, image_note)
        finally:
            ACTIVE.discard(key)

    async def _finish_schedule(self, interaction, record, announce, body, runtime_min, ratings,
                               img_path, img_bytes, vconf_g, vrc_linked, event_url,
                               ratings_note, image_note):
        guild = interaction.guild
        conf = self.conf
        title, unix = record["title"], record["unix"]

        # Post the announcement, unless it's been toggled off for this server.
        announce_note = ""
        if conf.get("announce_enabled", True):
            footer = f"\n\n:calendar_spiral: **Event:** {event_url}"
            prefix = ""
            allowed = discord.AllowedMentions.none()
            ping_role = (guild.get_role(conf["ping_role_id"])
                         if conf.get("ping_role_id") else None)
            if ping_role:
                if ping_role.is_default():
                    prefix = "@everyone\n"
                    allowed = discord.AllowedMentions(everyone=True)
                else:
                    prefix = f"{ping_role.mention}\n"
                    allowed = discord.AllowedMentions(roles=[ping_role])
            post = prefix + discord_announcement(
                body, runtime_min, ratings,
                limit=2000 - len(prefix) - len(footer),
            ) + footer
            record.update(channel_id=announce.id, announcement_state="sending")
            save_events(strict=True)
            try:
                kwargs = {"allowed_mentions": allowed}
                if img_bytes:
                    kwargs["file"] = discord.File(io.BytesIO(img_bytes), filename="backdrop.jpg")
                msg = await announce.send(content=post, **kwargs)
                record["message_id"] = msg.id
                record["announcement_state"] = "posted"
                announce_note = f" and announced it in {announce.mention}"
            except discord.Forbidden:
                record["announcement_state"] = "failed"
                announce_note = (f", but I couldn't post in {announce.mention} "
                                 "(missing Send Messages/Embed/Attach)")
            except (discord.HTTPException, asyncio.TimeoutError, OSError):
                record["announcement_state"] = "unknown"
                announce_note = ", but the announcement result is uncertain; check the channel before retrying"
            finally:
                if kwargs.get("file"):
                    kwargs["file"].close()
            save_events(strict=True)
        else:
            announce_note = " (announcement skipped — disabled for this server)"

        # Optional: also create a VRChat group calendar event (best-effort).
        vrc_note = ""
        if vrc_linked and not self.make_vrchat:
            vrc_note = "\n:globe_with_meridians: VRChat event skipped (Discord-only watch party)."
        elif vrc_linked:
            vrc_note = "\n" + await create_record_vrc(record, vconf_g, img_bytes)

        await interaction.followup.send(
            f"✅ Scheduled **{title}** for <t:{unix}:F>, created the event"
            f"{announce_note}.\n{event_url}{vrc_note}{ratings_note}{image_note}",
            ephemeral=True, view=RepairShortcut(RepairSession(sys.modules[__name__], interaction, None), record["event_id"]))
        log(f"/movie schedule by {interaction.user} in guild {guild.id}: "
            f"'{title}' scheduled")


async def create_record_vrc(record, conf, img_bytes=None, *, existing_ids=None):
    """Persist intent and each confirmed side effect; never delete uncertain artwork."""
    if record.get("vrc_event_id") or record.get("vrc_state") in ("creating", "unknown"):
        return ":warning: This creation was already attempted. Reconcile it with `/movie-vrchat recover`."
    if conf.get("group_id") != record.get("vrc_group_id") or not conf.get("auth_cookie"):
        record["vrc_state"] = "failed"
        save_events(strict=True)
        return ":warning: VRChat login is missing. Re-link, then use `/movie-vrchat recover`."
    cookies = vrc_cookies(conf)
    try:
        if existing_ids is None:
            existing_ids = [event["id"] for event in await vrchat.list_events(cookies, record["vrc_group_id"])]
    except vrchat.VRChatError as exc:
        log(f"vrchat_stage=preflight error_type={type(exc).__name__} status={exc.status}")
        record["vrc_state"] = "failed"
        record["vrc_last_error"] = {
            "stage": "preflight", "type": type(exc).__name__, "http_status": exc.status,
        }
        save_events(strict=True)
        return ":warning: VRChat calendar could not be read; no creation was attempted. Check the link and recover later."
    record["vrc_existing_ids"] = list(existing_ids)
    save_events(strict=True)
    warning = ""
    if img_bytes and not record.get("vrc_file_id"):
        png = await asyncio.to_thread(_to_vrchat_png, img_bytes)
        if png:
            try:
                record["vrc_file_id"] = await vrchat.upload_image(
                    cookies, png, tag="gallery", filename=f"movie-night-{record['event_id']}.png")
                record["vrc_payload"]["imageId"] = record["vrc_file_id"]
                save_events(strict=True)
            except vrchat.VRChatAuthError as exc:
                log(f"vrchat_stage=image_upload error_type={type(exc).__name__} status={exc.status}")
                warning = "VRChat image upload failed: session expired."
            except vrchat.VRChatError as exc:
                log(f"vrchat_stage=image_upload error_type={type(exc).__name__} status={exc.status}")
                warning = "VRChat image upload failed; check the linkage and connection."
        else:
            warning = "The backdrop could not be converted for VRChat."
    record["vrc_state"] = "creating"
    save_events(strict=True)
    started = time.monotonic()
    failure = None
    try:
        event = await vrchat.create_event(cookies, record["vrc_group_id"], record["vrc_payload"],
                                          existing_ids=record["vrc_existing_ids"])
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
            raise vrchat.VRChatWriteUncertainError(
                "Calendar creation did not return an event identifier.",
                existing_ids=record["vrc_existing_ids"])
        record.update(vrc_event_id=event["id"], vrc_state="created")
        result = ":globe_with_meridians: Also posted to the VRChat group calendar."
        if record.get("vrc_file_id"):
            result += " (with the movie image)"
        elif warning:
            result += "\n:warning: " + warning
    except vrchat.VRChatAuthError as exc:
        failure = {"stage": "calendar_create", "type": type(exc).__name__, "http_status": exc.status}
        record["vrc_state"] = "failed"
        result = (":warning: VRChat session expired. Run `/movie-vrchat link`, then "
                  "`/movie-vrchat recover`. The Discord booking is saved.")
    except vrchat.VRChatWriteUncertainError as exc:
        failure = {
            "stage": "calendar_create", "type": type(exc).__name__,
            "http_status": exc.status, "validation_problems": list(exc.validation_problems),
        }
        record.update(vrc_state="unknown", vrc_existing_ids=list(exc.existing_ids))
        if exc.calendar_id:
            record["vrc_event_id"] = exc.calendar_id
            result = (":warning: VRChat created an event, but its fields need review. "
                      "The event and cover are retained. Use `/movie-vrchat recover`; "
                      "do not create another event.")
        else:
            result = (":warning: VRChat creation is uncertain. The submitted request and cover are saved. "
                      "Use `/movie-vrchat recover`; do not create another event.")
    except (vrchat.VRChatError, asyncio.TimeoutError) as exc:
        failure = {
            "stage": "calendar_create", "type": type(exc).__name__,
            "http_status": getattr(exc, "status", None),
        }
        record["vrc_state"] = "unknown" if isinstance(exc, asyncio.TimeoutError) else "failed"
        result = (":warning: VRChat calendar creation was not confirmed. Use "
                  "`/movie-vrchat recover` to reconcile it before retrying. The uploaded cover is retained.")
    if failure:
        record["vrc_last_error"] = failure
    else:
        record.pop("vrc_last_error", None)
    save_events(strict=True)
    log(f"vrchat_stage=calendar_create state={record['vrc_state']} "
        f"elapsed_seconds={time.monotonic() - started:.2f} error={json.dumps(failure)}")
    return result


movie_group = app_commands.Group(
    name="movie", description="Schedule or repair this server's movie nights.",
    guild_only=True, default_permissions=discord.Permissions(manage_events=True))


@movie_group.command(name="schedule", description="Schedule a watch party (creates an event + announcement).")
@app_commands.describe(
    type="What you're showing — adapts the wording (default: Movie).",
    vrchat="Also create a VRChat group event? (default: yes when linked).",
    image="Optional landscape image to use instead of TMDB (JPEG/PNG/WebP, under 8 MB).",
    imdb="Optional IMDb identifier, e.g. tt31349844, to resolve titles with the same name.")
@app_commands.choices(type=[
    app_commands.Choice(name="🎬 Movie", value="movie"),
    app_commands.Choice(name="📺 TV Series", value="tv"),
    app_commands.Choice(name="🌸 Anime", value="anime"),
    app_commands.Choice(name="🍿 Other", value="other"),
])
async def movie(interaction: discord.Interaction,
                type: app_commands.Choice[str] | None = None,
                vrchat: bool | None = None,
                image: discord.Attachment | None = None,
                imdb: str | None = None):
    if imdb is not None:
        imdb = imdb.strip()
        if not IMDB_ID.fullmatch(imdb):
            await interaction.response.send_message(
                "⚠️ Use an IMDb identifier such as `tt31349844`, not a title or URL.",
                ephemeral=True)
            return
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
            f"⚠️ `/movie schedule` can only be used in {where}.", ephemeral=True)
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
        MovieModal(conf, show_type, make_vrchat, prefill_date, prefill_time, image, imdb))


@movie_group.command(name="unfuck", description="Pick a movie night and repair its film details or artwork.")
@app_commands.describe(image="Optional replacement landscape cover (JPEG/PNG/WebP under 8 MB).")
async def movie_unfuck(interaction: discord.Interaction, image: discord.Attachment | None = None):
    session = RepairSession(sys.modules[__name__], interaction, image)
    if not await session.allowed(interaction):
        return
    if not session.records():
        return await interaction.response.send_message("No upcoming Movie Bot bookings in this server.", ephemeral=True)
    await interaction.response.send_message("Which movie night needs fixing?", view=Picker(session), ephemeral=True)


tree.add_command(movie_group)


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
        value=("**/movie schedule** — open a form (title, year, date, time, runtime) to "
               "create a scheduled event + announcement. Pick a **type** "
               "(Movie / TV Series / Anime / Other) and the wording adapts to "
               "the show and time of day (e.g. *Anime Matinée*). Set "
               "**vrchat:False** for a Discord-only watch party (skips the "
               "VRChat event even when linked). *Only works in the configured "
               "mod channel.* Use **image:** to attach a landscape backdrop "
               "instead of looking it up on TMDB. Use **imdb:** to identify "
               "a particular film or series when names overlap.\n"
               "**/movie-test** — dry-run that checks config & permissions "
               "without posting anything. Run this first.\n"
               "**/movie-cancel** — pick an upcoming movie night to delete its "
               "event **and** announcement (and the VRChat event if linked).\n"
               "**/movie-help** — show this guide."),
        inline=False)
    embed.add_field(
        name="Repairs",
        value="**/movie unfuck**: pick an upcoming booking, describe the problem and preview a repair. "
              "Use **image:** for a custom cover, or describe a **text only** repair to keep both covers. "
              "Reopen the picker to resume an unfinished repair. Nothing changes until **Apply repair**.",
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
               "**recover**: reconcile a saved unfinished booking after re-linking, without blindly reposting it.\n"
               "**unlink** — remove the VRChat link and stored session."),
        inline=False)
    embed.add_field(
        name="Image credits",
        value=("Backdrops: [TMDB](https://www.themoviedb.org).\n"
               "This product uses the TMDB API but is not endorsed or certified by TMDB."),
        inline=False)
    embed.add_field(
        name="First-time setup",
        value=("1. `/movie-config set` your channels.\n"
               "2. *(optional)* `/movie-vrchat link` your VRChat group.\n"
               "3. `/movie-test` to confirm I have the access I need.\n"
               "4. `/movie schedule` in the mod channel to schedule a night."),
        inline=False)
    embed.set_thumbnail(url="attachment://tmdb-logo.png")
    await interaction.response.send_message(
        embed=embed, file=discord.File(HERE / "assets" / "tmdb-logo.png"),
        ephemeral=True)


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
        lines.append("ℹ️ Announcements are **off** — `/movie schedule` will create the "
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

    # 5. Mod channel — view (where /movie schedule is used)
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
        lines.append("\n**All required checks passed — you're ready to run `/movie schedule`.** 🎉")
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
        tz = ZoneInfo(gconf(interaction.guild_id).get("timezone") or CFG["default_timezone"])
        when = dt.datetime.fromtimestamp(r.get("unix", 0), tz).strftime("%d %b %H:%M %Z")
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
    key = interaction.guild_id, event_id
    if key in ACTIVE:
        await interaction.followup.send("This booking is already being changed. Wait for that operation.", ephemeral=True)
        return
    ACTIVE.add(key)
    try:
        await cancel_record(interaction, rec)
    except OSError:
        await interaction.followup.send(
            "⚠️ Cancellation state could not be saved. No further deletions were attempted. "
            "Keep this booking and retry once events.json storage is restored.", ephemeral=True)
    finally:
        ACTIVE.discard(key)


def _artwork_file_ids(rec):
    return set(filter(None, (
        rec.get("vrc_file_id"), (rec.get("vrc_payload") or {}).get("imageId"),
        (rec.get("repair_pending") or {}).get("upload_id"),
        *(rec.get("retained_vrc_files") or []),
        *(rec.get("cancel_artwork_ids") or []),
    )))


async def cancel_record(interaction, rec):
    guild = interaction.guild
    event_id = rec["event_id"]
    notes = []
    rec["cancel_requested"] = True
    progress = rec.setdefault("cancel_progress", {})
    save_events(strict=True)

    # Delete the scheduled event
    if not progress.get("event"):
        try:
            event = guild.get_scheduled_event(event_id) or await guild.fetch_scheduled_event(event_id)
            await event.delete()
            progress["event"] = True
            notes.append("event deleted")
        except discord.NotFound:
            progress["event"] = True
            notes.append("event already gone")
        except (discord.HTTPException, asyncio.TimeoutError, OSError):
            notes.append("⚠️ event deletion failed; retry cancellation")
        save_events(strict=True)

    # Delete the announcement message
    if not progress.get("announcement"):
        if not rec.get("message_id"):
            if rec.get("announcement_state") in ("sending", "unknown"):
                notes.append("⚠️ announcement result is uncertain; locate its message before completing cancellation")
            else:
                progress["announcement"] = True
        else:
            try:
                ch = guild.get_channel(rec.get("channel_id"))
                if not ch:
                    raise ValueError("Announcement channel missing.")
                msg = await ch.fetch_message(rec["message_id"])
                await msg.delete()
                progress["announcement"] = True
                notes.append("announcement deleted")
            except discord.NotFound:
                progress["announcement"] = True
                notes.append("announcement already gone")
            except (ValueError, discord.HTTPException, asyncio.TimeoutError, OSError):
                notes.append("⚠️ announcement deletion failed; retry cancellation")
        save_events(strict=True)

    # Delete the linked VRChat calendar event, if any
    vconf_g = vconf(interaction.guild_id)
    cookies = vrc_cookies(vconf_g)
    if not progress.get("vrchat"):
        if rec.get("vrc_event_id") and rec.get("vrc_group_id"):
            try:
                if not vconf_g.get("auth_cookie") or vconf_g.get("group_id") != rec["vrc_group_id"]:
                    raise ValueError("VRChat login or group changed.")
                try:
                    current = await vrchat.get_event(
                        cookies, rec["vrc_group_id"], rec["vrc_event_id"])
                except vrchat.VRChatError as exc:
                    if exc.status != 404:
                        raise
                    current = {}
                image_id = current.get("imageId")
                if image_id:
                    vrchat._validate_id(image_id, "file")
                    captured = rec.setdefault("cancel_artwork_ids", [])
                    if image_id not in captured:
                        captured.append(image_id)
                    save_events(strict=True)
                ok = await vrchat.delete_event(
                    cookies, rec["vrc_group_id"], rec["vrc_event_id"])
                progress["vrchat"] = True
                notes.append("VRChat event deleted" if ok
                             else "VRChat event already gone")
            except (ValueError, vrchat.VRChatError, asyncio.TimeoutError, OSError):
                notes.append("⚠️ VRChat event not deleted; check the link and retry cancellation")
        elif rec.get("vrc_state") in ("creating", "unknown"):
            notes.append("⚠️ VRChat creation was uncertain; reconcile it with `/movie-vrchat recover` before deleting its image")
        else:
            progress["vrchat"] = True
        save_events(strict=True)

    # A cover is safe to remove only once the calendar event is confirmed gone.
    if progress.get("vrchat"):
        files = _artwork_file_ids(rec)
        shared = set()
        for records in EVENTS.values():
            for other in records:
                if other.get("event_id") == event_id:
                    continue
                if other.get("cancel_requested") and (other.get("cancel_progress") or {}).get("vrchat"):
                    continue
                if other.get("vrc_event_id") or other.get("vrc_state") in ("pending", "failed", "creating", "unknown"):
                    shared.update(_artwork_file_ids(other))
        removed = rec.setdefault("cancelled_files", [])
        errors = rec.setdefault("cancel_file_errors", {})
        deleted_count = absent_count = 0
        for file_id in files.difference(removed, shared):
            try:
                if not vconf_g.get("auth_cookie") or vconf_g.get("group_id") != rec.get("vrc_group_id"):
                    raise ValueError("VRChat login or group changed.")
                deleted = await vrchat.delete_file(cookies, file_id)
                removed.append(file_id)
                errors.pop(file_id, None)
                deleted_count += bool(deleted)
                absent_count += not deleted
            except (ValueError, vrchat.VRChatError, asyncio.TimeoutError, OSError) as exc:
                errors[file_id] = {"type": type(exc).__name__, "http_status": getattr(exc, "status", None)}
                log(f"vrchat_stage=cancel_artwork error_type={type(exc).__name__} "
                    f"status={getattr(exc, 'status', None)}")
                notes.append("⚠️ VRChat image cleanup failed; retry cancellation")
            save_events(strict=True)
        if deleted_count:
            notes.append(f"{deleted_count} VRChat artwork file{'s' if deleted_count != 1 else ''} deleted")
        if absent_count:
            notes.append(f"{absent_count} VRChat artwork file{'s' if absent_count != 1 else ''} already gone")
        if files.intersection(shared).difference(removed):
            notes.append("⚠️ Artwork is still tracked by another booking; cleanup deferred")
        progress["images"] = files.issubset(removed)
        save_events(strict=True)

    complete = all(progress.get(step) for step in ("event", "announcement", "vrchat", "images"))
    if complete:
        remove_event_record(interaction.guild_id, event_id)
    log(f"/movie-cancel in guild {guild.id}: complete={complete}")
    await interaction.followup.send(
        f"{'🗑️ Cancelled' if complete else '⚠️ Cancellation incomplete for'} **{rec['title']}**. "
        + ("; ".join(notes) or "Previously completed steps were not repeated.")
        + ("" if complete else " The booking is retained so failed cleanup can be retried."),
        ephemeral=True)


config_group = app_commands.Group(
    name="movie-config", description="Configure the movie-night bot for this server.",
    guild_only=True, default_permissions=discord.Permissions(manage_events=True))


@config_group.command(name="set", description="Set this server's channels, timezone and options.")
@app_commands.describe(
    mod_channel="Channel where mods may run /movie schedule",
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
    try:
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
    except OSError:
        await interaction.followup.send(
            "⚠️ The refreshed VRChat login could not be saved. The previous link is unchanged. "
            "Restore state-file storage and re-link before recovering a booking.", ephemeral=True)
        return
    log(f"/movie-vrchat link by {interaction.user} in guild "
        f"{interaction.guild_id}: {me.get('displayName')} -> {group_id}")
    await interaction.followup.send(
        f"✅ Linked VRChat account **{me.get('displayName')}** to group "
        f"`{group_id}`. Movie nights will now also post to its calendar.\n"
        "For a previously missed calendar booking, run `/movie-vrchat recover` and pick it.\n"
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


@vrc_group.command(name="recover", description="Recover a saved, unfinished VRChat calendar booking after re-linking.")
@app_commands.describe(movie="Which saved movie booking to reconcile")
@app_commands.autocomplete(movie=_cancel_autocomplete)
async def vrc_recover(interaction: discord.Interaction, movie: str):
    session = RepairSession(sys.modules[__name__], interaction, None)
    if not await session.allowed(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        event_id = int(movie)
    except ValueError:
        return await interaction.followup.send("Pick a booking from the list.", ephemeral=True)
    rec = next((r for r in EVENTS.get(str(interaction.guild_id), []) if r["event_id"] == event_id), None)
    if not rec or not rec.get("vrc_payload"):
        return await interaction.followup.send(
            "No saved VRChat creation request for this booking. Older or Discord-only bookings "
            "need a reviewed manual backfill; they won't be created automatically.", ephemeral=True)
    key = interaction.guild_id, event_id
    if key in ACTIVE:
        return await interaction.followup.send("This booking is already being changed. Try again later.", ephemeral=True)
    ACTIVE.add(key)
    try:
        note = await recover_record_vrc(interaction, rec)
        await interaction.followup.send(note, ephemeral=True)
    except OSError:
        await interaction.followup.send(
            "Booking state could not be saved; no further writes were attempted. Restore storage before retrying.",
            ephemeral=True)
    except (ValueError, discord.HTTPException, vrchat.VRChatError):
        await interaction.followup.send(
            "Recovery could not confirm the booking. Check the linked account/group and connection before retrying.",
            ephemeral=True)
    finally:
        ACTIVE.discard(key)


async def recover_record_vrc(interaction, rec):
    conf = dict(vconf(interaction.guild_id))
    if not conf.get("auth_cookie") or conf.get("group_id") != rec["vrc_group_id"]:
        return "Re-link the original VRChat group before recovering this booking."
    cookies = vrc_cookies(conf)
    if rec.get("vrc_event_id"):
        event = await vrchat.get_event(cookies, rec["vrc_group_id"], rec["vrc_event_id"])
        if vrchat._event_problems(event, rec["vrc_payload"]):
            return "The saved VRChat event needs manual review; its title, times or artwork differ from the saved request."
        rec["vrc_state"] = "created"
        save_events(strict=True)
        return "The existing VRChat calendar event is confirmed. No new event or notification was created."
    excluded = set(rec.get("vrc_existing_ids", []))
    excluded.update(r["vrc_event_id"] for r in EVENTS.get(str(interaction.guild_id), [])
                    if r is not rec and r.get("vrc_event_id"))
    if rec.get("vrc_state") in ("creating", "unknown") and "vrc_existing_ids" in rec:
        found = await vrchat.find_created_event(cookies, rec["vrc_group_id"], rec["vrc_payload"], list(excluded))
        if found:
            rec.update(vrc_event_id=found["id"], vrc_state="created")
            save_events(strict=True)
            return "Recovered the existing VRChat calendar event. No new event or notification was created."
        return ("No unique matching event was confirmed. This calendar creation remains uncertain; "
                "another event will not be posted. Review the calendar manually.")
    events = await vrchat.list_events(cookies, rec["vrc_group_id"])
    matches = [event for event in events if event["id"] not in excluded
               and not vrchat._event_problems(event, rec["vrc_payload"])]
    if len(matches) > 1:
        return "Several VRChat calendar events match this request. Review them manually; no new event was posted."
    if matches:
        return ("A matching calendar event exists, but this request has no saved before-write baseline. "
                "Review it manually rather than claiming or duplicating another event.")
    if rec.get("cancel_requested") or rec.get("vrc_state") not in ("pending", "failed"):
        return ("No unique matching VRChat event was confirmed. The previous creation result is uncertain, "
                "so another calendar event will not be posted. Review the calendar manually.")
    live = await interaction.guild.fetch_scheduled_event(rec["event_id"])
    if live.status != discord.EventStatus.scheduled or live.start_time.timestamp() <= time.time():
        return "This Discord booking is no longer upcoming; no VRChat event was created."
    if live.start_time.timestamp() != rec["unix"] or (
        rec.get("end_unix") and (not live.end_time or live.end_time.timestamp() != rec["end_unix"])
    ):
        return "The booking time changed. Review it before creating the missing VRChat event."
    image = None
    if not rec.get("vrc_file_id") and rec.get("backdrop_path"):
        try:
            image = await asyncio.to_thread(read_backdrop, Path(rec["backdrop_path"]))
        except (BackdropError, OSError):
            pass
    return await create_record_vrc(rec, conf, image, existing_ids=[event["id"] for event in events])


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
    global RATINGS_API_KEY, OMDB_API_KEY
    token = os.environ.get("MOVIE_BOT_TOKEN", "").strip()
    if not token and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
    if not token:
        log(f"FATAL: no token. Set MOVIE_BOT_TOKEN or create {TOKEN_FILE}")
        raise SystemExit(1)
    try:
        RATINGS_API_KEY = load_api_key()
        log("MDBList ratings credential loaded.")
    except RatingsError as exc:
        log(f"WARN: ratings unavailable at startup: {exc}")
    try:
        OMDB_API_KEY = load_omdb_api_key()
        log("OMDb ratings credential loaded.")
    except RatingsError as exc:
        OMDB_API_KEY = ""
        log(f"WARN: OMDb ratings unavailable at startup: {exc}")
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    log("Starting Movie Night bot.")
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
