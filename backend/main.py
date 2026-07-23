import asyncio
import os
import re
from typing import Optional, List
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
# SCHEMAS (Matches Frontend Javascript Payloads)
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
# HELPER FUNCTIONS & SANITIZERS
# =====================================================================

def clean_tiktok_url(text: str) -> str:
    """Extracts the first valid HTTP/HTTPS URL from user input, removing app share clutter."""
    match = re.search(r'https?://[^\s]+', text)
    if match:
        return match.group(0)
    return text.strip()

def format_count(count: Optional[int]) -> str:
    """Format view counts into readable K/M strings."""
    if not count:
        return "N/A"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)

def format_duration(seconds: Optional[float]) -> str:
    """Format duration into MM:SS."""
    if not seconds:
        return "00:00"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins:02d}:{secs:02d}"

def get_common_yt_dlp_opts() -> dict:
    """Base options with proxy rotation configured."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'socket_timeout': 15,
    }
    if DATAIMPULSE_PROXY:
        opts['proxy'] = DATAIMPULSE_PROXY
    return opts

# =====================================================================
# CORE SYNCHRONOUS EXTRACTORS
# =====================================================================

def _sync_download_single(video_url: str) -> dict:
    opts = get_common_yt_dlp_opts()
    opts.update({
        'format': 'bestvideo+bestaudio/best',
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        if not info:
            raise ValueError("Unable to retrieve video metadata.")

        download_url = info.get('url')
        if not download_url and 'requested_formats' in info:
            download_url = info['requested_formats'][0].get('url')

        return {
            "title": info.get('title', 'TikTok Video'),
            "author": f"@{info.get('uploader_id', info.get('uploader', 'creator'))}",
            "views": format_count(info.get('view_count')),
            "thumbnail": info.get('thumbnail', ''),
            "download_url": download_url or video_url
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
    return {"status": "online", "domain": "savetokhd.com"}

@app.post("/api/download-single", response_model=SingleVideoResponse)
async def api_download_single(payload: SingleVideoRequest):
    # Extract clean URL from messy input
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
            detail=f"Failed to process video: {str(e)}"
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
            detail=f"Failed to extract profile: {str(e)}"
        )
