# Bot Reporting Rules

When a bot needs to push RSS updates, run one command and forward the output verbatim.

## Workflow (2 steps)

```
Step 1: python3 scripts/lets_go_rss.py --status
Step 2: Send the command output as-is — no processing needed.
```

## Output Format

`--status` produces plain text like:

```
📡 RSS 更新摘要 | 2026-02-21 18:23 | 3 个账号有新内容

🆕 📺 影视飓风  02-18 03:00
   [【4K限免】你的新设备能顶住吗？](https://t.bilibili.com/1170572725010300960)

🆕 🐦 歸藏(guizang.ai)  02-14 17:15
   [Tweet by @op7418](https://x.com/op7418/status/2022721414462374031)

🎬 Matthew Encina  12-07 00:00
   [Why Moving on Helps You Grow](https://www.youtube.com/watch?v=xxxxx)
```

Each entry: emoji + account name + publish time → linked title. 🆕 marks accounts with new content.

## Prohibited Actions

- Do not reformat: no platform grouping, tables, or heading levels
- Do not split into multiple messages
- Do not remove or modify links
- Do not add preamble or closing text (e.g. "Here are the RSS updates")
- Do not run `--update` during push — read cache only

## No Updates Available

When `--status` shows no new updates, reply with a single line:

```
RSS 暂无新更新 ✅
```

Do not list each account's latest content — just confirm no updates.
