# TikTokExtract API — Backend

A production-grade FastAPI backend for TikTok video downloading and bulk profile extraction. This backend serves the **TikTokExtract** frontend and provides metadata extraction, watermark-free CDN proxying, and bulk profile scraping.

---

## Project Structure

```
tiktok-backend/
├── main.py                  # FastAPI app, middleware, CORS, endpoints
├── config.py                # Application settings and environment variables
├── requirements.txt         # Python dependencies
├── Dockerfile               # Container deployment configuration
├── render.yaml              # Render.com deployment manifest
├── services/
│   ├── __init__.py
│   └── tiktok_service.py    # Core extraction logic (yt-dlp + httpx fallback)
├── schemas/
│   ├── __init__.py
│   └── payload_models.py    # Pydantic request/response validation models
└── utils/
    ├── __init__.py
    └── formatters.py        # View count, duration, and URL helpers
```

---

## API Endpoints

| Method | Path                   | Description                              |
|--------|------------------------|------------------------------------------|
| GET    | `/api/health`          | Health check — returns status and version |
| POST   | `/api/download-single` | Extract single video metadata            |
| POST   | `/api/extract-bulk`    | Extract all public videos from a profile |
| GET    | `/api/proxy-download`  | Stream a video from TikTok CDN           |

---

## Quick Start

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The API documentation is available at `http://localhost:8000/docs`.

### Docker

```bash
docker build -t tiktokextract-api .
docker run -p 8000:8000 tiktokextract-api
```

### Deploy to Render.com

1. Push this repository to GitHub.
2. Create a new Web Service on Render.com.
3. Import the repository and use `render.yaml` as the build configuration.
4. Deploy.

---

## Endpoint Details

### POST `/api/download-single`

**Request Body:**
```json
{
  "url": "https://www.tiktok.com/@username/video/123456789"
}
```

**Response:**
```json
{
  "title": "Sample TikTok Video Title",
  "author": "@username",
  "views": "1.2M",
  "thumbnail": "https://p16-sign-va.tiktokcdn.com/...",
  "download_url": "https://v16-webapp-prime.tiktok.com/..."
}
```

### POST `/api/extract-bulk`

**Request Body:**
```json
{
  "username": "target_user",
  "delay": 1.0
}
```

**Response:**
```json
{
  "username": "@target_user",
  "total_videos": 25,
  "videos": [
    {
      "caption": "Video description text...",
      "views": "15.4K",
      "duration": "00:45",
      "url": "https://www.tiktok.com/@target_user/video/123456789"
    }
  ]
}
```

### GET `/api/proxy-download`

**Query Parameter:** `url` (URL-encoded TikTok CDN video URL)

Returns a streaming `video/mp4` response with the header:
```
Content-Disposition: attachment; filename="tiktok_video_nowatermark.mp4"
```

---

## Architecture Notes

- **yt-dlp** is used as the primary extraction engine for metadata-only parsing (no file download).
- A fallback scraping path using **httpx.AsyncClient** handles cases where yt-dlp cannot resolve the URL.
- All blocking yt-dlp operations run inside `asyncio.to_thread` to keep the FastAPI event loop non-blocking.
- CORS is configured with explicit allowed origins for the frontend.
- Custom HTTP exception handlers return JSON errors matching the frontend's `errorData.detail` pattern.
- The proxy-download endpoint validates that the target URL belongs to a TikTok domain before proxying.

---

## Environment Variables

| Variable       | Default                                              | Description                          |
|----------------|------------------------------------------------------|--------------------------------------|
| `DEBUG`        | `false`                                              | Enable debug logging                 |
| `CORS_ORIGINS` | `https://savetokhd-app.onrender.com,...`             | Comma-separated allowed origins      |
| `HOST`         | `0.0.0.0`                                            | Server bind address                  |
| `PORT`         | `8000`                                               | Server port                          |
