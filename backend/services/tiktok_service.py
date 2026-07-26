"""
TikTok extraction service.

Primary engine: yt-dlp (metadata extraction only — no file download).
Fallback: httpx-based scraping when yt-dlp cannot resolve the URL.

All blocking yt-dlp calls are executed inside asyncio.to_thread so the
FastAPI event loop stays non-blocking.
"""

import asyncio
import json
import logging
import re
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
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    }


def _run_yt_dlp_blocking(url: str, ydl_opts: dict) -> Optional[dict]:
    """
    Run yt-dlp in a blocking fashion. Called from asyncio.to_thread.
    Returns the extracted info dict or None on failure.
    """
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return info
        except yt_dlp.utils.DownloadError as e:
            logger.warning("yt-dlp DownloadError for %s: %s", url, e)
            return None
        except Exception as e:
            logger.warning("yt-dlp unexpected error for %s: %s", url, e)
            return None


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

    # Try yt-dlp first
    info = await asyncio.to_thread(_run_yt_dlp_blocking, url, ydl_opts)

    if info and _info_has_video_data(info):
        return _normalise_single_info(info)

    # Fallback: try httpx scraping
    logger.info("yt-dlp could not resolve %s, trying httpx fallback", url)
    info = await _fallback_single_extract(url)

    if info and _info_has_video_data(info):
        return _normalise_single_info(info)

    raise RuntimeError(
        "Could not extract video information. The video may be private, deleted, or the region is restricted."
    )


def _info_has_video_data(info: dict) -> bool:
    """Check if the info dict has at least a title or download URL."""
    if info.get("title") or info.get("description"):
        return True
    if info.get("url"):
        return True
    formats = info.get("formats", []) or []
    return len(formats) > 0


async def _fallback_single_extract(url: str) -> Optional[dict]:
    """
    Fallback extraction using httpx.  Attempts to follow redirects and
    parse the page for canonical video metadata from TikTok's embedded JSON.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers={
                "User-Agent": settings.DEFAULT_USER_AGENT,
                "Referer": settings.DEFAULT_REFERER,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        # Try to extract the download URL from TikTok's embedded JSON
        # Pattern 1: __UNIVERSAL_DATA_FOR_REHYDRATION__
        match = re.search(
            r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>\s*(\{.+?)\s*</script>',
            html,
            re.DOTALL,
        )
        if match:
            try:
                data = json.loads(match.group(1))
                result = _parse_universal_data(data)
                if result:
                    return result
            except json.JSONDecodeError:
                pass

        # Pattern 2: SIGI_STATE (older TikTok format)
        match2 = re.search(
            r'<script\s+id="SIGI_STATE"[^>]*>\s*(\{.+?)\s*</script>',
            html,
            re.DOTALL,
        )
        if match2:
            try:
                data = json.loads(match2.group(1))
                result = _parse_sigi_state(data)
                if result:
                    return result
            except json.JSONDecodeError:
                pass

        # Pattern 3: SSR_HYDRATED_DATA
        match3 = re.search(
            r'window\.__SSR_HYDRATED_DATA__\s*=\s*(\{.+?\})\s*;?\s*</script>',
            html,
            re.DOTALL,
        )
        if match3:
            try:
                data = json.loads(match3.group(1))
                result = _parse_ssr_data(data)
                if result:
                    return result
            except json.JSONDecodeError:
                pass

    except httpx.HTTPStatusError as e:
        logger.warning("HTTP error during fallback for %s: %s", url, e.response.status_code)
    except Exception as exc:
        logger.warning("Fallback extraction failed for %s: %s", url, exc)

    return None


def _parse_universal_data(data: dict) -> Optional[dict]:
    """Parse TikTok's __UNIVERSAL_DATA_FOR_REHYDRATION__ embedded JSON."""
    try:
        default_scope = data.get("__DEFAULT_SCOPE__", {})
        webapp_data = default_scope.get("webapp.video-detail", {})
        item_info = webapp_data.get("itemInfo", {}).get("itemStruct", {})
        if not item_info:
            return None

        video_data = item_info.get("video", {})
        author_data = item_info.get("author", {})
        stats = item_info.get("stats", {})

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
            "title": item_info.get("desc", ""),
            "author": f"@{author_data.get('uniqueId', author_data.get('nickname', 'unknown'))}",
            "view_count": stats.get("playCount", 0),
            "thumbnail": video_data.get("cover", ""),
            "download_url": download_url,
            "duration": video_data.get("duration", 0),
        }
    except (KeyError, TypeError):
        return None


def _parse_sigi_state(data: dict) -> Optional[dict]:
    """Parse TikTok's older SIGI_STATE embedded JSON format."""
    try:
        item_module = data.get("ItemModule", {})
        if not item_module:
            return None
        # Get the first (and usually only) video item
        item_key = next(iter(item_module.keys()))
        item = item_module[item_key]

        video_data = item.get("video", {})
        author_data = item.get("author", "")
        stats = item.get("stats", {})

        download_url = video_data.get("downloadAddr", "")

        return {
            "title": item.get("desc", ""),
            "author": f"@{author_data}",
            "view_count": stats.get("playCount", 0),
            "thumbnail": video_data.get("cover", ""),
            "download_url": download_url,
            "duration": video_data.get("duration", 0),
        }
    except (KeyError, TypeError, StopIteration):
        return None


def _parse_ssr_data(data: dict) -> Optional[dict]:
    """Parse TikTok's SSR_HYDRATED_DATA embedded JSON."""
    try:
        # Navigate SSR data structure
        detail = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
        item_info = detail.get("itemInfo", {}).get("itemStruct", {})
        if not item_info:
            return None

        return _parse_universal_data(data)
    except (KeyError, TypeError):
        return None


def _normalise_single_info(info: dict) -> dict:
    """
    Normalise raw yt-dlp (or fallback) output into the shape expected by the frontend.
    """
    # Determine the best download URL
    download_url = _pick_download_url(info)
    if not download_url:
        download_url = info.get("webpage_url", info.get("url", ""))

    return {
        "title": safe_caption(info.get("title", info.get("description", ""))),
        "author": info.get("uploader", info.get("channel", info.get("author", "@unknown"))),
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

    # Fallback: use the best quality format URL
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

    # Try yt-dlp first
    info = await asyncio.to_thread(_run_yt_dlp_blocking, profile_url, ydl_opts)

    if info and info.get("entries"):
        return _normalise_bulk_info(info, username)

    # Fallback: try httpx scraping
    logger.info("yt-dlp could not resolve profile %s, trying httpx fallback", username)
    info = await _fallback_bulk_extract(username)

    if info and info.get("entries"):
        return _normalise_bulk_info(info, username)

    raise RuntimeError(
        f"Could not extract profile data for @{username}. "
        "Ensure the account is public and has videos available."
    )


def _normalise_bulk_info(info: dict, username: str) -> dict:
    """Normalise yt-dlp bulk output into the expected response shape."""
    entries = info.get("entries", []) or []
    entries = entries[:settings.MAX_BULK_VIDEOS]

    videos = []
    for entry in entries:
        videos.append(_normalise_bulk_entry(entry))

    return {
        "username": f"@{username}",
        "total_videos": len(videos),
        "videos": videos,
    }


async def _fallback_bulk_extract(username: str) -> Optional[dict]:
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
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ) as client:
            resp = await client.get(f"https://www.tiktok.com/@{username}")
            resp.raise_for_status()
            html = resp.text

        # Try __UNIVERSAL_DATA_FOR_REHYDRATION__
        match = re.search(
            r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>\s*(\{.+?)\s*</script>',
            html,
            re.DOTALL,
        )
        if match:
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
            except json.JSONDecodeError:
                pass

        # Try SIGI_STATE
        match2 = re.search(
            r'<script\s+id="SIGI_STATE"[^>]*>\s*(\{.+?)\s*</script>',
            html,
            re.DOTALL,
        )
        if match2:
            try:
                data = json.loads(match2.group(1))
                user_module = data.get("UserModule", {})
                user_info = user_module.get("users", {}).get(username, {})
                items = user_info.get("itemList", [])
                if items:
                    entries = []
                    for item in items[:settings.MAX_BULK_VIDEOS]:
                        video_data = item.get("video", {})
                        stats = item.get("stats", {})
                        entries.append({
                            "title": item.get("desc", ""),
                            "uploader": f"@{username}",
                            "view_count": stats.get("playCount", 0),
                            "duration": video_data.get("duration", 0),
                            "webpage_url": f"https://www.tiktok.com/@{username}/video/{item.get('id', '')}",
                        })
                    return {"entries": entries}
            except json.JSONDecodeError:
                pass

    except Exception as exc:
        logger.warning("Fallback bulk extraction failed for @%s: %s", username, exc)

    return None


def _normalise_bulk_entry(entry: dict) -> dict:
    """Normalise a single entry from yt-dlp bulk output."""
    # Try multiple paths for view count
    view_count = entry.get("view_count", 0)
    if not view_count:
        stats = entry.get("stats", {})
        view_count = stats.get("viewCount", stats.get("playCount", 0))

    return {
        "caption": safe_caption(entry.get("title", entry.get("description", ""))),
        "views": format_views(view_count),
        "duration": format_duration(entry.get("duration", 0)),
        "url": entry.get("webpage_url", entry.get("url", "")),
    }
