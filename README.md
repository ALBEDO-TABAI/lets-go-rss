# Let's Go RSS 🛰️

> **AI-Powered Universal RSS Subscription Manager | AI 驱动的全平台 RSS 订阅管理器**

A lightweight RSS aggregator designed to work as a **Claude Skill** inside AI-powered IDEs and agents. Add subscriptions from 7 platforms, auto-update with deduplication, and get digest reports — all through simple CLI commands that your AI assistant can run for you.

一个轻量级 RSS 聚合工具，设计为 **Claude Skill** 在 AI IDE 和 Agent 中运行。支持 7 个平台的订阅管理、自动更新去重、智能摘要推送——通过简单的命令行指令，让你的 AI 助手自动完成。

---

## 🤖 Designed for AI IDEs | 为 AI IDE 设计

This Skill is built to be used with AI-powered coding environments:

本 Skill 设计为配合以下 AI 编程环境使用：

- **[Claude Code](https://claude.ai/code)** — Anthropic's AI coding agent (recommended)
- **[Cursor](https://cursor.sh)** — AI-first code editor
- **[Windsurf](https://codeium.com/windsurf)** — AI-powered IDE by Codeium
- **[OpenClaw](https://github.com/nicepkg/openclaw)** — Open-source Claude Code alternative

Just share this repo's URL with your AI assistant, and it will read `SKILL.md` to understand how to manage your RSS subscriptions automatically.

只需将本仓库 URL 分享给你的 AI 助手，它会读取 `SKILL.md` 并自动帮你管理 RSS 订阅。

---

## ✨ Features | 功能特性

| Feature | 功能 | Description |
|---------|------|-------------|
| 📡 7-Platform Support | 7 平台支持 | YouTube, Vimeo, Behance, Bilibili, Weibo, Douyin, Xiaohongshu |
| 🔄 Incremental Updates | 增量更新 | SQLite-based dedup, only fetches new content |
| 📋 Digest Mode | 摘要模式 | `--digest` shows latest 1 item per account |
| 🤖 AI Classification | AI 分类 | Optional Claude-powered topic categorization |
| 📰 Standard Output | 标准输出 | RSS 2.0 XML + Markdown reports |
| ⏰ Schedulable | 可定时 | Works with crontab for automated updates |

---

## 🚀 Quick Start | 快速开始

### Install | 安装

```bash
# Core dependencies | 核心依赖
pip install httpx yt-dlp
```

### Basic Usage | 基本使用

```bash
# Add subscriptions | 添加订阅
python3 scripts/lets_go_rss.py --add "https://www.youtube.com/@MatthewEncina"
python3 scripts/lets_go_rss.py --add "https://vimeo.com/xkstudio"
python3 scripts/lets_go_rss.py --add "https://www.behance.net/yokohara6e48"

# Update all | 更新全部
python3 scripts/lets_go_rss.py --update --no-llm

# Digest mode (1 item per account) | 摘要模式（每账号 1 条）
python3 scripts/lets_go_rss.py --update --no-llm --digest

# List subscriptions | 查看订阅
python3 scripts/lets_go_rss.py --list
```

---

## 🏗️ Architecture | 架构

```
┌──────────────────────────────────────────────────┐
│  Tier 1: Native RSS (zero dependency)            │
│  Vimeo / Behance → httpx reads RSS directly      │
├──────────────────────────────────────────────────┤
│  Tier 1b: yt-dlp (pip install)                   │
│  YouTube → yt-dlp extracts metadata              │
├──────────────────────────────────────────────────┤
│  Tier 2: RSSHub Proxy (optional Docker)          │
│  Weibo / Douyin / Bilibili / XHS → local RSSHub  │
└──────────────────────────────────────────────────┘
```

## 📊 Platform Support | 平台支持

| Platform | Method | Dependency | Ready? |
|----------|--------|------------|:------:|
| YouTube | yt-dlp | `pip install yt-dlp` | ✅ |
| Vimeo | Native RSS | `httpx` | ✅ |
| Behance | Native RSS | `httpx` | ✅ |
| Weibo 微博 | RSSHub | Docker | ⚠️ |
| Douyin 抖音 | RSSHub | Docker | ⚠️ |
| Bilibili B站 | RSSHub | Docker | ⚠️ |
| Xiaohongshu 小红书 | RSSHub | Docker | ⚠️ |

---

## 🇨🇳 Chinese Platforms Setup | 中国平台配置

For Weibo, Douyin, Bilibili, and Xiaohongshu, you need a self-hosted [RSSHub](https://docs.rsshub.app/):

使用微博、抖音、B站、小红书需要自建 [RSSHub](https://docs.rsshub.app/)：

```bash
docker run -d --name rsshub -p 1200:1200 diygod/rsshub:chromium-bundled
export RSSHUB_BASE_URL="http://localhost:1200"
```

---

## 📂 Project Structure | 项目结构

```
lets-go-rss/
├── SKILL.md              # Claude Skill entry point | AI 技能入口
├── README.md             # This file | 本文件
├── requirements.txt      # Python deps | Python 依赖
├── scripts/
│   ├── lets_go_rss.py    # Main entry | 主入口
│   ├── rss_engine.py     # Core engine | 核心引擎
│   ├── scrapers.py       # Platform scrapers | 平台爬虫
│   ├── database.py       # SQLite manager | 数据库
│   ├── classifier.py     # AI classification | AI 分类
│   ├── rss_generator.py  # XML generation | XML 生成
│   └── report_generator.py # Markdown reports | 报告生成
└── assets/               # Runtime data (gitignored) | 运行时数据
```

## ⏰ Scheduled Updates | 定时更新

```bash
# crontab -e — update every 2 hours | 每 2 小时更新
0 */2 * * * cd /path/to/lets-go-rss && python3 scripts/lets_go_rss.py --update --no-llm --digest
```

## 🤝 AI Classification (Optional) | AI 分类（可选）

```bash
pip install anthropic
export ANTHROPIC_API_KEY="your-key"

# Update with AI classification | 使用 AI 分类更新
python3 scripts/lets_go_rss.py --update
```

## License

MIT
