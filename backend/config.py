"""
Application settings and environment configuration.
All tunable parameters are centralized here for easy management.
"""

import os


class Settings:
    # Application metadata
    APP_NAME: str = "TikTokExtract API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Server binding
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # CORS allowed origins — include all known frontend domains
    # Render.com proxies may present different origin headers, so we include
    # both the .onrender.com domain and the custom domain.
    CORS_ORIGINS: list[str] = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "https://savetokhd-app.onrender.com,https://savetokhd.com,http://localhost:5500,http://127.0.0.1:5500,http://localhost:3000",
        ).split(",")
        if origin.strip()
    ]

    # If "*" is in the env var, allow all origins (useful for debugging)
    CORS_ALLOW_ALL: bool = any(
        origin.strip() == "*"
        for origin in os.getenv("CORS_ORIGINS", "").split(",")
    )

    # yt-dlp configuration
    YT_DLP_MAX_RETRIES: int = 3
    YT_DLP_SO_TIMEOUT: int = 30

    # Rate-limiting safety
    MAX_BULK_VIDEOS: int = 50
    DEFAULT_BULK_DELAY: float = 1.0

    # Proxy streaming configuration
    PROXY_CHUNK_SIZE: int = 1024 * 1024  # 1 MB chunks

    # Spoofed headers for TikTok CDN access
    DEFAULT_USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    DEFAULT_REFERER: str = "https://www.tiktok.com/"

    # TikTok supported host patterns
    TIKTOK_HOSTS: list[str] = [
        r"(?:www\.|vm\.|vt\.|m\.)?tiktok\.com",
        r"(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/",
    ]


settings = Settings()
