"""
TikTok extraction service.

Primary engine: yt-dlp (metadata extraction only — no file download).
Fallback: httpx-based scraping when yt-dlp cannot resolve the URL.

All blocking yt-dlp calls are executed inside asyncio.to_thread so the
FastAPI event loop stays non-blocking.
"""

import asyncio
import logging
import re
import time
from typing import Any, Optional

import httpx
import yt_dlp

from config import settings
from utils.formatters import (
    format_duration,
    format_views,
    is_valid_tiktok_url,
    normalise_username,
    safe_caption,
)

logger = logging.getLogger(__name__)


# ─── Shared yt-dlp options ────────────────────────────────────────

def _base_ydl_opts() -> dict:
    """Return the base yt-dlp options dictionary."""
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "socket_timeout": settings.YT_DLP_SO_TIMEOUT,
        "retries": settings.YT_DLP_MAX_RETRIES,
        "http_headers": {
            "User-Agent": settings.DEFAULT_USER_AGENT,
            "Referer": settings.DEFAULT_REFERER,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }


# ─── Single Video Extraction ──────────────────────────────────────

async def extract_single_video(url: str) -> dict[str, Any]:
    """
    Extract metadata for a single TikTok video.

    Returns a dict matching the ``SingleVideoResponse`` schema.
    Raises ``ValueError`` if the URL is invalid.
    Raises ``RuntimeError`` if extraction fails.
    """
    url = url.strip()
    if not is_valid_tiktok_url(url):
        raise ValueError(
            "Invalid TikTok URL. Please provide a valid link from tiktok.com, "
            "vm.tiktok.com, or vt.tiktok.com."
        )

    ydl_opts = _base_ydl_opts()
    try:
        info = await asyncio.to_thread(_run_yt_dlp, url, ydl_opts)
    except Exception as exc:
        logger.warning("yt-dlp failed for %s: %s — trying fallback", url, exc)
        info = await _fallback_single_extract(url)

    if not info:
        raise RuntimeError("Could not extract video information. The video may be private or deleted.")

    return _normalise_single_info(info)


def _run_yt_dlp(url: str, ydl_opts: dict) -> Optional[dict]:
    """Run yt-dlp in a blocking fashion (called from asyncio.to_thread)."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError:
            return None


async def _fallback_single_extract(url: str) -> Optional[dict]:
    """
    Fallback extraction using httpx.  Attempts to follow redirects and
    parse the page for canonical video metadata.
    This is a best-effort path — not guaranteed to work.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={
                "User-Agent": settings.DEFAULT_USER_AGENT,
                "Referer": settings.DEFAULT_REFERER,
            },
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        # Try to extract the download URL from TikTok's embedded JSON
        # TikTok embeds video data in a <script> tag with id="__UNIVERSAL_DATA_FOR_REHYDRATION__"
        match = re.search(
            r'__UNIVERSAL_DATA_FOR_REHYDRATION__\s*=\s*(\{.+?\})\s*</script>',
            html,
            re.DOTALL,
        )
        if match:
            import json
            try:
                data = json.loads(match.group(1))
                # Navigate TikTok's data structure
                default_scope = data.get("__DEFAULT_SCOPE__", {})
                webapp_data = default_scope.get("webapp.video-detail", {})
                item_info = webapp_data.get("itemInfo", {}).get("itemStruct", {})
                if item_info:
                    return _parse_tiktok_embed_data(item_info)
            except (json.JSONDecodeError, KeyError):
                pass

    except Exception as exc:
        logger.warning("Fallback extraction also failed: %s", exc)

    return None


def _parse_tiktok_embed_data(item: dict) -> dict:
    """Parse the TikTok embed JSON into our standard format."""
    video_data = item.get("video", {})
    author_data = item.get("author", {})
    stats = item.get("stats", {})

    # Find the watermark-free download URL
    download_url = ""
    for bit in video_data.get("bitrateInfo", []):
        candidate = bit.get("PlayAddr", {}).get("UrlList", [])
        if candidate:
            download_url = candidate[0]
            break
    if not download_url:
        download_url = video_data.get("downloadAddr", "")

    return {
        "title": item.get("desc", ""),
        "author": f"@{author_data.get('uniqueId', author_data.get('nickname', 'unknown'))}",
        "view_count": stats.get("playCount", 0),
        "thumbnail": video_data.get("cover", ""),
        "download_url": download_url,
        "duration": video_data.get("duration", 0),
    }


def _normalise_single_info(info: dict) -> dict:
    """
    Normalise raw yt-dlp (or fallback) output into the shape expected by the frontend.
    """
    # Determine the best download URL — prefer no-watermark variants
    download_url = _pick_download_url(info)
    if not download_url:
        # Fall back to the video URL itself if yt-dlp resolved it
        download_url = info.get("webpage_url", info.get("url", ""))

    return {
        "title": safe_caption(info.get("title", info.get("description", ""))),
        "author": info.get("uploader", info.get("channel", "@unknown")),
        "views": format_views(info.get("view_count", 0)),
        "thumbnail": info.get("thumbnail", ""),
        "download_url": download_url,
    }


def _pick_download_url(info: dict) -> str:
    """
    Select the best available video URL from yt-dlp output.
    Prefer formats tagged as no-watermark when available.
    """
    formats = info.get("formats", []) or []

    # Sort by resolution descending to get the best quality
    sorted_formats = sorted(
        formats,
        key=lambda f: (f.get("height", 0) or 0, f.get("vbr", 0) or 0),
        reverse=True,
    )

    # Look for TikTok-specific no-watermark URLs
    for fmt in sorted_formats:
        url = fmt.get("url", "")
        if url and ("no_watermark" in url.lower() or "play_addr" in url.lower()):
            return url

    # Fallback: just use the best quality format URL
    for fmt in sorted_formats:
        url = fmt.get("url", "")
        if url:
            return url

    return info.get("url", "")


# ─── Bulk Profile Extraction ──────────────────────────────────────

async def extract_bulk_profile(username_raw: str, delay: float = 1.0) -> dict:
    """
    Extract metadata for all public videos on a TikTok profile.

    Returns a dict matching ``BulkExtractResponse``.
    """
    username = normalise_username(username_raw)
    profile_url = f"https://www.tiktok.com/@{username}"

    ydl_opts = _base_ydl_opts()
    try:
        info = await asyncio.to_thread(_run_yt_dlp, profile_url, ydl_opts)
    except Exception as exc:
        logger.warning("yt-dlp failed for profile %s: %s — trying fallback", username, exc)
        info = await _fallback_bulk_extract(username, delay)

    if not info:
        raise RuntimeError(
            f"Could not extract profile data for @{username}. "
            "Ensure the account is public."
        )

    entries = info.get("entries", []) or []
    # Limit to configured maximum to avoid rate limiting
    entries = entries[: settings.MAX_BULK_VIDEOS]

    videos = []
    for entry in entries:
        await asyncio.sleep(delay)
        videos.append(_normalise_bulk_entry(entry))

    return {
        "username": f"@{username}",
        "total_videos": len(videos),
        "videos": videos,
    }


async def _fallback_bulk_extract(username: str, delay: float = 1.0) -> Optional[dict]:
    """
    Fallback bulk extraction using httpx scraping.
    Scrapes the profile page and attempts to parse embedded JSON data.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={
                "User-Agent": settings.DEFAULT_USER_AGENT,
                "Referer": settings.DEFAULT_REFERER,
            },
        ) as client:
            resp = await client.get(f"https://www.tiktok.com/@{username}")
            resp.raise_for_status()
            html = resp.text

        match = re.search(
            r'__UNIVERSAL_DATA_FOR_REHYDRATION__\s*=\s*(\{.+?\})\s*</script>',
            html,
            re.DOTALL,
        )
        if match:
            import json
            try:
                data = json.loads(match.group(1))
                default_scope = data.get("__DEFAULT_SCOPE__", {})
                user_profile = default_scope.get("webapp.user-detail", {})
                item_list = (
                    user_profile
                    .get("userInfo", {})
                    .get("user", {})
                    .get("itemList", [])
                )
                if item_list:
                    # Build a yt-dlp-compatible structure
                    entries = []
                    for item in item_list[:settings.MAX_BULK_VIDEOS]:
                        entries.append({
                            "title": item.get("desc", ""),
                            "uploader": f"@{username}",
                            "view_count": item.get("stats", {}).get("playCount", 0),
                            "duration": item.get("video", {}).get("duration", 0),
                            "webpage_url": f"https://www.tiktok.com/@{username}/video/{item.get('id', '')}",
                        })
                    return {"entries": entries}
            except (json.JSONDecodeError, KeyError):
                pass

    except Exception as exc:
        logger.warning("Fallback bulk extraction failed: %s", exc)

    return None


def _normalise_bulk_entry(entry: dict) -> dict:
    """Normalise a single entry from yt-dlp bulk output."""
    return {
        "caption": safe_caption(entry.get("title", entry.get("description", ""))),
        "views": format_views(entry.get("view_count", entry.get("stats", {}).get("viewCount", 0))),
        "duration": format_duration(entry.get("duration", 0)),
        "url": entry.get("webpage_url", entry.get("url", "")),
    }
