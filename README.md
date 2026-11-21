# WoW Forever EU Discord Player Tracker

A Discord bot for tracking WoW Forever EU characters using the public character profile API.
It maintains a roster message and polls tracked character profiles every 1 minute by default.

## Features

- `/addplayer` — add a character and optional server nickname (identity label)
- `/setnickname` — change or clear a character's nickname without removing them
- `/removeplayer` — remove a character
- `/players` — list tracked characters
- `/player` — look up a character
- `/refresh` — force an immediate roster refresh
- Automatically orders characters from highest to lowest level
- Gold, silver and bronze medals for the top three tracked characters
- Server custom race and class emojis beside each character
- First and surname are standardised with an initial capital, even when entered in lowercase
- PvP ruleset text is not displayed
- SQLite persistence and multiple roster pages when more than 25 players are tracked

Commands that change the roster or force a refresh require the human user to have Discord's **Manage Messages** permission. The bot itself does not need the Manage Messages permission.

## 1. Discord application

1. Create an application at https://discord.com/developers/applications.
2. Create its bot and copy the bot token.
3. Under OAuth2 > URL Generator, select `bot` and `applications.commands`.
4. Under Bot Permissions select **View Channels**, **Send Messages**, **Embed Links**, and **Read Message History**. Also ensure the bot can view and send messages in the `#genny` announcement channel.
5. Use the generated URL to invite the bot to your server.
6. Enable Developer Mode in Discord > User Settings > Advanced.
7. Right-click `#player-list` and copy its channel ID. Right-click your server name/icon and copy the server ID.

No privileged Gateway Intents are required. The bot uses slash commands and does not read ordinary chat messages.

## 2. Blizzard API credentials

1. Create an API client at https://develop.battle.net/access/clients.
2. Copy its Client ID and Client Secret.
3. Do not share or commit the Client Secret.

The bot uses the OAuth client credentials flow. Players do not need to provide BattleTags or log in.

## 3. Configure custom server emojis

The bot reads `emoji_config.json`. Its default mappings expect your server emojis to have these exact names:

**Class emojis**

`warrior`, `paladin`, `hunter`, `rogue`, `priest`, `shaman`, `mage`, `warlock`, `druid`

**Race emojis**

`human`, `dwarf`, `night_elf`, `gnome`, `orc`, `undead`, `tauren`, `troll`

Upload your custom emojis to your Discord server and name them to match the values above. Or, change the values in `emoji_config.json` to match the names you chose. The left-hand labels (for example `Night Elf`) must remain the race/class names returned by the Armory; the right-hand values are your Discord emoji names.

Example: if your Night Elf emoji is named `nelf`, change:

```json
"Night Elf": "night_elf"
```

to:

```json
"Night Elf": "nelf"
```

The bot automatically resolves emoji names from the server it is in. If an emoji is missing or misspelled, the roster still works; it simply omits that emoji and logs a warning. The emojis must be in the same Discord server as the bot.

## 4. Run locally (optional)

Install Python 3.11 or newer. Copy `.env.example` to `.env` and fill in:

- `DISCORD_TOKEN`
- `BLIZZARD_CLIENT_ID`
- `BLIZZARD_CLIENT_SECRET`
- `DISCORD_CHANNEL_ID`
- `DISCORD_GUILD_ID` (recommended, for fast server-specific slash-command sync)

Then:

```bash
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\activate
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Install and run:

```bash
pip install -r requirements.txt
python bot.py
```

## 5. Deploy to Railway

1. Create a private GitHub repository.
2. Upload all project files except `.env`, `*.sqlite3`, and `.venv`.
3. In Railway, create a project and deploy from the GitHub repository.
4. Railway uses the included Dockerfile.
5. Add these variables to the Railway service:
   - `DISCORD_TOKEN`
   - `BLIZZARD_CLIENT_ID`
   - `BLIZZARD_CLIENT_SECRET`
   - `DISCORD_CHANNEL_ID`
   - `DISCORD_GUILD_ID`
   - `UPDATE_MINUTES` = `1` (fastest polling interval; see the refresh note below)
   - `LEVEL_UP_CHANNEL_ID` = `1552220682329395240`
   - `DATABASE_PATH` = `/app/data/tracker.sqlite3`
   - `EMOJI_CONFIG_PATH` = `emoji_config.json`
6. Add a Railway Volume and mount it at **`/app/data`**.
7. Deploy/redeploy and check the logs for `Logged in as ...` and `Slash commands synced to server ...`.

The `/app/data` volume keeps the SQLite database separate from the bot's program files. If you change the database path, make sure it remains inside the mounted directory. Check Railway's current pricing and volume availability before deployment; cloud hosting is not guaranteed to be free.

## 6. Discord commands

Add a character (nickname is optional):

```text
/addplayer first_name: Rubin surname: Unneouette nickname: Seb
```

Change an existing nickname:

```text
/setnickname first_name: Rubin surname: Unneouette nickname: Seb
```

Clear a nickname by entering a single hyphen (`-`) as its value.

Other commands:

```text
/players
/player first_name: Rubin surname: Unneouette
/refresh
/removeplayer first_name: Rubin surname: Unneouette
```

`/addplayer` checks the character profile before adding it. If the Armory profile cannot be found, check spelling and try again after the character has logged out and the public profile has updated.

## 7. Roster behaviour

- The list is sorted by current level, highest first. Players whose Armory lookup fails are placed after successful profiles.
- The three highest-level successful character profiles receive 🥇, 🥈 and 🥉 in that order. Ties are resolved by character name, so the three positions remain deterministic.
- Each character line shows medal (where applicable), race emoji, class emoji, character name and optional server nickname.
- Ruleset/PvP text is omitted.
- First and surname are stored/displayed with an initial capital even if entered in lowercase.
- Nicknames are labels you assign; the Armory does not provide them.
- Checks every 30 minutes by default. The Armory itself may update later than the bot's check.

## Troubleshooting

- **Bot is offline:** verify `DISCORD_TOKEN` and check Railway logs.
- **Commands do not appear:** verify the bot was invited with both `bot` and `applications.commands`; check `DISCORD_GUILD_ID`.
- **Roster cannot be posted/edited:** the bot needs View Channels, Send Messages, Embed Links and Read Message History in the roster channel.
- **Level-up announcements do not appear:** check `LEVEL_UP_CHANNEL_ID`, and give the bot View Channel and Send Messages in that channel. Check Railway logs for missing custom emoji names or Discord permission errors.
- **Class/race emoji missing:** check the exact server emoji name against `emoji_config.json`; ensure the bot is in the same server as the emojis.
- **401 from Blizzard:** verify Client ID and Client Secret.
- **404 for a character:** verify first/surname spelling and EU region; profiles may be unavailable or not refreshed yet.
- **Database lost after redeploy:** ensure a persistent Railway volume is mounted at `/app/data` and `DATABASE_PATH=/app/data/tracker.sqlite3`.

## Security

Never share `.env`, Discord bot tokens, Blizzard Client Secrets or your SQLite database publicly. If a token or secret is exposed, regenerate it.
