"""
TikTokExtract API — FastAPI Backend

Production-grade backend for TikTok video downloading and bulk profile extraction.
Matches the frontend contract defined in the static HTML/JS client.

Endpoints:
  GET  /api/health            — Health check
  POST /api/download-single   — Extract single video metadata
  POST /api/extract-bulk      — Extract bulk profile metadata
  GET  /api/proxy-download    — Proxy-stream video from TikTok CDN
"""

import asyncio
import logging
from urllib.parse import urlparse
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse

from config import settings
from schemas.payload_models import (
    BulkExtractRequest,
    BulkExtractResponse,
    HealthResponse,
    SingleDownloadRequest,
    SingleVideoResponse,
)
from services.tiktok_service import (
    extract_bulk_profile,
    extract_single_video,
    generate_mirror_urls,
    swap_cdn_domain,
)
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
    Returns metadata including a no-watermark CDN download URL.
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


# ─── Proxy Download (Server-side Streaming) ───────────────────────

def _get_cdn_headers() -> dict:
    """
    Build browser-like headers that TikTok CDN accepts.
    These mimic a real browser fetching a video from TikTok.
    """
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "Range": "bytes=0-",
    }


async def _stream_video_from_cdn(url: str, filename: str):
    """
    Stream a video from the TikTok CDN through our server.
    
    This is a generator that yields chunks of video data as they arrive,
    allowing the browser to start downloading immediately without waiting
    for the full file to be downloaded server-side first.
    """
    headers = _get_cdn_headers()
    
    async with httpx.AsyncClient(
        timeout=120.0,
        follow_redirects=True,
        headers=headers,
    ) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code in (403, 404):
                    logger.error(
                        "CDN rejected stream request: %d for %s",
                        response.status_code, url[:80]
                    )
                    return
                
                if response.status_code != 200 and response.status_code != 206:
                    logger.warning(
                        "CDN returned %d for %s", response.status_code, url[:80]
                    )
                    return
                
                content_type = response.headers.get("content-type", "video/mp4")
                content_length = response.headers.get("content-length", "")
                
                # Check if the response is actually video content (not an HTML error page)
                if "text/html" in content_type.lower():
                    logger.warning(
                        "CDN returned HTML instead of video for %s (possible redirect to 404)",
                        url[:80],
                    )
                    return
                
                # Yield response metadata for the outer function
                yield {
                    "_meta": True,
                    "content_type": content_type,
                    "content_length": content_length,
                    "status_code": response.status_code,
                }
                
                # Stream chunks
                chunk_count = 0
                async for chunk in response.aiter_bytes(chunk_size=settings.PROXY_CHUNK_SIZE):
                    chunk_count += 1
                    yield chunk
                    
                logger.info(
                    "Streamed %d chunks (%.1f MB) from CDN for %s",
                    chunk_count,
                    chunk_count * settings.PROXY_CHUNK_SIZE / (1024 * 1024),
                    url[:60],
                )
                
        except httpx.ConnectError:
            logger.error("Connection error streaming from CDN: %s", url[:80])
        except httpx.TimeoutException:
            logger.error("Timeout streaming from CDN: %s", url[:80])
        except Exception as e:
            logger.error("Stream error: %s — %s", e, url[:80])


async def _probe_cdn_url(url: str) -> Optional[dict]:
    """
    Fast probe of a CDN URL — sends a small Range request to check if it works.
    Returns metadata dict if successful, None if failed.
    All mirrors are probed in parallel, so total time = fastest working mirror.
    """
    headers = _get_cdn_headers()
    
    try:
        async with httpx.AsyncClient(
            timeout=8.0,  # Short timeout for probing
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code in (403, 404, 500):
                    return None
                
                content_type = response.headers.get("content-type", "video/mp4")
                content_length = response.headers.get("content-length", "")
                
                # Reject HTML error pages
                if "text/html" in content_type.lower():
                    return None
                
                return {
                    "_meta": True,
                    "content_type": content_type,
                    "content_length": content_length,
                    "status_code": response.status_code,
                }
    except Exception:
        return None


@app.api_route("/api/proxy-download", methods=["GET", "HEAD"], tags=["Download"])
async def proxy_download(
    url: str = Query(..., description="Encoded TikTok CDN video URL"),
):
    """
    Proxy-stream the video from TikTok CDN through our server.
    
    TikTok CDN URLs expire quickly and are session-dependent. Instead of
    redirecting (which causes 0-byte downloads when the CDN rejects the
    browser's request), we stream the video through our server using
    browser-like headers that the CDN accepts.
    
    The browser receives the video data directly from our server with
    proper Content-Disposition headers for download.
    """
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' parameter.")

    # Validate that the URL points to a TikTok-related domain
    try:
        parsed = urlparse(url)
        allowed_domains = (
            "tiktok.com", "tiktokcdn.com", "tokcdn.com",
            "tiktokcdn-us.com", "tiktokv.com", "byteoversea.com", "bytegecko-i18n.com",
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

    logger.info("Proxy-streaming video from: %s", url[:100])
    
    filename = "tiktok_video_nowatermark.mp4"
    
    # Try the original URL first, then fall back to CDN mirrors (in parallel)
    urls_to_try = [url]
    urls_to_try.extend(generate_mirror_urls(url))
    
    # Phase 1: Probe all URLs in parallel (fast, small requests)
    # This tests all mirrors at the same time — total time = time of the fastest working one
    probe_results = await asyncio.gather(
        *[_probe_cdn_url(u) for u in urls_to_try],
        return_exceptions=True,
    )
    
    # Find the first working URL
    working_url = None
    meta = None
    for i, result in enumerate(probe_results):
        if isinstance(result, dict) and result.get("_meta"):
            working_url = urls_to_try[i]
            meta = result
            break
    
    if working_url is None:
        # Server IP is blocked by TikTok CDN, but the user's browser (residential IP)
        # is NOT blocked. Redirect the browser directly to the CDN URL — the browser
        # can access it even when the server cannot.
        logger.info("Server blocked from all CDN domains, redirecting browser directly to: %s", url[:80])
        # Add headers to encourage download instead of inline play
        return RedirectResponse(
            url=url,
            status_code=302,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    
    if working_url != url:
        logger.info("Using CDN mirror: %s (original was %s)", working_url[:80], url[:80])
    
    content_type = meta.get("content_type", "video/mp4")
    content_length = meta.get("content_length", "")
    status_code = meta.get("status_code", 200)
    
    # Phase 2: Stream the full video from the working URL
    final_gen = _stream_video_from_cdn(working_url, filename)
    
    # Skip the meta yield from the new generator
    try:
        await final_gen.__anext__()
    except StopAsyncIteration:
        pass
    
    async def video_stream():
        async for chunk in final_gen:
            yield chunk
    
    response_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
    }
    if content_length:
        response_headers["Content-Length"] = content_length
    
    return StreamingResponse(
        video_stream(),
        media_type=content_type,
        headers=response_headers,
    )


# ─── Root endpoint ────────────────────────────────────────────────

@app.get("/")
async def root():
    """Root endpoint — returns API info."""
    return {"message": "TikTokExtract API v" + settings.APP_VERSION, "docs": "/docs"}
