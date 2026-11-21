
# WoW Forever EU Discord Player Tracker

A small Discord bot that tracks WoW Forever EU characters through Blizzard's public
profile API. It maintains a roster message in one Discord channel and refreshes it
on a schedule.

## Features

- `/addplayer` — add a character and an optional nickname
- `/removeplayer` — remove a character
- `/players` — list tracked characters
- `/player` — look up a character
- `/refresh` — force an immediate roster refresh
- Automatic refresh every 30 minutes by default
- SQLite persistence for players and Discord roster message IDs
- Multiple roster messages if more than 25 characters are tracked

Commands that change the roster or force a refresh require the Discord
**Manage Messages** permission.

## 1. Create the Discord application

1. Open https://discord.com/developers/applications
2. Create a New Application and name it `WoW Forever Player Tracker`.
3. Open **Bot**, create the bot, and copy its token.
4. Under **OAuth2 > URL Generator**, select the `bot` and `applications.commands`
   scopes.
5. Under Bot Permissions, select **View Channels**, **Send Messages**,
   **Embed Links**, **Read Message History**, and **Manage Messages**.
6. Use the generated URL to invite the bot to your server.
7. In Discord, enable Developer Mode under **User Settings > Advanced**.
8. Right-click the destination `#player-list` channel and copy its channel ID.
9. Right-click your server icon/name and copy the server ID. Set this as `DISCORD_GUILD_ID` in your environment for near-instant slash-command registration (recommended).

The bot does not need Message Content Intent. It uses slash commands.

## 2. Create Blizzard API credentials

1. Open https://develop.battle.net/access/clients
2. Create an API client.
3. Copy the Client ID and Client Secret.
4. Keep the Client Secret private. Never commit it to GitHub.

The bot uses OAuth client-credentials authentication. It does not ask players for
Battle.net passwords, BattleTags, or account access.

## 3. Test locally (optional)

Install Python 3.11 or newer.

Copy `.env.example` to `.env` and fill in the four required values:

- `DISCORD_TOKEN`
- `BLIZZARD_CLIENT_ID`
- `BLIZZARD_CLIENT_SECRET`
- `DISCORD_CHANNEL_ID`
- `DISCORD_GUILD_ID` (recommended; your server ID, so commands appear on that server quickly)

Then run:

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

Install and start:

```bash
pip install -r requirements.txt
python bot.py
```

Leave the process running. Stop it with Ctrl+C.

## 4. Deploy continuously on Railway

This project includes a Dockerfile and `railway.json` for Railway deployment.

1. Create a private GitHub repository, for example `wow-forever-discord-tracker`.
2. Upload the project files (do not upload `.env`).
3. Sign up at https://railway.com/ and create a new project.
4. Choose **Deploy from GitHub Repo** and select the repository.
5. Railway should detect the Dockerfile and build the worker.
6. Open the service's **Variables** tab and add:
   - `DISCORD_TOKEN`
   - `BLIZZARD_CLIENT_ID`
   - `BLIZZARD_CLIENT_SECRET`
   - `DISCORD_CHANNEL_ID`
   - `DISCORD_GUILD_ID` (your server ID; recommended for quick slash-command registration)
- `DISCORD_GUILD_ID` (recommended; your server ID, so commands appear on that server quickly)
   - `UPDATE_MINUTES` = `30`
   - `DATABASE_PATH` = `tracker.sqlite3`
7. Deploy/redeploy the service.
8. Open Railway's deployment logs. Look for `Logged in as ...` and
   `Slash commands synced.`

Use a persistent Railway volume mounted at `/app` if your Railway plan supports
volumes, so the SQLite database survives container replacement/redeployment.
If you do not attach persistent storage, the database may be lost when the
container is replaced. Back up `tracker.sqlite3` regularly.

Check Railway's current pricing and persistent-volume availability before
deploying; cloud hosting is not guaranteed to be free.

## 5. Use it in Discord

After the bot is online, slash commands should appear. If you set `DISCORD_GUILD_ID`, they are synced to that server; otherwise global command propagation can take longer:

```text
/addplayer first_name: Rubin surname: Unneouette nickname: Seb
/addplayer first_name: Peachykeen surname: Peachykeen nickname: Tibbz
/addplayer first_name: Darug surname: Darug nickname: JOSH
/addplayer first_name: Limog surname: Little nickname: damix
```

The command checks the character before adding it. If Blizzard cannot find the
profile, it will not add the character. Try again after the character has logged
out and the public Armory has updated.

Then use:

```text
/players
/refresh
/player first_name: Rubin surname: Unneouette
/removeplayer first_name: Rubin surname: Unneouette
```

## How the Armory data works

The bot calls:

`https://eu.api.blizzard.com/profile/wow/character/{first}-{surname}?namespace=profile-forever-eu&locale=en_GB`

WoW Forever characters do not use realm slugs in this endpoint. The profile
namespace and route are based on community testing because Blizzard's Forever
API documentation is not yet complete.

Public profiles generally appear from level 10 and after the character logs out.
A 404 can also occur for an existing character due to temporary or profile-specific
API availability issues. The bot reports this per character and continues with
the rest of the roster.

The API response supplies character level, race, and class. The nickname is
your own label stored in the bot's SQLite database.

## Troubleshooting

- **Bot is offline:** check Railway logs and verify `DISCORD_TOKEN`.
- **Missing slash commands:** wait briefly, restart the bot, and check that the
  invite included `applications.commands`.
- **Cannot send/edit roster:** check View Channel, Send Messages, Embed Links,
  Read Message History, and Manage Messages permissions.
- **401 from Blizzard:** verify the Client ID and Client Secret.
- **403 from Blizzard:** verify that the API client is active and the namespace
  is exactly `profile-forever-eu`.
- **404 for a character:** verify the spelling and EU region; make sure the
  character has reached level 10 and logged out. The public profile may also
  be temporarily unavailable.
- **Database resets after deploy:** attach persistent storage at `/app` and
  set `DATABASE_PATH=/app/tracker.sqlite3`.

## Security

- Do not share your `.env` file or paste tokens into Discord.
- Keep the GitHub repository private if it contains any operational notes.
- If a Discord token or Blizzard secret is exposed, regenerate it immediately.
