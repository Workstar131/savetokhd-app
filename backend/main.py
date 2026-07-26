import asyncio
import os
import re
from typing import Optional, List
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import httpx
import yt_dlp

# =====================================================================
# CONFIGURATION & PROXY SETUP
# =====================================================================

DATAIMPULSE_PROXY = os.getenv("PROXY_URL")

app = FastAPI(
    title="SaveTokHD Engine",
    description="Asynchronous backend API for TikTok extraction and downloading.",
    version="2.0.0"
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Type", "Content-Length", "Location"],
)

# =====================================================================
# SCHEMAS
# =====================================================================

class SingleVideoRequest(BaseModel):
    url: str

class SingleVideoResponse(BaseModel):
    title: str
    author: str
    views: str
    thumbnail: str
    download_url: str

class BulkExtractRequest(BaseModel):
    username: str
    delay: Optional[float] = 1.0

class BulkVideoItem(BaseModel):
    id: str
    caption: str
    views: str
    duration: str
    url: str

class BulkExtractResponse(BaseModel):
    username: str
    total_videos: int
    videos: List[BulkVideoItem]

# =====================================================================
# HELPER FUNCTIONS
# =====================================================================

def clean_tiktok_url(text: str) -> str:
    """Extracts valid HTTP/HTTPS URL and resolves short links."""
    match = re.search(r'https?://[^\s]+', text)
    if not match:
        return text.strip()

    url = match.group(0)

    if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            }
            with httpx.Client(follow_redirects=True, timeout=8.0, headers=headers) as client:
                res = client.get(url)
                if res.status_code == 200:
                    url = str(res.url)
        except Exception:
            pass

    return url

def format_count(count: Optional[int]) -> str:
    if not count:
        return "N/A"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)

def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "00:00"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins:02d}:{secs:02d}"

def get_common_yt_dlp_opts() -> dict:
    """Options with realistic browser headers and mobile API fallback to bypass TikTok blocks."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': False,
        'socket_timeout': 15,
        'geo_bypass': True,
        'extractor_args': {
            'tiktok': {
                'app_version': '31.5.3',
                'manifest_app_version': '3153',
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Fetch-Mode': 'navigate',
        }
    }
    if DATAIMPULSE_PROXY and DATAIMPULSE_PROXY.strip():
        proxy_str = DATAIMPULSE_PROXY.strip()
        if proxy_str.startswith("https://"):
            proxy_str = "http://" + proxy_str[8:]
        elif not proxy_str.startswith("http://"):
            proxy_str = "http://" + proxy_str

        opts["proxy"] = proxy_str

    return opts

# =====================================================================
# URL EXTRACTION HELPER
# =====================================================================

def extract_download_url(info: dict) -> Optional[str]:
    """
    Extract the DIRECT video download URL from yt-dlp info dict.
    """
    if not info:
        return None

    # Strategy 1: Top-level 'url'
    raw_url = info.get('url')
    if raw_url and raw_url.startswith('http'):
        return raw_url

    # Strategy 2: 'video_url' field
    raw_url = info.get('video_url')
    if raw_url and raw_url.startswith('http'):
        return raw_url

    # Strategy 3: From 'requested_formats'
    requested_formats = info.get('requested_formats')
    if requested_formats:
        for fmt in requested_formats:
            fmt_url = fmt.get('url')
            if fmt_url and fmt_url.startswith('http'):
                return fmt_url

    # Strategy 4: From 'formats' array (direct MP4 URLs from CDN)
    formats = info.get('formats')
    if formats:
        for fmt in formats:
            fmt_url = fmt.get('url')
            if not fmt_url or not fmt_url.startswith('http'):
                continue
            ext = fmt.get('ext', '')
            if ext == 'mp4':
                return fmt_url

        for fmt in formats:
            fmt_url = fmt.get('url')
            if fmt_url and fmt_url.startswith('http'):
                return fmt_url

    # Strategy 5: Manifest URLs (fallback)
    raw_url = info.get('manifest_url') or info.get('hls_manifest_url')
    if raw_url and raw_url.startswith('http'):
        return raw_url

    return None

def extract_thumbnail(info: dict) -> str:
    """Extract the best thumbnail URL from yt-dlp info dict."""
    thumb = info.get('thumbnail')
    if thumb and thumb.startswith('http'):
        return thumb

    thumbnails = info.get('thumbnails')
    if thumbnails and isinstance(thumbnails, list):
        for t in thumbnails:
            if t.get('id') == 'origin_cover' and t.get('url'):
                return t['url']
        for t in thumbnails:
            if t.get('id') == 'cover' and t.get('url'):
                return t['url']
        for t in thumbnails:
            if t.get('url') and t['url'].startswith('http'):
                return t['url']

    return ''

# =====================================================================
# SYNCHRONOUS EXTRACTORS
# =====================================================================

def _sync_download_single(video_url: str) -> dict:
    opts = get_common_yt_dlp_opts()
    opts.update({'format': 'best'})

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        if not info:
            raise ValueError("TikTok blocked metadata extraction for this link.")

        direct_cdn_url = extract_download_url(info)
        if not direct_cdn_url:
            raise ValueError("Could not extract download URL from TikTok video metadata.")

        return {
            "title": info.get('title', 'TikTok Video'),
            "author": f"@{info.get('uploader_id', info.get('uploader', 'creator'))}",
            "views": format_count(info.get('view_count')),
            "thumbnail": extract_thumbnail(info),
            "download_url": direct_cdn_url
        }

def _sync_extract_bulk(username: str) -> dict:
    clean_user = username.replace('@', '').strip()
    profile_url = f"https://www.tiktok.com/@{clean_user}"
    
    opts = get_common_yt_dlp_opts()
    opts.update({
        'extract_flat': True,
        'skip_download': True,
    })

    raw_videos = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(profile_url, download=False)
        if not info or 'entries' not in info:
            raise ValueError("Could not find public profile or videos.")

        entries = list(info['entries'])
        for entry in entries:
            if not entry:
                continue
            
            video_id = entry.get('id')
            v_url = entry.get('url')
            if not v_url and video_id:
                v_url = f"https://www.tiktok.com/@{clean_user}/video/{video_id}"

            raw_videos.append({
                "id": str(video_id or len(raw_videos) + 1),
                "caption": entry.get('title', 'No description'),
                "views": format_count(entry.get('view_count')),
                "duration": format_duration(entry.get('duration')),
                "url": v_url or "#"
            })

    return {
        "username": f"@{clean_user}",
        "total_videos": len(raw_videos),
        "videos": raw_videos
    }

# =====================================================================
# API ENDPOINTS
# =====================================================================

@app.get("/api/health")
async def health_check():
    return {
        "status": "online", 
        "domain": "savetokhd.com", 
        "yt_dlp_version": yt_dlp.version.__version__
    }

@app.get("/api/proxy-download")
async def proxy_download(url: str, filename: Optional[str] = "tiktok_video.mp4"):
    """
    Proxies video stream from TikTok's CDN and forces the browser 
    to trigger a download dialog using Content-Disposition header.
    """
    if not url or url.strip() == '' or url == 'none':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid download URL."
        )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }

    async def video_stream():
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    raise HTTPException(status_code=400, detail="Failed to retrieve video stream from CDN.")
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):  # 1MB Chunks
                    yield chunk

    encoded_filename = quote(filename)
    response_headers = {
        "Content-Disposition": f'attachment; filename="{encoded_filename}"; filename*=UTF-8\'\'{encoded_filename}',
        "Content-Type": "video/mp4",
    }

    return StreamingResponse(video_stream(), headers=response_headers, media_type="video/mp4")

@app.post("/api/download-single", response_model=SingleVideoResponse)
async def api_download_single(payload: SingleVideoRequest):
    sanitized_url = clean_tiktok_url(payload.url)

    if "tiktok.com" not in sanitized_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid URL. Please enter a valid TikTok link."
        )

    try:
        data = await asyncio.to_thread(_sync_download_single, sanitized_url)
        return data
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Extraction failed: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error: {str(e)}"
        )

@app.post("/api/extract-bulk", response_model=BulkExtractResponse)
async def api_extract_bulk(payload: BulkExtractRequest):
    if not payload.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Username or profile link is required."
        )

    try:
        data = await asyncio.to_thread(_sync_extract_bulk, payload.username)
        return data
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Extraction failed: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error: {str(e)}"
        )