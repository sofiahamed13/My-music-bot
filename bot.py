import asyncio
import hashlib
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
    print("ERROR: DISCORD_TOKEN is missing. Set it in environment variables.")
    sys.exit(1)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger("musicbot")
logging.getLogger("discord").setLevel(logging.ERROR)
logging.getLogger("yt_dlp").setLevel(logging.ERROR)


# ============================================
# FFmpeg check
# ============================================
def check_ffmpeg():
    try:
        r = subprocess.run([FFMPEG_PATH, "-version"], capture_output=True, text=True)
        if r.returncode == 0:
            print("FFmpeg: OK")
            return True
    except FileNotFoundError:
        pass
    print("FFmpeg: Not found")
    return False


if not check_ffmpeg():
    sys.exit(1)


# ============================================
# Bot setup
# ============================================
intents = discord.Intents.default()
intents.message_content = False
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

music_data = {}
guild_locks = {}


def get_lock(gid: int) -> asyncio.Lock:
    if gid not in guild_locks:
        guild_locks[gid] = asyncio.Lock()
    return guild_locks[gid]


# ============================================
# Cache management
# ============================================
def cleanup_cache(max_age: int = 1800):
    now = time.time()
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file():
            with suppress(Exception):
                if now - f.stat().st_mtime > max_age:
                    f.unlink()


def safe_remove(path):
    if not path:
        return
    with suppress(Exception):
        p = Path(path)
        if p.exists():
            p.unlink()


# ============================================
# COMPLETE download - wait until file is 100% ready
# ============================================
def search_and_download_complete(query: str):
    """
    1. Search YouTube
    2. Download FULLY to disk as MP3
    3. Verify file is complete and valid
    4. Return song info only after everything is done
    """
    cleanup_cache()

    uid = uuid.uuid4().hex[:12]
    base = DOWNLOAD_DIR / f"s_{uid}"

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "source_address": "0.0.0.0",
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "outtmpl": str(base) + ".%(ext)s",
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
        # Force yt-dlp to wait for postprocessing to finish
        "keepvideo": False,
        "writethumbnail": False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Step 1: Search
            sr = ydl.extract_info(
                f"ytsearch5:{query} official audio",
                download=False
            )
            if not sr or "entries" not in sr:
                return None

            entries = [e for e in sr["entries"] if e]
            if not entries:
                return None

            # Pick best match
            chosen = None
            terms = (
                "official audio", "official video",
                "official music video", "audio",
                "lyric video", "full song",
            )
            for e in entries:
                tl = (e.get("title") or "").lower()
                if any(t in tl for t in terms):
                    chosen = e
                    break
            if not chosen:
                chosen = entries[0]

            url = chosen.get("webpage_url") or chosen.get("url")
            if not url:
                return None

            # Step 2: Download completely
            # This call blocks until download AND postprocessing are done
            info = ydl.extract_info(url, download=True)
            if not info:
                return None

        # Step 3: Find the output file
        mp3 = base.with_suffix(".mp3")
        found = None

        if mp3.exists():
            found = mp3
        else:
            for f in sorted(
                DOWNLOAD_DIR.glob(f"s_{uid}.*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            ):
                if f.is_file() and f.suffix in (".mp3", ".m4a", ".webm", ".opus", ".ogg"):
                    found = f
                    break

        if not found or not found.exists():
            return None

        size = found.stat().st_size
        if size < 10000:
            safe_remove(str(found))
            return None

        # Step 4: Verify file is playable with FFprobe
        verify_ok = verify_audio_file(str(found))
        if not verify_ok:
            safe_remove(str(found))
            return None

        # Step 5: Build song info
        title = info.get("title", "Unknown")
        uploader = info.get("uploader", "Unknown Artist")

        sn = title
        an = uploader
        if " - " in title:
            p = title.split(" - ", 1)
            sn, an = p[0].strip(), p[1].strip()
        elif " \u2013 " in title:
            p = title.split(" \u2013 ", 1)
            sn, an = p[0].strip(), p[1].strip()

        tags = [
            "(Official Audio)", "(Official Video)",
            "(Official Music Video)", "[Official Audio]",
            "[Official Video]", "(Lyric Video)",
            "(Audio)", "(HD)", "(Full Song)",
            "[Full Song]", "(Lyrics)", "[Lyrics]",
            "(Official)", "[Official]",
            "(Full Audio)", "[Full Audio]",
        ]
        for t in tags:
            sn = sn.replace(t, "").strip()
            an = an.replace(t, "").strip()

        thumbs = info.get("thumbnails", [])
        thumb = thumbs[-1]["url"] if thumbs else None
        dur = info.get("duration") or 0

        return {
            "name": sn or title,
            "artist": an or uploader,
            "album": "YouTube Music",
            "duration_sec": dur,
            "thumbnail": thumb,
            "file_path": str(found),
            "file_size": size,
            "title": title,
        }

    except Exception as e:
        logger.warning("Download error: %s", e)
        return None


def verify_audio_file(path: str) -> bool:
    """Use ffprobe to verify the audio file is valid and complete."""
    try:
        r = subprocess.run(
            [
                FFMPEG_PATH.replace("ffmpeg", "ffprobe"),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path
            ],
            capture_output=True,
            text=True,
            timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            dur = float(r.stdout.strip())
            return dur > 1.0
    except Exception:
        pass

    # Fallback: if ffprobe not available, check file size
    try:
        return Path(path).stat().st_size > 50000
    except Exception:
        return False


# ============================================
# Embed
# ============================================
def make_embed(song: dict, gid: int, status: str = "playing"):
    data = music_data.get(gid, {})
    dur = max(int(song.get("duration_sec", 0)), 1)

    if status == "playing" and "start_time" in data:
        tp = data.get("total_paused", 0.0)
        el = min(time.time() - data["start_time"] - tp, dur)
    elif status == "paused" and data.get("pause_time"):
        tp = data.get("total_paused", 0.0)
        el = min(data["pause_time"] - data["start_time"] - tp, dur)
    else:
        el = 0

    el = max(0, int(el))
    rem = max(0, dur - el)
    ratio = min(el / dur, 1.0)
    filled = int(20 * ratio)
    bar = "\u2588" * filled + "\u2500" * (20 - filled)

    cmap = {
        "playing": discord.Color.green(),
        "paused": discord.Color.yellow(),
        "stopped": discord.Color.red(),
    }
    smap = {
        "playing": "Now Playing",
        "paused": "Paused",
        "stopped": "Stopped",
    }

    em = discord.Embed(title="Music Player", color=cmap.get(status, discord.Color.blue()))
    em.add_field(name="Song", value=f"**{song['name']}**", inline=False)
    em.add_field(name="Artist", value=song["artist"], inline=True)
    em.add_field(name="Album", value=song.get("album", "Unknown"), inline=True)
    em.add_field(name="Status", value=smap.get(status, "Unknown"), inline=True)
    em.add_field(
        name="Progress",
        value=f"`{timedelta(seconds=el)}` {bar} `{timedelta(seconds=dur)}`",
        inline=False
    )
    em.add_field(name="Remaining", value=f"**{timedelta(seconds=rem)}**", inline=True)

    if song.get("thumbnail"):
        em.set_thumbnail(url=song["thumbnail"])
    em.set_footer(text="Music Bot | Use /song to play")
    return em


async def safe_edit(msg, **kw):
    with suppress(Exception):
        await msg.edit(**kw)


# ============================================
# Session management
# ============================================
async def end_session(gid: int, disconnect: bool = True):
    data = music_data.pop(gid, None)
    if not data:
        return
    vc = data.get("vc")
    fp = data.get("song_info", {}).get("file_path")
    if vc:
        with suppress(Exception):
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        if disconnect:
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()
    safe_remove(fp)


async def on_track_end(gid: int, sid: str, err):
    async with get_lock(gid):
        data = music_data.get(gid)
        if not data or data.get("sid") != sid:
            return

        song = data.get("song_info")
        vc = data.get("vc")
        msg = data.get("message")
        fp = song.get("file_path") if song else None

        music_data.pop(gid, None)

        if vc:
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()
        safe_remove(fp)

        if msg:
            if err:
                em = discord.Embed(
                    title="Playback Error",
                    description="An error occurred during playback.",
                    color=discord.Color.red()
                )
            else:
                em = discord.Embed(title="Song Finished", color=discord.Color.blue())
                if song:
                    em.add_field(name="Song", value=f"**{song['name']}**", inline=True)
                    em.add_field(name="Artist", value=song["artist"], inline=True)
                    if song.get("thumbnail"):
                        em.set_thumbnail(url=song["thumbnail"])
                em.set_footer(text="Use /song to play another track")
            await safe_edit(msg, embed=em, view=discord.ui.View())


# ============================================
# Audio source from LOCAL file
# ============================================
def make_source(path: str):
    return discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(
            path,
            executable=FFMPEG_PATH,
            before_options="-nostdin",
            options="-vn -sn -dn -ar 48000 -ac 2 -b:a 192k"
        ),
        volume=0.7
    )


# ============================================
# Controls
# ============================================
class Controls(discord.ui.View):
    def __init__(self, song, gid):
        super().__init__(timeout=None)
        self.song = song
        self.gid = gid

    @discord.ui.button(label="Pause", emoji="\u23f8\ufe0f", style=discord.ButtonStyle.primary, custom_id="m_pause")
    async def btn_pause(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d or not d.get("vc"):
                return await inter.response.send_message("Nothing is playing.", ephemeral=True)
            vc = d["vc"]
            if not vc.is_playing():
                return await inter.response.send_message("Already paused.", ephemeral=True)
            vc.pause()
            d["is_paused"] = True
            d["pause_time"] = time.time()
            await inter.response.edit_message(embed=make_embed(self.song, self.gid, "paused"), view=self)

    @discord.ui.button(label="Resume", emoji="\u25b6\ufe0f", style=discord.ButtonStyle.success, custom_id="m_resume")
    async def btn_resume(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d or not d.get("vc"):
                return await inter.response.send_message("Nothing is playing.", ephemeral=True)
            vc = d["vc"]
            if not vc.is_paused():
                return await inter.response.send_message("Already playing.", ephemeral=True)
            if d.get("pause_time"):
                d["total_paused"] = d.get("total_paused", 0.0) + (time.time() - d["pause_time"])
                d["pause_time"] = None
            vc.resume()
            d["is_paused"] = False
            await inter.response.edit_message(embed=make_embed(self.song, self.gid, "playing"), view=self)

    @discord.ui.button(label="Stop", emoji="\u23f9\ufe0f", style=discord.ButtonStyle.danger, custom_id="m_stop")
    async def btn_stop(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d:
                return await inter.response.send_message("Nothing is playing.", ephemeral=True)
            await end_session(self.gid, disconnect=True)
            for c in self.children:
                c.disabled = True
            await inter.response.edit_message(embed=make_embed(self.song, self.gid, "stopped"), view=self)

    @discord.ui.button(label="New Song", emoji="\u23ed\ufe0f", style=discord.ButtonStyle.secondary, custom_id="m_new")
    async def btn_new(self, inter, btn):
        await inter.response.send_modal(SongModal(self.gid))


# ============================================
# Modal
# ============================================
class SongModal(discord.ui.Modal, title="Play New Song"):
    inp = discord.ui.TextInput(label="Song Name", placeholder="Enter song name...", required=True, max_length=200)

    def __init__(self, gid):
        super().__init__()
        self.gid = gid

    async def on_submit(self, inter):
        await inter.response.defer(thinking=True)
        d = music_data.get(self.gid)
        vc_ch = None
        if d and d.get("vc") and d["vc"].channel:
            vc_ch = d["vc"].channel
        elif inter.user.voice and inter.user.voice.channel:
            vc_ch = inter.user.voice.channel
        if not vc_ch:
            return await inter.followup.send("Not connected to a voice channel.", ephemeral=True)
        await play_song(inter, self.inp.value, vc_ch)


# ============================================
# Main play function
# ============================================
async def play_song(inter, query: str, vc_channel):
    guild = inter.guild
    if not guild:
        return await inter.followup.send("Server only command.", ephemeral=True)

    gid = guild.id

    async with get_lock(gid):
        # Status message
        msg = await inter.followup.send(
            embed=discord.Embed(
                title="Searching and Processing",
                description=f"```{query}```\nPlease wait...",
                color=discord.Color.blue()
            )
        )

        # Stop old session but keep VC
        await end_session(gid, disconnect=False)

        # Connect to VC
        try:
            cur = guild.voice_client
            if cur and cur.is_connected():
                if cur.channel != vc_channel:
                    await cur.move_to(vc_channel)
                vc = cur
            else:
                vc = await vc_channel.connect(timeout=30.0, self_deaf=True)
        except Exception as e:
            return await safe_edit(msg, embed=discord.Embed(
                title="Voice Connection Error", description=f"`{e}`", color=discord.Color.red()
            ))

        # Download COMPLETELY in background thread
        loop = asyncio.get_running_loop()
        song = await loop.run_in_executor(None, search_and_download_complete, query)

        if not song:
            return await safe_edit(msg, embed=discord.Embed(
                title="Song Not Found",
                description="Could not find or download that song. Try a different name.",
                color=discord.Color.red()
            ))

        # Update status
        await safe_edit(msg, embed=discord.Embed(
            title="Starting Playback",
            description=f"**{song['name']}** by {song['artist']}",
            color=discord.Color.blue()
        ))

        # Build audio source from COMPLETED local file
        try:
            source = make_source(song["file_path"])
        except Exception as e:
            safe_remove(song.get("file_path"))
            return await safe_edit(msg, embed=discord.Embed(
                title="Audio Source Error", description=f"`{e}`", color=discord.Color.red()
            ))

        sid = uuid.uuid4().hex

        music_data[gid] = {
            "sid": sid,
            "vc": vc,
            "song_info": song,
            "is_paused": False,
            "start_time": time.time(),
            "pause_time": None,
            "total_paused": 0.0,
            "message": msg,
        }

        def after_cb(err):
            asyncio.run_coroutine_threadsafe(on_track_end(gid, sid, err), bot.loop)

        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                await asyncio.sleep(0.2)
            vc.play(source, after=after_cb)
        except Exception as e:
            cur = music_data.get(gid)
            if cur and cur.get("sid") == sid:
                music_data.pop(gid, None)
            safe_remove(song.get("file_path"))
            return await safe_edit(msg, embed=discord.Embed(
                title="Playback Error", description=f"`{e}`", color=discord.Color.red()
            ))

        # Verify playback started
        await asyncio.sleep(0.8)
        cur = music_data.get(gid)
        if not cur or cur.get("sid") != sid:
            return

        if not vc.is_playing() and not vc.is_paused():
            music_data.pop(gid, None)
            safe_remove(song.get("file_path"))
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()
            return await safe_edit(msg, embed=discord.Embed(
                title="Playback Failed",
                description="File downloaded but playback could not start. Try another song.",
                color=discord.Color.red()
            ))

        # Show now playing
        view = Controls(song, gid)
        await safe_edit(msg, embed=make_embed(song, gid, "playing"), view=view)

        # Start updater
        bot.loop.create_task(updater(gid, sid, song, view, msg))


# ============================================
# Embed updater
# ============================================
async def updater(gid, sid, song, view, msg):
    await asyncio.sleep(15)
    while True:
        d = music_data.get(gid)
        if not d or d.get("sid") != sid:
            break
        vc = d.get("vc")
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            break
        st = "paused" if d.get("is_paused") else "playing"
        try:
            await msg.edit(embed=make_embed(song, gid, st), view=view)
        except Exception:
            break
        await asyncio.sleep(25)


# ============================================
# Events
# ============================================
@bot.event
async def on_ready():
    print(f"Bot Online: {bot.user} ({bot.user.id})")
    try:
        s = await bot.tree.sync()
        print(f"Synced {len(s)} commands")
    except Exception as e:
        logger.warning("Sync failed: %s", e)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="/song | Music Bot")
    )


@bot.tree.error
async def on_cmd_error(inter, error):
    orig = getattr(error, "original", error)
    logger.error("Command error: %s: %s", type(orig).__name__, orig)
    m = "An internal error occurred."
    if isinstance(orig, discord.Forbidden):
        m = "Missing permissions."
    elif isinstance(orig, asyncio.TimeoutError):
        m = "Operation timed out. Try again."
    with suppress(Exception):
        if inter.response.is_done():
            await inter.followup.send(m, ephemeral=True)
        else:
            await inter.response.send_message(m, ephemeral=True)


# ============================================
# Commands
# ============================================
@bot.tree.command(name="song", description="Play a song in your voice channel")
@app_commands.describe(name="Enter the song name")
async def song_cmd(inter, name: str):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)
    if not inter.user.voice or not inter.user.voice.channel:
        return await inter.response.send_message(
            embed=discord.Embed(title="Join a Voice Channel First", color=discord.Color.red()),
            ephemeral=True
        )
    await inter.response.defer(thinking=True)
    await play_song(inter, name, inter.user.voice.channel)


@bot.tree.command(name="stop", description="Stop playback and disconnect")
async def stop_cmd(inter):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)
    gid = inter.guild.id
    async with get_lock(gid):
        if gid not in music_data:
            return await inter.response.send_message("Nothing is playing.", ephemeral=True)
        await end_session(gid, disconnect=True)
        await inter.response.send_message(
            embed=discord.Embed(title="Stopped", description="Playback stopped.", color=discord.Color.red())
        )


# ============================================
# Start
# ============================================
print("Starting bot...")
bot.run(DISCORD_TOKEN, log_level=logging.ERROR)
