"""
Utility helper functions for formatting view counts, durations, and URL validation.
These are pure functions with no external dependencies.
"""

import re
import math
from urllib.parse import urlparse


# ─── TikTok URL Validation ────────────────────────────────────────

# Matches common TikTok URL patterns including shortened (vm/vt) and full forms.
TIKTOK_URL_PATTERN = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/.*",
    re.IGNORECASE,
)


def is_valid_tiktok_url(url: str) -> bool:
    """
    Return True if *url* matches a recognised TikTok host and path.
    This is intentionally lenient — yt-dlp will do the heavy validation.
    """
    return bool(TIKTOK_URL_PATTERN.match(url.strip()))


def is_valid_tiktok_username(raw: str) -> bool:
    """
    Return True if *raw* looks like a TikTok username.
    Accepts bare usernames, with or without a leading '@'.
    """
    username = raw.lstrip("@").strip()
    # TikTok usernames are 2–24 chars: letters, numbers, underscores, periods
    return bool(re.match(r"^[a-zA-Z0-9._]{2,24}$", username))


def normalise_username(raw: str) -> str:
    """Strip leading '@' and whitespace, then lowercase for consistency."""
    return raw.lstrip("@").strip().lower()


# ─── View Count Formatter ─────────────────────────────────────────

def format_views(count) -> str:
    """
    Convert an integer (or numeric string) view count into a human-readable
    abbreviation such as ``12.5K``, ``1.1M``, ``342``.
    """
    if count is None:
        return "0"
    try:
        count = int(count)
    except (TypeError, ValueError):
        return str(count)

    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}K"
    if count < 1_000_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{count / 1_000_000_000:.1f}B"


# ─── Duration Formatter ───────────────────────────────────────────

def format_duration(seconds) -> str:
    """
    Format a duration (seconds) into ``MM:SS`` or ``H:MM:SS`` format.
    Returns ``0:00`` for None / invalid input.
    """
    if seconds is None:
        return "0:00"
    try:
        total = int(math.ceil(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"

    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ─── Caption / Description Normaliser ─────────────────────────────

def safe_caption(text) -> str:
    """
    Clean up a video caption for display: strip HTML, collapse whitespace,
    truncate at 200 characters.
    """
    if not text:
        return "No caption"
    # Remove common HTML tags that may leak through
    cleaned = re.sub(r"<[^>]+>", "", str(text))
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned
