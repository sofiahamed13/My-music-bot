import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import time
import os
import subprocess
from datetime import timedelta

# ============================================
# CONFIG
# ============================================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "MTUxMjAxOTAzOTc2ODE1NDEyMg.G-ZRVG.8FLd8Dvmg-XVys1WwFRJv5q2oRe7LZvVU9TrTo"
FFMPEG_PATH = "/usr/bin/ffmpeg"
DOWNLOAD_DIR = "/tmp/music_cache"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ============================================
# FFmpeg Check
# ============================================
def check_ffmpeg():
    try:
        r = subprocess.run([FFMPEG_PATH, "-version"], capture_output=True)
        if r.returncode == 0:
            return True
    except FileNotFoundError:
        pass
    os.system("apt-get install -y ffmpeg > /dev/null 2>&1")
    return True

check_ffmpeg()

# ============================================
# Bot Setup
# ============================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)
music_data = {}

# ============================================
# Cleanup old cached files
# ============================================
def cleanup_cache():
    try:
        now = time.time()
        for f in os.listdir(DOWNLOAD_DIR):
            fp = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(fp):
                if now - os.path.getmtime(fp) > 1800:
                    os.remove(fp)
    except Exception:
        pass

# ============================================
# Search YouTube + Download Audio
# ============================================
def search_and_download(query):
    cleanup_cache()

    filename = os.path.join(
        DOWNLOAD_DIR, f"song_{int(time.time())}"
    )

    ydl_opts = {
        "format": (
            "bestaudio[ext=webm]/bestaudio[ext=m4a]/"
            "bestaudio/best"
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "source_address": "0.0.0.0",
        "socket_timeout": 30,
        "retries": 5,
        "outtmpl": filename + ".%(ext)s",
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
            result = ydl.extract_info(
                f"ytsearch3:{query} official audio",
                download=False
            )

            if not result or "entries" not in result:
                return None

            entries = [e for e in result["entries"] if e]
            if not entries:
                return None

            chosen = None
            for entry in entries:
                t = (entry.get("title") or "").lower()
                if any(k in t for k in [
                    "official audio", "official video",
                    "full song", "audio"
                ]):
                    chosen = entry
                    break
            if not chosen:
                chosen = entries[0]

            video_url = chosen.get("webpage_url") or chosen.get("url")
            if not video_url:
                return None

            info = ydl.extract_info(video_url, download=True)
            if not info:
                return None

            mp3_file = filename + ".mp3"
            if not os.path.exists(mp3_file):
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(os.path.basename(filename)):
                        mp3_file = os.path.join(DOWNLOAD_DIR, f)
                        break

            if not os.path.exists(mp3_file):
                return None

            if os.path.getsize(mp3_file) < 10000:
                os.remove(mp3_file)
                return None

            title = info.get("title", "Unknown")
            uploader = info.get("uploader", "Unknown Artist")

            if " - " in title:
                parts = title.split(" - ", 1)
                song_name = parts[0].strip()
                artist_name = parts[1].strip()
            elif " – " in title:
                parts = title.split(" – ", 1)
                song_name = parts[0].strip()
                artist_name = parts[1].strip()
            else:
                song_name = title
                artist_name = uploader

            for tag in [
                "(Official Audio)", "(Official Video)",
                "(Official Music Video)", "[Official Audio]",
                "[Official Video]", "(Lyric Video)",
                "(Audio)", "(HD)", "(Full Song)",
                "[Full Song]", "(Lyrics)", "[Lyrics]",
                "(Official)", "[Official]",
                "(Full Audio)", "[Full Audio]",
            ]:
                song_name = song_name.replace(tag, "").strip()
                artist_name = artist_name.replace(tag, "").strip()

            thumbnails = info.get("thumbnails", [])
            thumbnail = thumbnails[-1]["url"] if thumbnails else None
            duration = info.get("duration") or 0

            return {
                "name": song_name,
                "artist": artist_name,
                "album": "YouTube Music",
                "duration_sec": duration,
                "thumbnail": thumbnail,
                "file_path": mp3_file,
                "title": title,
            }

    except Exception:
        return None


# ============================================
# Create Embed
# ============================================
def create_embed(song, gid, status="playing"):
    data = music_data.get(gid, {})
    dur = max(song.get("duration_sec", 0), 1)

    if status == "playing" and "start_time" in data:
        tp = data.get("total_paused", 0)
        el = min(time.time() - data["start_time"] - tp, dur)
    elif status == "paused" and data.get("pause_time"):
        tp = data.get("total_paused", 0)
        el = min(data["pause_time"] - data["start_time"] - tp, dur)
    else:
        el = 0

    el = max(0, el)
    rem = max(0, dur - el)
    prog = min(el / dur, 1.0)
    filled = int(20 * prog)
    bar = "=" * filled + "-" * (20 - filled)

    colors = {
        "playing": discord.Color.green(),
        "paused": discord.Color.yellow(),
        "stopped": discord.Color.red(),
    }
    emojis = {
        "playing": "Now Playing",
        "paused": "Paused",
        "stopped": "Stopped",
    }

    em = discord.Embed(
        title="Music Player",
        color=colors.get(status, discord.Color.blue())
    )
    em.add_field(
        name="Song", value=f"**{song['name']}**", inline=False
    )
    em.add_field(name="Artist", value=song["artist"], inline=True)
    em.add_field(
        name="Album", value=song.get("album", "YT"), inline=True
    )
    em.add_field(
        name="Status",
        value=emojis.get(status, "Unknown"),
        inline=True
    )
    em.add_field(
        name="Progress",
        value=(
            f"`{timedelta(seconds=int(el))}` [{bar}] "
            f"`{timedelta(seconds=int(dur))}`"
        ),
        inline=False
    )
    em.add_field(
        name="Remaining",
        value=f"**{timedelta(seconds=int(rem))}**",
        inline=True
    )
    if song.get("thumbnail"):
        em.set_thumbnail(url=song["thumbnail"])
    em.set_footer(text="Music Bot | /song to play")
    return em


# ============================================
# Music Controls View
# ============================================
class Controls(discord.ui.View):
    def __init__(self, song, gid):
        super().__init__(timeout=None)
        self.song = song
        self.gid = gid

    @discord.ui.button(
        label="Pause", emoji="⏸️",
        style=discord.ButtonStyle.primary,
        custom_id="btn_pause"
    )
    async def pause(self, inter, btn):
        d = music_data.get(self.gid)
        if not d or not d.get("vc"):
            return await inter.response.send_message(
                "Not playing!", ephemeral=True
            )
        vc = d["vc"]
        if vc.is_playing():
            vc.pause()
            d["is_paused"] = True
            d["pause_time"] = time.time()
            await inter.response.edit_message(
                embed=create_embed(self.song, self.gid, "paused"),
                view=self
            )
        else:
            await inter.response.send_message(
                "Already paused!", ephemeral=True
            )

    @discord.ui.button(
        label="Resume", emoji="▶️",
        style=discord.ButtonStyle.success,
        custom_id="btn_resume"
    )
    async def resume(self, inter, btn):
        d = music_data.get(self.gid)
        if not d or not d.get("vc"):
            return await inter.response.send_message(
                "Not playing!", ephemeral=True
            )
        vc = d["vc"]
        if vc.is_paused():
            if d.get("pause_time"):
                d["total_paused"] = (
                    d.get("total_paused", 0) +
                    time.time() - d["pause_time"]
                )
                d["pause_time"] = None
            vc.resume()
            d["is_paused"] = False
            await inter.response.edit_message(
                embed=create_embed(self.song, self.gid, "playing"),
                view=self
            )
        else:
            await inter.response.send_message(
                "Already playing!", ephemeral=True
            )

    @discord.ui.button(
        label="Stop", emoji="⏹️",
        style=discord.ButtonStyle.danger,
        custom_id="btn_stop"
    )
    async def stop_btn(self, inter, btn):
        d = music_data.get(self.gid)
        if not d or not d.get("vc"):
            return await inter.response.send_message(
                "Not playing!", ephemeral=True
            )
        vc = d["vc"]
        d["user_stop"] = True
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        if vc.is_connected():
            await vc.disconnect()

        fp = d.get("song_info", {}).get("file_path")
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except Exception:
                pass

        for c in self.children:
            c.disabled = True
        await inter.response.edit_message(
            embed=create_embed(self.song, self.gid, "stopped"),
            view=self
        )
        if self.gid in music_data:
            del music_data[self.gid]

    @discord.ui.button(
        label="New Song", emoji="⏭️",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_new"
    )
    async def new_song(self, inter, btn):
        await inter.response.send_modal(SongModal(self.gid))


# ============================================
# New Song Modal
# ============================================
class SongModal(discord.ui.Modal, title="Play New Song"):
    inp = discord.ui.TextInput(
        label="Song Name",
        placeholder="Enter song name here...",
        required=True,
        max_length=200
    )

    def __init__(self, gid):
        super().__init__()
        self.gid = gid

    async def on_submit(self, inter):
        await inter.response.defer(thinking=True)
        d = music_data.get(self.gid)
        if not d or not d.get("vc"):
            return await inter.followup.send("Use /song first!")
        vc = d["vc"]
        d["user_stop"] = True
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await asyncio.sleep(0.5)
        await play_song(inter, self.inp.value, vc)


# ============================================
# Main Play Function
# ============================================
async def play_song(inter, query, vc):
    gid = inter.guild.id

    msg = await inter.followup.send(
        embed=discord.Embed(
            title="Searching & Downloading...",
            description=f"```{query}```\nPlease wait...",
            color=discord.Color.blue()
        )
    )

    loop = asyncio.get_event_loop()
    song = await loop.run_in_executor(
        None, search_and_download, query
    )

    if not song:
        return await msg.edit(
            embed=discord.Embed(
                title="Not Found!",
                description=f"**{query}** was not found. Try another name.",
                color=discord.Color.red()
            )
        )

    await msg.edit(
        embed=discord.Embed(
            title="Starting playback...",
            description=f"**{song['name']}** - {song['artist']}",
            color=discord.Color.blue()
        )
    )

    try:
        source = discord.FFmpegPCMAudio(
            song["file_path"],
            executable=FFMPEG_PATH,
            options="-vn -ar 48000 -ac 2"
        )
        source = discord.PCMVolumeTransformer(source, volume=0.7)
    except Exception as e:
        return await msg.edit(
            embed=discord.Embed(
                title="Audio Error",
                description=f"`{e}`",
                color=discord.Color.red()
            )
        )

    music_data[gid] = {
        "vc": vc,
        "song_info": song,
        "is_paused": False,
        "start_time": time.time(),
        "pause_time": None,
        "total_paused": 0,
        "message": msg,
        "user_stop": False,
    }

    def after(err):
        asyncio.run_coroutine_threadsafe(on_end(gid), bot.loop)

    try:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await asyncio.sleep(0.3)
        vc.play(source, after=after)
    except Exception as e:
        return await msg.edit(
            embed=discord.Embed(
                title="Play Error",
                description=f"`{e}`",
                color=discord.Color.red()
            )
        )

    await asyncio.sleep(1)
    if not vc.is_playing() and not vc.is_paused():
        if gid in music_data:
            del music_data[gid]
        return await msg.edit(
            embed=discord.Embed(
                title="Playback Failed!",
                description="Could not play this song. Try another one.",
                color=discord.Color.red()
            )
        )

    view = Controls(song, gid)
    await msg.edit(
        embed=create_embed(song, gid, "playing"),
        view=view
    )

    bot.loop.create_task(updater(gid, song, view, msg))


# ============================================
# Embed Auto Updater
# ============================================
async def updater(gid, song, view, msg):
    await asyncio.sleep(10)
    while gid in music_data:
        d = music_data.get(gid)
        if not d:
            break
        vc = d.get("vc")
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            break
        st = "paused" if d.get("is_paused") else "playing"
        try:
            await msg.edit(
                embed=create_embed(song, gid, st), view=view
            )
        except Exception:
            break
        await asyncio.sleep(20)


# ============================================
# Song End Handler
# ============================================
async def on_end(gid):
    d = music_data.get(gid)
    if not d:
        return
    if d.get("user_stop"):
        return

    vc = d.get("vc")
    msg = d.get("message")
    song = d.get("song_info")

    if vc and vc.is_connected():
        try:
            await vc.disconnect()
        except Exception:
            pass

    if song and song.get("file_path"):
        try:
            if os.path.exists(song["file_path"]):
                os.remove(song["file_path"])
        except Exception:
            pass

    if msg and song:
        try:
            em = discord.Embed(
                title="Song Finished!",
                color=discord.Color.blue()
            )
            em.add_field(
                name="Song",
                value=f"**{song['name']}**",
                inline=True
            )
            em.add_field(
                name="Artist", value=song["artist"], inline=True
            )
            if song.get("thumbnail"):
                em.set_thumbnail(url=song["thumbnail"])
            em.set_footer(text="Use /song to play another!")
            await msg.edit(embed=em, view=discord.ui.View())
        except Exception:
            pass

    if gid in music_data:
        del music_data[gid]


# ============================================
# Bot Ready Event
# ============================================
@bot.event
async def on_ready():
    print(f"Bot Online: {bot.user.name} ({bot.user.id})")
    try:
        s = await bot.tree.sync()
        print(f"Synced {len(s)} commands")
    except Exception:
        pass
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="/song | Music Bot"
        )
    )


# ============================================
# /song Command
# ============================================
@bot.tree.command(name="song", description="Play a song in voice channel!")
@app_commands.describe(name="Enter the song name")
async def song_cmd(inter, name: str):
    if not inter.user.voice:
        return await inter.response.send_message(
            embed=discord.Embed(
                title="Join a Voice Channel first!",
                color=discord.Color.red()
            ),
            ephemeral=True
        )

    vc_ch = inter.user.voice.channel
    gid = inter.guild.id
    await inter.response.defer(thinking=True)

    if gid in music_data:
        old = music_data[gid]
        old["user_stop"] = True
        ovc = old.get("vc")
        if ovc:
            try:
                if ovc.is_playing() or ovc.is_paused():
                    ovc.stop()
                await asyncio.sleep(0.3)
                if ovc.is_connected():
                    await ovc.disconnect()
                await asyncio.sleep(0.3)
            except Exception:
                pass

        old_fp = old.get("song_info", {}).get("file_path")
        if old_fp and os.path.exists(old_fp):
            try:
                os.remove(old_fp)
            except Exception:
                pass
        del music_data[gid]

    try:
        ex = inter.guild.voice_client
        if ex and ex.is_connected():
            await ex.move_to(vc_ch)
            vc = ex
        else:
            vc = await vc_ch.connect(
                timeout=30.0, reconnect=True, self_deaf=True
            )
    except Exception as e:
        return await inter.followup.send(f"VC Error: `{e}`")

    await play_song(inter, name, vc)


# ============================================
# /stop Command
# ============================================
@bot.tree.command(name="stop", description="Stop music and disconnect")
async def stop_cmd(inter):
    gid = inter.guild.id
    d = music_data.get(gid)
    if not d or not d.get("vc"):
        return await inter.response.send_message(
            "Nothing is playing!", ephemeral=True
        )
    vc = d["vc"]
    d["user_stop"] = True
    try:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        if vc.is_connected():
            await vc.disconnect()
    except Exception:
        pass

    fp = d.get("song_info", {}).get("file_path")
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception:
            pass

    if gid in music_data:
        del music_data[gid]
    await inter.response.send_message(
        embed=discord.Embed(
            title="Stopped!", color=discord.Color.red()
        )
    )


# ============================================
# Start Bot
# ============================================
bot.run(DISCORD_TOKEN, log_level=40)
