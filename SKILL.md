---
name: lets-go-rss
description: 轻量级全平台 RSS 订阅管理器。一键聚合 YouTube、Vimeo、Behance、Twitter/X、知识星球、B站、微博、抖音、小红书的内容更新，支持增量去重和 AI 智能分类。
---

# Let's Go RSS

全平台 RSS 内容聚合工具，支持增量更新、去重、AI 分类。

## 快速使用

### 添加订阅
```bash
python3 scripts/lets_go_rss.py --add "https://www.youtube.com/@MatthewEncina"
python3 scripts/lets_go_rss.py --add "https://vimeo.com/xkstudio"
python3 scripts/lets_go_rss.py --add "https://www.behance.net/yokohara6e48"
```

### 更新全部（耗时操作，建议用 crontab 后台跑）
```bash
python3 scripts/lets_go_rss.py --update --digest --skip-setup
```

### 读取缓存报告（Bot 推送用，瞬间返回）
```bash
python3 scripts/lets_go_rss.py --status
```

### 查看订阅
```bash
python3 scripts/lets_go_rss.py --list
python3 scripts/lets_go_rss.py --stats
```

### 查看全量概览（所有订阅最新一条）
```bash
python3 scripts/lets_go_rss.py --overview
```

### 健康体检 & 自动修复
```bash
python3 scripts/lets_go_rss.py --doctor      # 只打印状态
python3 scripts/lets_go_rss.py --auto-fix    # 自动重启挂掉的 RSSHub、清僵尸锁
```

## Bot 推送最佳实践

**问题**：`--update` 需要 30-60 秒抓取全部订阅，Bot 定时任务可能超时。

**方案**：抓取和推送解耦——crontab 提前跑更新，Bot 只读缓存文件。

### 稳定命令（推荐）
```bash
# 后台更新（内置超时参数 + 并发防重入锁）
./scripts/run_update_cron.sh

# Bot 推送只读缓存
./scripts/run_status_push.sh
```

```bash
# crontab -e
# 每 2 小时的 55 分更新（提前 5 分钟准备好数据）
55 */2 * * * cd /path/to/lets-go-rss && ./scripts/run_update_cron.sh >> /tmp/rss_cron.log 2>&1

# Bot 在整点读缓存推送（瞬间完成）
0 */2 * * * cd /path/to/lets-go-rss && ./scripts/run_status_push.sh
```

Bot 只需调用 `--status`，该命令直接读取 `assets/latest_update.md` 并输出内容，无需网络请求、无需等待。

## 平台支持

| 平台 | 主策略 | 备注 |
|------|--------|:----:|
| Vimeo | Native RSS | ✅ |
| Behance | Native RSS | ✅ |
| YouTube | yt-dlp / Atom feed fallback | ✅ |
| 微博 | RSSHub → Chrome session fallback | ⚠️ 需本机 Chrome 开启 remote debugging |
| 抖音 | RSSHub | ⚠️ 当前不稳定 |
| B站 | RSSHub | ⚠️ 需配置 |
| 小红书 | Chrome session primary | ⚠️ 需本机 Chrome 已登录且开启 remote debugging |
| Twitter/X | Syndication API | ✅ |
| 知识星球 | pub-api (公开) | ✅ |

## 安装依赖

```bash
# 基础（YouTube + Vimeo + Behance + Twitter syndication + Zsxq pub-api）
pip install httpx yt-dlp anthropic

# 配置环境变量：把 ANTHROPIC_API_KEY 写进 .env（已 git-ignore）
cp .env.example .env && $EDITOR .env

# 中国平台（B站/微博/抖音/小红书 RSSHub 路由）
# 方式 A（推荐）：skill 自管的 RSSHub，首次 start 自动 npm install
python3 scripts/rsshub_manager.py start
# 方式 B：若已经跑了 ready-cowork 的 :1200 RSSHub，skill 会自动作为 fallback
# 方式 C：自己起 Docker rsshub 并通过 RSSHUB_BASE_URL_FALLBACK 指过去

# 可选：启用真实 Chrome 会话抓取（微博 / 小红书推荐）
# 1) 打开 Chrome
# 2) 访问 chrome://inspect/#remote-debugging
# 3) 勾选 Allow remote debugging for this browser instance
```

### RSSHub 自管与升级

首次 `rsshub_manager.py start` 会：
1. 在 `<skill>/node_modules/rsshub` 下 `npm install rsshub@latest`(~50MB,首次 1-2 分钟)
2. 如未装,自动下载 puppeteer 要求的 Chrome for Testing(约 200MB 到 `~/.cache/puppeteer`)
3. 启动 `scripts/rsshub_worker.mjs` 监听 :1201,写 pidfile 到 `assets/.rsshub.pid`

后续 `run_update_cron.sh` 在每次 cron 前都会调 `start-if-needed`(最多等 10 秒),保证 :1201 健在。每周一早晨 06:xx 额外调 `update` 拉最新 RSSHub(后台跑,不阻塞该轮 cron)。

```bash
# 常用运维
python3 scripts/rsshub_manager.py status
python3 scripts/rsshub_manager.py restart
python3 scripts/rsshub_manager.py update      # npm install + restart
python3 scripts/rsshub_manager.py logs        # tail worker log
```

## Bot 汇报规范（⚠️ 必须严格遵守）

当 Bot 需要推送 RSS 更新时，**只需执行一个命令，然后原样转发输出**。

### 完整流程（仅 2 步）

```
步骤 1: 运行命令
python3 scripts/lets_go_rss.py --status

步骤 2: 把命令输出原封不动地作为你的回复发送
```

**就这么简单。不需要任何额外处理。**

### 输出格式说明

`--status` 命令会输出类似以下格式的纯文本（自动生成，不需要 Bot 构造）：

```
📡 RSS 增量更新 | 2026-02-21 18:23 | 3 个账号有新内容

🆕 📺 影视飓风  02-18 03:00
   [【4K限免】你的新设备能顶住吗？](https://t.bilibili.com/1170572725010300960)

🆕 🐦 歸藏(guizang.ai)  02-14 17:15
   [Tweet by @op7418](https://x.com/op7418/status/2022721414462374031)
```

仅显示有新内容的账号，无更新时显示"暂无新更新"。若需查看所有订阅的全量状态，使用 `--overview`。

### ❌ 禁止行为

- ❌ **不得重新排版**：不可以按平台分组、加表格、加标题 `#` 层级
- ❌ **不得分多条消息**：所有内容必须在一条消息内发送
- ❌ **不得删除/修改链接**：标题中的链接不可去掉或替换
- ❌ **不得添加前言后语**：不要加"以下是 RSS 更新"等多余文字
- ❌ **不得执行 --update**：推送时只读缓存，不做抓取

### ⏸️ 暂无更新时的处理

当 `--status` 输出中显示"暂无新更新"或类似表述时，**只需回复一句话**：

```
RSS 暂无新更新 ✅
```

**不需要列出各账号的最新内容**，直接说暂无更新即可。

---

## 输出文件

| 文件 | 说明 |
|------|------|
| `assets/latest_update.md` | 增量更新报告（`--status` 读取，仅含变动账号） |
| `assets/full_overview.md` | 全量概览（`--overview` 读取，含所有账号） |
| `assets/feed.xml` | 标准 RSS 2.0 XML |
| `assets/summary.md` | 统计摘要 |
| `assets/subscriptions.opml` | OPML 订阅导出 |

