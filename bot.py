import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
BLIZZARD_CLIENT_ID = os.getenv("BLIZZARD_CLIENT_ID", "").strip()
BLIZZARD_CLIENT_SECRET = os.getenv("BLIZZARD_CLIENT_SECRET", "").strip()
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
UPDATE_MINUTES = max(1, int(os.getenv("UPDATE_MINUTES", "1")))
LEVEL_UP_CHANNEL_ID = int(os.getenv("LEVEL_UP_CHANNEL_ID", "1552220682329395240"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "tracker.sqlite3")
EMOJI_CONFIG_PATH = os.getenv("EMOJI_CONFIG_PATH", "emoji_config.json")

API_BASE = "https://eu.api.blizzard.com"
API_NAMESPACE = "profile-forever-eu"
OAUTH_URL = "https://oauth.battle.net/token"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("wow-tracker")

if not all([DISCORD_TOKEN, BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET, CHANNEL_ID, LEVEL_UP_CHANNEL_ID]):
    raise RuntimeError(
        "Set DISCORD_TOKEN, BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET "
        "DISCORD_CHANNEL_ID and LEVEL_UP_CHANNEL_ID in your environment."
    )


# ---------- Name formatting ----------

def standardise_name(value: str) -> str:
    """Trim whitespace and capitalise the first character of the name."""
    value = value.strip()
    if not value:
        raise ValueError("Character names cannot be blank.")
    return value[0].upper() + value[1:].lower()


def character_slug(first_name: str, surname: str) -> str:
    return f"{standardise_name(first_name)}-{standardise_name(surname)}".casefold()


# ---------- SQLite storage ----------

def db_connect():
    db_path = Path(DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS players (
                slug TEXT PRIMARY KEY,
                first_name TEXT NOT NULL,
                surname TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS roster_messages (
                page INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)
        # Upgrade older installations without deleting their existing player data.
        player_columns = {row["name"] for row in con.execute("PRAGMA table_info(players)")}
        if "last_level" not in player_columns:
            con.execute("ALTER TABLE players ADD COLUMN last_level INTEGER")

        # Outbox prevents missed/duplicate notifications if Discord is briefly unavailable.
        con.execute("""
            CREATE TABLE IF NOT EXISTS level_up_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                level INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sent_message_id INTEGER,
                UNIQUE(slug, level)
            )
        """)


def list_players():
    with db_connect() as con:
        return con.execute(
            "SELECT * FROM players ORDER BY lower(first_name), lower(surname)"
        ).fetchall()


def add_player(first_name: str, surname: str, nickname: str, initial_level: int | None = None):
    first_name = standardise_name(first_name)
    surname = standardise_name(surname)
    slug = character_slug(first_name, surname)
    with db_connect() as con:
        con.execute(
            """INSERT INTO players(slug, first_name, surname, nickname, added_at, last_level)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (slug, first_name, surname, nickname.strip(),
             datetime.now(timezone.utc).isoformat(), initial_level)
        )


def update_nickname(first_name: str, surname: str, nickname: str) -> bool:
    slug = character_slug(first_name, surname)
    with db_connect() as con:
        cur = con.execute(
            "UPDATE players SET nickname = ? WHERE slug = ?",
            (nickname.strip(), slug)
        )
        return cur.rowcount > 0


def delete_player(first_name: str, surname: str) -> bool:
    slug = character_slug(first_name, surname)
    with db_connect() as con:
        cur = con.execute("DELETE FROM players WHERE slug = ?", (slug,))
        return cur.rowcount > 0


def get_roster_messages():
    with db_connect() as con:
        rows = con.execute(
            "SELECT page, message_id FROM roster_messages ORDER BY page"
        ).fetchall()
        return {row["page"]: row["message_id"] for row in rows}


def save_roster_message(page: int, message_id: int):
    with db_connect() as con:
        con.execute(
            """INSERT INTO roster_messages(page, message_id) VALUES (?, ?)
               ON CONFLICT(page) DO UPDATE SET message_id=excluded.message_id""",
            (page, message_id)
        )


def delete_roster_message_page(page: int):
    with db_connect() as con:
        con.execute("DELETE FROM roster_messages WHERE page = ?", (page,))


# ---------- Level-up announcements ----------

# Each eligible template has an equal chance of being selected. Conditions add
# candidates to the pool; they do not take priority over the general messages.
GENERAL_LEVEL_MESSAGES = [
    "{name} has just hit level {level}! Thick lightskin from la... and she got lil bellay :blush:",
    "{name} has just hit level {level}! Gayming warlord!",
    "{name} has just hit level {level}! Leave some XP for the rest of us, pal :joy:",
    "{name} has just hit level {level}! Shit class anyway, go next character :sob:",
    "{name} has just hit level {level}! What a fucking plonker :rofl:",
    "{name} has just hit level {level}! Get up mush :flex:",
    "{name} has just hit level {level}! up the ra",
    "{name} has just hit level {level}! Goodness gracious what a gaymer",
    "{name} has just hit level {level}! FUCK YOU",
]

UPPER_HALF_LEVEL_MESSAGES = [
    "{name} has just hit level {level}! Time to take a shower?",
    "{name} has just hit level {level}! Do NOT come within smelling distance of this guy :skull:",
    (
        "{name} has just hit level {level}!\n"
        "Our Father who art in heaven\n"
        "hallowed be thy name\n"
        "thy kingdom come\n"
        "thy will be done\n"
        "on earth as it is in heaven\n"
        "Give us this day our daily bread\n"
        "And forgive us our trespasses\n"
        "as we forgive those who trespass against us\n"
        "And lead us not into temptation\n"
        "but deliver us from evil\n"
        "For thine is the kingdom\n"
        "the power and the glory\n"
        "for ever and ever\n"
        "Amen"
    ),
]

LOWER_HALF_LEVEL_MESSAGES = [
    "{name} has just hit level {level}! At the rate this guy's going, he might be 60 by 2030 :rofl:",
    "{name} has just hit level {level}! Sluggish gaming :sob: :snail:",
    "{name} has just hit level {level}! 3FanZ levelling speeds over here :sob: :snail:",
]

LOWER_HALF_LEVEL_MESSAGES = [
    "{name} has just hit level {level}! At the rate this guy's going, he might be 60 by 2030 :rofl:",
    "{name} has just hit level {level}! Sluggish gaming :sob: :snail:",
    "{name} has just hit level {level}! 3FanZ levelling speeds over here :sob: :snail:",
]

SPECIAL_PLAYER_MESSAGES = {
    "burberry scarf": [
        "{name} has just hit level {level}! 68 Doidge Road, B23 7SG, Birmingham, West Midlands :rofl:"
    ],
    "peachykeen peachykeen": [
        "{name} has just hit level {level}! yo wipe my dungeon again I WILL rape you tymon :joy:"
    ],
    "jittle yang": [
        "{name} has just hit level {level}! World class huntard :sob: :ethan:"
    ],
}

# Standard Discord emoji shortcodes plus server-specific custom emoji names.
BUILTIN_EMOJI_ALIASES = {
    "joy": "😂",
    "rofl": "🤣",
    "sob": "😭",
    "blush": "😊",
    "flex": "💪",
    "skull": "💀",
    "snail": "🐌",
}
EMOJI_TOKEN_RE = re.compile(r":([A-Za-z0-9_]+):")


def resolve_emoji_tokens(content: str, guild: discord.Guild | None) -> str:
    custom = {}
    if guild is not None:
        custom = {emoji.name.casefold(): str(emoji) for emoji in guild.emojis}

    def replace(match):
        emoji_name = match.group(1)
        key = emoji_name.casefold()
        if key in BUILTIN_EMOJI_ALIASES:
            return BUILTIN_EMOJI_ALIASES[key]
        if key in custom:
            return custom[key]
        log.warning("Announcement references :%s: but no matching server emoji exists.", emoji_name)
        return match.group(0)

    return EMOJI_TOKEN_RE.sub(replace, content)


def choose_level_up_message(character_full_name: str, display_name: str, level: int, rank: int, total_players: int) -> str:
    eligible = list(GENERAL_LEVEL_MESSAGES)

    # For odd-sized rosters, the upper half gets the extra position. Ranking is
    # by current level descending, then character name alphabetically for ties.
    upper_half_size = (total_players + 1) // 2
    if rank <= upper_half_size:
    eligible.extend(UPPER_HALF_LEVEL_MESSAGES)
else:
    eligible.extend(LOWER_HALF_LEVEL_MESSAGES)

    eligible.extend(SPECIAL_PLAYER_MESSAGES.get(character_full_name.casefold(), []))
    selected = random.choice(eligible)
    return selected.format(name=display_name, level=level)


def queue_level_up_events(results):
    """Compare current levels with stored levels and persist new notifications."""
    successful = [item for item in results if item["data"].get("ok")]
    successful.sort(key=lambda item: (
        -int(item["data"].get("level", 0)),
        f'{item["data"].get("name", "")} {item["data"].get("surname", "")}'.casefold()
    ))
    rank_by_slug = {item["player"]["slug"]: index for index, item in enumerate(successful, start=1)}
    total_players = len(successful)

    with db_connect() as con:
        for item in successful:
            player = item["player"]
            data = item["data"]
            try:
                current_level = int(data["level"])
            except (KeyError, TypeError, ValueError):
                continue

            row = con.execute(
                "SELECT last_level FROM players WHERE slug = ?", (player["slug"],)
            ).fetchone()
            if row is None:
                continue

            previous_level = row["last_level"]
            # Existing players from the previous bot version have no baseline yet.
            # Store their current level without announcing a false level-up.
            if previous_level is None:
                con.execute(
                    "UPDATE players SET last_level = ? WHERE slug = ?",
                    (current_level, player["slug"])
                )
                continue

            if current_level > int(previous_level):
                full_name = f'{data["name"]} {data["surname"]}'
                nickname = player.get("nickname", "")
                display_name = full_name + (f" ({nickname})" if nickname else "")
                content = choose_level_up_message(
                    full_name, display_name, current_level,
                    rank_by_slug[player["slug"]], total_players
                )
                con.execute(
                    """INSERT OR IGNORE INTO level_up_events
                       (slug, level, content, status, created_at)
                       VALUES (?, ?, ?, 'pending', ?)""",
                    (player["slug"], current_level, content,
                     datetime.now(timezone.utc).isoformat())
                )
                con.execute(
                    "UPDATE players SET last_level = ? WHERE slug = ?",
                    (current_level, player["slug"])
                )
            elif current_level < int(previous_level):
                # Correct a stale/higher saved level without announcing a level-up.
                con.execute(
                    "UPDATE players SET last_level = ? WHERE slug = ?",
                    (current_level, player["slug"])
                )


async def deliver_pending_level_up_events():
    """Send queued announcements and mark them sent only after Discord accepts them."""
    channel = bot.get_channel(LEVEL_UP_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(LEVEL_UP_CHANNEL_ID)
    guild = getattr(channel, "guild", None)

    with db_connect() as con:
        pending = con.execute(
            "SELECT event_id, content FROM level_up_events WHERE status = 'pending' ORDER BY event_id"
        ).fetchall()

    for event in pending:
        content = resolve_emoji_tokens(event["content"], guild)
        message = await channel.send(
            content, allowed_mentions=discord.AllowedMentions.none()
        )
        with db_connect() as con:
            con.execute(
                "UPDATE level_up_events SET status = 'sent', sent_message_id = ? WHERE event_id = ?",
                (message.id, event["event_id"])
            )


# ---------- Blizzard Forever API ----------

_access_token = None
_token_expires_at = 0


def get_access_token():
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at:
        return _access_token

    response = requests.post(
        OAUTH_URL,
        auth=(BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    _access_token = data["access_token"]
    _token_expires_at = time.time() + int(data.get("expires_in", 86400)) - 120
    return _access_token


def lookup_character(first_name: str, surname: str):
    first_name = standardise_name(first_name)
    surname = standardise_name(surname)
    slug = character_slug(first_name, surname)
    url = f"{API_BASE}/profile/wow/character/{quote(slug, safe='-')}"

    response = requests.get(
        url,
        params={"namespace": API_NAMESPACE, "locale": "en_GB"},
        headers={"Authorization": f"Bearer {get_access_token()}"},
        timeout=20
    )

    if response.status_code == 404:
        return {
            "ok": False,
            "error": "Not found in the public Armory (or profile not available yet)."
        }

    response.raise_for_status()
    data = response.json()

    race = (data.get("race") or {}).get("name", "Unknown race")
    char_class = (data.get("character_class") or {}).get("name", "Unknown class")

    return {
        "ok": True,
        "name": standardise_name(data.get("name") or first_name),
        "surname": standardise_name(data.get("surname") or surname),
        "level": data.get("level", "?"),
        "race": race,
        "class": char_class
    }


async def lookup_character_async(first_name: str, surname: str):
    return await asyncio.to_thread(lookup_character, first_name, surname)


# ---------- Custom emoji configuration ----------

def load_emoji_config():
    path = Path(EMOJI_CONFIG_PATH)
    if not path.exists():
        log.warning("Emoji config %s not found; roster will show no class/race emojis.", path)
        return {"classes": {}, "races": {}}

    try:
        with path.open("r", encoding="utf-8") as f:
            config = json.load(f)
        return {
            "classes": config.get("classes", {}),
            "races": config.get("races", {})
        }
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read emoji configuration %s", path)
        return {"classes": {}, "races": {}}


def make_emoji_lookup(guild: discord.Guild | None):
    if guild is None:
        return {}
    return {emoji.name.casefold(): str(emoji) for emoji in guild.emojis}


def custom_emoji(game_name: str, category: str, config: dict, available: dict) -> str:
    emoji_name = config.get(category, {}).get(game_name, "")
    if not emoji_name:
        return ""
    found = available.get(str(emoji_name).casefold())
    if not found:
        log.warning(
            "Missing server emoji '%s' configured for %s '%s'.",
            emoji_name, category, game_name
        )
        return ""
    return found


# ---------- Roster embeds ----------

MEDALS = ["🥇", "🥈", "🥉"]


def level_sort_key(item):
    data = item["data"]
    if not data.get("ok"):
        return (1, 0, item["player"]["first_name"].casefold())
    try:
        level = int(data.get("level", 0))
    except (TypeError, ValueError):
        level = 0
    return (0, -level, data.get("name", "").casefold())


def build_embeds(results, guild: discord.Guild | None):
    # Successful profiles rank by level. Failed lookups are placed after them.
    ordered = sorted(results, key=level_sort_key)
    emoji_config = load_emoji_config()
    available_emojis = make_emoji_lookup(guild)

    checked_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    chunks = [ordered[i:i + 25] for i in range(0, len(ordered), 25)] or [[]]
    pages = []

    # Rank only successful character profiles, and do so across all pages.
    rank_by_slug = {}
    successful = [item for item in ordered if item["data"].get("ok")]
    for rank, item in enumerate(successful[:3], start=1):
        rank_by_slug[item["player"]["slug"]] = MEDALS[rank - 1]

    for page_number, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title="WoW Forever — Player List",
            description="EU character roster · Ordered by level · Automatically refreshed",
            colour=discord.Colour.gold()
        )

        if not ordered:
            embed.description = "No characters are being tracked yet. Use `/addplayer` to add one."

        for item in chunk:
            player = item["player"]
            data = item["data"]
            nickname = player["nickname"]

            if data.get("ok"):
                character_name = f'{data["name"]} {data["surname"]}'
                title = character_name + (f" ({nickname})" if nickname else "")
                race_emoji = custom_emoji(data["race"], "races", emoji_config, available_emojis)
                class_emoji = custom_emoji(data["class"], "classes", emoji_config, available_emojis)
                rank_emoji = rank_by_slug.get(player["slug"], "")
                prefix = " ".join(part for part in [rank_emoji, race_emoji, class_emoji] if part)
                if prefix:
                    title = f"{prefix} {title}"
                value = f'**Level {data["level"]}** · {data["race"]} {data["class"]}'
            else:
                title = f'{player["first_name"]} {player["surname"]}'
                if nickname:
                    title += f" ({nickname})"
                value = f'⚠️ {data["error"]}'

            embed.add_field(name=title[:256], value=value[:1024], inline=False)

        embed.set_footer(
            text=f"Last checked: {checked_at} · Updates every {UPDATE_MINUTES} minutes · Page {page_number}/{len(chunks)}"
        )
        pages.append(embed)

    return pages


# ---------- Discord bot ----------

intents = discord.Intents.default()


class WoWTracker(commands.Bot):
    async def setup_hook(self):
        init_db()
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to server %s.", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Global slash commands synced; propagation can take a while.")
        if not scheduled_refresh.is_running():
            scheduled_refresh.start()


bot = WoWTracker(command_prefix="!", intents=intents)


async def get_target_channel():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CHANNEL_ID)
    return channel


async def refresh_roster():
    players = list_players()
    results = []

    for player in players:
        try:
            data = await lookup_character_async(player["first_name"], player["surname"])
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "HTTP"
            data = {"ok": False, "error": f"Blizzard API error ({status})."}
            log.warning("Armory request failed for %s: %s", player["slug"], exc)
        except requests.RequestException as exc:
            data = {"ok": False, "error": "Blizzard API temporarily unavailable."}
            log.warning("Network error for %s: %s", player["slug"], exc)
        except Exception:
            data = {"ok": False, "error": "Unexpected lookup error; see bot logs."}
            log.exception("Unexpected lookup error for %s", player["slug"])

        results.append({"player": dict(player), "data": data})
        await asyncio.sleep(0.15)

    channel = await get_target_channel()
    embeds = build_embeds(results, getattr(channel, "guild", None))
    saved = get_roster_messages()

    for page, embed in enumerate(embeds):
        message = None
        if page in saved:
            try:
                message = await channel.fetch_message(saved[page])
                await message.edit(embed=embed)
            except discord.NotFound:
                message = None
            except discord.Forbidden:
                raise RuntimeError(
                    "The bot cannot edit its roster messages. Check channel permissions."
                )

        if message is None:
            message = await channel.send(embed=embed)
            save_roster_message(page, message.id)

    for page, old_message_id in saved.items():
        if page >= len(embeds):
            try:
                old_message = await channel.fetch_message(old_message_id)
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            delete_roster_message_page(page)

    # Queue and deliver level-up notifications after comparing the full fresh roster.
    queue_level_up_events(results)
    try:
        await deliver_pending_level_up_events()
    except discord.Forbidden:
        log.exception(
            "Cannot post level-up messages. Give the bot View Channel and Send Messages "
            "permissions in channel %s.", LEVEL_UP_CHANNEL_ID
        )
    except discord.NotFound:
        log.exception("Level-up channel %s was not found.", LEVEL_UP_CHANNEL_ID)
    except Exception:
        log.exception("Could not deliver pending level-up announcements; they will retry next poll.")

    log.info("Roster refreshed: %s tracked character(s).", len(players))
    return len(players)


@tasks.loop(minutes=UPDATE_MINUTES)
async def scheduled_refresh():
    try:
        await refresh_roster()
    except Exception:
        log.exception("Scheduled roster refresh failed.")


@scheduled_refresh.before_loop
async def before_scheduled_refresh():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        await refresh_roster()
    except Exception:
        log.exception("Initial roster refresh failed.")


# ---------- Slash commands ----------

@bot.tree.command(name="addplayer", description="Add a WoW Forever EU character to the tracked roster.")
@app_commands.describe(
    first_name="Character first name, as shown in the Armory",
    surname="Character surname, as shown in the Armory",
    nickname="Optional server nickname / identity label"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def addplayer(
    interaction: discord.Interaction,
    first_name: str,
    surname: str,
    nickname: str = ""
):
    await interaction.response.defer(ephemeral=True)
    try:
        first_name = standardise_name(first_name)
        surname = standardise_name(surname)
        data = await lookup_character_async(first_name, surname)
        if not data["ok"]:
            await interaction.followup.send(
                f"Couldn't verify **{first_name} {surname}**: {data['error']}\n"
                "Nothing was added. Check the spelling and try again after the character has logged out.",
                ephemeral=True
            )
            return

        try:
            add_player(first_name, surname, nickname, initial_level=int(data["level"]))
        except sqlite3.IntegrityError:
            await interaction.followup.send(
                "That character is already on the roster. Use `/setnickname` to change its nickname.",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f'Added **{data["name"]} {data["surname"]}**'
            + (f' ({nickname.strip()})' if nickname.strip() else "")
            + f' — level {data["level"]} {data["race"]} {data["class"]}.',
            ephemeral=True
        )
        await refresh_roster()
    except ValueError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
    except requests.RequestException:
        await interaction.followup.send(
            "Blizzard's API could not be reached. Please try again shortly.",
            ephemeral=True
        )


@bot.tree.command(name="setnickname", description="Set or change the server nickname shown beside a character.")
@app_commands.describe(
    first_name="Character first name",
    surname="Character surname",
    nickname="Nickname to display; enter a single hyphen (-) to clear it"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def setnickname(
    interaction: discord.Interaction,
    first_name: str,
    surname: str,
    nickname: str
):
    nickname_value = "" if nickname.strip() == "-" else nickname.strip()
    if not update_nickname(first_name, surname, nickname_value):
        await interaction.response.send_message(
            "That character isn't on the tracked roster. Add them first with `/addplayer`.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Updated nickname for **{standardise_name(first_name)} {standardise_name(surname)}**."
        + (f" Display name: **{nickname_value}**." if nickname_value else " Nickname cleared."),
        ephemeral=True
    )
    await refresh_roster()


@bot.tree.command(name="removeplayer", description="Remove a character from the tracked roster.")
@app_commands.describe(first_name="Character first name", surname="Character surname")
@app_commands.checks.has_permissions(manage_messages=True)
async def removeplayer(interaction: discord.Interaction, first_name: str, surname: str):
    if not delete_player(first_name, surname):
        await interaction.response.send_message(
            "That character isn't currently tracked.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Removed **{standardise_name(first_name)} {standardise_name(surname)}** from the roster.",
        ephemeral=True
    )
    await refresh_roster()


@bot.tree.command(name="players", description="List all characters currently being tracked.")
async def players(interaction: discord.Interaction):
    current = list_players()
    if not current:
        await interaction.response.send_message(
            "No characters are being tracked yet. Use `/addplayer` to add one.",
            ephemeral=True
        )
        return

    lines = []
    for player in current:
        label = f'{player["first_name"]} {player["surname"]}'
        if player["nickname"]:
            label += f' ({player["nickname"]})'
        lines.append(f"• {label}")

    await interaction.response.send_message(
        "**Tracked characters:**\n" + "\n".join(lines),
        ephemeral=True
    )


@bot.tree.command(name="refresh", description="Refresh the public player-list message now.")
@app_commands.checks.has_permissions(manage_messages=True)
async def refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        count = await refresh_roster()
        await interaction.followup.send(
            f"Roster refreshed. Checked {count} character(s).", ephemeral=True
        )
    except Exception:
        log.exception("Manual refresh failed.")
        await interaction.followup.send(
            "Refresh failed. Check the bot logs and channel permissions.",
            ephemeral=True
        )


@bot.tree.command(name="player", description="Look up one character in the WoW Forever Armory.")
@app_commands.describe(first_name="Character first name", surname="Character surname")
async def player(interaction: discord.Interaction, first_name: str, surname: str):
    await interaction.response.defer(ephemeral=True)
    data = await lookup_character_async(first_name, surname)

    if not data["ok"]:
        await interaction.followup.send(data["error"], ephemeral=True)
        return

    embed = discord.Embed(
        title=f'{data["name"]} {data["surname"]}',
        description=f'**Level {data["level"]}** · {data["race"]} {data["class"]}',
        colour=discord.Colour.blurple()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Messages** permission to use that command."
    else:
        log.error("Slash command error: %s", error, exc_info=error)
        message = "Something went wrong. Check the bot logs."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


bot.run(DISCORD_TOKEN)
