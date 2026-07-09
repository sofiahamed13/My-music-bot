import asyncio
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

# ============================================
# Config
# ============================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/music_cache"))

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN is missing.")
    print("Set DISCORD_TOKEN in Railway Variables.")
    sys.exit(1)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Keep logs low, but still show actual errors
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger("musicbot")

# Silence noisy libraries
logging.getLogger("discord").setLevel(logging.ERROR)
logging.getLogger("yt_dlp").setLevel(logging.ERROR)

# ============================================
# FFmpeg check
# ============================================
def check_ffmpeg():
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-version"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("FFmpeg: OK")
            return True
    except FileNotFoundError:
        pass

    print("FFmpeg: Not found")
    return False


if not check_ffmpeg():
    sys.exit(1)

# ============================================
# Discord bot setup
# ============================================
intents = discord.Intents.default()
intents.message_content = False
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================
# Runtime state
# ============================================
music_data = {}
guild_locks = {}


def get_guild_lock(guild_id: int) -> asyncio.Lock:
    lock = guild_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        guild_locks[guild_id] = lock
    return lock


def cleanup_cache(max_age_seconds: int = 1800):
    now = time.time()
    for file in DOWNLOAD_DIR.iterdir():
        if file.is_file():
            with suppress(Exception):
                if now - file.stat().st_mtime > max_age_seconds:
                    file.unlink()


def safe_remove(path: str | None):
    if not path:
        return
    with suppress(Exception):
        p = Path(path)
        if p.exists():
            p.unlink()


# ============================================
# YouTube search and local download
# ============================================
def search_and_download(query: str):
    cleanup_cache()

    base_name = f"song_{uuid.uuid4().hex}"
    base_path = DOWNLOAD_DIR / base_name

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "source_address": "0.0.0.0",
        "socket_timeout": 30,
        "retries": 5,
        "outtmpl": str(base_path) + ".%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_result = ydl.extract_info(
                f"ytsearch5:{query} official audio",
                download=False
            )

            if not search_result or "entries" not in search_result:
                return None

            entries = [e for e in search_result["entries"] if e]
            if not entries:
                return None

            chosen = None
            preferred_terms = (
                "official audio",
                "official video",
                "official music video",
                "audio",
                "lyric video",
                "full song",
            )

            for entry in entries:
                title_lower = (entry.get("title") or "").lower()
                if any(term in title_lower for term in preferred_terms):
                    chosen = entry
                    break

            if chosen is None:
                chosen = entries[0]

            video_url = chosen.get("webpage_url") or chosen.get("url")
            if not video_url:
                return None

            info = ydl.extract_info(video_url, download=True)
            if not info:
                return None

            downloaded_file = None
            expected_mp3 = base_path.with_suffix(".mp3")
            if expected_mp3.exists():
                downloaded_file = expected_mp3
            else:
                candidates = sorted(
                    DOWNLOAD_DIR.glob(base_name + ".*"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                for candidate in candidates:
                    if candidate.is_file():
                        downloaded_file = candidate
                        break

            if downloaded_file is None or not downloaded_file.exists():
                return None

            if downloaded_file.stat().st_size < 10000:
                safe_remove(str(downloaded_file))
                return None

            title = info.get("title", "Unknown")
            uploader = info.get("uploader", "Unknown Artist")

            song_name = title
            artist_name = uploader

            if " - " in title:
                parts = title.split(" - ", 1)
                song_name = parts[0].strip()
                artist_name = parts[1].strip()
            elif " – " in title:
                parts = title.split(" – ", 1)
                song_name = parts[0].strip()
                artist_name = parts[1].strip()

            clean_tags = [
                "(Official Audio)", "(Official Video)",
                "(Official Music Video)", "[Official Audio]",
                "[Official Video]", "(Lyric Video)",
                "(Audio)", "(HD)", "(Full Song)",
                "[Full Song]", "(Lyrics)", "[Lyrics]",
                "(Official)", "[Official]",
                "(Full Audio)", "[Full Audio]"
            ]

            for tag in clean_tags:
                song_name = song_name.replace(tag, "").strip()
                artist_name = artist_name.replace(tag, "").strip()

            thumbnails = info.get("thumbnails", [])
            thumbnail = thumbnails[-1]["url"] if thumbnails else None
            duration = info.get("duration") or 0

            return {
                "name": song_name or title,
                "artist": artist_name or uploader,
                "album": "YouTube Music",
                "duration_sec": duration,
                "thumbnail": thumbnail,
                "file_path": str(downloaded_file),
                "title": title,
            }

    except Exception as e:
        logger.warning("Download failed: %s", e)
        return None


# ============================================
# Embed helpers
# ============================================
def create_embed(song: dict, guild_id: int, status: str = "playing"):
    data = music_data.get(guild_id, {})
    duration = max(int(song.get("duration_sec", 0)), 1)

    if status == "playing" and "start_time" in data:
        total_paused = data.get("total_paused", 0.0)
        elapsed = min(time.time() - data["start_time"] - total_paused, duration)
    elif status == "paused" and data.get("pause_time"):
        total_paused = data.get("total_paused", 0.0)
        elapsed = min(data["pause_time"] - data["start_time"] - total_paused, duration)
    else:
        elapsed = 0

    elapsed = max(0, int(elapsed))
    remaining = max(0, duration - elapsed)
    progress_ratio = min(elapsed / duration, 1.0)
    filled = int(20 * progress_ratio)
    bar = "█" * filled + "─" * (20 - filled)

    colors = {
        "playing": discord.Color.green(),
        "paused": discord.Color.yellow(),
        "stopped": discord.Color.red(),
        "finished": discord.Color.blue(),
    }

    statuses = {
        "playing": "Now Playing",
        "paused": "Paused",
        "stopped": "Stopped",
        "finished": "Finished",
    }

    embed = discord.Embed(
        title="Music Player",
        color=colors.get(status, discord.Color.blue())
    )
    embed.add_field(name="Song", value=f"**{song['name']}**", inline=False)
    embed.add_field(name="Artist", value=song["artist"], inline=True)
    embed.add_field(name="Album", value=song.get("album", "Unknown"), inline=True)
    embed.add_field(name="Status", value=statuses.get(status, "Unknown"), inline=True)
    embed.add_field(
        name="Progress",
        value=(
            f"`{timedelta(seconds=elapsed)}` "
            f"{bar} "
            f"`{timedelta(seconds=duration)}`"
        ),
        inline=False
    )
    embed.add_field(
        name="Remaining",
        value=f"**{timedelta(seconds=remaining)}**",
        inline=True
    )

    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])

    embed.set_footer(text="Music Bot | Use /song to play")
    return embed


async def safe_edit_message(message: discord.Message, **kwargs):
    with suppress(Exception):
        await message.edit(**kwargs)


# ============================================
# Session helpers
# ============================================
async def stop_current_session(guild_id: int, disconnect: bool):
    data = music_data.pop(guild_id, None)
    if not data:
        return None

    vc = data.get("vc")
    file_path = data.get("song_info", {}).get("file_path")

    if vc:
        with suppress(Exception):
            if vc.is_playing() or vc.is_paused():
                vc.stop()

        if disconnect:
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()

    safe_remove(file_path)
    return data


async def handle_track_end(guild_id: int, session_id: str, error: Exception | None):
    async with get_guild_lock(guild_id):
        data = music_data.get(guild_id)
        if not data:
            return

        if data.get("session_id") != session_id:
            return

        song = data.get("song_info")
        vc = data.get("vc")
        msg = data.get("message")
        file_path = song.get("file_path") if song else None

        music_data.pop(guild_id, None)

        if vc:
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()

        safe_remove(file_path)

        if error:
            embed = discord.Embed(
                title="Playback Error",
                description="The track stopped because of an internal playback error.",
                color=discord.Color.red()
            )
        else:
            embed = discord.Embed(
                title="Song Finished",
                color=discord.Color.blue()
            )
            if song:
                embed.add_field(name="Song", value=f"**{song['name']}**", inline=True)
                embed.add_field(name="Artist", value=song["artist"], inline=True)
                if song.get("thumbnail"):
                    embed.set_thumbnail(url=song["thumbnail"])
            embed.set_footer(text="Use /song to play another track")

        if msg:
            await safe_edit_message(msg, embed=embed, view=discord.ui.View())


# ============================================
# Audio source
# ============================================
def build_source(file_path: str):
    return discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(
            file_path,
            executable=FFMPEG_PATH,
            before_options="-nostdin",
            options="-vn -sn -dn -ar 48000 -ac 2"
        ),
        volume=0.7
    )


# ============================================
# Controls view
# ============================================
class Controls(discord.ui.View):
    def __init__(self, song: dict, guild_id: int):
        super().__init__(timeout=None)
        self.song = song
        self.guild_id = guild_id

    @discord.ui.button(
        label="Pause",
        emoji="⏸️",
        style=discord.ButtonStyle.primary,
        custom_id="music_pause"
    )
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_guild_lock(self.guild_id):
            data = music_data.get(self.guild_id)
            if not data or not data.get("vc"):
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
                return

            vc = data["vc"]
            if not vc.is_playing():
                await interaction.response.send_message("Playback is already paused.", ephemeral=True)
                return

            vc.pause()
            data["is_paused"] = True
            data["pause_time"] = time.time()

            await interaction.response.edit_message(
                embed=create_embed(self.song, self.guild_id, "paused"),
                view=self
            )

    @discord.ui.button(
        label="Resume",
        emoji="▶️",
        style=discord.ButtonStyle.success,
        custom_id="music_resume"
    )
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_guild_lock(self.guild_id):
            data = music_data.get(self.guild_id)
            if not data or not data.get("vc"):
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
                return

            vc = data["vc"]
            if not vc.is_paused():
                await interaction.response.send_message("Playback is already running.", ephemeral=True)
                return

            if data.get("pause_time"):
                paused_duration = time.time() - data["pause_time"]
                data["total_paused"] = data.get("total_paused", 0.0) + paused_duration
                data["pause_time"] = None

            vc.resume()
            data["is_paused"] = False

            await interaction.response.edit_message(
                embed=create_embed(self.song, self.guild_id, "playing"),
                view=self
            )

    @discord.ui.button(
        label="Stop",
        emoji="⏹️",
        style=discord.ButtonStyle.danger,
        custom_id="music_stop"
    )
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_guild_lock(self.guild_id):
            data = music_data.get(self.guild_id)
            if not data:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
                return

            await stop_current_session(self.guild_id, disconnect=True)

            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(
                embed=create_embed(self.song, self.guild_id, "stopped"),
                view=self
            )

    @discord.ui.button(
        label="New Song",
        emoji="⏭️",
        style=discord.ButtonStyle.secondary,
        custom_id="music_new_song"
    )
    async def new_song_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NewSongModal(self.guild_id))


# ============================================
# Modal
# ============================================
class NewSongModal(discord.ui.Modal, title="Play New Song"):
    song_name = discord.ui.TextInput(
        label="Song Name",
        placeholder="Enter a song name...",
        required=True,
        max_length=200
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        data = music_data.get(self.guild_id)
        voice_channel = None

        if data and data.get("vc") and data["vc"].channel:
            voice_channel = data["vc"].channel
        elif interaction.user.voice and interaction.user.voice.channel:
            voice_channel = interaction.user.voice.channel

        if voice_channel is None:
            await interaction.followup.send("Bot is not connected to a voice channel.", ephemeral=True)
            return

        await play_song(interaction, self.song_name.value, voice_channel)


# ============================================
# Main play workflow
# ============================================
async def play_song(interaction: discord.Interaction, query: str, voice_channel: discord.VoiceChannel):
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    guild_id = guild.id

    async with get_guild_lock(guild_id):
        status_message = await interaction.followup.send(
            embed=discord.Embed(
                title="Searching and Downloading",
                description=f"```{query}```\nPlease wait...",
                color=discord.Color.blue()
            )
        )

        await stop_current_session(guild_id, disconnect=False)

        try:
            current_vc = guild.voice_client
            if current_vc and current_vc.is_connected():
                if current_vc.channel != voice_channel:
                    await current_vc.move_to(voice_channel)
                vc = current_vc
            else:
                vc = await voice_channel.connect(timeout=30.0, self_deaf=True)
        except Exception as e:
            await safe_edit_message(
                status_message,
                embed=discord.Embed(
                    title="Voice Connection Error",
                    description=f"`{e}`",
                    color=discord.Color.red()
                )
            )
            return

        loop = asyncio.get_running_loop()
        song = await loop.run_in_executor(None, search_and_download, query)

        if not song:
            await safe_edit_message(
                status_message,
                embed=discord.Embed(
                    title="Song Not Found",
                    description="Could not find or download that song. Try another query.",
                    color=discord.Color.red()
                )
            )
            return

        try:
            source = build_source(song["file_path"])
        except Exception as e:
            safe_remove(song.get("file_path"))
            await safe_edit_message(
                status_message,
                embed=discord.Embed(
                    title="Audio Source Error",
                    description=f"`{e}`",
                    color=discord.Color.red()
                )
            )
            return

        session_id = uuid.uuid4().hex

        music_data[guild_id] = {
            "session_id": session_id,
            "vc": vc,
            "song_info": song,
            "is_paused": False,
            "start_time": time.time(),
            "pause_time": None,
            "total_paused": 0.0,
            "message": status_message,
        }

        def after_playback(error):
            asyncio.run_coroutine_threadsafe(
                handle_track_end(guild_id, session_id, error),
                bot.loop
            )

        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                await asyncio.sleep(0.2)

            vc.play(source, after=after_playback)
        except Exception as e:
            current = music_data.get(guild_id)
            if current and current.get("session_id") == session_id:
                music_data.pop(guild_id, None)
            safe_remove(song.get("file_path"))

            await safe_edit_message(
                status_message,
                embed=discord.Embed(
                    title="Playback Error",
                    description=f"`{e}`",
                    color=discord.Color.red()
                )
            )
            return

        await asyncio.sleep(1.0)

        current = music_data.get(guild_id)
        if not current or current.get("session_id") != session_id:
            return

        if not vc.is_playing() and not vc.is_paused():
            music_data.pop(guild_id, None)
            safe_remove(song.get("file_path"))

            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()

            await safe_edit_message(
                status_message,
                embed=discord.Embed(
                    title="Playback Failed",
                    description="The file was downloaded, but playback could not start.",
                    color=discord.Color.red()
                )
            )
            return

        view = Controls(song, guild_id)
        await safe_edit_message(
            status_message,
            embed=create_embed(song, guild_id, "playing"),
            view=view
        )

        bot.loop.create_task(embed_updater(guild_id, session_id, song, view, status_message))


# ============================================
# Embed updater
# ============================================
async def embed_updater(
    guild_id: int,
    session_id: str,
    song: dict,
    view: discord.ui.View,
    message: discord.Message
):
    await asyncio.sleep(10)

    while True:
        data = music_data.get(guild_id)
        if not data:
            break

        if data.get("session_id") != session_id:
            break

        vc = data.get("vc")
        if vc is None:
            break

        if not vc.is_playing() and not vc.is_paused():
            break

        status = "paused" if data.get("is_paused") else "playing"

        try:
            await message.edit(
                embed=create_embed(song, guild_id, status),
                view=view
            )
        except Exception:
            break

        await asyncio.sleep(20)


# ============================================
# Events
# ============================================
@bot.event
async def on_ready():
    print(f"Bot Online: {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        logger.warning("Command sync failed: %s", e)

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="/song | Music Bot"
        )
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    logger.error("App command error: %s: %s", type(original).__name__, original)

    message = "An internal error occurred while processing the command."
    if isinstance(original, discord.Forbidden):
        message = "Missing permission to perform that action."
    elif isinstance(original, asyncio.TimeoutError):
        message = "The operation timed out. Please try again."

    with suppress(Exception):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


# ============================================
# Commands
# ============================================
@bot.tree.command(name="song", description="Play a song in your voice channel")
@app_commands.describe(name="Enter the song name")
async def song_command(interaction: discord.Interaction, name: str):
    try:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True
            )
            return

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Join a Voice Channel First",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        await play_song(interaction, name, interaction.user.voice.channel)

    except Exception as e:
        logger.error("song command failed: %s: %s", type(e).__name__, e)
        raise


@bot.tree.command(name="stop", description="Stop playback and disconnect the bot")
async def stop_command(interaction: discord.Interaction):
    try:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id

        async with get_guild_lock(guild_id):
            data = music_data.get(guild_id)
            if not data:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
                return

            await stop_current_session(guild_id, disconnect=True)

            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Stopped",
                    description="Playback stopped and bot disconnected.",
                    color=discord.Color.red()
                )
            )

    except Exception as e:
        logger.error("stop command failed: %s: %s", type(e).__name__, e)
        raise


# ============================================
# Start bot
# ============================================
print("Starting bot...")
bot.run(DISCORD_TOKEN, log_level=logging.ERROR)
