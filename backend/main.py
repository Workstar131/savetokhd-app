"""
TikTokExtract API — FastAPI Backend (v2 Architecture)

Production-grade backend for TikTok video downloading and bulk profile extraction.

NEW ARCHITECTURE (Server-Side Download + Local Cache):
  - Videos are downloaded to server disk immediately after extraction
  - Users download from our server (not TikTok CDN) — no 403 issues
  - Cached files are auto-cleaned after TTL

Endpoints:
  GET  /api/health            — Health check
  POST /api/download-single   — Extract metadata + download video to cache
  POST /api/extract-bulk      — Extract bulk profile metadata
  GET  /api/get-video/{video_id} — Download cached video from server
"""

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from config import settings
from schemas.payload_models import (
    BulkExtractRequest,
    BulkExtractResponse,
    HealthResponse,
    SingleDownloadRequest,
)
from services.tiktok_service import (
    extract_bulk_profile,
    extract_single_video,
)
from utils.formatters import is_valid_tiktok_url

# ─── Logging ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tiktokextract")

# ─── Video Cache ──────────────────────────────────────────────────

VIDEO_CACHE_DIR = os.path.join(tempfile.gettempdir(), "tiktok_video_cache")
os.makedirs(VIDEO_CACHE_DIR, exist_ok=True)

# Cache TTL: 30 minutes
CACHE_TTL = 30 * 60

# In-memory cache of {video_id: {"path": str, "filename": str, "created": float}}
_video_cache: dict[str, dict] = {}


def _generate_video_id(url: str) -> str:
    """Generate a unique ID for a video based on its CDN URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _cleanup_expired_cache():
    """Remove expired video files from the cache directory."""
    now = time.time()
    expired_ids = []
    for vid, info in _video_cache.items():
        if now - info["created"] > CACHE_TTL:
            expired_ids.append(vid)
    for vid in expired_ids:
        try:
            os.remove(_video_cache[vid]["path"])
        except OSError:
            pass
        del _video_cache[vid]


def _download_video_to_cache(cdn_url: str, filename: str, video_id: str) -> bool:
    """
    Download a video from CDN URL to local cache.
    Tries multiple CDN mirror domains in parallel.
    Returns True if successful.
    """
    filepath = os.path.join(VIDEO_CACHE_DIR, f"{video_id}.mp4")
    
    # Try the original URL first
    urls_to_try = [cdn_url]
    
    # Generate mirror URLs (replace domain with known TikTok CDN mirrors)
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(cdn_url)
    path = parsed.path
    query = parsed.query
    mirror_domains = [
        "v16.tokcdn.com",
        "v16m.tiktokcdn.com",
        "v19.tiktokcdn.com",
        "www.tiktok.com",
    ]
    for domain in mirror_domains:
        mirrored = urlunparse((
            "https",
            domain,
            path,
            "",
            query,
            ""
        ))
        urls_to_try.append(mirrored)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
    }
    
    for attempt_url in urls_to_try:
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as client:
                response = client.get(attempt_url)
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "")
                    # Verify it's actually a video
                    if "text/html" in content_type.lower():
                        logger.warning("Mirror returned HTML, skipping: %s", attempt_url[:60])
                        continue
                    if len(response.content) < 1000:
                        logger.warning("Response too small, skipping: %s", attempt_url[:60])
                        continue
                    
                    # Write to file
                    with open(filepath, "wb") as f:
                        f.write(response.content)
                    
                    logger.info(
                        "Downloaded video (%.1f MB) from %s -> %s",
                        len(response.content) / (1024*1024),
                        attempt_url[:50],
                        filepath,
                    )
                    return True
                else:
                    logger.debug("Mirror %s returned %d", attempt_url[:50], response.status_code)
        except Exception as e:
            logger.debug("Mirror %s failed: %s", attempt_url[:50], e)
            continue
    
    return False


async def _async_download_video(cdn_url: str, filename: str, video_id: str):
    """Async wrapper for downloading video to cache (runs in background)."""
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(
        None, _download_video_to_cache, cdn_url, filename, video_id
    )
    if success:
        _video_cache[video_id] = {
            "path": os.path.join(VIDEO_CACHE_DIR, f"{video_id}.mp4"),
            "filename": filename,
            "created": time.time(),
        }
        logger.info("Video cached with ID: %s", video_id)
    else:
        logger.warning("Failed to download video to cache: %s", video_id)


# ─── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── CORS Middleware ──────────────────────────────────────────────

if settings.CORS_ALLOW_ALL:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
        expose_headers=["*"],
        max_age=600,
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=600,
    )

# ─── Custom Exception Handlers ────────────────────────────────────

@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError):
    """Handle validation errors (invalid URLs, bad usernames)."""
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc)},
    )


@app.exception_handler(RuntimeError)
async def runtime_error_handler(request, exc: RuntimeError):
    """Handle runtime errors (private videos, extractor failures)."""
    return JSONResponse(
        status_code=404,
        content={"detail": str(exc)},
    )


# ─── Endpoints ────────────────────────────────────────────────────

@app.get("/api/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Health check endpoint. Returns service status and version."""
    return HealthResponse(status="healthy", version=settings.APP_VERSION)


@app.post(
    "/api/download-single",
    tags=["Download"],
)
async def download_single(request: SingleDownloadRequest):
    """
    Extract metadata for a single TikTok video AND download it to server cache.
    Returns metadata + a local download URL for the cached video.
    """
    url = request.url.strip()

    # Validate URL
    if not is_valid_tiktok_url(url):
        raise HTTPException(
            status_code=400,
            detail="Invalid TikTok URL. Please provide a valid link from tiktok.com, vm.tiktok.com, or vt.tiktok.com.",
        )

    try:
        data = await extract_single_video(url)
        logger.info("Successfully extracted single video: %s", data.get("title", "unknown"))
        
        # Extract the CDN URL
        cdn_url = data.get("download_url", "")
        title = data.get("title", "TikTok Video")
        
        # Generate a safe filename
        safe_title = "".join(c for c in title[:50] if c.isalnum() or c in " _-").strip()
        filename = f"{safe_title}_no_watermark.mp4" if safe_title else "tiktok_video_no_watermark.mp4"
        
        # Generate video ID
        video_id = _generate_video_id(cdn_url)
        
        # Start background download to cache
        # Don't await — let it run in background so the user gets the response quickly
        asyncio.create_task(_async_download_video(cdn_url, filename, video_id))
        
        # Return the response with local download URL
        result = {
            "title": data.get("title", ""),
            "author": data.get("author", ""),
            "views": data.get("views", ""),
            "thumbnail": data.get("thumbnail", ""),
            "download_url": cdn_url,
            "video_id": video_id,
            "filename": filename,
        }
        
        logger.info("Returning result with video_id: %s", video_id)
        return JSONResponse(content=result)
        
    except ValueError:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Unexpected error in download-single: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Internal server error. Please try again later.",
        )


@app.post(
    "/api/extract-bulk",
    response_model=BulkExtractResponse,
    tags=["Bulk"],
)
async def extract_bulk(request: BulkExtractRequest):
    """
    Extract metadata for all public videos on a TikTok profile.
    """
    username = request.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty.")

    try:
        data = await extract_bulk_profile(username, delay=request.delay)
        logger.info(
            "Successfully extracted %d videos for %s",
            data["total_videos"],
            data["username"],
        )
        return BulkExtractResponse(**data)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Unexpected error in extract-bulk: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Internal server error. Please try again later.",
        )


# ─── Get Video (Download from Cache) ──────────────────────────────

@app.get("/api/get-video/{video_id}", tags=["Download"])
async def get_video(video_id: str):
    """
    Download a cached video from the server.
    The video was pre-downloaded during extraction, so this always works
    (no TikTok CDN blocking issues).
    """
    # Cleanup expired cache
    _cleanup_expired_cache()
    
    # Check if video is in cache
    if video_id not in _video_cache:
        raise HTTPException(
            status_code=404,
            detail="Video not found in cache. Please extract the video again."
        )
    
    cache_info = _video_cache[video_id]
    filepath = cache_info["path"]
    filename = cache_info["filename"]
    
    if not os.path.exists(filepath):
        # File was deleted but cache entry still exists
        del _video_cache[video_id]
        raise HTTPException(
            status_code=404,
            detail="Video file was removed from cache. Please extract again."
        )
    
    logger.info("Serving cached video: %s (%s)", filename, video_id)
    
    return FileResponse(
        path=filepath,
        media_type="video/mp4",
        filename=filename,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
    )


# ─── Legacy Proxy Download (kept for backwards compatibility) ─────

@app.api_route("/api/proxy-download", methods=["GET", "HEAD"], tags=["Download"])
async def proxy_download(
    url: str = Query(..., description="Encoded TikTok CDN video URL"),
    filename: Optional[str] = Query(None, description="Desired download filename"),
):
    """
    Legacy proxy endpoint — downloads the video from CDN and returns it.
    Now uses the cache approach: downloads to disk first, then serves.
    """
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' parameter.")

    filename = filename or "tiktok_video_nowatermark.mp4"
    video_id = _generate_video_id(url)
    
    # Check if already cached
    if video_id in _video_cache and os.path.exists(_video_cache[video_id]["path"]):
        logger.info("Serving already-cached video: %s", video_id)
        return FileResponse(
            path=_video_cache[video_id]["path"],
            media_type="video/mp4",
            filename=_video_cache[video_id]["filename"],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    
    # Try to download to cache
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, _download_video_to_cache, url, filename, video_id)
    
    if success:
        _video_cache[video_id] = {
            "path": os.path.join(VIDEO_CACHE_DIR, f"{video_id}.mp4"),
            "filename": filename,
            "created": time.time(),
        }
        return FileResponse(
            path=os.path.join(VIDEO_CACHE_DIR, f"{video_id}.mp4"),
            media_type="video/mp4",
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    
    # If download failed, try re-extracting with a fresh URL
    logger.warning("Direct download failed, trying re-extraction...")
    raise HTTPException(
        status_code=502,
        detail="Failed to download video. Please try extracting again."
    )


# ─── Root endpoint ────────────────────────────────────────────────

@app.get("/")
async def root():
    """Root endpoint — returns API info."""
    return {"message": "TikTokExtract API v" + settings.APP_VERSION, "docs": "/docs"}
