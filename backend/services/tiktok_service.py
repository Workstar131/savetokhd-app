"""
TikTok extraction service.

Primary engine: TikWM API (https://tikwm.com) — a free no-watermark
TikTok video extractor that works server-side without IP restrictions.

Fallbacks: yt-dlp and httpx-based scraping when TikWM is unavailable.

All blocking calls are executed inside asyncio.to_thread so the
FastAPI event loop stays non-blocking.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import quote

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

# ─── TikWM API Configuration ──────────────────────────────────────

TIKWM_SUBMIT_URL = "https://tikwm.com/api/video/task/submit"
TIKWM_RESULT_BASE = "https://tikwm.com/api/video/task/result?task_id="
TIKWM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://tikwm.com",
    "Referer": "https://tikwm.com/",
    "x-requested-with": "XMLHttpRequest",
}
TIKWM_POLL_INTERVAL = 1.0  # seconds
TIKWM_POLL_ATTEMPTS = 40   # max polls before giving up
TIKWM_REQUEST_TIMEOUT = 25  # seconds


# ─── URL Helpers ──────────────────────────────────────────────────

def normalize_tiktok_url(url: str) -> str:
    """Normalize a TikTok URL by stripping fragments, query params, trailing slashes."""
    u = url.strip()
    if "#" in u:
        u = u.split("#")[0]
    if "?" in u:
        u = u.split("?")[0]
    u = u.rstrip("/")
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u


def resolve_short_link(url: str) -> str:
    """Resolve vt.tiktok.com / vm.tiktok.com short links to canonical URLs."""
    if not re.search(r"https?://(vt|vm)\.tiktok\.com/", url, re.IGNORECASE):
        return url
    try:
        with httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": TIKWM_HEADERS["User-Agent"]}) as client:
            resp = client.get(url)
            if resp.url and "tiktok.com" in str(resp.url).lower():
                return str(resp.url)
    except Exception as e:
        logger.warning("Failed to resolve short link %s: %s", url, e)
    return url


def extract_video_id_from_url(url: str) -> Optional[str]:
    """Extract the numeric video ID from a TikTok URL."""
    m = re.search(r"/(?:video|photo)/(\d+)", url)
    return m.group(1) if m else None


def url_candidates(tiktok_url: str) -> list:
    """Generate multiple URL candidates for TikWM API submission."""
    normalized = normalize_tiktok_url(tiktok_url)
    video_id = extract_video_id_from_url(normalized)
    candidates = [normalized]
    if video_id:
        for u in (
            video_id,
            f"https://www.tiktok.com/video/{video_id}",
            f"https://www.tiktok.com/@tiktok/video/{video_id}",
            f"https://m.tiktok.com/v/{video_id}.html",
        ):
            if u not in candidates:
                candidates.append(u)
    return candidates


# ─── TikWM API ────────────────────────────────────────────────────

def _submit_tikwm_task(url: str) -> Optional[dict]:
    """
    Submit a TikTok URL to TikWM and poll for the result.
    Returns a dict with play_url, author info, and metadata, or None on failure.
    """
    candidates = url_candidates(url)
    username_from_url = "unknown"
    video_id_from_url = extract_video_id_from_url(url)

    for candidate in candidates:
        try:
            body = f"web=1&url={quote(candidate)}"
            with httpx.Client(timeout=TIKWM_REQUEST_TIMEOUT) as client:
                r = client.post(TIKWM_SUBMIT_URL, data=body, headers=TIKWM_HEADERS)
                r.raise_for_status()
                j = r.json()

            code = j.get("code")
            data = j.get("data") or {}
            task_id = data.get("task_id") if isinstance(data, dict) else None
            if code != 0 or not task_id:
                continue

            # Poll for result
            for _ in range(TIKWM_POLL_ATTEMPTS):
                time.sleep(TIKWM_POLL_INTERVAL)
                try:
                    with httpx.Client(timeout=TIKWM_REQUEST_TIMEOUT) as client:
                        poll = client.get(TIKWM_RESULT_BASE + str(task_id), headers=TIKWM_HEADERS)
                    if poll.status_code != 200:
                        continue
                    j2 = poll.json()
                    if j2.get("code") != 0 or not isinstance(j2.get("data"), dict):
                        continue

                    result_data = j2["data"]
                    status = result_data.get("status")

                    if status == 2:  # Ready
                        detail = result_data.get("detail") or result_data
                        play_url = (
                            detail.get("play_url")
                            or detail.get("url")
                            or detail.get("play")
                            or result_data.get("play_url")
                            or result_data.get("url")
                        )
                        author = detail.get("author") or result_data.get("author") or {}
                        username = (
                            author.get("unique_id")
                            or author.get("nickname")
                            or "unknown"
                        )
                        vid = (
                            detail.get("video_id")
                            or result_data.get("video_id")
                            or video_id_from_url
                            or "unknown"
                        )
                        if isinstance(vid, (int, float)):
                            vid = str(int(vid))
                        create_time = (
                            detail.get("create_time")
                            or detail.get("createTime")
                            or result_data.get("create_time")
                        )
                        desc = (
                            detail.get("title")
                            or detail.get("desc")
                            or detail.get("caption")
                            or result_data.get("title")
                            or result_data.get("desc")
                            or ""
                        )
                        images = detail.get("images") or result_data.get("images") or []

                        if play_url:
                            return {
                                "play_url": play_url,
                                "username": username,
                                "video_id": vid,
                                "create_time": create_time,
                                "desc": desc,
                                "images": images if isinstance(images, list) else [],
                            }
                    elif status == 3:  # Failed
                        break
                except Exception as e:
                    logger.debug("TikWM poll error: %s", e)
                    continue

        except Exception as e:
            logger.debug("TikWM submit error for candidate %s: %s", candidate, e)
            continue

    return None


def _tikwm_single_result_to_schema(result: dict) -> dict:
    """Convert TikWM single video result to the SingleVideoResponse schema."""
    return {
        "title": safe_caption(result.get("desc", "")),
        "author": f"@{result.get('username', 'unknown')}",
        "views": "0",
        "thumbnail": "",
        "download_url": result.get("play_url", ""),
    }


# ─── Single Video Extraction ──────────────────────────────────────

async def extract_single_video(url: str) -> dict[str, Any]:
    """
    Extract metadata for a single TikTok video.

    Strategy:
      1. Resolve short links (vt/vm) to canonical URLs
      2. Try TikWM API (works server-side, no IP restrictions)
      3. Fallback to yt-dlp
      4. Fallback to httpx scraping

    Returns a dict matching the SingleVideoResponse schema.
    Raises ValueError if the URL is invalid.
    Raises RuntimeError if extraction fails.
    """
    url = url.strip()
    if not is_valid_tiktok_url(url):
        raise ValueError(
            "Invalid TikTok URL. Please provide a valid link from tiktok.com, "
            "vm.tiktok.com, or vt.tiktok.com."
        )

    # Step 1: Resolve short links
    canonical_url = await asyncio.to_thread(resolve_short_link, url)
    logger.info("Resolved URL: %s -> %s", url[:60], canonical_url[:60])

    # Step 2: Try TikWM API (primary method)
    logger.info("Trying TikWM API for %s", canonical_url[:60])
    tikwm_result = await asyncio.to_thread(_submit_tikwm_task, canonical_url)

    if tikwm_result and tikwm_result.get("play_url"):
        logger.info("TikWM returned video: %s", tikwm_result.get("desc", "")[:60])
        return _tikwm_single_result_to_schema(tikwm_result)

    # Step 3: Try yt-dlp
    logger.info("TikWM failed, trying yt-dlp for %s", canonical_url[:60])
    ydl_opts = _base_ydl_opts()
    info = await asyncio.to_thread(_run_yt_dlp_blocking, canonical_url, ydl_opts)

    if info and _info_has_video_data(info):
        return _normalise_single_info(info, canonical_url)

    # Step 4: Fallback to httpx scraping
    logger.info("yt-dlp failed, trying httpx fallback for %s", canonical_url[:60])
    info = await _fallback_single_extract(canonical_url)

    if info and _info_has_video_data(info):
        return _normalise_single_info(info, canonical_url)

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

        # Pattern 2: SIGI_STATE
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
        item_key = next(iter(item_module.keys()))
        item = item_module[item_key]

        video_data = item.get("video", {})
        author_data = item.get("author", "")
        stats = item.get("stats", {})

        return {
            "title": item.get("desc", ""),
            "author": f"@{author_data}",
            "view_count": stats.get("playCount", 0),
            "thumbnail": video_data.get("cover", ""),
            "download_url": video_data.get("downloadAddr", ""),
            "duration": video_data.get("duration", 0),
        }
    except (KeyError, TypeError, StopIteration):
        return None


def _normalise_single_info(info: dict, original_url: str = "") -> dict:
    """Normalise raw yt-dlp or fallback output into the shape expected by the frontend."""
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
    """Select the best available video URL from yt-dlp output."""
    formats = info.get("formats", []) or []

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

    Uses TikWM API for each video URL discovered from the profile page.

    Returns a dict matching BulkExtractResponse.
    """
    username = normalise_username(username_raw)
    profile_url = f"https://www.tiktok.com/@{username}"

    # Step 1: Try yt-dlp first (fastest for bulk)
    ydl_opts = _base_ydl_opts()
    info = await asyncio.to_thread(_run_yt_dlp_blocking, profile_url, ydl_opts)

    if info and info.get("entries"):
        return _normalise_bulk_info(info, username)

    # Step 2: Try httpx scraping to get video list from profile page
    logger.info("yt-dlp could not resolve profile %s, trying httpx fallback", username)
    video_urls = await _get_profile_video_urls(username)

    if not video_urls:
        raise RuntimeError(
            f"Could not extract profile data for @{username}. "
            "Ensure the account is public and has videos available."
        )

    # Step 3: Use TikWM API for each video to get no-watermark URLs
    logger.info("Found %d videos for @%s, extracting via TikWM", len(video_urls), username)
    videos = []
    for i, vid_data in enumerate(video_urls[:settings.MAX_BULK_VIDEOS]):
        if i > 0:
            await asyncio.sleep(min(delay, 2.0))

        video_url = vid_data.get("url")
        if not video_url:
            continue

        # Try TikWM for this video
        tikwm_result = await asyncio.to_thread(_submit_tikwm_task, video_url)
        if tikwm_result and tikwm_result.get("play_url"):
            videos.append({
                "caption": safe_caption(vid_data.get("desc", tikwm_result.get("desc", ""))),
                "views": format_views(vid_data.get("view_count", 0)),
                "duration": format_duration(vid_data.get("duration", 0)),
                "url": tikwm_result.get("play_url", video_url),
            })
        else:
            # Fallback: just use the TikTok page URL
            videos.append({
                "caption": safe_caption(vid_data.get("desc", "")),
                "views": format_views(vid_data.get("view_count", 0)),
                "duration": format_duration(vid_data.get("duration", 0)),
                "url": video_url,
            })

    if not videos:
        raise RuntimeError(
            f"Could not extract profile data for @{username}. "
            "Ensure the account is public and has videos available."
        )

    return {
        "username": f"@{username}",
        "total_videos": len(videos),
        "videos": videos,
    }


async def _get_profile_video_urls(username: str) -> list[dict]:
    """
    Scrape a TikTok profile page to extract video URLs and metadata.
    Returns a list of dicts with url, desc, view_count, duration.
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
                user_info = user_profile.get("userInfo", {}).get("user", {})
                item_list = user_info.get("itemList", [])

                if item_list:
                    videos = []
                    for item in item_list[:settings.MAX_BULK_VIDEOS]:
                        video_data = item.get("video", {})
                        stats = item.get("stats", {})
                        video_id = item.get("id", "")
                        videos.append({
                            "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
                            "desc": item.get("desc", ""),
                            "view_count": stats.get("playCount", 0),
                            "duration": video_data.get("duration", 0),
                        })
                    return videos
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
                    videos = []
                    for item in items[:settings.MAX_BULK_VIDEOS]:
                        video_data = item.get("video", {})
                        stats = item.get("stats", {})
                        videos.append({
                            "url": f"https://www.tiktok.com/@{username}/video/{item.get('id', '')}",
                            "desc": item.get("desc", ""),
                            "view_count": stats.get("playCount", 0),
                            "duration": video_data.get("duration", 0),
                        })
                    return videos
            except json.JSONDecodeError:
                pass

    except Exception as exc:
        logger.warning("Profile scraping failed for @%s: %s", username, exc)

    return []


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


def _normalise_bulk_entry(entry: dict) -> dict:
    """Normalise a single entry from yt-dlp bulk output."""
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
    try:
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
    except Exception as e:
        logger.warning("yt-dlp init error for %s: %s", url, e)
        return None
