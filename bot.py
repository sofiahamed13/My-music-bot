import asyncio
import json
import logging
import os
import random
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
# Emoji Config - Change these to custom animated emojis later
# Example: EMOJI_PLAY = "<a:play:123456789>"
# ============================================
EMOJI_PLAY = "▶️"
EMOJI_PAUSE = "⏸️"
EMOJI_STOP = "⏹️"
EMOJI_SKIP = "⏭️"
EMOJI_SEARCH = "🔍"
EMOJI_MUSIC = "🎵"
EMOJI_ARTIST = "🎤"
EMOJI_TIME = "⏱️"
EMOJI_LIST = "📜"
EMOJI_ADD = "✅"
EMOJI_REMOVE = "🗑️"
EMOJI_CANCEL = "❌"
EMOJI_FOLDER = "📁"
EMOJI_DOT_GREEN = "🟢"
EMOJI_DOT_YELLOW = "🟡"
EMOJI_DOT_RED = "🔴"
EMOJI_SPEAKER = "🔇"
EMOJI_SHUFFLE = "🔀"
EMOJI_FORWARD = "▶"
EMOJI_BACKWARD = "◀"
EMOJI_HEADPHONE = "🎧"
EMOJI_LOADING = "⏳"
EMOJI_CHECK = "☑️"
EMOJI_STAR = "⭐"
EMOJI_NEW = "🆕"
EMOJI_WARN = "⚠️"
EMOJI_LINK = "🔗"
EMOJI_USER = "👤"
EMOJI_COUNT = "🔢"
EMOJI_PLAYLIST = "📋"

# ============================================
# Config
# ============================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")
DATA_FILE = Path(os.getenv("DATA_FILE", "/tmp/playlists.json"))

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN missing.")
    sys.exit(1)

logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
logging.getLogger("discord").setLevel(logging.ERROR)
logging.getLogger("yt_dlp").setLevel(logging.ERROR)

# ============================================
# Data persistence - Multi playlist
# ============================================
# Structure: { "guild_id": { "playlist_name": ["song1", "song2"], ... } }
def load_data() -> dict:
    if DATA_FILE.exists():
        with suppress(Exception):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    return {}

def save_data(data: dict):
    with suppress(Exception):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

all_data = load_data()

def get_guild_data(gid: int) -> dict:
    return all_data.setdefault(str(gid), {})

def get_playlist(gid: int, name: str) -> list:
    gd = get_guild_data(gid)
    return gd.get(name, [])

def save_playlist(gid: int, name: str, songs: list):
    gd = get_guild_data(gid)
    gd[name] = songs
    all_data[str(gid)] = gd
    save_data(all_data)

def delete_playlist(gid: int, name: str):
    gd = get_guild_data(gid)
    gd.pop(name, None)
    all_data[str(gid)] = gd
    save_data(all_data)

def get_all_playlist_names(gid: int) -> list:
    return list(get_guild_data(gid).keys())

def get_default_playlist(gid: int) -> tuple:
    """Get first available playlist for auto-play."""
    gd = get_guild_data(gid)
    for name, songs in gd.items():
        if songs:
            return name, songs
    return None, []

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
# yt-dlp extraction (no download - stream URL)
# ============================================
FALLBACK_CLIENTS = [
    {"youtube": {"player_client": ["web", "android"]}},
    {"youtube": {"player_client": ["mediaconnect"]}},
    {"youtube": {"player_client": ["tv_embedded"]}},
]

BASE_YDL_OPTS = {
    "format": "bestaudio[ext=webm]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "geo_bypass": True,
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    },
}


def is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")


def extract_song_url(query: str) -> dict | None:
    for client_args in FALLBACK_CLIENTS:
        try:
            opts = dict(BASE_YDL_OPTS)
            opts["extractor_args"] = client_args

            with yt_dlp.YoutubeDL(opts) as ydl:
                if is_url(query):
                    info = ydl.extract_info(query, download=False)
                else:
                    info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                    if info and "entries" in info:
                        entries = [e for e in info["entries"] if e]
                        if not entries:
                            continue
                        info = entries[0]

                if not info:
                    continue

                stream_url = info.get("url")
                if not stream_url:
                    fmts = info.get("formats", [])
                    audio = [f for f in fmts if f.get("acodec") != "none" and f.get("url")]
                    if audio:
                        audio.sort(key=lambda x: x.get("abr") or 0, reverse=True)
                        stream_url = audio[0]["url"]

                if not stream_url:
                    continue

                return build_song_info(info, stream_url)
        except Exception:
            continue
    return None


def search_song_info(query: str) -> dict | None:
    for client_args in FALLBACK_CLIENTS:
        try:
            opts = dict(BASE_YDL_OPTS)
            opts["extractor_args"] = client_args

            with yt_dlp.YoutubeDL(opts) as ydl:
                if is_url(query):
                    info = ydl.extract_info(query, download=False)
                else:
                    info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                    if info and "entries" in info:
                        entries = [e for e in info["entries"] if e]
                        if entries:
                            info = entries[0]
                        else:
                            continue

                if not info:
                    continue

                thumbs = info.get("thumbnails", [])
                thumb = thumbs[-1]["url"] if thumbs else None
                return {
                    "title": info.get("title", query),
                    "url": info.get("webpage_url", ""),
                    "thumbnail": thumb,
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", "Unknown"),
                }
        except Exception:
            continue
    return None


def build_song_info(info: dict, stream_url: str) -> dict:
    title = info.get("title", "Unknown")
    uploader = info.get("uploader", "Unknown Artist")
    sn, an = title, uploader

    for sep in [" - ", " — ", " – "]:
        if sep in title:
            p = title.split(sep, 1)
            an, sn = p[0].strip(), p[1].strip()
            break

    remove_tags = [
        "(Official Audio)", "(Official Video)", "(Official Music Video)",
        "[Official Audio]", "[Official Video]", "(Lyric Video)",
        "(Audio)", "(HD)", "(Full Song)", "[Full Song]",
        "(Lyrics)", "[Lyrics]", "(Official)", "[Official]",
        "(Full Audio)", "[Full Audio]", "(Official Lyric Video)",
        "[Official Music Video]", "(Music Video)", "[Music Video]",
    ]
    for t in remove_tags:
        sn = sn.replace(t, "").replace(t.lower(), "").strip()
        an = an.replace(t, "").replace(t.lower(), "").strip()

    thumbs = info.get("thumbnails", [])
    thumb = thumbs[-1]["url"] if thumbs else None

    return {
        "name": sn or title,
        "artist": an or uploader,
        "duration_sec": info.get("duration") or 0,
        "thumbnail": thumb,
        "stream_url": stream_url,
        "title": title,
        "webpage_url": info.get("webpage_url", ""),
    }

# ============================================
# FFmpeg streaming source
# ============================================
FFMPEG_BEFORE = (
    "-nostdin -hide_banner -loglevel error "
    "-reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 "
    "-probesize 200000 -analyzeduration 200000"
)
FFMPEG_OPTS = "-vn -sn -dn -b:a 192k"

def make_source(url: str):
    return discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(url, executable=FFMPEG_PATH,
                               before_options=FFMPEG_BEFORE, options=FFMPEG_OPTS),
        volume=0.65,
    )

# ============================================
# Embed builder
# ============================================
def fmt_time(seconds: int) -> str:
    s = str(timedelta(seconds=seconds))
    return s[2:] if s.startswith("0:") else s

def make_embed(song: dict, gid: int, status: str = "playing"):
    data = music_data.get(gid, {})
    dur = max(int(song.get("duration_sec", 0)), 1)
    el = 0

    if status == "playing" and "start_time" in data:
        tp = data.get("total_paused", 0.0)
        el = min(time.time() - data["start_time"] - tp, dur)
    elif status == "paused" and data.get("pause_time"):
        tp = data.get("total_paused", 0.0)
        el = min(data["pause_time"] - data["start_time"] - tp, dur)

    el = max(0, int(el))
    rem = max(0, dur - el)
    ratio = min(el / dur, 1.0)
    filled = int(16 * ratio)
    bar = "▰" * filled + "▱" * (16 - filled)

    styles = {
        "playing": (discord.Color.from_rgb(30, 215, 96), EMOJI_PLAY, "Now Playing", EMOJI_DOT_GREEN),
        "paused": (discord.Color.from_rgb(255, 193, 7), EMOJI_PAUSE, "Paused", EMOJI_DOT_YELLOW),
        "stopped": (discord.Color.from_rgb(220, 53, 69), EMOJI_STOP, "Stopped", EMOJI_DOT_RED),
    }
    color, icon, label, dot = styles.get(status, styles["playing"])

    em = discord.Embed(color=color)
    em.set_author(name=f"{icon}  {label}")

    em.description = (
        f"**{song['name']}**\n"
        f"{EMOJI_ARTIST} {song['artist']}\n\n"
        f"`{fmt_time(el)}` {bar} `{fmt_time(dur)}`\n"
        f"{EMOJI_LOADING} **{fmt_time(rem)}** remaining"
    )

    # Playlist info line
    if data.get("playlist_mode"):
        idx = data.get("playlist_index", 0) + 1
        total = data.get("playlist_total", 0)
        pl_name = data.get("playlist_name", "")
        ptype = data.get("playlist_type", "")
        tl = {"start": f"{EMOJI_FORWARD} Sequential", "end": f"{EMOJI_BACKWARD} Reverse",
              "random": f"{EMOJI_SHUFFLE} Shuffle"}.get(ptype, "")
        em.description += f"\n{EMOJI_PLAYLIST} **{pl_name}** — {idx}/{total} {tl}"

    if song.get("thumbnail"):
        em.set_thumbnail(url=song["thumbnail"])

    em.set_footer(text=f"{EMOJI_HEADPHONE}  /song  ·  /playlist  ·  /addplay")
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
    if vc:
        with suppress(Exception):
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        if disconnect:
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()


async def on_track_end(gid: int, sid: str, err):
    async with get_lock(gid):
        data = music_data.get(gid)
        if not data or data.get("sid") != sid:
            return

        song = data.get("song_info")
        vc = data.get("vc")
        msg = data.get("message")
        pl_mode = data.get("playlist_mode", False)
        pl_order = data.get("playlist_order", [])
        pl_index = data.get("playlist_index", 0)
        pl_type = data.get("playlist_type", "")
        pl_name = data.get("playlist_name", "")
        vc_channel = vc.channel if vc and vc.is_connected() else None

        next_query = None
        next_index = 0
        next_order = pl_order
        next_pl_mode = pl_mode
        next_pl_type = pl_type
        next_pl_name = pl_name

        if vc_channel:
            if pl_mode and pl_order:
                ni = pl_index + 1
                if ni < len(pl_order):
                    next_query = pl_order[ni]
                    next_index = ni
                else:
                    # Restart cycle
                    fresh = get_playlist(gid, pl_name)
                    if fresh:
                        if pl_type == "random":
                            next_order = list(fresh)
                            random.shuffle(next_order)
                        elif pl_type == "end":
                            next_order = list(reversed(fresh))
                        else:
                            next_order = list(fresh)
                        next_query = next_order[0]
                        next_index = 0
            elif not pl_mode:
                # Auto-play from default playlist after /song
                pname, psongs = get_default_playlist(gid)
                if pname and psongs:
                    next_order = list(psongs)
                    next_query = next_order[0]
                    next_index = 0
                    next_pl_mode = True
                    next_pl_type = "start"
                    next_pl_name = pname

        music_data.pop(gid, None)

        if next_query and vc_channel:
            if msg:
                await safe_edit(msg, embed=discord.Embed(
                    title=f"{EMOJI_SKIP}  Loading Next",
                    description=f"```{next_query}```",
                    color=discord.Color.blurple(),
                ), view=discord.ui.View())

            await _play_next(gid, vc, msg, next_query, next_pl_mode,
                             next_order, next_index, next_pl_type, next_pl_name)
        else:
            if vc:
                with suppress(Exception):
                    if vc.is_connected():
                        await vc.disconnect()
            if msg:
                em = discord.Embed(color=discord.Color.from_rgb(30, 215, 96))
                if err:
                    em.title = f"{EMOJI_CANCEL}  Playback Error"
                    em.color = discord.Color.red()
                else:
                    em.title = f"{EMOJI_ADD}  Song Finished"
                    if song:
                        em.description = f"**{song['name']}** — {song['artist']}"
                        if song.get("thumbnail"):
                            em.set_thumbnail(url=song["thumbnail"])
                em.set_footer(text="Use /song or /playlist to play more")
                await safe_edit(msg, embed=em, view=discord.ui.View())


async def _play_next(gid, vc, old_msg, query, pl_mode, pl_order,
                     pl_index, pl_type, pl_name):
    loop = asyncio.get_running_loop()
    song = await loop.run_in_executor(None, extract_song_url, query)

    if not song:
        ni = pl_index + 1
        if ni < len(pl_order):
            await _play_next(gid, vc, old_msg, pl_order[ni], pl_mode,
                             pl_order, ni, pl_type, pl_name)
        else:
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()
            if old_msg:
                await safe_edit(old_msg, embed=discord.Embed(
                    title=f"{EMOJI_PLAYLIST}  Playlist Finished",
                    description="All songs played!",
                    color=discord.Color.blurple(),
                ), view=discord.ui.View())
        return

    try:
        source = make_source(song["stream_url"])
    except Exception:
        ni = pl_index + 1
        if ni < len(pl_order):
            await _play_next(gid, vc, old_msg, pl_order[ni], pl_mode,
                             pl_order, ni, pl_type, pl_name)
        return

    sid = uuid.uuid4().hex
    music_data[gid] = {
        "sid": sid, "vc": vc, "song_info": song,
        "is_paused": False, "start_time": time.time(),
        "pause_time": None, "total_paused": 0.0, "message": old_msg,
        "playlist_mode": pl_mode, "playlist_order": pl_order,
        "playlist_index": pl_index, "playlist_total": len(pl_order),
        "playlist_type": pl_type, "playlist_name": pl_name,
    }

    def after_cb(error):
        asyncio.run_coroutine_threadsafe(on_track_end(gid, sid, error), bot.loop)

    try:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await asyncio.sleep(0.3)
        vc.play(source, after=after_cb)
    except Exception:
        music_data.pop(gid, None)
        return

    await asyncio.sleep(0.5)
    if gid not in music_data or music_data[gid].get("sid") != sid:
        return

    view = Controls(song, gid)
    if old_msg:
        await safe_edit(old_msg, embed=make_embed(song, gid, "playing"), view=view)
    bot.loop.create_task(updater(gid, sid, song, view, old_msg))

# ============================================
# Controls
# ============================================
class Controls(discord.ui.View):
    def __init__(self, song, gid):
        super().__init__(timeout=None)
        self.song = song
        self.gid = gid

    @discord.ui.button(label="Pause", emoji=EMOJI_PAUSE, style=discord.ButtonStyle.primary, custom_id="m_pause")
    async def btn_pause(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d or not d.get("vc"):
                return await inter.response.send_message("Nothing playing.", ephemeral=True)
            vc = d["vc"]
            if not vc.is_playing():
                return await inter.response.send_message("Already paused.", ephemeral=True)
            vc.pause()
            d["is_paused"] = True
            d["pause_time"] = time.time()
            await inter.response.edit_message(embed=make_embed(self.song, self.gid, "paused"), view=self)

    @discord.ui.button(label="Resume", emoji=EMOJI_PLAY, style=discord.ButtonStyle.success, custom_id="m_resume")
    async def btn_resume(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d or not d.get("vc"):
                return await inter.response.send_message("Nothing playing.", ephemeral=True)
            vc = d["vc"]
            if not vc.is_paused():
                return await inter.response.send_message("Already playing.", ephemeral=True)
            if d.get("pause_time"):
                d["total_paused"] = d.get("total_paused", 0.0) + (time.time() - d["pause_time"])
                d["pause_time"] = None
            vc.resume()
            d["is_paused"] = False
            await inter.response.edit_message(embed=make_embed(self.song, self.gid, "playing"), view=self)

    @discord.ui.button(label="Stop", emoji=EMOJI_STOP, style=discord.ButtonStyle.danger, custom_id="m_stop")
    async def btn_stop(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d:
                return await inter.response.send_message("Nothing playing.", ephemeral=True)
            d["playlist_mode"] = False
            d["playlist_order"] = []
            await end_session(self.gid, disconnect=True)
            for c in self.children:
                c.disabled = True
            await inter.response.edit_message(embed=make_embed(self.song, self.gid, "stopped"), view=self)

    @discord.ui.button(label="Skip", emoji=EMOJI_SKIP, style=discord.ButtonStyle.secondary, custom_id="m_skip")
    async def btn_skip(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d or not d.get("vc"):
                return await inter.response.send_message("Nothing playing.", ephemeral=True)
            vc = d["vc"]
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await inter.response.send_message(f"{EMOJI_SKIP} Skipping...", ephemeral=True)

    @discord.ui.button(label="New Song", emoji=EMOJI_SEARCH, style=discord.ButtonStyle.secondary, custom_id="m_new")
    async def btn_new(self, inter, btn):
        await inter.response.send_modal(SongModal(self.gid))


class SongModal(discord.ui.Modal, title="Play New Song"):
    inp = discord.ui.TextInput(label="Song Name", placeholder="Song name or URL...",
                               required=True, max_length=200)
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
            return await inter.followup.send("Join a voice channel first.", ephemeral=True)
        await play_song(inter, self.inp.value, vc_ch)

# ============================================
# Addplay - Playlist name modal
# ============================================
class NewPlaylistModal(discord.ui.Modal, title="Create New Playlist"):
    name_input = discord.ui.TextInput(label="Playlist Name", placeholder="My Playlist...",
                                      required=True, max_length=50)
    def __init__(self, gid: int, song_query: str, user_id: int, user_name: str):
        super().__init__()
        self.gid = gid
        self.song_query = song_query
        self.user_id = user_id
        self.user_name = user_name

    async def on_submit(self, inter):
        pl_name = self.name_input.value.strip()
        if not pl_name:
            return await inter.response.send_message("Name cannot be empty.", ephemeral=True)

        existing = get_all_playlist_names(self.gid)
        if pl_name.lower() in [n.lower() for n in existing]:
            return await inter.response.send_message(
                f"{EMOJI_WARN} Playlist **{pl_name}** already exists. Use it from the dropdown.",
                ephemeral=True)

        # Create empty playlist first
        save_playlist(self.gid, pl_name, [])
        await inter.response.defer(thinking=True)
        await process_addplay(inter, self.gid, pl_name, self.song_query,
                              self.user_id, self.user_name)


class PlaylistSelectForAdd(discord.ui.View):
    def __init__(self, gid: int, existing: list, song_query: str, user_id: int, user_name: str):
        super().__init__(timeout=120)
        self.gid = gid
        self.song_query = song_query
        self.user_id = user_id
        self.user_name = user_name

        options = []
        for name in existing[:24]:
            count = len(get_playlist(gid, name))
            options.append(discord.SelectOption(
                label=name, description=f"{count} songs", emoji=EMOJI_MUSIC))
        options.append(discord.SelectOption(
            label="➕ Create New Playlist", value="__NEW__",
            description="Create a brand new playlist", emoji=EMOJI_ADD))

        select = discord.ui.Select(placeholder="Select a playlist...",
                                   options=options, custom_id="pl_select_add")
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, inter):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Not your request.", ephemeral=True)

        val = inter.data["values"][0]
        if val == "__NEW__":
            await inter.response.send_modal(
                NewPlaylistModal(self.gid, self.song_query, self.user_id, self.user_name))
        else:
            await inter.response.defer(thinking=True)
            await process_addplay(inter, self.gid, val, self.song_query,
                                  self.user_id, self.user_name)


async def process_addplay(inter, gid, pl_name, song_query, user_id, user_name):
    """Search song and show confirmation."""
    songs_to_add = [s.strip() for s in song_query.split(",") if s.strip()]

    loop = asyncio.get_running_loop()

    for sq in songs_to_add:
        info = await loop.run_in_executor(None, search_song_info, sq)

        if not info:
            await inter.followup.send(embed=discord.Embed(
                title=f"{EMOJI_CANCEL}  Not Found",
                description=f"Could not find: **{sq}**",
                color=discord.Color.red(),
            ), ephemeral=True)
            continue

        em = discord.Embed(
            title=f"{EMOJI_MUSIC}  Add to Playlist?",
            color=discord.Color.blurple(),
        )
        em.description = (
            f"**{info['title']}**\n"
            f"{EMOJI_ARTIST} {info['uploader']}"
        )
        if info.get("duration"):
            em.description += f"\n{EMOJI_TIME} {fmt_time(info['duration'])}"
        em.description += f"\n\n{EMOJI_FOLDER} Playlist: **{pl_name}**"

        if info.get("thumbnail"):
            em.set_image(url=info["thumbnail"])

        view = AddConfirmView(gid, pl_name, info["title"], user_id, user_name)
        await inter.followup.send(embed=em, view=view)


class AddConfirmView(discord.ui.View):
    def __init__(self, gid, pl_name, song_title, user_id, user_name):
        super().__init__(timeout=60)
        self.gid = gid
        self.pl_name = pl_name
        self.song_title = song_title
        self.user_id = user_id
        self.user_name = user_name
        self.done = False

    @discord.ui.button(label="Yes, Save It", emoji=EMOJI_ADD, style=discord.ButtonStyle.success)
    async def btn_yes(self, inter, btn):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Not your request.", ephemeral=True)
        if self.done:
            return
        self.done = True

        pl = get_playlist(self.gid, self.pl_name)
        if self.song_title.lower() not in [s.lower() for s in pl]:
            pl.append(self.song_title)
            save_playlist(self.gid, self.pl_name, pl)

        em = discord.Embed(
            title=f"{EMOJI_ADD}  Saved to Playlist",
            color=discord.Color.from_rgb(30, 215, 96),
        )
        em.description = (
            f"{EMOJI_MUSIC} **{self.song_title}**\n"
            f"{EMOJI_FOLDER} **{self.pl_name}**\n"
            f"{EMOJI_USER} {self.user_name}\n"
        )

        lines = []
        for i, s in enumerate(pl, 1):
            marker = f" {EMOJI_NEW}" if s == self.song_title else ""
            lines.append(f"`{i}.` {s}{marker}")
            if i >= 15:
                left = len(pl) - 15
                if left > 0:
                    lines.append(f"*+{left} more...*")
                break

        em.add_field(name=f"{EMOJI_LIST} Songs ({len(pl)})", value="\n".join(lines), inline=False)

        for c in self.children:
            c.disabled = True
        await inter.response.edit_message(embed=em, view=self)

    @discord.ui.button(label="No", emoji=EMOJI_CANCEL, style=discord.ButtonStyle.danger)
    async def btn_no(self, inter, btn):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Not your request.", ephemeral=True)
        if self.done:
            return
        self.done = True
        em = discord.Embed(
            title=f"{EMOJI_CANCEL}  Cancelled",
            description=f"**{self.song_title}** was not added.",
            color=discord.Color.greyple(),
        )
        for c in self.children:
            c.disabled = True
        await inter.response.edit_message(embed=em, view=self)

    async def on_timeout(self):
        self.done = True

# ============================================
# Delete views
# ============================================
class DeletePlaylistSelect(discord.ui.View):
    def __init__(self, gid: int, names: list, user_id: int):
        super().__init__(timeout=60)
        self.gid = gid
        self.user_id = user_id

        options = []
        for n in names[:25]:
            count = len(get_playlist(gid, n))
            options.append(discord.SelectOption(label=n, description=f"{count} songs", emoji=EMOJI_MUSIC))

        select = discord.ui.Select(placeholder="Select playlist...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, inter):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Not your request.", ephemeral=True)
        pl_name = inter.data["values"][0]
        songs = get_playlist(self.gid, pl_name)

        if not songs:
            delete_playlist(self.gid, pl_name)
            return await inter.response.edit_message(
                embed=discord.Embed(
                    title=f"{EMOJI_REMOVE}  Empty Playlist Deleted",
                    description=f"**{pl_name}** removed.",
                    color=discord.Color.red(),
                ), view=None)

        view = DeleteSongView(self.gid, pl_name, songs, self.user_id)
        em = discord.Embed(
            title=f"{EMOJI_REMOVE}  Delete from: {pl_name}",
            color=discord.Color.from_rgb(220, 53, 69),
        )
        lines = [f"`{i}.` {s}" for i, s in enumerate(songs[:25], 1)]
        em.description = "\n".join(lines)
        em.set_footer(text="Select songs to remove, or delete entire playlist")
        await inter.response.edit_message(embed=em, view=view)


class DeleteSongView(discord.ui.View):
    def __init__(self, gid, pl_name, songs, user_id):
        super().__init__(timeout=60)
        self.gid = gid
        self.pl_name = pl_name
        self.user_id = user_id

        options = []
        for i, s in enumerate(songs[:25]):
            options.append(discord.SelectOption(label=s[:100], value=str(i), emoji=EMOJI_MUSIC))

        select = discord.ui.Select(placeholder="Select songs to remove...",
                                   min_values=1, max_values=min(len(options), 25),
                                   options=options)
        select.callback = self.on_song_select
        self.add_item(select)

    @discord.ui.button(label="Delete Entire Playlist", emoji=EMOJI_REMOVE,
                       style=discord.ButtonStyle.danger, row=2)
    async def btn_delete_all(self, inter, btn):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Not your request.", ephemeral=True)
        delete_playlist(self.gid, self.pl_name)
        await inter.response.edit_message(
            embed=discord.Embed(
                title=f"{EMOJI_REMOVE}  Playlist Deleted",
                description=f"**{self.pl_name}** has been completely removed.",
                color=discord.Color.red(),
            ), view=None)

    async def on_song_select(self, inter):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Not your request.", ephemeral=True)

        pl = get_playlist(self.gid, self.pl_name)
        indices = sorted([int(v) for v in inter.data["values"]], reverse=True)
        removed = []
        for idx in indices:
            if 0 <= idx < len(pl):
                removed.append(pl.pop(idx))
        save_playlist(self.gid, self.pl_name, pl)

        em = discord.Embed(
            title=f"{EMOJI_REMOVE}  Songs Removed",
            color=discord.Color.from_rgb(220, 53, 69),
        )
        em.description = f"{EMOJI_CANCEL} " + ", ".join(f"**{r}**" for r in removed)

        if pl:
            lines = [f"`{i}.` {s}" for i, s in enumerate(pl, 1)]
            if len(lines) > 15:
                lines = lines[:15]
                lines.append(f"*+{len(pl)-15} more...*")
            em.add_field(name=f"{EMOJI_LIST} Remaining ({len(pl)})",
                         value="\n".join(lines), inline=False)
        else:
            em.add_field(name=EMOJI_LIST, value="*Playlist is now empty*", inline=False)
            delete_playlist(self.gid, self.pl_name)

        await inter.response.edit_message(embed=em, view=None)

# ============================================
# Main play function
# ============================================
async def play_song(inter, query: str, vc_channel, playlist_mode=False,
                    playlist_order=None, playlist_index=0,
                    playlist_type="", playlist_name=""):
    guild = inter.guild
    if not guild:
        return await inter.followup.send("Server only.", ephemeral=True)

    gid = guild.id

    async with get_lock(gid):
        msg = await inter.followup.send(embed=discord.Embed(
            title=f"{EMOJI_SEARCH}  Searching...",
            description=f"```{query}```",
            color=discord.Color.blurple(),
        ))

        old = music_data.get(gid)
        if old:
            old["playlist_mode"] = False
            old["playlist_order"] = []
        await end_session(gid, disconnect=False)

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
                title=f"{EMOJI_CANCEL}  Connection Failed",
                description=f"`{e}`", color=discord.Color.red()))

        loop = asyncio.get_running_loop()
        song = await loop.run_in_executor(None, extract_song_url, query)

        if not song:
            return await safe_edit(msg, embed=discord.Embed(
                title=f"{EMOJI_CANCEL}  Not Found",
                description="Try a different search term.",
                color=discord.Color.red()))

        try:
            source = make_source(song["stream_url"])
        except Exception as e:
            return await safe_edit(msg, embed=discord.Embed(
                title=f"{EMOJI_CANCEL}  Audio Error",
                description=f"`{e}`", color=discord.Color.red()))

        sid = uuid.uuid4().hex
        music_data[gid] = {
            "sid": sid, "vc": vc, "song_info": song,
            "is_paused": False, "start_time": time.time(),
            "pause_time": None, "total_paused": 0.0, "message": msg,
            "playlist_mode": playlist_mode,
            "playlist_order": playlist_order or [],
            "playlist_index": playlist_index,
            "playlist_total": len(playlist_order) if playlist_order else 0,
            "playlist_type": playlist_type,
            "playlist_name": playlist_name,
        }

        def after_cb(err):
            asyncio.run_coroutine_threadsafe(on_track_end(gid, sid, err), bot.loop)

        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                await asyncio.sleep(0.3)
            vc.play(source, after=after_cb)
        except Exception as e:
            music_data.pop(gid, None)
            return await safe_edit(msg, embed=discord.Embed(
                title=f"{EMOJI_CANCEL}  Playback Error",
                description=f"`{e}`", color=discord.Color.red()))

        await asyncio.sleep(0.8)
        cur = music_data.get(gid)
        if not cur or cur.get("sid") != sid:
            return

        if not vc.is_playing() and not vc.is_paused():
            music_data.pop(gid, None)
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()
            return await safe_edit(msg, embed=discord.Embed(
                title=f"{EMOJI_CANCEL}  Playback Failed",
                description="Could not start. Try again.",
                color=discord.Color.red()))

        view = Controls(song, gid)
        await safe_edit(msg, embed=make_embed(song, gid, "playing"), view=view)
        bot.loop.create_task(updater(gid, sid, song, view, msg))

# ============================================
# Updater
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
        await asyncio.sleep(30)

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
        print(f"Sync failed: {e}")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="/song · /playlist"))

@bot.tree.error
async def on_cmd_error(inter, error):
    orig = getattr(error, "original", error)
    m = "An error occurred."
    if isinstance(orig, discord.Forbidden):
        m = "Missing permissions."
    elif isinstance(orig, asyncio.TimeoutError):
        m = "Timed out. Try again."
    with suppress(Exception):
        if inter.response.is_done():
            await inter.followup.send(m, ephemeral=True)
        else:
            await inter.response.send_message(m, ephemeral=True)

# ============================================
# Slash Commands
# ============================================

@bot.tree.command(name="song", description="Play a song in your voice channel")
@app_commands.describe(name="Song name or YouTube URL")
async def song_cmd(inter, name: str):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)
    if not inter.user.voice or not inter.user.voice.channel:
        return await inter.response.send_message(embed=discord.Embed(
            title=f"{EMOJI_SPEAKER}  Join a Voice Channel",
            color=discord.Color.red()), ephemeral=True)
    await inter.response.defer(thinking=True)
    await play_song(inter, name, inter.user.voice.channel)


@bot.tree.command(name="stop", description="Stop playback and disconnect")
async def stop_cmd(inter):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)
    gid = inter.guild.id
    async with get_lock(gid):
        d = music_data.get(gid)
        if not d:
            return await inter.response.send_message("Nothing playing.", ephemeral=True)
        d["playlist_mode"] = False
        d["playlist_order"] = []
        await end_session(gid, disconnect=True)
        await inter.response.send_message(embed=discord.Embed(
            title=f"{EMOJI_STOP}  Stopped",
            description="Playback stopped.",
            color=discord.Color.red()))


@bot.tree.command(name="addplay", description="Add song(s) to a playlist")
@app_commands.describe(name="Song name(s) or URL(s) — separate with commas")
async def addplay_cmd(inter, name: str):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)

    gid = inter.guild.id
    existing = get_all_playlist_names(gid)

    if not existing:
        # No playlists exist, show modal to create one
        await inter.response.send_modal(
            NewPlaylistModal(gid, name, inter.user.id, inter.user.display_name))
    else:
        # Show dropdown to pick playlist or create new
        em = discord.Embed(
            title=f"{EMOJI_FOLDER}  Select Playlist",
            description=f"Choose where to add:\n```{name}```",
            color=discord.Color.blurple(),
        )
        view = PlaylistSelectForAdd(gid, existing, name, inter.user.id, inter.user.display_name)
        await inter.response.send_message(embed=em, view=view, ephemeral=True)


@bot.tree.command(name="playlist", description="Play songs from a playlist")
@app_commands.describe(order="Play order")
@app_commands.choices(order=[
    app_commands.Choice(name="▶ From Starting ↓", value="start"),
    app_commands.Choice(name="◀ From Ending ↑", value="end"),
    app_commands.Choice(name="🔀 Random", value="random"),
])
async def playlist_cmd(inter, order: str):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)
    if not inter.user.voice or not inter.user.voice.channel:
        return await inter.response.send_message(embed=discord.Embed(
            title=f"{EMOJI_SPEAKER}  Join a Voice Channel",
            color=discord.Color.red()), ephemeral=True)

    gid = inter.guild.id
    names = get_all_playlist_names(gid)

    if not names:
        return await inter.response.send_message(embed=discord.Embed(
            title=f"{EMOJI_PLAYLIST}  No Playlists",
            description="Use `/addplay` to create one first.",
            color=discord.Color.orange()), ephemeral=True)

    if len(names) == 1:
        # Only one playlist, play it directly
        pl_name = names[0]
        songs = get_playlist(gid, pl_name)
        if not songs:
            return await inter.response.send_message(embed=discord.Embed(
                title=f"{EMOJI_PLAYLIST}  Playlist Empty",
                description=f"**{pl_name}** has no songs.",
                color=discord.Color.orange()), ephemeral=True)

        await inter.response.defer(thinking=True)
        await _start_playlist(inter, gid, pl_name, songs, order)
    else:
        # Multiple playlists — show selector
        view = PlaylistPlaySelect(gid, names, order, inter.user.id,
                                  inter.user.voice.channel)
        em = discord.Embed(
            title=f"{EMOJI_FOLDER}  Select Playlist to Play",
            color=discord.Color.blurple(),
        )
        await inter.response.send_message(embed=em, view=view, ephemeral=True)


class PlaylistPlaySelect(discord.ui.View):
    def __init__(self, gid, names, order, user_id, vc_channel):
        super().__init__(timeout=60)
        self.gid = gid
        self.order = order
        self.user_id = user_id
        self.vc_channel = vc_channel

        options = []
        for n in names[:25]:
            count = len(get_playlist(gid, n))
            options.append(discord.SelectOption(label=n, description=f"{count} songs", emoji=EMOJI_MUSIC))

        select = discord.ui.Select(placeholder="Select playlist...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, inter):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Not your request.", ephemeral=True)

        pl_name = inter.data["values"][0]
        songs = get_playlist(self.gid, pl_name)

        if not songs:
            return await inter.response.edit_message(embed=discord.Embed(
                title=f"{EMOJI_PLAYLIST}  Empty",
                description=f"**{pl_name}** has no songs.",
                color=discord.Color.orange()), view=None)

        await inter.response.defer(thinking=True)
        await _start_playlist(inter, self.gid, pl_name, songs, self.order)


async def _start_playlist(inter, gid, pl_name, songs, order):
    if order == "end":
        play_order = list(reversed(songs))
    elif order == "random":
        play_order = list(songs)
        random.shuffle(play_order)
    else:
        play_order = list(songs)

    vc_ch = inter.user.voice.channel if inter.user.voice else None
    if not vc_ch:
        return await inter.followup.send("Join a voice channel.", ephemeral=True)

    await play_song(inter, play_order[0], vc_ch,
                    playlist_mode=True, playlist_order=play_order,
                    playlist_index=0, playlist_type=order,
                    playlist_name=pl_name)


@bot.tree.command(name="delete", description="Remove songs or playlists")
async def delete_cmd(inter):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)

    gid = inter.guild.id
    names = get_all_playlist_names(gid)

    if not names:
        return await inter.response.send_message(embed=discord.Embed(
            title=f"{EMOJI_PLAYLIST}  Nothing to Delete",
            description="No playlists exist.",
            color=discord.Color.orange()), ephemeral=True)

    if len(names) == 1:
        pl_name = names[0]
        songs = get_playlist(gid, pl_name)
        if not songs:
            delete_playlist(gid, pl_name)
            return await inter.response.send_message(embed=discord.Embed(
                title=f"{EMOJI_REMOVE}  Deleted",
                description=f"Empty playlist **{pl_name}** removed.",
                color=discord.Color.red()), ephemeral=True)

        view = DeleteSongView(gid, pl_name, songs, inter.user.id)
        em = discord.Embed(
            title=f"{EMOJI_REMOVE}  Delete from: {pl_name}",
            color=discord.Color.from_rgb(220, 53, 69),
        )
        lines = [f"`{i}.` {s}" for i, s in enumerate(songs[:25], 1)]
        em.description = "\n".join(lines)
        await inter.response.send_message(embed=em, view=view, ephemeral=True)
    else:
        view = DeletePlaylistSelect(gid, names, inter.user.id)
        em = discord.Embed(
            title=f"{EMOJI_REMOVE}  Select Playlist",
            description="Choose a playlist to manage.",
            color=discord.Color.from_rgb(220, 53, 69),
        )
        await inter.response.send_message(embed=em, view=view, ephemeral=True)


@bot.tree.command(name="showplaylist", description="View playlists and songs")
async def showplaylist_cmd(inter):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)

    gid = inter.guild.id
    gd = get_guild_data(gid)

    if not gd:
        return await inter.response.send_message(embed=discord.Embed(
            title=f"{EMOJI_LIST}  No Playlists",
            description="Use `/addplay` to create one.",
            color=discord.Color.blurple()))

    em = discord.Embed(
        title=f"{EMOJI_LIST}  Server Playlists",
        color=discord.Color.blurple(),
    )

    for name, songs in gd.items():
        if not songs:
            em.add_field(name=f"{EMOJI_FOLDER} {name}", value="*Empty*", inline=False)
            continue

        lines = [f"`{i}.` {s}" for i, s in enumerate(songs, 1)]
        if len(lines) > 10:
            display = lines[:10]
            display.append(f"*+{len(lines)-10} more...*")
        else:
            display = lines

        em.add_field(
            name=f"{EMOJI_FOLDER} {name}  —  {len(songs)} songs",
            value="\n".join(display),
            inline=False,
        )

    em.set_footer(text=f"{EMOJI_HEADPHONE}  /addplay to add  ·  /delete to remove  ·  /playlist to play")
    await inter.response.send_message(embed=em)

# ============================================
# Start
# ============================================
print("Starting bot...")
bot.run(DISCORD_TOKEN, log_level=logging.ERROR)
