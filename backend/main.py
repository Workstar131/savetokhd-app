"""
TikTokExtract API — FastAPI Backend

Production-grade backend for TikTok video downloading and bulk profile extraction.
Matches the frontend contract defined in the static HTML/JS client.

Endpoints:
  GET  /api/health            — Health check
  POST /api/download-single   — Extract single video metadata
  POST /api/extract-bulk      — Extract bulk profile metadata
  GET  /api/proxy-download    — Stream a video from TikTok CDN via server proxy
"""

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from config import settings
from schemas.payload_models import (
    BulkExtractRequest,
    BulkExtractResponse,
    HealthResponse,
    SingleDownloadRequest,
    SingleVideoResponse,
)
from services.tiktok_service import extract_bulk_profile, extract_single_video
from utils.formatters import is_valid_tiktok_url

# ─── Logging ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tiktokextract")

# ─── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── CORS Middleware ──────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    response_model=SingleVideoResponse,
    tags=["Download"],
)
async def download_single(request: SingleDownloadRequest):
    """
    Extract metadata for a single TikTok video.

    Validates the URL, runs yt-dlp to parse video details without downloading,
    and returns metadata including a watermark-free CDN download URL.
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
        return SingleVideoResponse(**data)
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

    Accepts a username (with or without @) and optional delay between requests.
    Returns structured video metadata suitable for table display and CSV export.
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


@app.get("/api/proxy-download", tags=["Download"])
async def proxy_download(url: str = Query(..., description="Encoded TikTok CDN video URL")):
    """
    Proxy a video download from TikTok's CDN.

    Streams the video through this server with spoofed headers so that
    TikTok's CDN accepts the request. Uses StreamingResponse to keep
    server memory usage low.
    """
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' parameter.")

    # Validate that the URL points to a TikTok-related domain
    try:
        parsed = urlparse(url)
        allowed_domains = ("tiktok.com", "tiktokcdn.com", "tiktokv.com")
        if not any(parsed.hostname and d in parsed.hostname for d in allowed_domains):
            raise HTTPException(
                status_code=400,
                detail="URL must point to a TikTok CDN or video domain.",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL format.")

    headers = {
        "User-Agent": settings.DEFAULT_USER_AGENT,
        "Referer": settings.DEFAULT_REFERER,
        "Accept": "*/*",
        "Accept-Encoding": "identity",  # Prevent gzip to stream raw bytes
        "Accept-Language": "en-US,en;q=0.9",
    }

    async def stream_content():
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=settings.PROXY_CHUNK_SIZE):
                    yield chunk

    return StreamingResponse(
        stream_content(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": 'attachment; filename="tiktok_video_nowatermark.mp4"',
            "Cache-Control": "no-cache",
        },
    )


# ─── Root redirect (optional) ─────────────────────────────────────

@app.get("/")
async def root():
    """Redirect root to the API documentation."""
    return {"message": "TikTokExtract API v" + settings.APP_VERSION, "docs": "/docs"}
