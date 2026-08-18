"""
TikTokExtract API — FastAPI Backend (v3 - TikWM Proxy Architecture)

Production-grade backend for TikTok video downloading and bulk profile extraction.

NEW ARCHITECTURE (TikWM Proxy):
  - TikWM has its own video proxy: /video/media/play/{video_id}.mp4
  - TikWM's servers can access TikTok CDN (they have residential IPs/proxies)
  - We route downloads through TikWM's proxy — NO CDN blocking issues
  - Videos are also cached locally for fast repeated downloads

Endpoints:
  GET  /api/health            — Health check
  POST /api/download-single   — Extract metadata + return TikWM proxy URL
  POST /api/extract-bulk      — Extract bulk profile metadata
  GET  /api/get-video/{video_id} — Stream video from TikWM proxy (or local cache)
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
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse

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

# In-memory cache of {tikwm_video_id: {"path": str, "filename": str, "created": float}}
_video_cache: dict[str, dict] = {}


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


def _download_video_from_tikwm_proxy(tikwm_video_id: str, filename: str) -> bool:
    """
    Download a video from TikWM's proxy endpoint to local cache.
    TikWM's server handles the TikTok CDN access (bypasses IP blocking).
    Returns True if successful.
    """
    tikwm_url = f"https://www.tikwm.com/video/media/play/{tikwm_video_id}.mp4"
    filepath = os.path.join(VIDEO_CACHE_DIR, f"{tikwm_video_id}.mp4")

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
        "Referer": "https://www.tikwm.com/",
        "Accept": "video/mp4,*/*",
    }

    try:
        with httpx.Client(timeout=120.0, follow_redirects=True, headers=headers) as client:
            response = client.get(tikwm_url)
            if response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                content_length = response.headers.get("content-length", "0")

                # Verify it's actually a video (not an HTML error page)
                if "text/html" in content_type.lower():
                    logger.warning("TikWM proxy returned HTML, skipping")
                    return False
                if int(content_length) < 1000:
                    logger.warning("Response too small (%s bytes), skipping", content_length)
                    return False

                # Write to file
                with open(filepath, "wb") as f:
                    f.write(response.content)

                logger.info(
                    "Downloaded video (%.1f MB) from TikWM proxy for %s",
                    len(response.content) / (1024 * 1024),
                    tikwm_video_id,
                )
                return True
            else:
                logger.warning("TikWM proxy returned %d for %s", response.status_code, tikwm_video_id)
    except Exception as e:
        logger.warning("TikWM proxy download failed for %s: %s", tikwm_video_id, e)

    return False


async def _async_download_to_cache(tikwm_video_id: str, filename: str):
    """Async wrapper for downloading video to cache (runs in background)."""
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, _download_video_from_tikwm_proxy, tikwm_video_id, filename)
    if success:
        _video_cache[tikwm_video_id] = {
            "path": os.path.join(VIDEO_CACHE_DIR, f"{tikwm_video_id}.mp4"),
            "filename": filename,
            "created": time.time(),
        }
        logger.info("Video cached: %s", tikwm_video_id)
    else:
        logger.warning("Failed to cache video: %s", tikwm_video_id)


async def _stream_from_tikwm_proxy(tikwm_video_id: str, filename: str):
    """
    Stream a video directly from TikWM's proxy endpoint.
    Returns a StreamingResponse that proxies the video to the user.
    """
    tikwm_url = f"https://www.tikwm.com/video/media/play/{tikwm_video_id}.mp4"

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
        "Referer": "https://www.tikwm.com/",
        "Accept": "video/mp4,*/*",
    }

    async def stream_gen():
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", tikwm_url) as response:
                if response.status_code != 200:
                    logger.error("TikWM proxy returned %d for %s", response.status_code, tikwm_video_id)
                    return

                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type.lower():
                    logger.error("TikWM proxy returned HTML for %s", tikwm_video_id)
                    return

                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    return StreamingResponse(
        stream_gen(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
        },
    )


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
    Extract metadata for a single TikTok video.
    Returns metadata + TikWM proxy URL for download.
    Also starts background caching of the video for fast repeated downloads.
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
        logger.info("Successfully extracted: %s", data.get("title", "unknown"))

        # Extract key data
        tikwm_video_id = data.get("video_id", "")
        tikwm_proxy_url = data.get("tikwm_proxy_url", "")
        title = data.get("title", "TikTok Video")

        # Generate a safe filename
        safe_title = "".join(c for c in title[:50] if c.isalnum() or c in " _-").strip()
        filename = f"{safe_title}_no_watermark.mp4" if safe_title else "tiktok_video_no_watermark.mp4"

        # Start background download to cache (don't block the response)
        if tikwm_video_id and tikwm_video_id != "unknown":
            asyncio.create_task(_async_download_to_cache(tikwm_video_id, filename))

        # Return the response
        result = {
            "title": data.get("title", ""),
            "author": data.get("author", ""),
            "views": data.get("views", ""),
            "thumbnail": data.get("thumbnail", ""),
            "download_url": tikwm_proxy_url,  # Use TikWM proxy URL (not raw CDN)
            "video_id": tikwm_video_id,
            "filename": filename,
        }

        logger.info("Returning result with TikWM proxy URL: %s", tikwm_proxy_url)
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


# ─── Get Video (Download) ────────────────────────────────────────

@app.get("/api/get-video/{video_id}", tags=["Download"])
async def get_video(video_id: str):
    """
    Download a TikTok video.
    Strategy:
      1. Check local cache first (fastest)
      2. If not cached, stream directly from TikWM's proxy
      3. In parallel, cache for next time
    TikWM's proxy bypasses CDN IP blocking — their servers handle the CDN access.
    """
    # Cleanup expired cache
    _cleanup_expired_cache()

    # Check if video is in cache
    if video_id in _video_cache and os.path.exists(_video_cache[video_id]["path"]):
        logger.info("Serving cached video: %s", video_id)
        return FileResponse(
            path=_video_cache[video_id]["path"],
            media_type="video/mp4",
            filename=_video_cache[video_id]["filename"],
            headers={"Content-Disposition": f'attachment; filename="{_video_cache[video_id]["filename"]}"'},
        )

    # Not cached — stream from TikWM's proxy
    logger.info("Streaming from TikWM proxy: %s", video_id)

    # Start background caching for next time
    if video_id and video_id != "unknown":
        filename = f"tiktok_{video_id}_no_watermark.mp4"
        asyncio.create_task(_async_download_to_cache(video_id, filename))

    # Stream from TikWM proxy
    tikwm_url = f"https://www.tikwm.com/video/media/play/{video_id}.mp4"

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
        "Referer": "https://www.tikwm.com/",
        "Accept": "video/mp4,*/*",
    }

    async def stream_gen():
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", tikwm_url) as response:
                if response.status_code != 200:
                    logger.error("TikWM proxy returned %d for %s", response.status_code, video_id)
                    raise HTTPException(status_code=502, detail="Failed to fetch video from proxy.")

                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type.lower():
                    logger.error("TikWM proxy returned HTML for %s", video_id)
                    raise HTTPException(status_code=502, detail="Invalid response from proxy.")

                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    return StreamingResponse(
        stream_gen(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="tiktok_{video_id}_no_watermark.mp4"',
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
        },
    )


# ─── Legacy Proxy Download (backwards compatibility) ─────────────

@app.api_route("/api/proxy-download", methods=["GET", "HEAD"], tags=["Download"])
async def proxy_download(
    url: str = Query(..., description="TikWM proxy URL or CDN URL"),
    filename: Optional[str] = Query(None, description="Desired download filename"),
):
    """
    Legacy proxy endpoint.
    If URL is a TikWM proxy URL, stream from TikWM.
    If URL is a raw CDN URL, try to extract video_id and use TikWM proxy instead.
    """
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' parameter.")

    filename = filename or "tiktok_video_no_watermark.mp4"

    # Check if this is a TikWM proxy URL
    if "tikwm.com/video/media/" in url:
        # Extract video_id from URL like: https://www.tikwm.com/video/media/play/VIDEOID.mp4
        import re
        match = re.search(r"/video/media/(?:hd)?play/([a-f0-9]+)\.mp4", url)
        if match:
            video_id = match.group(1)
            logger.info("Legacy proxy-download using TikWM proxy for: %s", video_id)
            # Reuse the same logic as get-video
            return await get_video(video_id)

    # If it's a raw CDN URL, try to extract the video_id from the CDN path
    # CDN URLs have the video ID in the path: /video/tos/.../ID/
    import re
    cdn_match = re.search(r"/(?:video/tos|[^/]+/tos[^/]*)/[^/]+/([A-Za-z0-9]{10,})/", url)
    if cdn_match:
        video_id = cdn_match.group(1)
        logger.info("Legacy proxy-download converted CDN URL to TikWM proxy for: %s", video_id[:20])
        return await get_video(video_id)

    # Can't determine video_id — try streaming the URL directly
    logger.warning("Cannot convert URL to TikWM proxy, streaming directly: %s", url[:60])

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
        "Referer": "https://www.tiktok.com/",
        "Accept": "video/mp4,*/*",
    }

    async def stream_gen():
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise HTTPException(status_code=502, detail="Failed to fetch video.")
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    return StreamingResponse(
        stream_gen(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "video/mp4",
        },
    )


# ─── Root endpoint ────────────────────────────────────────────────

@app.get("/")
async def root():
    """Root endpoint — returns API info."""
    return {"message": "TikTokExtract API v" + settings.APP_VERSION, "docs": "/docs"}
