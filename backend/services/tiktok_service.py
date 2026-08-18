"""
TikTok extraction service.

Primary engine: TikWM V2 API (https://tikwm.com/api/) — returns hdplay URLs
from server-accessible CDN domains (tiktokcdn-us.com, tokcdn.com).
Rejects webapp-prime.us.tiktok.com URLs which always 403 on server requests.

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

TIKWM_V2_URL = "https://tikwm.com/api/"
TIKWM_V1_SUBMIT_URL = "https://tikwm.com/api/video/task/submit"
TIKWM_V1_RESULT_BASE = "https://tikwm.com/api/video/task/result?task_id="

TIKWM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://tikwm.com",
    "Referer": "https://tiktok.com/",
    "x-requested-with": "XMLHttpRequest",
}
TIKWM_POLL_INTERVAL = 1.0
TIKWM_POLL_ATTEMPTS = 40
TIKWM_REQUEST_TIMEOUT = 25

# CDN domains that are server-accessible (don't block non-browser IPs)
SERVER_ACCESSIBLE_CDN_DOMAINS = (
    "tiktokcdn-us.com",
    "tiktokcdn.com",
    "tokcdn.com",
    "byteoversea.com",
    "bytegecko-i18n.com",
)

# CDN domains that block server requests (browser-session-dependent)
BLOCKED_CDN_DOMAINS = (
    "webapp-prime",
    "webapp-us",
    "webapp-va",
)

# Alternative CDN mirror domains to try when primary fails
# TikTok CDN URLs are often accessible across multiple mirrors
CDN_MIRROR_DOMAINS = (
    "v16.tokcdn.com",
    "www.tiktok.com",
    "v16m.tiktokcdn.com",
    "v16.tiktokcdn.com",
    "v19.tiktokcdn.com",
)


def swap_cdn_domain(url: str, new_domain: str) -> str:
    """
    Replace the CDN hostname in a TikTok CDN URL with an alternative domain.
    
    Example:
        https://v16-notes.tiktokcdn-us.com/path/video.mp4
        -> https://v16.tokcdn.com/path/video.mp4
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    new_parsed = parsed._replace(netloc=new_domain)
    return urlunparse(new_parsed)


def generate_mirror_urls(url: str) -> list[str]:
    """
    Generate alternative CDN URLs by swapping the hostname with known mirrors.
    Returns a list of URLs to try, starting with the original.
    """
    urls = [url]
    for mirror in CDN_MIRROR_DOMAINS:
        urls.append(swap_cdn_domain(url, mirror))
    return urls


def test_url_accessible(url: str, timeout: int = 8) -> bool:
    """
    Quick test if a CDN URL is accessible (returns non-403 status).
    Uses a small Range request to minimize data transfer.
    Only considers it accessible if the content-type is video (not HTML).
    """
    headers = {
        "User-Agent": settings.DEFAULT_USER_AGENT,
        "Referer": settings.DEFAULT_REFERER,
        "Range": "bytes=0-0",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            if r.status_code in (403, 404):
                return False
            # Reject non-video responses (HTML error pages)
            ct = r.headers.get("content-type", "")
            return not ("text/html" in ct.lower())
    except Exception:
        return False


async def find_accessible_mirror(url: str, timeout: int = 8) -> Optional[str]:
    """
    Test the original URL and all mirror domains in parallel.
    Returns the first accessible URL found, or None if all fail.
    
    Since all requests run simultaneously, total time = time of the fastest
    working URL (not sum of all attempts). This avoids slowdown.
    """
    urls_to_test = generate_mirror_urls(url)
    
    async def _test(u: str) -> Optional[str]:
        if test_url_accessible(u, timeout):
            return u
        return None
    
    # Run all tests in parallel
    results = await asyncio.gather(*[_test(u) for u in urls_to_test], return_exceptions=True)
    
    # Return first working URL
    for r in results:
        if isinstance(r, str):
            return r
    return None


def _is_server_accessible_url(url: str) -> bool:
    """Check if a CDN URL is from a server-accessible domain."""
    lower_url = url.lower()
    if any(d in lower_url for d in BLOCKED_CDN_DOMAINS):
        return False
    if any(d in lower_url for d in SERVER_ACCESSIBLE_CDN_DOMAINS):
        return True
    # Default: allow if it has tiktok/tokcdn in domain
    return "tiktok" in lower_url or "tokcdn" in lower_url


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


def extract_username_from_url(url: str) -> Optional[str]:
    """Extract the username from a TikTok URL."""
    m = re.search(r"tiktok\.com/@([^/]+)/", url)
    return m.group(1) if m else None


def url_candidates(tiktok_url: str) -> list:
    """Generate multiple URL candidates for TikWM API submission."""
    normalized = normalize_tiktok_url(tiktok_url)
    video_id = extract_video_id_from_url(normalized)
    username = extract_username_from_url(normalized)

    candidates = [normalized]

    if video_id:
        # Add candidates with known usernames to improve TikWM parsing
        if username:
            candidates.append(f"https://www.tiktok.com/@{username}/video/{video_id}")
        candidates.append(f"https://www.tiktok.com/video/{video_id}")
        candidates.append(f"https://www.tiktok.com/@tiktok/video/{video_id}")
        candidates.append(f"https://m.tiktok.com/v/{video_id}.html")
        candidates.append(video_id)

    return candidates


# ─── TikWM V2 API (Primary) ───────────────────────────────────────

async def _submit_tikwm_v2(url: str) -> Optional[dict]:
    """
    Submit to TikWM V2 API. Returns result only if the CDN URL is
    from a server-accessible domain. Tries multiple URL candidates.
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

            # V2 returns: hdplay, play, cover, origin_cover, dynamic_cover, title, author, stats, etc.
            hdplay = data.get("hdplay") or ""
            play = data.get("play") or ""

            # Prefer hdplay (almost always server-accessible)
            # Only use play if hdplay is empty AND play is from an accessible domain
            final_url = ""
            if hdplay and _is_server_accessible_url(hdplay):
                final_url = hdplay
            elif play and _is_server_accessible_url(play):
                final_url = play
            elif hdplay:
                # hdplay exists but not accessible — try next candidate
                logger.debug("hdplay not server-accessible for %s, trying next candidate", candidate[:40])
                continue

            if not final_url:
                continue

            # Extract metadata
            cover = data.get("cover") or data.get("origin_cover") or data.get("dynamic_cover") or ""
            
            # Test if the URL is actually accessible, try mirrors in parallel if not
            if final_url and not test_url_accessible(final_url):
                logger.info("Primary CDN URL not accessible, trying mirrors for %s", final_url[:60])
                accessible = await find_accessible_mirror(final_url)
                if accessible:
                    logger.info("Found accessible mirror: %s", accessible[:80])
                    final_url = accessible
                else:
                    # No mirror worked, continue to next candidate
                    logger.debug("No accessible mirror found, trying next candidate")
                    continue

            if not final_url:
                continue

            author = data.get("author") or {}
            if isinstance(author, dict):
                username = author.get("unique_id") or author.get("nickname") or "unknown"
            else:
                username = str(author) if author else "unknown"

            stats = data.get("stats") or {}
            view_count = stats.get("play_count") or stats.get("playCount") or 0
            duration = data.get("duration") or 0
            title = data.get("title") or ""

            # Extract video_id from multiple sources (most reliable first)
            # 1. From the original TikTok URL (always works if URL is well-formed)
            video_id = extract_video_id_from_url(url) or ""
            # 2. From TikWM response fields
            if not video_id:
                video_id = str(data.get("id", "")) or str(data.get("aweme_id", "")) or ""
            # 3. From CDN URL path
            if not video_id and final_url:
                m = re.search(r"/(\d{15,20})[._]", final_url)
                if m:
                    video_id = m.group(1)
            # 4. Fallback
            if not video_id:
                video_id = "unknown"

            return {
                "play_url": final_url,
                "cover": cover,
                "username": username,
                "video_id": video_id,
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

async def _submit_tikwm_v1(url: str) -> Optional[dict]:
    """
    Submit to TikWM V1 task-based API.
    Only used if V2 fails entirely. Same domain filtering applies.
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
                    if result_data.get("status") == 3:  # Failed
                        break

                    if result_data.get("status") != 2:  # Not ready yet
                        continue

                    detail = result_data.get("detail") or result_data

                    hdplay = detail.get("hdplay") or result_data.get("hdplay") or ""
                    play_url = (
                        detail.get("play_url")
                        or detail.get("url")
                        or detail.get("play")
                        or result_data.get("play_url")
                        or result_data.get("url")
                        or ""
                    )

                    final_url = ""
                    if hdplay and _is_server_accessible_url(hdplay):
                        final_url = hdplay
                    elif play_url and _is_server_accessible_url(play_url):
                        final_url = play_url
                    elif hdplay:
                        # hdplay exists but not accessible — try next candidate
                        logger.debug("V1 hdplay not accessible, trying next candidate")
                        break

                    if not final_url:
                        continue

                    # Test if the URL is accessible, try mirrors in parallel if not
                    if final_url and not test_url_accessible(final_url):
                        accessible = await find_accessible_mirror(final_url)
                        if accessible:
                            final_url = accessible

                    if not final_url:
                        continue

                    cover = (
                        detail.get("cover")
                        or detail.get("origin_cover")
                        or detail.get("dynamic_cover")
                        or result_data.get("cover")
                        or ""
                    )
                    author = detail.get("author") or result_data.get("author") or {}
                    if isinstance(author, dict):
                        username = author.get("unique_id") or author.get("nickname") or "unknown"
                    else:
                        username = str(author) if author else "unknown"

                    stats = detail.get("stats") or result_data.get("stats") or {}
                    view_count = (
                        stats.get("play_count")
                        or stats.get("playCount")
                        or detail.get("view_count")
                        or result_data.get("view_count")
                        or 0
                    )
                    duration = detail.get("duration") or result_data.get("duration") or 0

                    return {
                        "play_url": final_url,
                        "cover": cover,
                        "username": username,
                        "video_id": str(video_id_from_url) if video_id_from_url else "unknown",
                        "create_time": detail.get("create_time") or result_data.get("create_time"),
                        "desc": detail.get("title") or detail.get("desc") or result_data.get("title") or "",
                        "images": detail.get("images") or result_data.get("images") or [],
                        "view_count": int(view_count) if view_count else 0,
                        "duration": int(duration) if duration else 0,
                    }

                except Exception:
                    continue

        except Exception as e:
            logger.warning("V1 API error for %s: %s", candidate[:60], e)
            continue

    return None


# ─── Combined TikWM Submission ────────────────────────────────────

async def _submit_tikwm_task(url: str) -> Optional[dict]:
    """
    Submit to TikWM. Tries V2 first (direct, server-accessible URLs).
    Falls back to V1 only if V2 completely fails.
    """
    logger.debug("Trying TikWM V2 API for %s", url[:60])
    result = await _submit_tikwm_v2(url)
    if result:
        return result

    # Only fall back to V1 if V2 failed entirely (not just bad domain)
    logger.debug("V2 failed entirely, trying TikWM V1 API for %s", url[:60])
    return await _submit_tikwm_v1(url)


# ─── Schema Conversion ────────────────────────────────────────────

# TikWM proxy URL template - their server proxies the video (bypasses CDN blocking)
TIKWM_PROXY_URL_TEMPLATE = "https://www.tikwm.com/video/media/play/{video_id}.mp4"


def _tikwm_single_result_to_schema(result: dict) -> dict:
    """Convert TikWM single video result to the SingleVideoResponse schema.
    
    Also generates the TikWM proxy URL which bypasses CDN IP blocking
    by routing the video through TikWM's own servers.
    """
    video_id = result.get("video_id", "")
    tikwm_proxy_url = ""
    if video_id and video_id != "unknown":
        tikwm_proxy_url = TIKWM_PROXY_URL_TEMPLATE.format(video_id=video_id)
    
    return {
        "title": safe_caption(result.get("desc", "")),
        "author": f"@{result.get('username', 'unknown')}",
        "views": format_views(result.get("view_count", 0)),
        "thumbnail": result.get("cover", ""),
        "download_url": result.get("play_url", ""),
        "tikwm_proxy_url": tikwm_proxy_url,
        "video_id": video_id,
    }


# ─── Single Video Extraction ──────────────────────────────────────

async def extract_single_video(url: str) -> dict[str, Any]:
    """
    Extract metadata for a single TikTok video.

    Strategy:
      1. Resolve short links (vt/vm) to canonical URLs
      2. Try TikWM V2 API (direct, server-accessible CDN URLs)
      3. Fallback to TikWM V1 API (task-based)
      4. Fallback to yt-dlp
      5. Fallback to httpx scraping

    Returns a dict matching the SingleVideoResponse schema.
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
    tikwm_result = await _submit_tikwm_task(canonical_url)

    if tikwm_result and tikwm_result.get("play_url"):
        logger.info("TikWM returned video from: %s", tikwm_result.get("play_url", "")[:60])
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
    """Fallback extraction using httpx — parse TikTok's embedded JSON."""
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
            return None

        video = item_struct.get("video", {})
        play_addr = video.get("playAddr", "")
        author_data = item_struct.get("author", {})
        author_name = author_data.get("uniqueId") or author_data.get("nickname") or "unknown"
        stats = item_struct.get("stats", {})
        view_count = stats.get("playCount") or 0
        desc = item_struct.get("desc") or ""
        cover = video.get("cover") or video.get("originCover") or video.get("dynamicCover") or ""
        duration = video.get("duration") or 0

        return {
            "title": desc or "TikTok Video",
            "author": f"@{author_name}",
            "description": desc,
            "views": format_views(view_count),
            "view_count": view_count,
            "thumbnail": cover,
            "duration": duration,
            "url": play_addr,
            "formats": [],
        }
    except Exception:
        return None


def _parse_sigi_state(data: dict) -> Optional[dict]:
    """Parse TikTok's SIGI_STATE embedded JSON."""
    try:
        item_module = data.get("ItemModule", {})
        if not item_module:
            return None

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
    except Exception:
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
    """Run yt-dlp in blocking mode."""
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
    Filters out audio-only formats and server-inaccessible CDN domains.
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

    def _is_accessible(fmt: dict) -> bool:
        url = fmt.get("url", "")
        return _is_server_accessible_url(url)

    # Filter: must be muxed AND from accessible CDN
    good_formats = [f for f in formats if _is_muxed(f) and _is_accessible(f)]

    # If no formats pass both filters, just use muxed
    if not good_formats:
        good_formats = [f for f in formats if _is_muxed(f)]

    # If still nothing, use all formats
    if not good_formats:
        good_formats = formats

    sorted_formats = sorted(
        good_formats,
        key=lambda f: (f.get("height", 0) or 0, f.get("vbr", 0) or 0),
        reverse=True,
    )

    for fmt in sorted_formats:
        url = fmt.get("url", "")
        if url:
            return url

    return info.get("url", "")


# ─── Bulk Profile Extraction ──────────────────────────────────────

async def extract_bulk_profile(username_raw: str, delay: float = 1.0) -> dict:
    """Extract metadata for all public videos on a TikTok profile."""
    username = normalise_username(username_raw)
    profile_url = f"https://www.tiktok.com/@{username}"

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

    video_ids = re.findall(r'https?://(?:www\.)?tiktok\.com/@[^/]+/video/(\d+)', html)
    video_ids = list(set(video_ids))

    if not video_ids:
        raise RuntimeError(
            f"No public videos found for @{username}. The profile may be private or empty."
        )

    videos = []
    for video_id in video_ids:
        video_url = f"https://www.tiktok.com/@{username}/video/{video_id}"
        try:
            await asyncio.sleep(delay)
            result = await _submit_tikwm_task(video_url)
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
