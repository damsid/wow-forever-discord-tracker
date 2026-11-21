import asyncio
import json
import logging
import os
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
UPDATE_MINUTES = max(5, int(os.getenv("UPDATE_MINUTES", "30")))
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

if not all([DISCORD_TOKEN, BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET, CHANNEL_ID]):
    raise RuntimeError(
        "Set DISCORD_TOKEN, BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET "
        "and DISCORD_CHANNEL_ID in your environment."
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


def list_players():
    with db_connect() as con:
        return con.execute(
            "SELECT * FROM players ORDER BY lower(first_name), lower(surname)"
        ).fetchall()


def add_player(first_name: str, surname: str, nickname: str):
    first_name = standardise_name(first_name)
    surname = standardise_name(surname)
    slug = character_slug(first_name, surname)
    with db_connect() as con:
        con.execute(
            """INSERT INTO players(slug, first_name, surname, nickname, added_at)
               VALUES (?, ?, ?, ?, ?)""",
            (slug, first_name, surname, nickname.strip(),
             datetime.now(timezone.utc).isoformat())
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

    embeds = build_embeds(results, getattr((await get_target_channel()), "guild", None))
    channel = await get_target_channel()
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
            add_player(first_name, surname, nickname)
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
