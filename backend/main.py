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
    version="1.0.0"
)

# CORS Configuration
ORIGINS = [
    "https://savetokhd.com",
    "https://www.savetokhd.com",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

        opts['proxy'] = proxy_str

    return opts

# =====================================================================
# SYNCHRONOUS EXTRACTORS
# =====================================================================

def _sync_download_single(video_url: str) -> dict:
    opts = get_common_yt_dlp_opts()
    opts.update({
        'format': 'best',
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        if not info:
            raise ValueError("TikTok blocked metadata extraction for this link.")

        raw_download_url = info.get('url')
        if not raw_download_url and 'requested_formats' in info:
            raw_download_url = info['requested_formats'][0].get('url')

        proxied_url = f"https://savetokhd-app.onrender.com/api/proxy-download?url={quote(raw_download_url or video_url)}"

        return {
            "title": info.get('title', 'TikTok Video'),
            "author": f"@{info.get('uploader_id', info.get('uploader', 'creator'))}",
            "views": format_count(info.get('view_count')),
            "thumbnail": info.get('thumbnail', ''),
            "download_url": proxied_url
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
async def proxy_download(url: str):
    """Streams the raw video bytes asynchronously to avoid blocking the Uvicorn worker thread."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Referer": "https://www.tiktok.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    
    proxy_config = None
    if DATAIMPULSE_PROXY and DATAIMPULSE_PROXY.strip():
        p_str = DATAIMPULSE_PROXY.strip()
        if p_str.startswith("https://"):
            p_str = "http://" + p_str[8:]
        elif not p_str.startswith("http://"):
            p_str = "http://" + p_str
        proxy_config = p_str

    async def stream_chunks():
        async with httpx.AsyncClient(proxy=proxy_config, follow_redirects=True, timeout=60.0) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code not in (200, 206):
                    raise HTTPException(status_code=400, detail="Failed to stream video stream from CDN.")
                async for chunk in response.aiter_bytes(chunk_size=128 * 1024):
                    yield chunk

    return StreamingResponse(
        stream_chunks(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": 'attachment; filename="tiktok_video.mp4"',
            "Content-Type": "video/mp4",
        }
    )

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
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error: {str(e)}"
        )