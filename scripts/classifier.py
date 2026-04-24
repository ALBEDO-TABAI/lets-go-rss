"""
LLM-based content classification module
Classifies RSS items into categories using AI, with keyword-based hot fallback.
"""

import os
import json
import time
from typing import List, Dict, Any, Optional


CATEGORIES = ["科技", "人文", "设计", "娱乐", "其他"]

# Strengthened Chinese keyword table — covers common Bilibili/Xiaohongshu/Weibo content
SIMPLE_KEYWORDS: Dict[str, List[str]] = {
    "科技": [
        # 通用
        "技术", "编程", "代码", "算法", "开发", "开源", "软件", "硬件", "芯片",
        "人工智能", "AI", "机器学习", "深度学习", "大模型", "LLM", "GPT", "Claude",
        "量子", "航天", "火箭", "科学", "物理", "生物", "化学", "实验", "研究",
        "数据库", "云", "服务器", "网络", "安全", "漏洞", "加密", "区块链", "比特币",
        "Linux", "macOS", "Windows", "iOS", "Android", "Docker", "Kubernetes",
        "前端", "后端", "全栈", "API", "SDK", "框架", "库", "GitHub", "Git",
        "评测", "测评", "性能", "基准", "跑分", "配置", "升级", "安装",
        # 英文
        "tech", "code", "programming", "software", "hardware", "algorithm",
        "machine learning", "deep learning", "neural", "model", "compute",
    ],
    "人文": [
        "文学", "历史", "哲学", "社会", "文化", "人文", "思想", "书籍", "阅读", "读书",
        "诗", "诗歌", "散文", "小说", "经典", "古籍", "古代", "近代", "现代史",
        "宗教", "信仰", "伦理", "心理", "心理学", "社会学", "人类学", "政治",
        "传统", "民俗", "考古", "博物", "博物馆", "文物", "遗产",
        "literature", "history", "philosophy", "culture", "society", "politics",
    ],
    "设计": [
        "设计", "UI", "UX", "交互", "平面", "产品设计", "工业设计", "字体", "排版",
        "品牌", "VI", "logo", "标志", "海报", "插画", "配色", "色彩",
        "艺术", "摄影", "视觉", "美学", "审美", "灵感", "灵感来源", "案例",
        "手绘", "素描", "水彩", "油画", "装置", "雕塑", "空间", "建筑", "室内",
        "design", "art", "photography", "visual", "typography", "illustration",
        "architecture", "interior", "brand", "poster",
    ],
    "娱乐": [
        # 游戏
        "游戏", "手游", "端游", "主机", "Steam", "Switch", "PS5", "Xbox",
        "原神", "王者荣耀", "英雄联盟", "LOL", "吃鸡", "PUBG", "我的世界", "Minecraft",
        "攻略", "通关", "速通", "直播", "主播",
        # 影视
        "电影", "影视", "剧集", "电视剧", "综艺", "动漫", "番剧", "动画",
        "漫威", "DC", "奥斯卡", "票房", "导演", "演员", "明星", "偶像",
        "解说", "影评", "剪辑", "预告片",
        # 音乐 / 体育 / 生活
        "音乐", "歌曲", "MV", "演唱会", "乐队", "歌手", "专辑",
        "体育", "足球", "篮球", "NBA", "世界杯", "奥运", "电竞",
        "美食", "做饭", "菜谱", "探店", "吃播",
        "穿搭", "时尚", "美妆", "护肤", "口红", "化妆",
        "旅行", "旅游", "vlog", "日常", "生活", "搞笑", "段子",
        "game", "gaming", "movie", "film", "music", "entertainment",
        "sport", "vlog", "fashion", "makeup", "food",
    ],
}


def _notify(msg: str) -> None:
    """Uniform error surfacing — prefixed so it's greppable in cron logs."""
    print(f"  ⚠️  [classifier] {msg}", flush=True)


def _classify_error_kind(exc: BaseException) -> str:
    """Bucket an anthropic/HTTP error into a short kind tag."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name or "rate_limit" in msg or "429" in msg:
        return "rate_limit"
    if "auth" in name or "401" in msg or "403" in msg or "invalid x-api-key" in msg or "api key" in msg:
        return "auth"
    if "timeout" in name or "timeout" in msg or "connect" in name or "network" in msg:
        return "network"
    if "notfound" in name or "404" in msg or "model" in msg and "not found" in msg:
        return "model_missing"
    return "other"


class ContentClassifier:
    """Classifies content using Claude API, with SimpleClassifier as hot fallback."""

    DEFAULT_MODEL = os.environ.get(
        "RSS_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001"
    )

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required")

        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.model = model or self.DEFAULT_MODEL
        self.categories = CATEGORIES
        self._simple = SimpleClassifier()  # hot fallback
        self._consecutive_errors = 0

        self.system_prompt = """你是一个内容分类专家。你的任务是将提供的内容分类到以下类别之一:

- 科技: 技术、编程、科学、AI、软件、硬件等
- 人文: 文学、历史、哲学、社会、文化等
- 设计: UI/UX、平面设计、产品设计、艺术、摄影等
- 娱乐: 游戏、影视、音乐、综艺、体育、美食、时尚、生活等
- 其他: 不属于以上类别的内容

请只返回类别名称,不要添加任何解释。如果内容跨越多个类别,选择最主要的一个。"""

    def _fallback(self, title: str, description: str, reason: str) -> str:
        """LLM → SimpleClassifier → 其他 三级回退。"""
        cat = self._simple.classify_item(title, description)
        _notify(f"LLM classification failed ({reason}); keyword fallback → {cat}")
        return cat

    def classify_item(self, title: str, description: str = "") -> str:
        """Classify a single item. Never silently returns 其他 on LLM errors —
        always goes through keyword fallback first."""
        content = f"标题: {title}\n"
        if description:
            content += f"描述: {description[:500]}"

        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=50,
                temperature=0,
                system=self.system_prompt,
                messages=[{"role": "user", "content": content}],
            )
            self._consecutive_errors = 0
            category = message.content[0].text.strip()

            if category in self.categories:
                return category
            for cat in self.categories:
                if cat in category:
                    return cat
            # LLM returned something unparseable — treat as soft failure
            return self._fallback(title, description, f"unparseable response: {category[:40]!r}")

        except self._anthropic.RateLimitError as e:
            self._consecutive_errors += 1
            # Adaptive sleep; let the caller's batch loop retry on next item
            sleep = min(5.0, 1.0 * self._consecutive_errors)
            _notify(f"rate_limit — sleeping {sleep:.1f}s")
            time.sleep(sleep)
            return self._fallback(title, description, "rate_limit")
        except Exception as e:
            self._consecutive_errors += 1
            return self._fallback(title, description, f"{_classify_error_kind(e)}: {e}")

    def classify_batch(self, items: List[Dict[str, Any]], batch_size: int = 5) -> List[Dict[str, Any]]:
        """Classify multiple items with rate limiting."""
        classified_items = []
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            for item in batch:
                item["category"] = self.classify_item(
                    item.get("title", ""),
                    item.get("description", ""),
                )
                classified_items.append(item)
            if i + batch_size < len(items):
                time.sleep(0.4)
        return classified_items


class SimpleClassifier:
    """Keyword-based classifier — also used as hot fallback for ContentClassifier."""

    def __init__(self):
        self.keywords = SIMPLE_KEYWORDS

    def classify_item(self, title: str, description: str = "") -> str:
        text = (title + " " + description).lower()
        scores = {cat: 0 for cat in self.keywords}
        for category, keywords in self.keywords.items():
            for keyword in keywords:
                if keyword.lower() in text:
                    scores[category] += 1
        max_score = max(scores.values())
        if max_score > 0:
            return max(scores, key=scores.get)
        return "其他"


def get_classifier(use_llm: bool = True) -> Any:
    """Factory function to get appropriate classifier."""
    if use_llm:
        try:
            return ContentClassifier()
        except ValueError:
            print("⚠️  ANTHROPIC_API_KEY not found, falling back to keyword-based classifier")
            return SimpleClassifier()
    return SimpleClassifier()
