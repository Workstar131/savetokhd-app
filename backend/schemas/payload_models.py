"""
Pydantic schema models for request validation and response serialization.
These models define the exact contract between frontend and backend.
"""

from pydantic import BaseModel, Field, HttpUrl
from typing import Optional


# ─── Request Models ───────────────────────────────────────────────

class SingleDownloadRequest(BaseModel):
    """
    Request body for POST /api/download-single.
    The frontend sends a single TikTok video URL.
    """
    url: str = Field(
        ...,
        description="Full TikTok video URL (e.g. https://www.tiktok.com/@user/video/123)",
        min_length=10,
        max_length=2048,
    )


class BulkExtractRequest(BaseModel):
    """
    Request body for POST /api/extract-bulk.
    The frontend sends a username and optional delay between requests.
    """
    username: str = Field(
        ...,
        description="TikTok username (with or without leading @)",
        min_length=1,
        max_length=64,
    )
    delay: float = Field(
        default=1.0,
        description="Delay in seconds between video extractions to avoid rate limits",
        ge=0.5,
        le=5.0,
    )


# ─── Response Models ──────────────────────────────────────────────

class SingleVideoResponse(BaseModel):
    """
    Response for POST /api/download-single.
    Fields used by the frontend to render the video card.
    """
    title: str = Field(description="Video title / caption text")
    author: str = Field(description="Author display name or handle")
    views: str = Field(description="Human-readable view count, e.g. '1.2M'")
    thumbnail: str = Field(description="Thumbnail image URL")
    download_url: str = Field(description="Direct CDN URL for the watermark-free video")


class BulkVideoEntry(BaseModel):
    """
    A single video entry within the bulk extraction response.
    """
    caption: str = Field(description="Video caption text")
    views: str = Field(description="Human-readable view count, e.g. '12.5K'")
    duration: str = Field(description="Duration formatted as MM:SS or H:MM:SS")
    url: str = Field(description="Full TikTok URL for the individual video")


class BulkExtractResponse(BaseModel):
    """
    Response for POST /api/extract-bulk.
    Contains the username, total count, and array of video entries.
    """
    username: str = Field(description="Normalized username (without @)")
    total_videos: int = Field(description="Total number of videos found")
    videos: list[BulkVideoEntry] = Field(description="List of extracted video metadata")


class HealthResponse(BaseModel):
    """
    Response for GET /api/health.
    """
    status: str = Field(default="healthy")
    version: str = Field(default="1.0.0")
