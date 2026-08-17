"""
TikTok extraction service.

Primary engine: TikWM V2 API (https://tikwm.com/api/) — returns hdplay URLs
from server-accessible CDN domains (tiktokcdn-us.com, tokcdn.com).
Fallback: TikWM V1 task-based API for videos that V2 can't parse.

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

# V2 API: Direct POST, returns result immediately (preferred)
TIKWM_V2_URL = "https://tikwm.com/api/"
# V1 API: Task-based submit + poll (fallback)
TIKWM_V1_SUBMIT_URL = "https://tikwm.com/api/video/task/submit"
TIKWM_V1_RESULT_BASE = "https://tikwm.com/api/video/task/result?task_id="

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


# ─── TikWM V2 API (Primary) ───────────────────────────────────────

def _submit_tikwm_v2(url: str) -> Optional[dict]:
    """
    Submit to TikWM V2 API (direct POST, returns result immediately).
    V2 reliably returns hdplay URLs from server-accessible CDN domains.
    """
    candidates = url_candidates(url)

    for candidate in candidates:
        try:
            body = f"url={quote(candidate)}&hd=1"
            with httpx.Client(timeout=TIKWM_REQUEST_TIMEOUT) as client:
                r = client.post(TIKWM_V2_URL, data=body, headers=TIKWM_HEADERS)
                r.raise_for_status()
                j = r.json()

            if j.get("code") != 0:
                logger.debug("V2 API failed for %s: %s", candidate[:60], j.get("msg"))
                continue

            data = j.get("data") or {}
            if not data:
                continue

            # V2 returns direct data (not task-based)
            # Prefer hdplay (server-accessible) over play (may be browser-session-dependent)
            hdplay = data.get("hdplay") or ""
            play = data.get("play") or ""
            final_url = hdplay or play

            if not final_url:
                continue

            cover = data.get("cover") or data.get("origin_cover") or data.get("dynamic_cover") or ""
            author = data.get("author") or {}
            if isinstance(author, dict):
                username = author.get("unique_id") or author.get("nickname") or "unknown"
            else:
                username = str(author) if author else "unknown"

            stats = data.get("stats") or {}
            view_count = stats.get("play_count") or stats.get("playCount") or 0
            duration = data.get("duration") or 0
            title = data.get("title") or ""

            return {
                "play_url": final_url,
                "cover": cover,
                "username": username,
                "video_id": str(data.get("id", "")) or extract_video_id_from_url(url) or "unknown",
                "create_time": data.get("create_time") or data.get("createTime"),
                "desc": title or safe_caption(data.get("title", "")),
                "images": data.get("images") or [],
                "view_count": int(view_count) if view_count else 0,
                "duration": int(duration) if duration else 0,
            }

        except Exception as e:
            logger.warning("V2 API error for %s: %s", candidate[:60], e)
            continue

    return None


# ─── TikWM V1 Task API (Fallback) ─────────────────────────────────

def _submit_tikwm_v1(url: str) -> Optional[dict]:
    """
    Submit to TikWM V1 task-based API (submit + poll).
    Used as fallback when V2 fails to parse the URL.
    """
    candidates = url_candidates(url)
    video_id_from_url = extract_video_id_from_url(url)

    for candidate in candidates:
        try:
            body = f"web=1&url={quote(candidate)}"
            with httpx.Client(timeout=TIKWM_REQUEST_TIMEOUT) as client:
                r = client.post(TIKWM_V1_SUBMIT_URL, data=body, headers=TIKWM_HEADERS)
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
                        poll = client.get(TIKWM_V1_RESULT_BASE + str(task_id), headers=TIKWM_HEADERS)
                    if poll.status_code != 200:
                        continue
                    j2 = poll.json()
                    if j2.get("code") != 0 or not isinstance(j2.get("data"), dict):
                        continue

                    result_data = j2["data"]
                    status = result_data.get("status")

                    if status == 2:  # Ready
                        detail = result_data.get("detail") or result_data

                        # Prefer hdplay over play_url
                        hdplay = detail.get("hdplay") or result_data.get("hdplay") or ""
                        play_url = (
                            detail.get("play_url")
                            or detail.get("url")
                            or detail.get("play")
                            or result_data.get("play_url")
                            or result_data.get("url")
                        )
                        final_url = hdplay or play_url

                        cover = (
                            detail.get("cover")
                            or detail.get("origin_cover")
                            or detail.get("dynamic_cover")
                            or result_data.get("cover")
                            or result_data.get("origin_cover")
                            or result_data.get("dynamic_cover")
                            or ""
                        )

                        author = detail.get("author") or result_data.get("author") or {}
                        if isinstance(author, dict):
                            username = (
                                author.get("unique_id")
                                or author.get("nickname")
                                or "unknown"
                            )
                        else:
                            username = author or "unknown"

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
                        stats = detail.get("stats") or result_data.get("stats") or {}
                        view_count = (
                            stats.get("play_count")
                            or stats.get("playCount")
                            or stats.get("view_count")
                            or detail.get("view_count")
                            or result_data.get("view_count")
                            or 0
                        )
                        duration = (
                            detail.get("duration")
                            or result_data.get("duration")
                            or 0
                        )

                        if final_url:
                            return {
                                "play_url": final_url,
                                "cover": cover,
                                "username": username,
                                "video_id": vid,
                                "create_time": create_time,
                                "desc": desc,
                                "images": images if isinstance(images, list) else [],
                                "view_count": int(view_count) if view_count else 0,
                                "duration": int(duration) if duration else 0,
                            }
                    elif status == 3:  # Failed
                        break

                except Exception:
                    continue

        except Exception as e:
            logger.warning("V1 API error for %s: %s", candidate[:60], e)
            continue

    return None


# ─── Combined TikWM Submission ────────────────────────────────────

def _submit_tikwm_task(url: str) -> Optional[dict]:
    """
    Submit to TikWM API. Tries V2 first (fast, direct), then V1 (task-based).
    Both prioritize hdplay URLs which are server-accessible.
    """
    # Try V2 first (direct, faster, returns hdplay)
    logger.debug("Trying TikWM V2 API for %s", url[:60])
    result = _submit_tikwm_v2(url)
    if result:
        return result

    # Fallback to V1 (task-based)
    logger.debug("V2 failed, trying TikWM V1 API for %s", url[:60])
    return _submit_tikwm_v1(url)


# ─── Schema Conversion ────────────────────────────────────────────

def _tikwm_single_result_to_schema(result: dict) -> dict:
    """Convert TikWM single video result to the SingleVideoResponse schema."""
    return {
        "title": safe_caption(result.get("desc", "")),
        "author": f"@{result.get('username', 'unknown')}",
        "views": format_views(result.get("view_count", 0)),
        "thumbnail": result.get("cover", ""),
        "download_url": result.get("play_url", ""),
    }


# ─── Single Video Extraction ──────────────────────────────────────

async def extract_single_video(url: str) -> dict[str, Any]:
    """
    Extract metadata for a single TikTok video.

    Strategy:
      1. Resolve short links (vt/vm) to canonical URLs
      2. Try TikWM V2 API (direct, returns hdplay)
      3. Fallback to TikWM V1 API (task-based)
      4. Fallback to yt-dlp
      5. Fallback to httpx scraping

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

    # Step 2: Try TikWM API (V2 primary, V1 fallback)
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
    Fallback extraction using httpx. Attempts to parse the page for
    canonical video metadata from TikTok's embedded JSON.
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

        # Pattern: __UNIVERSAL_DATA_FOR_REHYDRATION__
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

        # Pattern: SIGI_STATE
        match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not match:
            match = re.search(r'"SIGI_STATE":\s*(\{.+?\})\s*[,}]', html, re.DOTALL)
        if match:
            try:
                raw = match.group(1)
                data = json.loads(raw)
                result = _parse_sigi_state(data)
                if result:
                    return result
            except json.JSONDecodeError:
                pass

    except Exception as e:
        logger.warning("httpx fallback failed for %s: %s", url[:60], e)

    return None


def _parse_universal_data(data: dict) -> Optional[dict]:
    """Parse TikTok's __UNIVERSAL_DATA_FOR_REHYDRATION__ embedded JSON."""
    try:
        scope = data.get("__DEFAULT_SCOPE__", {})
        detail = scope.get("webapp.video-detail", {})
        item_info = detail.get("itemInfo", {}) or detail
        item_struct = item_info.get("itemStruct", {})

        if not item_struct:
            # Try alternative path
            item_struct = detail.get("itemStruct") or {}

        if not item_struct:
            return None

        video = item_struct.get("video", {})
        play_addr = video.get("playAddr", "")

        # Extract author
        author_data = item_struct.get("author", {})
        author_name = author_data.get("uniqueId") or author_data.get("nickname") or "unknown"

        # Extract stats
        stats = item_struct.get("stats", {})
        view_count = stats.get("playCount") or 0

        # Extract description/title
        desc = item_struct.get("desc") or ""

        # Extract cover
        cover = video.get("cover") or video.get("originCover") or video.get("dynamicCover") or ""

        # Extract duration
        duration = video.get("duration") or 0

        # Extract music (audio URL)
        music = item_struct.get("music", {})
        music_url = music.get("playUrl") or ""

        return {
            "title": desc or "TikTok Video",
            "author": f"@{author_name}",
            "description": desc,
            "views": format_views(view_count),
            "view_count": view_count,
            "thumbnail": cover,
            "duration": duration,
            "url": play_addr or music_url,
            "formats": [],
        }

    except Exception as e:
        logger.debug("Failed to parse universal data: %s", e)
        return None


def _parse_sigi_state(data: dict) -> Optional[dict]:
    """Parse TikTok's SIGI_STATE embedded JSON."""
    try:
        item_module = data.get("ItemModule", {})
        if not item_module:
            return None

        # Get first video item
        first_key = next(iter(item_module), None)
        if not first_key:
            return None

        item = item_module[first_key]
        video = item.get("video", {})

        play_addr = video.get("playAddr", "")
        author_name = item.get("author", "")
        desc = item.get("desc", "")
        stats = item.get("stats", {})
        view_count = stats.get("playCount") or 0
        cover = video.get("cover") or ""
        duration = video.get("duration") or 0

        return {
            "title": desc or "TikTok Video",
            "author": f"@{author_name}" if author_name else "@unknown",
            "description": desc,
            "views": format_views(view_count),
            "view_count": view_count,
            "thumbnail": cover,
            "duration": duration,
            "url": play_addr,
            "formats": [],
        }

    except Exception as e:
        logger.debug("Failed to parse SIGI_STATE: %s", e)
        return None


# ─── yt-dlp Helpers ───────────────────────────────────────────────

def _base_ydl_opts() -> dict:
    """Base yt-dlp options for metadata-only extraction."""
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "no_download": True,
        "format": "best/bestvideo+bestaudio/best",
        "noplaylist": True,
        "ignoreerrors": True,
        "http_headers": {
            "User-Agent": settings.DEFAULT_USER_AGENT,
            "Referer": settings.DEFAULT_REFERER,
        },
    }


def _run_yt_dlp_blocking(url: str, ydl_opts: dict) -> Optional[dict]:
    """Run yt-dlp in blocking mode (called via asyncio.to_thread)."""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                return info
    except Exception as e:
        logger.warning("yt-dlp failed for %s: %s", url[:60], e)
    return None


def _normalise_single_info(info: dict, canonical_url: str) -> dict:
    """Convert yt-dlp info dict to the SingleVideoResponse schema."""
    download_url = _pick_download_url(info)

    return {
        "title": safe_caption(info.get("title", "") or info.get("description", "") or "TikTok Video"),
        "author": f"@{info.get('uploader', info.get('channel', 'unknown'))}",
        "views": format_views(info.get("view_count", 0)),
        "thumbnail": info.get("thumbnail", ""),
        "download_url": download_url,
    }


def _pick_download_url(info: dict) -> str:
    """
    Select the best available video URL from yt-dlp output.
    Filters out audio-only formats to ensure video+audio is returned.
    """
    formats = info.get("formats", []) or []

    def _is_muxed(fmt: dict) -> bool:
        vcodec = (fmt.get("vcodec") or "").lower()
        acodec = (fmt.get("acodec") or "").lower()
        height = fmt.get("height") or 0
        is_audio_only = (
            acodec
            and acodec not in ("none", "")
            and (not vcodec or vcodec in ("none", ""))
            and not height
        )
        return not is_audio_only

    muxed_formats = [f for f in formats if _is_muxed(f)]
    if not muxed_formats:
        muxed_formats = formats

    sorted_formats = sorted(
        muxed_formats,
        key=lambda f: (f.get("height", 0) or 0, f.get("vbr", 0) or 0),
        reverse=True,
    )

    # Look for TikTok-specific no-watermark URLs first
    for fmt in sorted_formats:
        url = fmt.get("url", "")
        if url and ("no_watermark" in url.lower() or "play_addr" in url.lower()):
            return url

    # Fallback: use the best quality muxed format URL
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
    """
    username = normalise_username(username_raw)
    profile_url = f"https://www.tiktok.com/@{username}"

    # Resolve profile page to get video URLs
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers={
                "User-Agent": settings.DEFAULT_USER_AGENT,
                "Referer": settings.DEFAULT_REFERER,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ) as client:
            resp = await client.get(profile_url)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        raise RuntimeError(f"Could not access profile {username}: {e}")

    # Extract video URLs from the page
    video_urls = re.findall(r'https?://(?:www\.)?tiktok\.com/@[^/]+/video/(\d+)', html)
    video_urls = list(set(video_urls))

    if not video_urls:
        raise RuntimeError(
            f"No public videos found for @{username}. The profile may be private or empty."
        )

    # Extract each video using TikWM
    videos = []
    for video_id in video_urls:
        video_url = f"https://www.tiktok.com/@{username}/video/{video_id}"
        try:
            await asyncio.sleep(delay)
            result = await asyncio.to_thread(_submit_tikwm_task, video_url)
            if result and result.get("play_url"):
                videos.append({
                    "caption": safe_caption(result.get("desc", "")),
                    "views": format_views(result.get("view_count", 0)),
                    "duration": format_duration(result.get("duration", 0)),
                    "url": result.get("play_url", ""),
                })
        except Exception as e:
            logger.warning("Failed to extract video %s: %s", video_id, e)

    return {
        "username": username,
        "total_videos": len(videos),
        "videos": videos,
    }
