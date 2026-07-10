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
# Config
# ============================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")
PLAYLIST_FILE = Path(os.getenv("PLAYLIST_FILE", "/tmp/playlist_data.json"))

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN missing.")
    sys.exit(1)

logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
logger = logging.getLogger("musicbot")
logging.getLogger("discord").setLevel(logging.ERROR)
logging.getLogger("yt_dlp").setLevel(logging.ERROR)

# ============================================
# Playlist persistence
# ============================================
def load_playlists() -> dict:
    if PLAYLIST_FILE.exists():
        with suppress(Exception):
            with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    return {}

def save_playlists(data: dict):
    with suppress(Exception):
        with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

playlists = load_playlists()

def get_guild_playlist(gid: int) -> list:
    return playlists.setdefault(str(gid), [])

def save_guild_playlist(gid: int, pl: list):
    playlists[str(gid)] = pl
    save_playlists(playlists)

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
# yt-dlp: Fast URL extraction (NO download)
# ============================================
YDL_SEARCH_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "geo_bypass": True,
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
    "extract_flat": False,
    "skip_download": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "android"],
        }
    },
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    },
}

YDL_EXTRACT_OPTS = {
    "format": "bestaudio[ext=webm]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "geo_bypass": True,
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "android"],
        }
    },
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    },
}

FALLBACK_CLIENTS = [
    {"youtube": {"player_client": ["web", "android"]}},
    {"youtube": {"player_client": ["mediaconnect"]}},
    {"youtube": {"player_client": ["tv_embedded"]}},
]


def extract_song_url(query: str) -> dict | None:
    """Search + extract stream URL without downloading. Fast."""
    is_url = query.startswith("http://") or query.startswith("https://")

    for i, client_args in enumerate(FALLBACK_CLIENTS):
        try:
            opts = dict(YDL_EXTRACT_OPTS)
            opts["extractor_args"] = client_args

            with yt_dlp.YoutubeDL(opts) as ydl:
                if is_url:
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
                    formats = info.get("formats", [])
                    audio_fmts = [f for f in formats if f.get("acodec") != "none" and f.get("url")]
                    if audio_fmts:
                        audio_fmts.sort(key=lambda x: x.get("abr") or 0, reverse=True)
                        stream_url = audio_fmts[0]["url"]

                if not stream_url:
                    continue

                return parse_song_info_stream(info, stream_url)

        except Exception:
            continue

    return None


def search_song_info(query: str) -> dict | None:
    """Quick search to get song title + thumbnail for /addplay confirmation."""
    for client_args in FALLBACK_CLIENTS:
        try:
            opts = dict(YDL_SEARCH_OPTS)
            opts["extractor_args"] = client_args

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                if info and "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    if entries:
                        e = entries[0]
                        thumbs = e.get("thumbnails", [])
                        thumb = thumbs[-1]["url"] if thumbs else None
                        return {
                            "title": e.get("title", query),
                            "url": e.get("webpage_url", ""),
                            "thumbnail": thumb,
                            "duration": e.get("duration", 0),
                            "uploader": e.get("uploader", "Unknown"),
                        }
        except Exception:
            continue
    return None


def parse_song_info_stream(info: dict, stream_url: str) -> dict:
    title = info.get("title", "Unknown")
    uploader = info.get("uploader", "Unknown Artist")

    sn = title
    an = uploader

    for sep in [" - ", " — ", " – "]:
        if sep in title:
            p = title.split(sep, 1)
            an, sn = p[0].strip(), p[1].strip()
            break

    tags = [
        "(Official Audio)", "(Official Video)", "(Official Music Video)",
        "[Official Audio]", "[Official Video]", "(Lyric Video)",
        "(Audio)", "(HD)", "(Full Song)", "[Full Song]",
        "(Lyrics)", "[Lyrics]", "(Official)", "[Official]",
        "(Full Audio)", "[Full Audio]", "(Official Lyric Video)",
        "[Official Music Video]", "(Music Video)", "[Music Video]",
    ]
    for t in tags:
        sn = sn.replace(t, "").replace(t.lower(), "").strip()
        an = an.replace(t, "").replace(t.lower(), "").strip()

    thumbs = info.get("thumbnails", [])
    thumb = thumbs[-1]["url"] if thumbs else None
    dur = info.get("duration") or 0

    return {
        "name": sn or title,
        "artist": an or uploader,
        "album": "YouTube Music",
        "duration_sec": dur,
        "thumbnail": thumb,
        "stream_url": stream_url,
        "title": title,
        "webpage_url": info.get("webpage_url", ""),
    }

# ============================================
# FFmpeg source from URL - smooth streaming
# ============================================
FFMPEG_BEFORE = (
    "-nostdin -hide_banner -loglevel error "
    "-reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 "
    "-probesize 200000 -analyzeduration 200000"
)
FFMPEG_OPTS = "-vn -sn -dn -b:a 192k"


def make_source(stream_url: str):
    return discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(
            stream_url,
            executable=FFMPEG_PATH,
            before_options=FFMPEG_BEFORE,
            options=FFMPEG_OPTS,
        ),
        volume=0.65,
    )

# ============================================
# Embed builder - Professional design
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
    filled = int(18 * ratio)
    bar = "▰" * filled + "▱" * (18 - filled)

    config = {
        "playing": {
            "color": discord.Color.from_rgb(30, 215, 96),
            "status_icon": "▶️",
            "status_text": "Now Playing",
            "dot": "🟢",
        },
        "paused": {
            "color": discord.Color.from_rgb(255, 193, 7),
            "status_icon": "⏸️",
            "status_text": "Paused",
            "dot": "🟡",
        },
        "stopped": {
            "color": discord.Color.from_rgb(220, 53, 69),
            "status_icon": "⏹️",
            "status_text": "Stopped",
            "dot": "🔴",
        },
    }
    c = config.get(status, config["playing"])

    # Playlist info
    pl_info = ""
    if data.get("playlist_mode"):
        pl_idx = data.get("playlist_index", 0) + 1
        pl_total = data.get("playlist_total", 0)
        pl_type = data.get("playlist_type", "")
        type_label = {"start": "▶ Sequential", "end": "◀ Reverse", "random": "🔀 Shuffle"}.get(pl_type, "")
        pl_info = f"\n📋 Playlist: **{pl_idx}/{pl_total}** • {type_label}"

    em = discord.Embed(color=c["color"])
    em.set_author(name=f"{c['status_icon']} {c['status_text']}", icon_url="https://i.imgur.com/jbPMuCO.png")

    em.add_field(
        name="🎵 Song",
        value=f"**{song['name']}**",
        inline=True,
    )
    em.add_field(
        name="🎤 Artist",
        value=f"{song['artist']}",
        inline=True,
    )
    em.add_field(
        name=f"{c['dot']} Status",
        value=f"{c['status_text']}",
        inline=True,
    )

    elapsed_str = str(timedelta(seconds=el))
    if elapsed_str.startswith("0:"):
        elapsed_str = elapsed_str[2:]
    dur_str = str(timedelta(seconds=dur))
    if dur_str.startswith("0:"):
        dur_str = dur_str[2:]
    rem_str = str(timedelta(seconds=rem))
    if rem_str.startswith("0:"):
        rem_str = rem_str[2:]

    em.add_field(
        name="⏱️ Progress",
        value=f"`{elapsed_str}` {bar} `{dur_str}`\n⏳ Remaining: **{rem_str}**{pl_info}",
        inline=False,
    )

    if song.get("thumbnail"):
        em.set_thumbnail(url=song["thumbnail"])

    em.set_footer(text="🎧 Music Bot  •  /song  •  /playlist  •  /addplay")

    return em

# ============================================
# Helper
# ============================================
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
        channel = data.get("text_channel")
        playlist_mode = data.get("playlist_mode", False)
        playlist_order = data.get("playlist_order", [])
        playlist_index = data.get("playlist_index", 0)
        playlist_type = data.get("playlist_type", "")
        playlist_played = data.get("playlist_played", set())
        vc_channel = vc.channel if vc and vc.is_connected() else None

        # Check if should auto-play next from playlist
        next_query = None
        next_index = 0
        next_played = set(playlist_played)

        guild_pl = get_guild_playlist(gid)

        if guild_pl and vc_channel:
            if playlist_mode and playlist_order:
                # Playing from /playlist command
                ni = playlist_index + 1
                if ni < len(playlist_order):
                    next_query = playlist_order[ni]
                    next_index = ni
                elif playlist_type == "random":
                    # Restart random cycle
                    new_order = list(guild_pl)
                    random.shuffle(new_order)
                    playlist_order = new_order
                    next_query = playlist_order[0]
                    next_index = 0
                    next_played = set()
                else:
                    # Restart sequential
                    if playlist_type == "start":
                        playlist_order = list(guild_pl)
                    else:
                        playlist_order = list(reversed(guild_pl))
                    next_query = playlist_order[0]
                    next_index = 0
                    next_played = set()
            elif not playlist_mode and guild_pl:
                # Auto-play from default playlist after /song ends
                playlist_order = list(guild_pl)
                next_query = playlist_order[0]
                next_index = 0
                playlist_mode = True
                playlist_type = "start"
                next_played = set()

        music_data.pop(gid, None)

        if next_query and vc_channel:
            # Auto-play next song
            if msg:
                await safe_edit(msg, embed=discord.Embed(
                    title="⏭️ Loading Next Song",
                    description=f"```{next_query}```",
                    color=discord.Color.blue(),
                ), view=discord.ui.View())

            await _play_next(
                gid, vc, vc_channel, channel, next_query,
                playlist_mode, playlist_order, next_index,
                playlist_type, next_played, msg
            )
        else:
            # No playlist - end
            if vc:
                with suppress(Exception):
                    if vc.is_connected():
                        await vc.disconnect()
            if msg:
                if err:
                    em = discord.Embed(
                        title="❌ Playback Error",
                        description="An error occurred.",
                        color=discord.Color.red(),
                    )
                else:
                    em = discord.Embed(
                        title="✅ Song Finished",
                        color=discord.Color.from_rgb(30, 215, 96),
                    )
                    if song:
                        em.add_field(name="🎵 Song", value=f"**{song['name']}**", inline=True)
                        em.add_field(name="🎤 Artist", value=song["artist"], inline=True)
                        if song.get("thumbnail"):
                            em.set_thumbnail(url=song["thumbnail"])
                    em.set_footer(text="Use /song or /playlist to play more music")
                await safe_edit(msg, embed=em, view=discord.ui.View())


async def _play_next(gid, vc, vc_channel, text_channel, query,
                     playlist_mode, playlist_order, playlist_index,
                     playlist_type, playlist_played, old_msg):
    """Internal: play next song in playlist chain."""
    loop = asyncio.get_running_loop()
    song = await loop.run_in_executor(None, extract_song_url, query)

    if not song:
        # Skip this song, try next
        ni = playlist_index + 1
        if ni < len(playlist_order):
            await _play_next(
                gid, vc, vc_channel, text_channel,
                playlist_order[ni], playlist_mode, playlist_order,
                ni, playlist_type, playlist_played, old_msg
            )
        else:
            with suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()
            if old_msg:
                await safe_edit(old_msg, embed=discord.Embed(
                    title="📋 Playlist Finished",
                    description="All songs have been played!",
                    color=discord.Color.blue(),
                ), view=discord.ui.View())
        return

    try:
        source = make_source(song["stream_url"])
    except Exception:
        # Skip
        ni = playlist_index + 1
        if ni < len(playlist_order):
            await _play_next(
                gid, vc, vc_channel, text_channel,
                playlist_order[ni], playlist_mode, playlist_order,
                ni, playlist_type, playlist_played, old_msg
            )
        return

    sid = uuid.uuid4().hex

    music_data[gid] = {
        "sid": sid,
        "vc": vc,
        "song_info": song,
        "is_paused": False,
        "start_time": time.time(),
        "pause_time": None,
        "total_paused": 0.0,
        "message": old_msg,
        "text_channel": text_channel,
        "playlist_mode": playlist_mode,
        "playlist_order": playlist_order,
        "playlist_index": playlist_index,
        "playlist_total": len(playlist_order),
        "playlist_type": playlist_type,
        "playlist_played": playlist_played,
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
    cur = music_data.get(gid)
    if not cur or cur.get("sid") != sid:
        return

    view = Controls(song, gid)
    if old_msg:
        await safe_edit(old_msg, embed=make_embed(song, gid, "playing"), view=view)

    bot.loop.create_task(updater(gid, sid, song, view, old_msg))


# ============================================
# Controls View
# ============================================
class Controls(discord.ui.View):
    def __init__(self, song, gid):
        super().__init__(timeout=None)
        self.song = song
        self.gid = gid

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="m_pause")
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

    @discord.ui.button(label="Resume", emoji="▶️", style=discord.ButtonStyle.success, custom_id="m_resume")
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

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="m_stop")
    async def btn_stop(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d:
                return await inter.response.send_message("Nothing playing.", ephemeral=True)
            # Disable playlist auto-play
            d["playlist_mode"] = False
            d["playlist_order"] = []
            await end_session(self.gid, disconnect=True)
            for c in self.children:
                c.disabled = True
            await inter.response.edit_message(embed=make_embed(self.song, self.gid, "stopped"), view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="m_skip")
    async def btn_skip(self, inter, btn):
        async with get_lock(self.gid):
            d = music_data.get(self.gid)
            if not d or not d.get("vc"):
                return await inter.response.send_message("Nothing playing.", ephemeral=True)
            vc = d["vc"]
            if vc.is_playing() or vc.is_paused():
                vc.stop()  # triggers after_cb -> on_track_end -> next song
            await inter.response.send_message("⏭️ Skipping...", ephemeral=True)

    @discord.ui.button(label="New Song", emoji="🔍", style=discord.ButtonStyle.secondary, custom_id="m_new")
    async def btn_new(self, inter, btn):
        await inter.response.send_modal(SongModal(self.gid))


# ============================================
# Song Modal
# ============================================
class SongModal(discord.ui.Modal, title="🎵 Play New Song"):
    inp = discord.ui.TextInput(
        label="Song Name",
        placeholder="Enter song name or YouTube URL...",
        required=True,
        max_length=200,
    )

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
# Addplay confirmation view
# ============================================
class AddPlayConfirm(discord.ui.View):
    def __init__(self, gid: int, song_title: str, user_id: int, user_name: str):
        super().__init__(timeout=60)
        self.gid = gid
        self.song_title = song_title
        self.user_id = user_id
        self.user_name = user_name
        self.responded = False

    @discord.ui.button(label="Yes, Save It", emoji="✅", style=discord.ButtonStyle.success)
    async def btn_yes(self, inter, btn):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Only the requester can confirm.", ephemeral=True)
        if self.responded:
            return
        self.responded = True

        pl = get_guild_playlist(self.gid)
        # Check duplicate
        if self.song_title.lower() not in [s.lower() for s in pl]:
            pl.append(self.song_title)
            save_guild_playlist(self.gid, pl)

        # Build playlist embed
        em = discord.Embed(
            title="✅ Song Added to Playlist",
            color=discord.Color.from_rgb(30, 215, 96),
        )
        em.add_field(name="🎵 Added Song", value=f"**{self.song_title}**", inline=False)
        em.add_field(name="👤 Added By", value=f"{self.user_name}", inline=True)
        em.add_field(name="📋 Total Songs", value=f"**{len(pl)}**", inline=True)

        song_list = ""
        for i, s in enumerate(pl, 1):
            marker = " 🆕" if s == self.song_title else ""
            song_list += f"`{i}.` {s}{marker}\n"
            if i >= 20:
                remaining = len(pl) - 20
                if remaining > 0:
                    song_list += f"\n*...and {remaining} more*"
                break

        em.add_field(name="📜 Current Playlist", value=song_list, inline=False)
        em.set_footer(text="🎧 Use /playlist to play • /delete to remove songs")

        for c in self.children:
            c.disabled = True
        await inter.response.edit_message(embed=em, view=self)

    @discord.ui.button(label="No", emoji="❌", style=discord.ButtonStyle.danger)
    async def btn_no(self, inter, btn):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("Only the requester can cancel.", ephemeral=True)
        if self.responded:
            return
        self.responded = True

        em = discord.Embed(
            title="❌ Cancelled",
            description=f"**{self.song_title}** was not added.",
            color=discord.Color.red(),
        )
        for c in self.children:
            c.disabled = True
        await inter.response.edit_message(embed=em, view=self)

    async def on_timeout(self):
        self.responded = True


# ============================================
# Delete song select menu
# ============================================
class DeleteSelect(discord.ui.Select):
    def __init__(self, gid: int, songs: list):
        self.gid = gid
        options = []
        for i, s in enumerate(songs[:25]):
            options.append(discord.SelectOption(
                label=s[:100],
                value=str(i),
                description=f"Position #{i+1}",
                emoji="🎵",
            ))
        super().__init__(
            placeholder="Select songs to delete...",
            min_values=1,
            max_values=min(len(options), 25),
            options=options,
        )

    async def callback(self, inter):
        pl = get_guild_playlist(self.gid)
        indices = sorted([int(v) for v in self.values], reverse=True)
        removed = []
        for idx in indices:
            if 0 <= idx < len(pl):
                removed.append(pl.pop(idx))
        save_guild_playlist(self.gid, pl)

        em = discord.Embed(
            title="🗑️ Songs Removed",
            color=discord.Color.from_rgb(220, 53, 69),
        )
        em.add_field(
            name="❌ Removed",
            value="\n".join(f"• {r}" for r in removed) or "None",
            inline=False,
        )

        if pl:
            song_list = ""
            for i, s in enumerate(pl, 1):
                song_list += f"`{i}.` {s}\n"
                if i >= 20:
                    remaining = len(pl) - 20
                    if remaining > 0:
                        song_list += f"\n*...and {remaining} more*"
                    break
            em.add_field(name="📜 Updated Playlist", value=song_list, inline=False)
        else:
            em.add_field(name="📜 Playlist", value="*Empty - use /addplay to add songs*", inline=False)

        em.set_footer(text=f"📋 {len(pl)} songs remaining")

        self.disabled = True
        await inter.response.edit_message(embed=em, view=self.view)


class DeleteView(discord.ui.View):
    def __init__(self, gid: int, songs: list):
        super().__init__(timeout=60)
        self.add_item(DeleteSelect(gid, songs))


# ============================================
# Main play function
# ============================================
async def play_song(inter, query: str, vc_channel, playlist_mode=False,
                    playlist_order=None, playlist_index=0,
                    playlist_type="", playlist_played=None):
    guild = inter.guild
    if not guild:
        return await inter.followup.send("Server only.", ephemeral=True)

    gid = guild.id

    async with get_lock(gid):
        msg = await inter.followup.send(
            embed=discord.Embed(
                title="🔍 Searching...",
                description=f"```{query}```",
                color=discord.Color.blue(),
            )
        )

        # Stop current playback but don't disconnect
        old_data = music_data.get(gid)
        if old_data:
            old_data["playlist_mode"] = False
            old_data["playlist_order"] = []
        await end_session(gid, disconnect=False)

        # Connect to voice
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
                title="❌ Voice Connection Error",
                description=f"`{e}`",
                color=discord.Color.red(),
            ))

        # Extract stream URL (no download!)
        loop = asyncio.get_running_loop()
        song = await loop.run_in_executor(None, extract_song_url, query)

        if not song:
            return await safe_edit(msg, embed=discord.Embed(
                title="❌ Song Not Found",
                description="Could not find that song. Try a different name.",
                color=discord.Color.red(),
            ))

        # Create audio source
        try:
            source = make_source(song["stream_url"])
        except Exception as e:
            return await safe_edit(msg, embed=discord.Embed(
                title="❌ Audio Error",
                description=f"`{e}`",
                color=discord.Color.red(),
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
            "text_channel": inter.channel,
            "playlist_mode": playlist_mode,
            "playlist_order": playlist_order or [],
            "playlist_index": playlist_index,
            "playlist_total": len(playlist_order) if playlist_order else 0,
            "playlist_type": playlist_type,
            "playlist_played": playlist_played or set(),
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
                title="❌ Playback Error",
                description=f"`{e}`",
                color=discord.Color.red(),
            ))

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
                title="❌ Playback Failed",
                description="Could not start playback. Try again.",
                color=discord.Color.red(),
            ))

        view = Controls(song, gid)
        await safe_edit(msg, embed=make_embed(song, gid, "playing"), view=view)

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
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="/song • /playlist",
        )
    )


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
# Commands
# ============================================

# /song
@bot.tree.command(name="song", description="🎵 Play a song in your voice channel")
@app_commands.describe(name="Song name or YouTube URL")
async def song_cmd(inter, name: str):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)
    if not inter.user.voice or not inter.user.voice.channel:
        return await inter.response.send_message(
            embed=discord.Embed(
                title="🔇 Join a Voice Channel First",
                description="You need to be in a voice channel to play music.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
    await inter.response.defer(thinking=True)
    await play_song(inter, name, inter.user.voice.channel)


# /stop
@bot.tree.command(name="stop", description="⏹️ Stop playback and disconnect")
async def stop_cmd(inter):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)
    gid = inter.guild.id
    async with get_lock(gid):
        d = music_data.get(gid)
        if not d:
            return await inter.response.send_message("Nothing is playing.", ephemeral=True)
        d["playlist_mode"] = False
        d["playlist_order"] = []
        await end_session(gid, disconnect=True)
        await inter.response.send_message(
            embed=discord.Embed(
                title="⏹️ Stopped",
                description="Playback stopped and disconnected.",
                color=discord.Color.red(),
            )
        )


# /addplay
@bot.tree.command(name="addplay", description="📋 Add song(s) to the default playlist")
@app_commands.describe(name="Song name(s) - separate multiple with commas")
async def addplay_cmd(inter, name: str):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)

    gid = inter.guild.id
    songs = [s.strip() for s in name.split(",") if s.strip()]

    if not songs:
        return await inter.response.send_message("Please provide at least one song name.", ephemeral=True)

    await inter.response.defer(thinking=True)

    loop = asyncio.get_running_loop()

    for song_name in songs:
        # Search for the song
        info = await loop.run_in_executor(None, search_song_info, song_name)

        if not info:
            await inter.followup.send(
                embed=discord.Embed(
                    title="❌ Not Found",
                    description=f"Could not find: **{song_name}**",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            continue

        # Show confirmation with thumbnail
        em = discord.Embed(
            title="🎵 Song Found - Add to Playlist?",
            color=discord.Color.from_rgb(88, 101, 242),
        )
        em.add_field(name="🎵 Title", value=f"**{info['title']}**", inline=False)
        em.add_field(name="🎤 Artist", value=info["uploader"], inline=True)
        if info.get("duration"):
            dur_str = str(timedelta(seconds=info["duration"]))
            if dur_str.startswith("0:"):
                dur_str = dur_str[2:]
            em.add_field(name="⏱️ Duration", value=dur_str, inline=True)

        if info.get("thumbnail"):
            em.set_image(url=info["thumbnail"])

        em.set_footer(text="Click below to confirm")

        view = AddPlayConfirm(gid, info["title"], inter.user.id, inter.user.display_name)
        await inter.followup.send(embed=em, view=view)


# /playlist
class PlaylistOrderChoice(app_commands.Choice[str]):
    pass

@bot.tree.command(name="playlist", description="🎶 Play songs from the default playlist")
@app_commands.describe(order="Choose play order")
@app_commands.choices(order=[
    app_commands.Choice(name="▶ From Starting ↓", value="start"),
    app_commands.Choice(name="◀ From Ending ↑", value="end"),
    app_commands.Choice(name="🔀 Random", value="random"),
])
async def playlist_cmd(inter, order: str):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)
    if not inter.user.voice or not inter.user.voice.channel:
        return await inter.response.send_message(
            embed=discord.Embed(
                title="🔇 Join a Voice Channel First",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    gid = inter.guild.id
    pl = get_guild_playlist(gid)

    if not pl:
        return await inter.response.send_message(
            embed=discord.Embed(
                title="📋 Playlist Empty",
                description="Use `/addplay` to add songs first.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    # Build order
    if order == "start":
        play_order = list(pl)
    elif order == "end":
        play_order = list(reversed(pl))
    elif order == "random":
        play_order = list(pl)
        random.shuffle(play_order)
    else:
        play_order = list(pl)

    await inter.response.defer(thinking=True)

    first_song = play_order[0]
    await play_song(
        inter, first_song, inter.user.voice.channel,
        playlist_mode=True,
        playlist_order=play_order,
        playlist_index=0,
        playlist_type=order,
        playlist_played=set(),
    )


# /delete
@bot.tree.command(name="delete", description="🗑️ Remove song(s) from the playlist")
async def delete_cmd(inter):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)

    gid = inter.guild.id
    pl = get_guild_playlist(gid)

    if not pl:
        return await inter.response.send_message(
            embed=discord.Embed(
                title="📋 Playlist Empty",
                description="Nothing to delete.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    em = discord.Embed(
        title="🗑️ Select Songs to Delete",
        description="Choose one or more songs to remove from the playlist.",
        color=discord.Color.from_rgb(220, 53, 69),
    )

    song_list = ""
    for i, s in enumerate(pl, 1):
        song_list += f"`{i}.` {s}\n"
        if i >= 25:
            break

    em.add_field(name="📜 Current Playlist", value=song_list, inline=False)
    em.set_footer(text=f"📋 {len(pl)} songs total")

    view = DeleteView(gid, pl)
    await inter.response.send_message(embed=em, view=view, ephemeral=True)


# /showplaylist - bonus command to view playlist
@bot.tree.command(name="showplaylist", description="📜 View the current playlist")
async def showplaylist_cmd(inter):
    if not inter.guild:
        return await inter.response.send_message("Server only.", ephemeral=True)

    gid = inter.guild.id
    pl = get_guild_playlist(gid)

    em = discord.Embed(
        title="📜 Server Playlist",
        color=discord.Color.from_rgb(88, 101, 242),
    )

    if not pl:
        em.description = "*Empty - use `/addplay` to add songs*"
    else:
        song_list = ""
        for i, s in enumerate(pl, 1):
            song_list += f"`{i}.` 🎵 {s}\n"
            if i >= 30:
                remaining = len(pl) - 30
                if remaining > 0:
                    song_list += f"\n*...and {remaining} more*"
                break
        em.add_field(name=f"🎶 Songs ({len(pl)} total)", value=song_list, inline=False)

    em.set_footer(text="🎧 /playlist to play • /addplay to add • /delete to remove")
    await inter.response.send_message(embed=em)


# ============================================
# Start
# ============================================
print("Starting bot...")
bot.run(DISCORD_TOKEN, log_level=logging.ERROR)
