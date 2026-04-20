---
name: lets-go-rss
description: "Lightweight multi-platform RSS subscription manager. Aggregates content updates from YouTube, Vimeo, Behance, Twitter/X, 知识星球, Bilibili, Weibo, Douyin, and Xiaohongshu with incremental deduplication and optional AI classification. Use when the user wants to subscribe to, fetch updates from, or manage RSS feeds across video and social media platforms — including cron-based background updates and cached status reads for bot integration."
---

# Let's Go RSS

Multi-platform RSS content aggregation tool with incremental updates, deduplication, and AI classification.

## Quick Start

### Add subscriptions
```bash
python3 scripts/lets_go_rss.py --add "https://www.youtube.com/@MatthewEncina"
python3 scripts/lets_go_rss.py --add "https://vimeo.com/xkstudio"
python3 scripts/lets_go_rss.py --add "https://www.behance.net/yokohara6e48"
```

### Update all feeds (slow — use crontab for background runs)
```bash
python3 scripts/lets_go_rss.py --update --no-llm --digest --skip-setup
```

### Read cached report (instant — for bot push)
```bash
python3 scripts/lets_go_rss.py --status
```

### View subscriptions
```bash
python3 scripts/lets_go_rss.py --list
python3 scripts/lets_go_rss.py --stats
```

## Bot Integration

`--update` takes 30–60 s to scrape all feeds. Decouple fetch and push: crontab runs updates ahead of time, bot reads the cache.

```bash
# Background update (built-in timeout + concurrency lock)
./scripts/run_update_cron.sh

# Bot push reads cache only
./scripts/run_status_push.sh
```

```bash
# crontab -e
55 */2 * * * cd /path/to/lets-go-rss && ./scripts/run_update_cron.sh >> /tmp/rss_cron.log 2>&1
0 */2 * * * cd /path/to/lets-go-rss && ./scripts/run_status_push.sh
```

For detailed bot output rules and formatting requirements, see [references/bot-reporting-rules.md](references/bot-reporting-rules.md).

## Platform Support

| Platform | Dependency | Ready |
|----------|------------|:-----:|
| YouTube | yt-dlp | ✅ |
| Vimeo | httpx | ✅ |
| Behance | httpx | ✅ |
| Twitter/X | Syndication API | ✅ |
| 知识星球 | pub-api | ✅ |
| Bilibili (B站) | RSSHub | ⚠️ |
| Weibo (微博) | RSSHub | ⚠️ |
| Douyin (抖音) | RSSHub | ⚠️ |
| Xiaohongshu (小红书) | RSSHub | ⚠️ |

For RSSHub setup, environment variables, and optional AI classification config, see [references/platform-setup.md](references/platform-setup.md).

## Output Files

| File | Purpose |
|------|---------|
| `assets/latest_update.md` | Update report (`--status` reads this) |
| `assets/feed.xml` | Standard RSS 2.0 XML |
| `assets/summary.md` | Statistics summary |
| `assets/subscriptions.opml` | OPML subscription export |

