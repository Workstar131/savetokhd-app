"""
TikTokExtract API — FastAPI Backend

Production-grade backend for TikTok video downloading and bulk profile extraction.
Matches the frontend contract defined in the static HTML/JS client.

Endpoints:
  GET  /api/health            — Health check
  POST /api/download-single   — Extract single video metadata
  POST /api/extract-bulk      — Extract bulk profile metadata
  GET  /api/proxy-download    — Redirect to TikTok CDN video URL
"""

import logging
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

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


@app.api_route("/api/proxy-download", methods=["GET", "HEAD"], tags=["Download"])
async def proxy_download(
    url: str = Query(..., description="Encoded TikTok CDN video URL"),
):
    """
    Redirect the browser to the TikTok CDN video URL for download.

    The CDN URL already contains temporary authentication tokens/signatures
    that are valid for a limited time. By returning a 302 redirect, the
    browser accesses the CDN directly — no server proxy needed.

    This avoids the 403 Forbidden error that occurs when the server tries
    to proxy the request without the proper yt-dlp resolved cookies/headers.
    """
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' parameter.")

    # Validate that the URL points to a TikTok-related domain
    try:
        parsed = urlparse(url)
        allowed_domains = (
            "tiktok.com", "tiktokcdn.com", "tiktokv.com",
            "byteoversea.com", "bytegecko-i18n.com",
        )
        hostname = parsed.hostname or ""
        if not any(d in hostname for d in allowed_domains):
            raise HTTPException(
                status_code=400,
                detail="URL must point to a TikTok CDN or video domain.",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL format.")

    # Return a 302 redirect so the browser downloads directly from the CDN
    logger.info("Redirecting to CDN: %s", url[:100])
    return RedirectResponse(
        url=url,
        status_code=302,
        headers={
            "Content-Disposition": 'attachment; filename="tiktok_video_nowatermark.mp4"',
        },
    )


# ─── Root endpoint ────────────────────────────────────────────────

@app.get("/")
async def root():
    """Root endpoint — returns API info."""
    return {"message": "TikTokExtract API v" + settings.APP_VERSION, "docs": "/docs"}
