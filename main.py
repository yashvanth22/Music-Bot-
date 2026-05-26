from dotenv import load_dotenv
load_dotenv()

import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import json
import os
import ctypes
import ctypes.util
import aiohttp
import time
import re
from typing import Optional

TOKEN = os.environ.get("DISCORD_TOKEN", "")
BOT_START_TIME = time.time()

# ── Opus loader ───────────────────────────────────────────────────────────────
def load_opus():
    if discord.opus.is_loaded():
        return
    candidates = [
        ctypes.util.find_library("opus"),
        "libopus.so.0",
        "libopus.so",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "/usr/local/lib/libopus.so.0",
    ]
    for path in candidates:
        if path:
            try:
                discord.opus.load_opus(path)
                print(f"Opus loaded from: {path}")
                return
            except Exception:
                pass
    print("Warning: Could not load opus. Voice may not work.")

load_opus()

# ── Spotify helpers ───────────────────────────────────────────────────────────
SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_ALBUM_RE = re.compile(r"open\.spotify\.com/album/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)")

def parse_spotify_url(url: str) -> tuple[str, str]:
    m = SPOTIFY_TRACK_RE.search(url)
    if m:
        return "track", m.group(1)
    m = SPOTIFY_ALBUM_RE.search(url)
    if m:
        return "album", m.group(1)
    m = SPOTIFY_PLAYLIST_RE.search(url)
    if m:
        return "playlist", m.group(1)
    return "", ""

async def spotify_track_query(track_url: str) -> Optional[str]:
    try:
        oembed = f"https://open.spotify.com/oembed?url={track_url}"
        async with aiohttp.ClientSession() as session:
            async with session.get(oembed, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title", "").strip()
                    if title:
                        return title
    except Exception as e:
        print(f"Spotify oEmbed error: {e}")
    return None

async def get_spotify_queries(url: str) -> tuple[str, list[str]]:
    kind, sid = parse_spotify_url(url)
    if not kind:
        return "", []

    if kind == "track":
        query = await spotify_track_query(url)
        return kind, [query] if query else []

    embed_url = f"https://open.spotify.com/embed/{kind}/{sid}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(embed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return kind, []
                html = await resp.text()

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
        if not match:
            return kind, []

        data = json.loads(match.group(1))
        track_list = (
            data.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity", {})
                .get("trackList", [])
        )

        queries = []
        for track in track_list[:50]:
            title = track.get("title", "").strip()
            artist = track.get("subtitle", "").strip()
            if title:
                queries.append(f"{artist} - {title}" if artist else title)

        return kind, queries

    except Exception as e:
        print(f"Spotify scrape error: {e}")
        return kind, []

# ── Per-guild music state ─────────────────────────────────────────────────────
class GuildMusic:
    def __init__(self):
        self.queue: list[dict] = []
        self.current: Optional[dict] = None
        self.loop: bool = False

guild_music: dict[int, GuildMusic] = {}

def get_state(guild_id: int) -> GuildMusic:
    if guild_id not in guild_music:
        guild_music[guild_id] = GuildMusic()
    return guild_music[guild_id]

# ── Cookies setup ─────────────────────────────────────────────────────────────
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

cookies_env = os.environ.get("YOUTUBE_COOKIES", "")
if cookies_env:
    with open(COOKIES_FILE, "w") as f:
        f.write(cookies_env)
    print("Cookies written from environment variable.")
elif os.path.isfile(COOKIES_FILE):
    print(f"Using cookies from file: {COOKIES_FILE}")
else:
    print("No cookies found — continuing without (may work fine on Render).")

# ── YT-DLP helpers ────────────────────────────────────────────────────────────
YDL_OPTS: dict = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android_vr"]}},
}

if os.path.isfile(COOKIES_FILE):
    YDL_OPTS["cookiefile"] = COOKIES_FILE

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

async def fetch_info(query: str) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "url": info["url"],
                "title": info.get("title", "Unknown"),
                "webpage_url": info.get("webpage_url", query),
                "duration": info.get("duration", 0),
            }
    try:
        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        print(f"yt-dlp error: {e}")
        return None

def play_next(guild_id: int, vc: discord.VoiceClient):
    state = get_state(guild_id)
    if not state.queue:
        state.current = None
        return
    state.current = state.queue.pop(0)
    source = discord.FFmpegPCMAudio(state.current["url"], **FFMPEG_OPTS)
    def after(err):
        if err:
            print(f"Player error: {err}")
        play_next(guild_id, vc)
    vc.play(source, after=after)

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    try:
        cmds = await bot.tree.sync()
        print(f"Synced {len(cmds)} commands globally.")
    except Exception as e:
        print(f"Global sync failed: {e}")

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="/help | Music Bot"
        )
    )

    app_id = bot.user.id
    invite = f"https://discord.com/api/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot+applications.commands"
    print(f"\nLogged in as {bot.user}")
    print(f"Invite link: {invite}\n")

# ════════════════════════════════════════════════════════════════════════════
#  MUSIC COMMANDS
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="join", description="Join your voice channel")
async def cmd_join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
    channel = interaction.user.voice.channel
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()
    await interaction.response.send_message(f"Joined **{channel.name}**")

@tree.command(name="leave", description="Leave the voice channel")
async def cmd_leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("Not in a voice channel.", ephemeral=True)
    await vc.disconnect()
    get_state(interaction.guild_id).queue.clear()
    await interaction.response.send_message("Disconnected.")

@tree.command(name="play", description="Play a song — YouTube, Spotify track/album/playlist, or search query")
@app_commands.describe(query="Song name, YouTube URL, or Spotify link")
async def cmd_play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)

    await interaction.response.defer()

    vc = interaction.guild.voice_client
    if not vc:
        vc = await interaction.user.voice.channel.connect()
    elif interaction.user.voice.channel != vc.channel:
        await vc.move_to(interaction.user.voice.channel)

    state = get_state(interaction.guild_id)

    # ── Spotify URL ──────────────────────────────────────────────────────────
    kind, _ = parse_spotify_url(query)
    if kind:
        kind, spotify_queries = await get_spotify_queries(query)
        if not spotify_queries:
            return await interaction.followup.send(
                "Could not resolve that Spotify link. Try pasting the song name instead."
            )

        label = "track" if kind == "track" else ("album" if kind == "album" else "playlist")

        if len(spotify_queries) > 1:
            await interaction.followup.send(
                f"Found **{len(spotify_queries)}** tracks from Spotify {label}. Queuing..."
            )

        first_played = False
        queued = 0
        for sq in spotify_queries:
            info = await fetch_info(sq)
            if not info:
                continue
            state.queue.append(info)
            queued += 1
            if not first_played and not vc.is_playing() and not vc.is_paused():
                play_next(interaction.guild_id, vc)
                first_played = True

        if len(spotify_queries) > 1:
            if queued == 0:
                msg = "❌ Could not queue any tracks. Try again or check bot logs."
            else:
                msg = f"Queued **{queued}** of {len(spotify_queries)} track(s) from Spotify {label}."
                if state.current:
                    msg += f"\nNow playing: **{state.current['title']}**"
            await interaction.channel.send(msg)
        else:
            if queued == 0:
                await interaction.followup.send("❌ Could not find that track. Try searching by name instead.")
            elif first_played and state.current:
                await interaction.followup.send(f"Now playing: **{state.current['title']}**")
            elif state.queue:
                await interaction.followup.send(
                    f"Added to queue: **{state.queue[-1]['title']}** (position {len(state.queue)})"
                )
        return

    # ── YouTube / search ─────────────────────────────────────────────────────
    info = await fetch_info(query)
    if not info:
        return await interaction.followup.send("❌ Could not find that song. Please try again.")

    state.queue.append(info)

    if not vc.is_playing() and not vc.is_paused():
        play_next(interaction.guild_id, vc)
        await interaction.followup.send(f"Now playing: **{info['title']}**")
    else:
        await interaction.followup.send(f"Added to queue: **{info['title']}** (position {len(state.queue)})")

@tree.command(name="stop", description="Stop playback and clear the queue")
async def cmd_stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    state = get_state(interaction.guild_id)
    state.queue.clear()
    state.current = None
    vc.stop()
    await interaction.response.send_message("Stopped and queue cleared.")

@tree.command(name="pause", description="Pause the current song")
async def cmd_pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)

@tree.command(name="resume", description="Resume the paused song")
async def cmd_resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)

@tree.command(name="skip", description="Skip the current song")
async def cmd_skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message("Nothing to skip.", ephemeral=True)
    vc.stop()
    await interaction.response.send_message("Skipped.")

@tree.command(name="queue", description="Show the current queue")
async def cmd_queue(interaction: discord.Interaction):
    state = get_state(interaction.guild_id)
    if not state.current and not state.queue:
        return await interaction.response.send_message("Queue is empty.")

    embed = discord.Embed(title="Music Queue", color=discord.Color.blurple())
    if state.current:
        embed.add_field(name="Now Playing", value=f"**{state.current['title']}**", inline=False)
    if state.queue:
        lines = [f"`{i+1}.` {s['title']}" for i, s in enumerate(state.queue[:15])]
        if len(state.queue) > 15:
            lines.append(f"... and {len(state.queue)-15} more")
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)

# ════════════════════════════════════════════════════════════════════════════
#  HELP COMMAND
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="help", description="Show all available commands")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎵 Bot Commands",
        color=discord.Color.blurple(),
        description="All commands use `/` (slash commands — works anywhere in Discord)",
    )
    music = (
        "`/join` — Join your voice channel\n"
        "`/leave` — Leave the voice channel\n"
        "`/play <query>` — Play a song, YouTube URL, or Spotify track/album/playlist link\n"
        "`/stop` — Stop and clear queue\n"
        "`/pause` — Pause playback\n"
        "`/resume` — Resume playback\n"
        "`/skip` — Skip the current song\n"
        "`/queue` — Show the current queue"
    )
    info = (
        "`/log` — Show all servers the bot is in\n"
        "`/logbot` — Show bot status, ping, uptime and servers"
    )
    embed.add_field(name="🎵 Music", value=music, inline=False)
    embed.add_field(name="ℹ️ Info", value=info, inline=False)
    await interaction.response.send_message(embed=embed)

# ════════════════════════════════════════════════════════════════════════════
#  LOG COMMAND
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="log", description="Show all servers the bot is in")
async def cmd_log(interaction: discord.Interaction):
    try:
        guilds = bot.guilds
        if not guilds:
            return await interaction.response.send_message("Bot is not in any servers.", ephemeral=True)

        lines = []
        for i, g in enumerate(guilds, 1):
            count = g.member_count if g.member_count is not None else "?"
            lines.append(f"`{i}.` **{g.name}** — `{count}` members | ID: `{g.id}`")

        embeds = []
        chunk_size = 10
        for page, i in enumerate(range(0, len(lines), chunk_size)):
            chunk = lines[i:i + chunk_size]
            embed = discord.Embed(
                title=f"🌐 Servers the bot is in ({len(guilds)} total)" if page == 0 else "🌐 Servers (continued)",
                description="\n".join(chunk),
                color=discord.Color.green(),
            )
            embeds.append(embed)

        await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)
    except Exception as e:
        print(f"/log error: {e}")
        try:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        except Exception:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

# ════════════════════════════════════════════════════════════════════════════
#  LOGBOT COMMAND
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="logbot", description="Show bot status and all servers it's in")
async def cmd_logbot(interaction: discord.Interaction):
    try:
        guilds = bot.guilds
        latency_ms = round(bot.latency * 1000)

        uptime_secs = int(time.time() - BOT_START_TIME)
        hours, rem = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        total_members = sum(g.member_count or 0 for g in guilds)

        if latency_ms < 100:
            status_icon = "🟢 Online"
        elif latency_ms < 300:
            status_icon = "🟡 Slow"
        else:
            status_icon = "🔴 High Latency"

        embed = discord.Embed(
            title=f"🤖 {bot.user.name} — Bot Status",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=bot.user.display_avatar.url)
        embed.add_field(name="Status",  value=status_icon,          inline=True)
        embed.add_field(name="Ping",    value=f"`{latency_ms}ms`",  inline=True)
        embed.add_field(name="Uptime",  value=f"`{uptime_str}`",    inline=True)
        embed.add_field(name="Servers", value=f"`{len(guilds)}`",   inline=True)
        embed.add_field(name="Members", value=f"`{total_members}`", inline=True)
        embed.add_field(name="Bot ID",  value=f"`{bot.user.id}`",   inline=True)

        if guilds:
            lines = []
            for i, g in enumerate(guilds, 1):
                count = g.member_count if g.member_count is not None else "?"
                lines.append(f"`{i}.` **{g.name}** — `{count}` members")

            chunk_size = 10
            for page_i, i in enumerate(range(0, len(lines), chunk_size)):
                chunk = lines[i:i + chunk_size]
                label = "📋 Servers" if page_i == 0 else "📋 Servers (cont.)"
                embed.add_field(name=label, value="\n".join(chunk), inline=False)

        embed.set_footer(text=f"discord.py {discord.__version__}")
        await interaction.response.send_message(embed=embed)

    except Exception as e:
        print(f"/logbot error: {e}")
        try:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        except Exception:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

# ════════════════════════════════════════════════════════════════════════════
#  Run
# ════════════════════════════════════════════════════════════════════════════

if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is not set.")
    print("Set DISCORD_TOKEN=your_token_here before running.")
else:
    retry_delay = 5
    while True:
        try:
            print("Starting bot...")
            bot.run(TOKEN, reconnect=True, log_handler=None)
        except discord.errors.LoginFailure:
            print("Invalid token — check your DISCORD_TOKEN.")
            break
        except KeyboardInterrupt:
            print("Stopped.")
            break
        except Exception as e:
            print(f"Bot crashed: {e} — restarting in {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue
        retry_delay = 5
