import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
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
        "Set DISCORD_TOKEN, BLIZZARD_CLIENT_ID, "
        "BLIZZARD_CLIENT_SECRET and DISCORD_CHANNEL_ID in your .env file."
    )


# ---------- SQLite storage ----------

def db_connect():
    con = sqlite3.connect(DATABASE_PATH)
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
            "SELECT * FROM players ORDER BY lower(nickname), lower(first_name)"
        ).fetchall()


def add_player(first_name, surname, nickname):
    slug = f"{first_name.strip()}-{surname.strip()}".casefold()
    with db_connect() as con:
        con.execute(
            """INSERT INTO players(slug, first_name, surname, nickname, added_at)
               VALUES (?, ?, ?, ?, ?)""",
            (slug, first_name.strip(), surname.strip(), nickname.strip(),
             datetime.now(timezone.utc).isoformat())
        )


def delete_player(slug):
    with db_connect() as con:
        cur = con.execute("DELETE FROM players WHERE slug = ?", (slug,))
        return cur.rowcount > 0


def get_roster_messages():
    with db_connect() as con:
        rows = con.execute(
            "SELECT page, message_id FROM roster_messages ORDER BY page"
        ).fetchall()
        return {row["page"]: row["message_id"] for row in rows}


def save_roster_message(page, message_id):
    with db_connect() as con:
        con.execute(
            """INSERT INTO roster_messages(page, message_id) VALUES (?, ?)
               ON CONFLICT(page) DO UPDATE SET message_id=excluded.message_id""",
            (page, message_id)
        )


def delete_roster_message_page(page):
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


def lookup_character(first_name, surname):
    # WoW Forever characters have no realm. Their EU slug is first-surname.
    slug = f"{first_name.strip()}-{surname.strip()}".casefold()
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
        "name": f'{data.get("name", first_name)} {data.get("surname", surname)}',
        "level": data.get("level", "?"),
        "race": race,
        "class": char_class,
        "ruleset": (data.get("ruleset") or {}).get("name", "")
    }


async def lookup_character_async(first_name, surname):
    return await asyncio.to_thread(lookup_character, first_name, surname)


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
            log.info("Global slash commands synced; global propagation can take a while.")
        if not scheduled_refresh.is_running():
            scheduled_refresh.start()


bot = WoWTracker(command_prefix="!", intents=intents)


def build_embeds(results):
    # Discord allows at most 25 fields per embed. Split larger rosters into pages.
    pages = []
    chunks = [results[i:i + 25] for i in range(0, len(results), 25)] or [[]]

    checked_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    for page_number, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title="WoW Forever — Player List",
            description="EU character roster · Automatically refreshed",
            colour=discord.Colour.blurple()
        )

        for item in chunk:
            player = item["player"]
            nickname = player["nickname"]
            display_name = (
                f'{player["first_name"]} {player["surname"]}'
                + (f' ({nickname})' if nickname else "")
            )

            if item["data"]["ok"]:
                char = item["data"]
                value = f'**Level {char["level"]}** · {char["race"]} {char["class"]}'
                if char.get("ruleset"):
                    value += f'\n{text_ruleset(char["ruleset"])}'
            else:
                value = f'⚠️ {item["data"]["error"]}'

            embed.add_field(name=display_name[:256], value=value[:1024], inline=False)

        embed.set_footer(
            text=f"Last checked: {checked_at} · Updates every {UPDATE_MINUTES} minutes · Page {page_number}/{len(chunks)}"
        )
        pages.append(embed)

    return pages


def text_ruleset(value):
    return f"Ruleset: {value}"


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

    embeds = build_embeds(results)
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

    # Remove old pages if the roster has become shorter.
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
    first_name="Character's first name, exactly as shown in the Armory",
    surname="Character's surname, exactly as shown in the Armory",
    nickname="Your private/friendly nickname for this player"
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
        # Check that the character can currently be found before adding.
        data = await lookup_character_async(first_name, surname)
        if not data["ok"]:
            await interaction.followup.send(
                f"Couldn't verify **{first_name} {surname}**: {data['error']}\n"
                "The character may be below level 10, not logged out yet, "
                "or temporarily missing from the public Armory. Nothing was added.",
                ephemeral=True
            )
            return

        try:
            add_player(first_name, surname, nickname)
        except sqlite3.IntegrityError:
            await interaction.followup.send(
                "That character is already on the roster.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f'Added **{data["name"]}**'
            + (f' ({nickname})' if nickname else "")
            + f' — level {data["level"]} {data["race"]} {data["class"]}.',
            ephemeral=True
        )
        await refresh_roster()

    except requests.RequestException:
        await interaction.followup.send(
            "Blizzard's API could not be reached. Please try again shortly.",
            ephemeral=True
        )


@bot.tree.command(name="removeplayer", description="Remove a character from the tracked roster.")
@app_commands.describe(first_name="Character first name", surname="Character surname")
@app_commands.checks.has_permissions(manage_messages=True)
async def removeplayer(
    interaction: discord.Interaction,
    first_name: str,
    surname: str
):
    slug = f"{first_name.strip()}-{surname.strip()}".casefold()
    if not delete_player(slug):
        await interaction.response.send_message(
            "That character isn't currently tracked.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Removed **{first_name} {surname}** from the roster.", ephemeral=True
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


@bot.tree.command(name="player", description="Look up one tracked character.")
@app_commands.describe(first_name="Character first name", surname="Character surname")
async def player(
    interaction: discord.Interaction,
    first_name: str,
    surname: str
):
    await interaction.response.defer(ephemeral=True)
    data = await lookup_character_async(first_name, surname)

    if not data["ok"]:
        await interaction.followup.send(data["error"], ephemeral=True)
        return

    embed = discord.Embed(
        title=data["name"],
        description=f'**Level {data["level"]}** · {data["race"]} {data["class"]}',
        colour=discord.Colour.blurple()
    )
    if data.get("ruleset"):
        embed.add_field(name="Ruleset", value=data["ruleset"])
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Messages** permission to use that command."
    else:
        log.exception("Slash command error", exc_info=error)
        message = "Something went wrong. Check the bot logs."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


bot.run(DISCORD_TOKEN)
