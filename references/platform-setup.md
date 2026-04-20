# Platform Setup

## Basic Dependencies (YouTube + Vimeo + Behance)

```bash
pip install httpx yt-dlp
```

## Optional: AI Classification

```bash
pip install anthropic
export ANTHROPIC_API_KEY="your-key"
```

## Optional: Chinese Platforms (requires Docker)

Weibo, Douyin, Bilibili, and Xiaohongshu require a local RSSHub instance:

```bash
docker run -d --name rsshub -p 1200:1200 diygod/rsshub:chromium-bundled
export RSSHUB_BASE_URL="http://localhost:1200"
```

## Optional: Timeout Tuning (for bot timeout scenarios)

```bash
export RSS_HTTP_TIMEOUT="10"
export RSS_HTTP_RETRIES="2"
export RSS_XHS_TIMEOUT="6"
export RSS_XHS_RETRIES="1"
export RSS_YTDLP_TIMEOUT="12"
```
