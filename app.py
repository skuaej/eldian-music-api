import os
import sys
import json
import glob
import tempfile
import urllib.parse
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel
import time
import requests
import yt_dlp
import pymongo
from pymongo.errors import PyMongoError
from config import (
    PORT, HOST, PUBLIC_URL, MONGO_URI, MONGO_DB_NAME, MONGO_COLLECTION_NAME,
    CACHE_EXPIRY_SECONDS, load_proxy_pool, load_cookie_pool
)

app = FastAPI(
    title="yt-dlp Video JSON & Direct Link API",
    description="Extracts YouTube stream links, format specifications, metadata, and paired video+audio streams up to 8K with sound.",
    version="2.0.0"
)

# ==========================================
# MONGODB CLIENT SETUP & CACHE HELPERS
# ==========================================
mongo_client = None
mongo_coll = None

try:
    mongo_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
    mongo_db = mongo_client[MONGO_DB_NAME]
    mongo_coll = mongo_db[MONGO_COLLECTION_NAME]
    # Create indexes for fast lookup and TTL expiration
    mongo_coll.create_index("url_key", unique=True)
    mongo_coll.create_index("created_at")
except Exception as e:
    print(f"MongoDB connection warning (cache will run in memory/bypass): {e}")
    mongo_coll = None

def get_cached_metadata(url_key: str) -> Optional[Dict[str, Any]]:
    """
    Checks if format metadata is already cached in MongoDB and not expired.
    """
    if mongo_coll is None:
        return None
    try:
        doc = mongo_coll.find_one({"url_key": url_key})
        if doc:
            created_at = doc.get("created_at", 0)
            if time.time() - created_at < CACHE_EXPIRY_SECONDS:
                cached_data = doc.get("data")
                if cached_data:
                    return cached_data
    except Exception as e:
        print(f"MongoDB cache read error: {e}")
    return None

def save_cached_metadata(url_key: str, data: Dict[str, Any]):
    """
    Saves extracted metadata to MongoDB with current timestamp.
    """
    if mongo_coll is None:
        return
    try:
        mongo_coll.update_one(
            {"url_key": url_key},
            {"$set": {"url_key": url_key, "data": data, "created_at": time.time()}},
            upsert=True
        )
    except Exception as e:
        print(f"MongoDB cache write error: {e}")

# ==========================================
# ROTATING PROXIES & COOKIES POOL
# ==========================================
class FailoverManager:
    def __init__(self):
        self.proxy_index = 0
        self.cookie_index = 0
        self.proxies = load_proxy_pool()
        self.cookies = load_cookie_pool()

    def refresh(self):
        self.proxies = load_proxy_pool()
        self.cookies = load_cookie_pool()

    def get_current_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        return self.proxies[self.proxy_index % len(self.proxies)]

    def rotate_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        self.proxy_index += 1
        chosen = self.proxies[self.proxy_index % len(self.proxies)]
        print(f"[FAILOVER] Switched to proxy: {chosen}")
        return chosen

    def get_current_cookie(self) -> Optional[str]:
        if not self.cookies:
            return None
        return self.cookies[self.cookie_index % len(self.cookies)]

    def rotate_cookie(self) -> Optional[str]:
        if not self.cookies:
            return None
        self.cookie_index += 1
        chosen = self.cookies[self.cookie_index % len(self.cookies)]
        print(f"[FAILOVER] Switched to cookie file: {chosen}")
        return chosen

failover = FailoverManager()

def get_effective_base_url(request: Optional[Request] = None) -> str:
    if PUBLIC_URL:
        url = PUBLIC_URL.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        return url.rstrip('/')
    if request:
        proto = request.headers.get("x-forwarded-proto")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if host:
            scheme = proto if proto else request.url.scheme
            return f"{scheme}://{host}".rstrip('/')
        return str(request.base_url).rstrip('/')
    return "http://127.0.0.1:8000"

def format_bytes(size: Optional[int]) -> Optional[str]:
    if not size:
        return None
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"

def format_duration(seconds: Optional[int]) -> Optional[str]:
    if not seconds:
        return None
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"

def get_ydl_opts(proxy: Optional[str] = None, cookiefile: Optional[str] = None, use_cookies: bool = True):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["web_embedded", "web_safari", "android"]
            }
        },
        "js_runtimes": {
            "node": {},
            "deno": {}
        },
        "skip_download": True,
    }

    if proxy:
        opts["proxy"] = proxy

    if use_cookies and cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile

    return opts

class InfoDataRequest(BaseModel):
    url: str
    disable_proxy: Optional[bool] = False

def find_best_audio(audio_formats: List[Dict[str, Any]], target_ext: str = "mp4") -> Optional[Dict[str, Any]]:
    if not audio_formats:
        return None

    preferred = [
        a for a in audio_formats 
        if (target_ext == "mp4" and (a.get("ext") == "m4a" or "mp4a" in str(a.get("acodec")))) or
           (target_ext == "webm" and (a.get("ext") == "webm" or "opus" in str(a.get("acodec"))))
    ]

    candidates = preferred if preferred else audio_formats

    def sort_key(a):
        abr = a.get("abr") or 0
        tbr = a.get("tbr") or 0
        return max(abr, tbr)

    return max(candidates, key=sort_key)

def make_stream_url(direct_url: Optional[str], base_url: str = "http://127.0.0.1:8000") -> Optional[str]:
    if not direct_url:
        return None
    encoded = urllib.parse.quote(direct_url, safe='')
    return f"{base_url}/api/stream?url={encoded}"

def get_clean_quality_label(height: Optional[int], fps: Optional[int], raw_note: Optional[str]) -> str:
    fps_suffix = f" {fps}fps" if fps and fps > 30 else ""
    if height:
        if height >= 4320:
            prefix = "8K (4320p)"
        elif height >= 2160:
            prefix = "4K (2160p)"
        elif height >= 1440:
            prefix = "1440p (2K)"
        elif height >= 1080:
            prefix = "1080p (FHD)"
        elif height >= 720:
            prefix = "720p (HD)"
        else:
            prefix = f"{height}p"
        return f"{prefix}{fps_suffix}".strip()
    return raw_note or "Unknown"

def get_or_create_merged_video(url: str, quality: str, disable_proxy: bool = False) -> str:
    """
    Downloads and merges video + audio into a single MP4 with sound using yt-dlp + FFmpeg.
    Caches the file locally so repeated clicks on stream_url or download_url are instant.
    """
    selected_proxy = None if disable_proxy else failover.get_current_proxy()
    selected_cookie = failover.get_current_cookie()
    temp_dir = os.path.join(tempfile.gettempdir(), "ytdlp_downloads")
    os.makedirs(temp_dir, exist_ok=True)

    height_map = {
        "8k": "4320", "4320p": "4320",
        "4k": "2160", "2160p": "2160",
        "2k": "1440", "1440p": "1440",
        "1080p": "1080", "720p": "720",
        "480p": "480", "360p": "360",
        "240p": "240", "144p": "144",
    }
    h = height_map.get(quality.lower().strip(), "720")

    # Check cache by extracting basic info
    opts_meta = get_ydl_opts(proxy=selected_proxy, cookiefile=selected_cookie)
    with yt_dlp.YoutubeDL(opts_meta) as ydl:
        info = ydl.extract_info(url, download=False)
        video_id = info.get("id") or "video"

    cached_target = os.path.join(temp_dir, f"{video_id}_{h}p.mp4")
    if os.path.exists(cached_target) and os.path.getsize(cached_target) > 1000:
        return cached_target

    # Prioritize H.264 (AVC) video and AAC (MP4A) audio for universal compatibility with all PC players and browsers
    format_spec = (
        f"bestvideo[height<={h}][vcodec^=avc]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[height<={h}][vcodec^=avc]+bestaudio/"
        f"bestvideo[height<={h}]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    )
    out_template = os.path.join(temp_dir, f"{video_id}_{h}p.%(ext)s")

    opts = get_ydl_opts(proxy=selected_proxy, cookiefile=selected_cookie)
    opts.update({
        "format": format_spec,
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "skip_download": False,
        "postprocessor_args": {
            "merger": [
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart"
            ]
        }
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    if os.path.exists(cached_target):
        return cached_target

    matches = glob.glob(os.path.join(temp_dir, f"{video_id}_{h}p.*"))
    if matches:
        return matches[0]

    raise HTTPException(status_code=500, detail="Failed to create merged video with audio.")

def get_or_create_audio(url: str, format_id: str, disable_proxy: bool = False) -> str:
    """
    Downloads and extracts an audio track reliably with MP3 transcoding so it never times out or drops.
    """
    selected_proxy = None if disable_proxy else failover.get_current_proxy()
    selected_cookie = failover.get_current_cookie()
    temp_dir = os.path.join(tempfile.gettempdir(), "ytdlp_downloads")
    os.makedirs(temp_dir, exist_ok=True)

    opts_meta = get_ydl_opts(proxy=selected_proxy, cookiefile=selected_cookie)
    with yt_dlp.YoutubeDL(opts_meta) as ydl:
        info = ydl.extract_info(url, download=False)
        video_id = info.get("id") or "video"

    cached_target = os.path.join(temp_dir, f"{video_id}_audio_{format_id}.mp3")
    if os.path.exists(cached_target) and os.path.getsize(cached_target) > 1000:
        return cached_target

    out_template = os.path.join(temp_dir, f"{video_id}_audio_{format_id}.%(ext)s")
    opts = get_ydl_opts(proxy=selected_proxy, cookiefile=selected_cookie)
    opts.update({
        "format": format_id,
        "outtmpl": out_template,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "skip_download": False,
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    if os.path.exists(cached_target):
        return cached_target

    matches = glob.glob(os.path.join(temp_dir, f"{video_id}_audio_{format_id}.*"))
    if matches:
        return matches[0]

    raise HTTPException(status_code=500, detail="Failed to extract audio track.")


def extract_all_infodata(url: str, disable_proxy: bool = False, base_url: str = "http://127.0.0.1:8000"):
    encoded_video_url = urllib.parse.quote(url, safe='')
    cache_key = f"{url}_base_{base_url}"

    # 1. CHECK MONGODB CACHE (Instant response & prevents rate limits)
    cached = get_cached_metadata(cache_key)
    if cached:
        return cached

    # 2. FAILOVER RETRY LOOP ACROSS ROTATING PROXIES AND COOKIES
    failover.refresh()
    proxies_to_try = [None] if disable_proxy else (failover.proxies if failover.proxies else [None])
    cookies_to_try = failover.cookies if failover.cookies else [None]

    last_error = None
    info = None

    for attempt in range(max(len(proxies_to_try) * len(cookies_to_try), 1)):
        active_proxy = None if disable_proxy else failover.get_current_proxy()
        active_cookie = failover.get_current_cookie()
        ydl_opts = get_ydl_opts(proxy=active_proxy, cookiefile=active_cookie)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and info.get("formats"):
                    break
        except Exception as e:
            err_msg = str(e)
            print(f"[FAILOVER] Error on proxy {active_proxy}, cookie {active_cookie}: {err_msg}")
            last_error = err_msg
            # Rotate cookie on bot check / sign in / rate limit
            if "Sign in" in err_msg or "bot" in err_msg.lower() or "rate-limit" in err_msg.lower() or "429" in err_msg:
                failover.rotate_cookie()
            # Rotate proxy on network/connect/timeout/403 errors
            failover.rotate_proxy()

    if not info:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to extract video information after proxy/cookie failover. Error: {last_error}"
        )

    raw_formats = info.get("formats", [])
    audio_formats = []
    video_formats = []
    combined_formats = []
    all_formats = []
    encoded_video_url = urllib.parse.quote(url, safe='')

    for f in raw_formats:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        filesize = f.get("filesize") or f.get("filesize_approx")
        raw_url = f.get("url")
        height = f.get("height")
        fps = f.get("fps")

        format_entry = {
            "format_id": f.get("format_id"),
            "format_note": f.get("format_note"),
            "quality_label": get_clean_quality_label(height, fps, f.get("format_note")),
            "ext": f.get("ext"),
            "resolution": f.get("resolution"),
            "width": f.get("width"),
            "height": height,
            "fps": fps,
            "dynamic_range": f.get("dynamic_range"),
            "vcodec": vcodec,
            "acodec": acodec,
            "abr": f.get("abr"),
            "vbr": f.get("vbr"),
            "tbr": f.get("tbr"),
            "asr": f.get("asr"),
            "audio_channels": f.get("audio_channels"),
            "filesize": filesize,
            "filesize_formatted": format_bytes(filesize),
            "container": f.get("container"),
            "protocol": f.get("protocol"),
            "url": raw_url,
            "stream_url": make_stream_url(raw_url, base_url),
            "http_headers": f.get("http_headers"),
        }

        all_formats.append(format_entry)

        is_video = vcodec not in (None, "none")
        is_audio = acodec not in (None, "none")

        if is_video and is_audio:
            combined_formats.append(format_entry)
        elif is_video and not is_audio:
            video_formats.append(format_entry)
        elif is_audio and not is_video:
            audio_entry = dict(format_entry)
            # Stable audio stream that doesn't drop or time out
            audio_entry["stream_url"] = f"{base_url}/api/stream_audio?url={encoded_video_url}&format_id={f.get('format_id')}"
            audio_entry["raw_proxy_url"] = format_entry["stream_url"]
            audio_formats.append(audio_entry)

    # 1. SORT AUDIO FORMATS (Highest audio bitrate/quality first)
    audio_formats.sort(
        key=lambda a: (a.get("abr") or 0, a.get("tbr") or 0, a.get("filesize") or 0),
        reverse=True
    )

    # 2. SORT VIDEO FORMATS UP TO 8K (Highest resolution 8K -> 4K -> 2K -> 1080p -> ... -> 144p)
    video_formats.sort(
        key=lambda v: (
            v.get("height") or 0,
            v.get("width") or 0,
            v.get("fps") or 0,
            v.get("tbr") or v.get("vbr") or 0
        ),
        reverse=True
    )

    # 3. SORT COMBINED LEGACY FORMATS (Highest resolution first)
    combined_formats.sort(
        key=lambda c: (c.get("height") or 0, c.get("tbr") or 0),
        reverse=True
    )

    paired_streams = []
    title_sanitized = "".join(c for c in (info.get("title") or "video") if c.isalnum() or c in (' ', '_', '-')).rstrip()
    encoded_video_url = urllib.parse.quote(url, safe='')

    for v in video_formats:
        v_ext = v.get("ext") or "mp4"
        matching_audio = find_best_audio(audio_formats, target_ext=v_ext)

        v_size = v.get("filesize") or 0
        a_size = (matching_audio.get("filesize") or 0) if matching_audio else 0
        total_size = (v_size + a_size) if (v_size and a_size) else (v_size or a_size or None)

        h = v.get("height") or 720
        clean_label = v.get("quality_label")
        filename = f"{title_sanitized}_{h}p.{v_ext}"

        v_url = v.get("url")
        a_url = matching_audio.get("url") if matching_audio else None

        ffmpeg_cmd = (
            f'ffmpeg -i "{v_url}" -i "{a_url}" -c copy -y "{filename}"'
            if v_url and a_url else None
        )

        # Combined streaming endpoint (plays inline with sound!)
        stream_with_sound_url = f"{base_url}/api/stream_video?url={encoded_video_url}&quality={h}p"
        # Combined download endpoint (saves file with sound!)
        download_with_sound_url = f"{base_url}/api/download?url={encoded_video_url}&quality={h}p"

        paired_streams.append({
            "quality_label": clean_label,
            "resolution": v.get("resolution"),
            "width": v.get("width"),
            "height": v.get("height"),
            "fps": v.get("fps"),
            "container": "mp4",
            "dynamic_range": v.get("dynamic_range"),
            "estimated_total_filesize": total_size,
            "estimated_total_filesize_formatted": format_bytes(total_size),
            # STREAM DIRECTLY IN BROWSER WITH SOUND (COMBINED VIDEO + AUDIO):
            "stream_url": stream_with_sound_url,
            "stream_with_sound_url": stream_with_sound_url,
            "download_with_sound_url": download_with_sound_url,
            "video": {
                "format_id": v.get("format_id"),
                "vcodec": v.get("vcodec"),
                "vbr": v.get("vbr"),
                "filesize": v_size,
                "filesize_formatted": format_bytes(v_size),
                "url": v_url,
                "stream_url_video_only": make_stream_url(v_url, base_url),
            },
            "audio": {
                "format_id": matching_audio.get("format_id") if matching_audio else None,
                "acodec": matching_audio.get("acodec") if matching_audio else None,
                "abr": matching_audio.get("abr") if matching_audio else None,
                "filesize": a_size if matching_audio else None,
                "filesize_formatted": format_bytes(a_size) if matching_audio else None,
                "url": a_url,
                "stream_url_audio_only": make_stream_url(a_url, base_url),
            } if matching_audio else None,
            "ffmpeg_command": ffmpeg_cmd
        })

    result_data = {
        "success": True,
        "metadata": {
            "id": info.get("id"),
            "title": info.get("title"),
            "description": info.get("description"),
            "duration": info.get("duration"),
            "duration_formatted": format_duration(info.get("duration")),
            "uploader": info.get("uploader"),
            "uploader_id": info.get("uploader_id"),
            "uploader_url": info.get("uploader_url"),
            "channel": info.get("channel"),
            "channel_id": info.get("channel_id"),
            "channel_url": info.get("channel_url"),
            "upload_date": info.get("upload_date"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "thumbnail": info.get("thumbnail"),
            "thumbnails": info.get("thumbnails", []),
            "webpage_url": info.get("webpage_url"),
            "tags": info.get("tags", []),
            "categories": info.get("categories", []),
        },
        "summary": {
            "total_audio_formats": len(audio_formats),
            "total_video_formats_upto_8k": len(video_formats),
            "total_paired_video_audio": len(paired_streams),
            "total_combined_legacy": len(combined_formats),
            "max_resolution_available": video_formats[0].get("quality_label") if video_formats else None,
            "best_audio_bitrate": f"{audio_formats[0].get('abr')} kbps" if audio_formats and audio_formats[0].get('abr') else None,
        },
        # 1. AUDIO FIRST (Arranged from highest quality/bitrate to lowest)
        "audio_formats": audio_formats,
        # 2. VIDEO FORMATS UP TO 8K (Arranged from 8K / 4K / 2K / 1080p down to 144p)
        "video_formats_upto_8k": video_formats,
        # 3. PAIRED VIDEO + AUDIO (With sound combined in stream_url!)
        "paired_video_audio": paired_streams,
        # 4. COMBINED PROGRESSIVE (Legacy single-file formats with audio)
        "combined_formats": combined_formats,
        # 5. ALL RAW FORMAT ENTRIES
        "all_formats": all_formats
    }

    # Save to MongoDB before links expire
    save_cached_metadata(cache_key, result_data)
    return result_data

@app.get("/")
@app.get("/index.html")
def root_web():
    index_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "online", "message": "index.html not found"}

@app.get("/api/status")
def api_status(request: Request):
    base_url = get_effective_base_url(request)
    return {
        "status": "online",
        "service": "yt-dlp Video & Audio Streaming API (Railway / Koyeb / Render)",
        "server_base_url": base_url,
        "diagnostics": {
            "mongodb_connected": mongo_coll is not None,
            "mongodb_database": MONGO_DB_NAME,
            "proxies_loaded": len(failover.proxies),
            "cookies_loaded": [os.path.basename(c) for c in failover.cookies],
            "active_proxy": failover.get_current_proxy(),
            "active_cookie": os.path.basename(failover.get_current_cookie()) if failover.get_current_cookie() else None
        },
        "endpoints": {
            "web_ui": f"{base_url}/",
            "streams_only_audio_and_video": f"{base_url}/api/streams?url=<YOUTUBE_URL>",
            "infodata_all_formats": f"{base_url}/infodata?url=<URL>",
            "api_infodata": f"{base_url}/api/infodata?url=<URL>",
            "stream_video_with_sound": f"{base_url}/api/stream_video?url=<URL>&quality=1080p",
            "download_with_sound": f"{base_url}/api/download?url=<URL>&quality=1080p",
            "stream_audio": f"{base_url}/api/stream_audio?url=<URL>&format_id=140",
            "search": f"{base_url}/api/search?q=<QUERY>",
            "docs": f"{base_url}/docs"
        }
    }

@app.get("/api/search")
def search_youtube(
    q: str = Query(..., description="Search keyword like 'AD KLAYI'"),
    limit: int = Query(8, description="Number of results to return"),
    disable_proxy: bool = Query(False, description="Set to true to bypass proxy")
):
    """
    Search YouTube by keywords instantly via direct InnerTube endpoint with yt-dlp fallback.
    """
    # 1. Ultra-fast direct YouTube InnerTube query (< 1 second response)
    try:
        data = json.dumps({
            "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}},
            "query": q
        }).encode("utf-8")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json"
        }
        
        # Use direct connection or active rotating proxy
        active_p = None if disable_proxy else failover.get_current_proxy()
        proxies = {"http": active_p, "https": active_p} if active_p else None
        resp = requests.post(
            "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
            data=data,
            headers=headers,
            proxies=proxies,
            timeout=6
        )
        if resp.status_code == 200:
            resp_json = resp.json()
            items = []
            contents = resp_json.get("contents", {}).get("twoColumnSearchResultsRenderer", {}).get("primaryContents", {}).get("sectionListRenderer", {}).get("contents", [])
            for section in contents:
                item_section = section.get("itemSectionRenderer", {}).get("contents", [])
                for c in item_section:
                    v = c.get("videoRenderer")
                    if v and v.get("videoId"):
                        vid = v.get("videoId")
                        title = v.get("title", {}).get("runs", [{}])[0].get("text") or "Video"
                        channel = v.get("ownerText", {}).get("runs", [{}])[0].get("text") or "YouTube"
                        duration_str = v.get("lengthText", {}).get("simpleText") or ""
                        views_str = v.get("shortViewCountText", {}).get("simpleText") or ""
                        items.append({
                            "id": vid,
                            "title": title,
                            "channel": channel,
                            "duration_formatted": duration_str,
                            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "view_count_text": views_str
                        })
                        if len(items) >= limit:
                            break
                if len(items) >= limit:
                    break
            if items:
                return {"query": q, "count": len(items), "results": items}
    except Exception as e:
        pass

    # 2. Fallback to yt-dlp flat extraction
    selected_proxy = None if disable_proxy else failover.get_current_proxy()
    selected_cookie = failover.get_current_cookie()
    opts = get_ydl_opts(proxy=selected_proxy, cookiefile=selected_cookie)
    opts.update({
        "extract_flat": True,
        "skip_download": True,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
            results = []
            for entry in res.get("entries", []):
                if not entry:
                    continue
                vid_id = entry.get("id")
                dur = entry.get("duration")
                results.append({
                    "id": vid_id,
                    "title": entry.get("title"),
                    "channel": entry.get("uploader") or entry.get("channel"),
                    "duration": dur,
                    "duration_formatted": format_duration(dur),
                    "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "view_count": entry.get("view_count"),
                })
            return {"query": q, "count": len(results), "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.get("/api/stream_video")
@app.get("/api/stream_merged")
def stream_video_with_sound(
    url: str = Query(..., description="YouTube video URL or ID"),
    quality: str = Query("720p", description="Target quality: 8k, 4k, 2k, 1080p, 720p, 480p, 360p"),
    disable_proxy: bool = Query(False, description="Set to true to bypass proxy")
):
    """
    Streams the combined video WITH AUDIO track directly in the browser video player (inline).
    """
    try:
        file_path = get_or_create_merged_video(url, quality, disable_proxy)
        return FileResponse(
            path=file_path,
            media_type="video/mp4",
            content_disposition_type="inline"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Streaming error: {str(e)}")

@app.get("/api/download")
def download_merged_video(
    url: str = Query(..., description="YouTube video URL or ID"),
    quality: str = Query("1080p", description="Target quality: 8k, 4k, 2k, 1080p, 720p, 480p, 360p, best"),
    disable_proxy: bool = Query(False, description="Set to true to bypass proxy")
):
    """
    Downloads the merged video WITH AUDIO track as an MP4 attachment.
    """
    try:
        file_path = get_or_create_merged_video(url, quality, disable_proxy)
        return FileResponse(
            path=file_path,
            filename=os.path.basename(file_path),
            media_type="video/mp4",
            content_disposition_type="attachment"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download error: {str(e)}")

@app.get("/api/stream_audio")
def stream_audio_track(
    url: str = Query(..., description="YouTube video URL or ID"),
    format_id: str = Query("140", description="Audio format ID"),
    disable_proxy: bool = Query(False, description="Set to true to bypass proxy")
):
    """
    Streams extracted audio track as MP3 directly without connection drops.
    """
    try:
        file_path = get_or_create_audio(url, format_id, disable_proxy)
        return FileResponse(
            path=file_path,
            media_type="audio/mpeg",
            content_disposition_type="inline"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio error: {str(e)}")


@app.get("/api/stream")
def proxy_raw_stream(
    url: str = Query(..., description="GoogleVideo stream URL"),
    request: Request = None
):
    """
    Direct proxy for individual raw streams (audio-only or video-only) to bypass 403 errors.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if request:
        range_header = request.headers.get("range")
        if range_header:
            headers["Range"] = range_header

    active_proxy = failover.get_current_proxy()
    proxies = {
        "http": active_proxy,
        "https": active_proxy,
    } if active_proxy else None

    try:
        req = requests.get(url, headers=headers, proxies=proxies, stream=True, timeout=30)
        
        response_headers = {
            "Content-Type": req.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",
        }
        if "Content-Length" in req.headers:
            response_headers["Content-Length"] = req.headers["Content-Length"]
        if "Content-Range" in req.headers:
            response_headers["Content-Range"] = req.headers["Content-Range"]

        def iter_stream():
            try:
                for chunk in req.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        yield chunk
            finally:
                req.close()

        return StreamingResponse(
            iter_stream(),
            status_code=req.status_code,
            headers=response_headers
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy stream error: {str(e)}")

@app.get("/infodata")
@app.get("/api/infodata")
def get_infodata(
    url: str = Query(..., description="YouTube video URL or ID"),
    disable_proxy: bool = Query(False, description="Set to true to bypass proxy"),
    request: Request = None
):
    base_url = get_effective_base_url(request)
    try:
        return extract_all_infodata(url, disable_proxy, base_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/infodata")
@app.post("/api/infodata")
def post_infodata(req: InfoDataRequest, request: Request = None):
    base_url = get_effective_base_url(request)
    try:
        return extract_all_infodata(req.url, req.disable_proxy, base_url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/video")
def get_video_info(
    url: str = Query(..., description="YouTube video URL or ID"),
    disable_proxy: bool = Query(False, description="Set to true to bypass proxy"),
    request: Request = None
):
    base_url = get_effective_base_url(request)
    try:
        data = extract_all_infodata(url, disable_proxy, base_url)
        return {
            "id": data["metadata"]["id"],
            "title": data["metadata"]["title"],
            "channel": data["metadata"]["channel"],
            "duration": data["metadata"]["duration"],
            "thumbnail": data["metadata"]["thumbnail"],
            "paired_video_audio": data["paired_video_audio"],
            "streams": {
                "combined_legacy": data["combined_formats"],
                "video_only": data["video_formats_upto_8k"],
                "audio_only": data["audio_formats"],
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/streams")
@app.get("/api/railway/streams")
def get_streams_only(
    url: str = Query(..., description="YouTube video URL or ID"),
    disable_proxy: bool = Query(False, description="Set to true to bypass proxy"),
    request: Request = None
):
    """
    Dedicated endpoint that ONLY returns clean stream links hosted on your Railway/Server URL:
    - Video streams with sound muxed (8k down to 144p)
    - Audio streams (highest bitrate down to lowest)
    """
    base_url = get_effective_base_url(request)
    encoded_video_url = urllib.parse.quote(url, safe='')

    try:
        data = extract_all_infodata(url, disable_proxy, base_url)
        meta = data.get("metadata", {})
        
        # 1. Clean list of video streams with audio combined
        clean_videos = []
        seen_qualities = set()
        for p in data.get("paired_video_audio", []):
            q_label = p.get("quality_label")
            if q_label in seen_qualities:
                continue
            seen_qualities.add(q_label)
            clean_videos.append({
                "quality": q_label,
                "height": p.get("height"),
                "container": "mp4",
                "estimated_size": p.get("estimated_total_filesize_formatted"),
                "stream_url": p.get("stream_with_sound_url"),
                "download_url": p.get("download_with_sound_url")
            })

        # 2. Clean list of audio streams
        clean_audios = []
        for a in data.get("audio_formats", []):
            clean_audios.append({
                "format_id": a.get("format_id"),
                "ext": a.get("ext") or "mp3",
                "codec": a.get("acodec"),
                "bitrate": f"{round(a.get('abr'))} kbps" if a.get("abr") else "Unknown",
                "filesize": a.get("filesize_formatted"),
                "stream_url": f"{base_url}/api/stream_audio?url={encoded_video_url}&format_id={a.get('format_id')}"
            })

        return {
            "success": True,
            "server_base_url": base_url,
            "video_info": {
                "id": meta.get("id"),
                "title": meta.get("title"),
                "channel": meta.get("channel"),
                "duration": meta.get("duration_formatted"),
                "thumbnail": meta.get("thumbnail"),
            },
            # ONLY STREAM LINKS HOSTED ON YOUR SERVER / RAILWAY:
            "video_streams_with_sound": clean_videos,
            "audio_streams": clean_audios
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/raw")
def get_raw_json(
    url: str = Query(..., description="YouTube video URL or ID"),
    disable_proxy: bool = Query(False, description="Set to true to bypass proxy")
):
    selected_proxy = None if disable_proxy else PROXY_URL
    ydl_opts = get_ydl_opts(proxy=selected_proxy)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            sanitized = ydl.sanitize_info(info)
            return JSONResponse(content=sanitized)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
