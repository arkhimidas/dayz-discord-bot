import os
import json
import asyncio
import signal
import time
import datetime as dt
import re
import random
import logging
import sys

import discord
from discord import app_commands

from dotenv import load_dotenv
import aiohttp

# shared aiohttp session (created in on_ready)
AIOHTTP_SESSION: aiohttp.ClientSession | None = None
# Protect file writes to avoid races when multiple coroutines write JSON files
FILE_WRITE_LOCK = asyncio.Lock()

# Simple in-memory cache for BattleMetrics responses to avoid excessive API calls
STATUS_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 10.0  # seconds


async def aiohttp_request(url: str, *, return_type: str = "json", headers: dict | None = None, timeout: int = 10, retries: int = 3, base_backoff: float = 0.5):
    """
    Helper wrapper for aiohttp requests with simple retry + exponential backoff and jitter.
    return_type: 'json' or 'text'
    """
    global AIOHTTP_SESSION
    if AIOHTTP_SESSION is None:
        # fallback to creating a temporary session (shouldn't normally happen)
        sess = aiohttp.ClientSession()
        close_after = True
    else:
        sess = AIOHTTP_SESSION
        close_after = False

    try:
        for attempt in range(retries):
            try:
                # use ClientTimeout for clearer timeout semantics
                timeout_obj = aiohttp.ClientTimeout(total=timeout)
                async with sess.get(url, headers=headers, timeout=timeout_obj) as resp:
                    status = resp.status
                    if status == 429:
                        # respect Retry-After header if present
                        ra = resp.headers.get("Retry-After")
                        if ra:
                            try:
                                wait = float(ra)
                            except Exception:
                                wait = base_backoff * (2 ** attempt)
                            await asyncio.sleep(wait)
                            continue
                        else:
                            # simple backoff then retry
                            pass

                    if status >= 400:
                        text = await resp.text()
                        raise RuntimeError(f"HTTP {status}: {text}")

                    if return_type == "json":
                        return await resp.json()
                    else:
                        return await resp.text()

            except asyncio.CancelledError:
                # allow cancellation to propagate
                raise
            except Exception as e:
                if attempt == retries - 1:
                    raise
                delay = base_backoff * (2 ** attempt) + random.uniform(0, 0.5)
                logging.warning("Request to %s failed (attempt %d/%d): %s — retrying in %.2fs", url, attempt + 1, retries, e, delay)
                await asyncio.sleep(delay)
    finally:
        if close_after:
            try:
                await sess.close()
            except Exception:
                logging.exception("Error closing temporary aiohttp session")
from bs4 import BeautifulSoup

SERVERS_FILE = "servers.json"


def load_servers():
    if not os.path.exists(SERVERS_FILE):
        return {"servers": []}
    with open(SERVERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


async def save_servers(data) -> None:
    tmp = f"{SERVERS_FILE}.tmp"
    try:
        async with FILE_WRITE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SERVERS_FILE)
    except Exception:
        logging.exception("Failed to save servers to %s", SERVERS_FILE)


def extract_bm_id(url: str) -> str | None:
    """
    Poimii BattleMetrics server ID:n URL:sta.
    """
    if not url:
        return None
    url = url.strip()
    # Try full URL pattern first
    m = re.search(r"/servers/dayz/(\d+)", url)
    if m:
        return m.group(1)
    # If user supplied just the numeric id, accept it
    if re.fullmatch(r"\d+", url):
        return url
    return None


async def remove_server_by_id(server_id: str) -> bool:
    """
    Poistaa serverin servers.json:sta.
    Palauttaa True jos poistettiin.
    """
    db = load_servers()
    servers = db.get("servers", [])

    new_servers = [s for s in servers if str(s.get("id")) != str(server_id)]

    if len(new_servers) == len(servers):
        return False  # ei löytynyt

    db["servers"] = new_servers
    await save_servers(db)
    return True


async def fetch_bm_server_name(server_id: str) -> str | None:
    """
    Hakee BattleMetrics API:sta serverin nimen asynkronisesti.
    Palauttaa nimen tai None jos epäonnistuu.
    """
    api_url = f"https://api.battlemetrics.com/servers/{server_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Discord bot; status checker)"}

    try:
        payload = await aiohttp_request(api_url, return_type="json", headers=headers, timeout=10)
        attrs = (payload.get("data") or {}).get("attributes") or {}
        name = attrs.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    except Exception:
        logging.exception("fetch_bm_server_name failed for %s", server_id)
        return None


load_dotenv()

# Configure logging early
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.getenv("DISCORD_TOKEN")
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID", "0"))
GUILD_ID = os.getenv("GUILD_ID")
if GUILD_ID:
    try:
        GUILD_ID = int(GUILD_ID)
    except Exception:
        logging.exception("Invalid GUILD_ID value: %s", GUILD_ID)
        GUILD_ID = None

if not TOKEN:
    logging.error("DISCORD_TOKEN is not set. Exiting.")
    sys.exit(1)

if STATUS_CHANNEL_ID == 0:
    logging.warning("STATUS_CHANNEL_ID is not set or is 0. Status updates may fail.")

# BattleMetrics server id from URL: https://www.battlemetrics.com/servers/dayz/<ID>
BATTLEMETRICS_SERVER_ID = os.getenv("BATTLEMETRICS_SERVER_ID")
BATTLEMETRICS_URL = os.getenv("BATTLEMETRICS_URL")  # optional, used for scraping "Time"

STATE_FILE = "status_state.json"  # saves status message id so it persists across restarts


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


async def save_state(state: dict) -> None:
    tmp = f"{STATE_FILE}.tmp"
    try:
        async with FILE_WRITE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
    except Exception:
        logging.exception("Failed to save state to %s", STATE_FILE)


async def get_in_game_time_from_battlemetrics_page(url: str) -> str | None:
    headers = {"User-Agent": "Mozilla/5.0 (Discord bot; status checker)"}
    try:
        text = await aiohttp_request(url, return_type="text", headers=headers, timeout=10)
        soup = BeautifulSoup(text, "html.parser")
        txt = soup.get_text("\n")
        m = re.search(r"Time\s*\n\s*([0-9]{1,2}:[0-9]{2})", txt)
        if m:
            return m.group(1)
    except Exception:
        logging.exception("Failed to scrape in-game time from %s", url)
    return None


async def get_status_battlemetrics(server_id: str) -> dict:
    api_url = f"https://api.battlemetrics.com/servers/{server_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Discord bot; status checker)"}
    # Check cache
    now = time.time()
    cached = STATUS_CACHE.get(server_id)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    try:
        payload = await aiohttp_request(api_url, return_type="json", headers=headers, timeout=10)
    except Exception:
        logging.exception("Failed to fetch status for %s", server_id)
        result = {"online": False, "error": "Failed to fetch from BattleMetrics", "server_id": server_id}
        STATUS_CACHE[server_id] = (time.time(), result)
        return result
    attrs = (payload.get("data") or {}).get("attributes") or {}

    name = attrs.get("name") or f"Server {server_id}"
    status = str(attrs.get("status") or "").lower()
    players = attrs.get("players")
    max_players = attrs.get("maxPlayers")

    ip = attrs.get("ip")
    port = attrs.get("port")
    game_port = f"{ip}:{port}" if ip and port else None

    server_time = None
    try:
        bm_url = f"https://www.battlemetrics.com/servers/dayz/{server_id}"
        server_time = await get_in_game_time_from_battlemetrics_page(bm_url)
    except Exception:
        server_time = None

    online = (status == "online")

    result = {
        "online": online,
        "name": name,
        "players": players,
        "max_players": max_players,
        "game_port": game_port,
        "server_time": server_time,
        "source": "BattleMetrics",
        "server_id": server_id,
    }
    STATUS_CACHE[server_id] = (time.time(), result)
    return result


def build_embed(data: dict) -> discord.Embed:
    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = "DayZ Server Status"

    if data.get("online"):
        embed = discord.Embed(
            title=title,
            description=f"✅ **ONLINE**\nPäivitetty: {updated}",
        )

        embed.add_field(name="Nimi", value=data.get("name", "—"), inline=False)

        embed.add_field(
            name="Pelaajat",
            value=f"{data.get('players','?')}/{data.get('max_players','?')}",
            inline=True,
        )

        # In-game time from BattleMetrics (not real-world clock)
        embed.add_field(
            name="Time (in-game)",
            value=data.get("server_time") or "—",
            inline=True,
        )

        embed.add_field(
            name="Game Port",
            value=f"`{data.get('game_port','—')}`",
            inline=False,
        )

        embed.add_field(name="Lähde", value="BattleMetrics", inline=True)

    else:
        embed = discord.Embed(
            title=title,
            description=f"❌ **OFFLINE / EI VASTAA**\nPäivitetty: {updated}",
        )

        if "error" in data and data["error"]:
            embed.add_field(name="Virhe", value=f"`{data['error']}`", inline=False)

        embed.add_field(name="Lähde", value="BattleMetrics", inline=True)

    return embed


async def fetch_status(server_id: str) -> dict:
    try:
        return await get_status_battlemetrics(server_id)
    except Exception as e:
        logging.exception("fetch_status failed for %s", server_id)
        return {"online": False, "error": str(e), "server_id": server_id}
    

class ServerSelect(discord.ui.Select):
    def __init__(self, selected_id: str | None = None):
        db = load_servers()
        servers = db.get("servers", [])

        options = []
        for s in servers[:25]:
            sid = str(s.get("id"))
            label = s.get("name") or f"DayZ {sid}"
            options.append(discord.SelectOption(label=label[:100], value=sid, default=(sid == selected_id)))

        if not options:
            options = [discord.SelectOption(label="Ei servereitä lisätty", value="none")]

        super().__init__(
            placeholder="Valitse serveri…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="server_select_menu"
        )

    async def callback(self, interaction: discord.Interaction):
        server_id = self.values[0]
        if server_id == "none":
            await interaction.response.send_message("Lisää serveri ensin komennolla /addserver", ephemeral=True)
            return

        data = await fetch_status(server_id)
        embed = build_embed(data)
        await interaction.response.edit_message(embed=embed, view=self.view)


class AddServerModal(discord.ui.Modal):
    def __init__(self, select: ServerSelect | None = None):
        self.select = select
        super().__init__(title="Lisää serveri")
        self.url = discord.ui.TextInput(label="BattleMetrics linkki tai ID", placeholder="https://www.battlemetrics.com/servers/dayz/12345 tai 12345", required=True)
        self.add_item(self.url)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Tarvitset admin-oikeudet.", ephemeral=True)
            return

        raw = self.url.value.strip()
        server_id = extract_bm_id(raw)
        if not server_id:
            await interaction.response.send_message("❌ Virheellinen BattleMetrics linkki tai ID.", ephemeral=True)
            return

        db = load_servers()
        if any(s.get("id") == server_id for s in db.get("servers", [])):
            await interaction.response.send_message("⚠️ Serveri on jo listassa.", ephemeral=True)
            return

        server_name = await fetch_bm_server_name(server_id) or f"DayZ {server_id}"
        db.setdefault("servers", []).append({"id": server_id, "name": server_name})
        await save_servers(db)

        try:
            channel = await interaction.client.fetch_channel(STATUS_CHANNEL_ID)
            if isinstance(channel, discord.abc.Messageable):
                await upsert_status_message(channel)
        except Exception:
            logging.exception("Failed to update status message after adding server via modal")

        await interaction.response.send_message(f"✅ Server lisätty: **{server_name}** (`{server_id}`)", ephemeral=True)


class AddServerButton(discord.ui.Button):
    def __init__(self, select: ServerSelect | None = None):
        super().__init__(label="Lisää serveri", style=discord.ButtonStyle.secondary, custom_id="add_server_button")
        self.select = select

    async def callback(self, interaction: discord.Interaction):
        modal = AddServerModal(select=self.select)
        await interaction.response.send_modal(modal)


class RemoveServerButton(discord.ui.Button):
    def __init__(self, select: ServerSelect | None = None):
        super().__init__(label="Poista serveri", style=discord.ButtonStyle.danger, custom_id="remove_server_button")
        self.select = select

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Tarvitset admin-oikeudet poistaaksesi serverin.", ephemeral=True)
            return

        server_id = None
        try:
            if getattr(self.select, 'values', None):
                server_id = self.select.values[0]
        except Exception:
            server_id = None

        if not server_id or server_id == 'none':
            db = load_servers()
            servers = db.get('servers', [])
            server_id = str(servers[0]['id']) if servers else None

        if not server_id:
            await interaction.response.send_message('Ei servereitä listassa.', ephemeral=True)
            return

        # remember current index to pick the next server
        db_before = load_servers()
        servers_before = db_before.get('servers', [])
        idx = None
        for i, s in enumerate(servers_before):
            if str(s.get('id')) == str(server_id):
                idx = i
                break

        removed = await remove_server_by_id(server_id)
        if not removed:
            await interaction.response.send_message('⚠️ Serveriä ei löytynyt listasta.', ephemeral=True)
            return

        # determine next selected id
        db_after = load_servers()
        servers_after = db_after.get('servers', [])
        selected_next = None
        if servers_after:
            if idx is None:
                selected_next = str(servers_after[0]['id'])
            else:
                if idx < len(servers_after):
                    selected_next = str(servers_after[idx]['id'])
                else:
                    selected_next = str(servers_after[-1]['id'])

        # Päivitä statusviesti ja aseta valinta seuraavaksi
        try:
            channel = await interaction.client.fetch_channel(STATUS_CHANNEL_ID)
            if hasattr(channel, 'send'):
                await upsert_status_message(channel, selected_id=selected_next)
        except Exception:
            logging.exception('Failed to update status message after removing server via button')

        await interaction.response.send_message(f'🗑️ Server poistettu (`{server_id}`)', ephemeral=True)


class ServerSelectView(discord.ui.View):
    def __init__(self, selected_id: str | None = None):
        super().__init__(timeout=None)
        # Keep a reference to the select so other components (like the refresh button)
        # can know which server is currently selected.
        self.select = ServerSelect(selected_id=selected_id)
        self.add_item(self.select)
        self.add_item(AddServerButton(self.select))
        self.add_item(RefreshButton(self.select))
        self.add_item(RemoveServerButton(self.select))


class RefreshButton(discord.ui.Button):
    def __init__(self, select: ServerSelect):
        super().__init__(label="Päivitä", style=discord.ButtonStyle.primary, custom_id="refresh_button")
        self.select = select

    async def callback(self, interaction: discord.Interaction):
        # Determine server id: prefer current selection, otherwise first server in list
        server_id = None
        try:
            if getattr(self.select, 'values', None):
                server_id = self.select.values[0]
        except Exception:
            server_id = None

        if not server_id or server_id == 'none':
            db = load_servers()
            servers = db.get('servers', [])
            server_id = str(servers[0]['id']) if servers else None

        if not server_id:
            await interaction.response.send_message('Ei servereitä lisätty', ephemeral=True)
            return

        # Notify user that refresh is in progress
        try:
            await interaction.response.send_message('Päivitetään…', ephemeral=True)
        except Exception:
            logging.exception('Failed to send ephemeral updating message')

        data = await fetch_status(server_id)
        embed = build_embed(data)
        try:
            # Edit the original message that contains the embed and the view
            await interaction.message.edit(embed=embed, view=self.view)
        except Exception:
            logging.exception('Failed to edit message on refresh')


intents = discord.Intents.default()
client = discord.Client(intents=intents)
state = load_state()
tree = app_commands.CommandTree(client)


async def upsert_status_message(channel, selected_id: str | None = None) -> None:
    msg_id = state.get("status_message_id")

    db = load_servers()
    servers = db.get("servers", [])
    default_id = str(servers[0]["id"]) if servers else "none"

    data = await fetch_status(default_id) if default_id != "none" else {"online": False, "error": "Ei servereitä lisätty"}
    embed = build_embed(data)

    view = ServerSelectView(selected_id=selected_id)

    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logging.error("Missing permissions to fetch/edit status message in channel %s", getattr(channel, "id", None))
            return

    msg = await channel.send(embed=embed, view=view)
    state["status_message_id"] = msg.id
    await save_state(state)


@client.event
async def on_ready():
    logging.info("Logged in as %s", client.user)

    try:
        # create shared aiohttp session
        global AIOHTTP_SESSION
        if AIOHTTP_SESSION is None:
            AIOHTTP_SESSION = aiohttp.ClientSession()

        # Register signal handlers to ensure aiohttp session is closed on shutdown
        try:
            loop = asyncio.get_running_loop()

            def _signal_shutdown():
                async def _do():
                    global AIOHTTP_SESSION
                    try:
                        if AIOHTTP_SESSION and not getattr(AIOHTTP_SESSION, "closed", False):
                            await AIOHTTP_SESSION.close()
                    except Exception:
                        logging.exception("Error closing aiohttp session")
                    try:
                        await client.close()
                    except Exception:
                        logging.exception("Error closing Discord client")

                asyncio.create_task(_do())

            loop.add_signal_handler(signal.SIGINT, _signal_shutdown)
            loop.add_signal_handler(signal.SIGTERM, _signal_shutdown)
        except Exception:
            # add_signal_handler may not be available on all platforms
            pass

        if GUILD_ID:
            await tree.sync(guild=discord.Object(id=GUILD_ID))
            logging.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            await tree.sync()
            logging.info("Global slash commands synced")
    except Exception:
        logging.exception("Failed to sync slash commands")

    # Diagnostic: list commands currently registered in the CommandTree
    try:
        cmds = list(tree.walk_commands())
        logging.info("Registered commands (%d): %s", len(cmds), ", ".join(c.name for c in cmds))
    except Exception:
        logging.exception("Failed to list commands after sync")

    try:
        channel = await client.fetch_channel(STATUS_CHANNEL_ID)
    except Exception:
        logging.exception("fetch_channel failed for %s", STATUS_CHANNEL_ID)
        return

    if not isinstance(channel, discord.abc.Messageable):
        logging.error("This channel is not a message channel: %s", type(channel))
        return

    try:
        await upsert_status_message(channel)
    except Exception:
        logging.exception("Failed initial upsert_status_message")

    async def loop():
        while True:
            try:
                await asyncio.sleep(60)
                await upsert_status_message(channel)
            except Exception:
                logging.exception("Error during periodic status update")
                await asyncio.sleep(60)

    asyncio.create_task(loop())


@tree.command(name="addserver", description="Lisää DayZ server BattleMetrics linkillä")
@app_commands.describe(url="BattleMetrics server link")
async def addserver(interaction: discord.Interaction, url: str):

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ Tarvitset admin-oikeudet.",
            ephemeral=True
        )
        return

    server_id = extract_bm_id(url)
    if not server_id:
        await interaction.response.send_message(
            "❌ Virheellinen BattleMetrics linkki.",
            ephemeral=True
        )
        return

    db = load_servers()

    if any(s.get("id") == server_id for s in db.get("servers", [])):
        await interaction.response.send_message(
            "⚠️ Serveri on jo lisätty.",
            ephemeral=True
        )
        return

    # fetch_bm_server_name can block; run in thread to avoid blocking event loop
    server_name = await fetch_bm_server_name(server_id) or f"DayZ {server_id}"

    db.setdefault("servers", []).append({
        "id": server_id,
        "name": server_name
    })
    await save_servers(db)

    # 🔄 päivitä statusviesti (optional mutta hyvä)
    try:
        channel = await interaction.client.fetch_channel(STATUS_CHANNEL_ID)
        if isinstance(channel, discord.abc.Messageable):
            await upsert_status_message(channel)
    except Exception:
        logging.exception("Failed to update status message after adding server")

    # ✅ vastaus käyttäjälle
    await interaction.response.send_message(
        f"✅ Server lisätty: **{server_name}** (`{server_id}`)"
    )


@tree.command(name="removeserver", description="Poista DayZ server BattleMetrics linkillä")
@app_commands.describe(url="BattleMetrics server link")
async def removeserver(interaction: discord.Interaction, url: str):

    # 🔒 admin check
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ Tarvitset admin-oikeudet.",
            ephemeral=True
        )
        return

    server_id = extract_bm_id(url)
    if not server_id:
        await interaction.response.send_message(
            "❌ Virheellinen BattleMetrics linkki.",
            ephemeral=True
        )
        return

    removed = await remove_server_by_id(server_id)

    if not removed:
        await interaction.response.send_message(
            "⚠️ Serveriä ei löytynyt listasta.",
            ephemeral=True
        )
        return

    # 🔄 päivitä status/dropdown heti
    try:
        channel = await interaction.client.fetch_channel(STATUS_CHANNEL_ID)
        if isinstance(channel, discord.abc.Messageable):
            await upsert_status_message(channel)
    except Exception:
        logging.exception("Failed to update status message after removing server")

    await interaction.response.send_message(
        f"🗑️ Server poistettu (`{server_id}`)"
    )


client.run(TOKEN)
