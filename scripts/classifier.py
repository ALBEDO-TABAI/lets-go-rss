"""
LLM-based content classification module
Classifies RSS items into categories using AI
"""

import os
import json
from typing import List, Dict, Any, Optional
import anthropic


class ContentClassifier:
    """Classifies content using Claude API"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required")

        self.client = anthropic.Anthropic(api_key=self.api_key)

        # Define categories
        self.categories = ["科技", "人文", "设计", "娱乐", "其他"]

        # System prompt for classification
        self.system_prompt = """你是一个内容分类专家。你的任务是将提供的内容分类到以下类别之一:

- 科技: 技术、编程、科学、AI、软件、硬件等
- 人文: 文学、历史、哲学、社会、文化等
- 设计: UI/UX、平面设计、产品设计、艺术、摄影等
- 娱乐: 游戏、影视、音乐、综艺、体育等
- 其他: 不属于以上类别的内容

请只返回类别名称,不要添加任何解释。如果内容跨越多个类别,选择最主要的一个。"""

    def classify_item(self, title: str, description: str = "") -> str:
        """Classify a single item"""
        content = f"标题: {title}\n"
        if description:
            content += f"描述: {description[:500]}"  # Limit description length

        try:
            message = self.client.messages.create(
                model="claude-3-5-haiku-20241022",  # Use faster model for classification
                max_tokens=50,
                temperature=0,
                system=self.system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": content
                    }
                ]
            )

            category = message.content[0].text.strip()

            # Validate category
            if category in self.categories:
                return category
            else:
                # Try to match partial response
                for cat in self.categories:
                    if cat in category:
                        return cat
                return "其他"

        except Exception as e:
            print(f"Classification error: {e}")
            return "其他"

    def classify_batch(self, items: List[Dict[str, Any]], batch_size: int = 10) -> List[Dict[str, Any]]:
        """Classify multiple items with rate limiting"""
        classified_items = []

        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]

            for item in batch:
                category = self.classify_item(
                    item.get("title", ""),
                    item.get("description", "")
                )
                item["category"] = category
                classified_items.append(item)

            # Rate limiting - pause between batches
            if i + batch_size < len(items):
                import time
                time.sleep(1)

        return classified_items

    def classify_with_prompt(self, title: str, description: str = "") -> Dict[str, Any]:
        """Classify with detailed response including reasoning"""
        content = f"标题: {title}\n"
        if description:
            content += f"描述: {description[:500]}"

        enhanced_prompt = f"""{self.system_prompt}

请以JSON格式返回结果:
{{
  "category": "类别名称",
  "confidence": "high/medium/low",
  "reasoning": "简短的分类理由"
}}"""

        try:
            message = self.client.messages.create(
                model="claude-3-5-haiku-20241022",
                max_tokens=200,
                temperature=0,
                system=enhanced_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": content
                    }
                ]
            )

            response_text = message.content[0].text.strip()

            # Try to parse JSON response
            try:
                result = json.loads(response_text)
                if result.get("category") not in self.categories:
                    result["category"] = "其他"
                return result
            except json.JSONDecodeError:
                # Fallback to simple classification
                return {
                    "category": self.classify_item(title, description),
                    "confidence": "low",
                    "reasoning": "Simple classification fallback"
                }

        except Exception as e:
            print(f"Classification error: {e}")
            return {
                "category": "其他",
                "confidence": "low",
                "reasoning": str(e)
            }


class SimpleClassifier:
    """Fallback keyword-based classifier (no API required)"""

    def __init__(self):
        self.keywords = {
            "科技": ["技术", "编程", "代码", "AI", "人工智能", "软件", "硬件", "科学", "算法",
                   "tech", "code", "programming", "AI", "software", "hardware"],
            "人文": ["文学", "历史", "哲学", "社会", "文化", "人文", "思想", "书籍",
                   "literature", "history", "philosophy", "culture"],
            "设计": ["设计", "UI", "UX", "平面", "产品", "艺术", "摄影", "视觉",
                   "design", "art", "photography", "visual"],
            "娱乐": ["游戏", "影视", "电影", "音乐", "综艺", "体育", "娱乐",
                   "game", "movie", "music", "entertainment", "sport"],
        }

    def classify_item(self, title: str, description: str = "") -> str:
        """Classify based on keyword matching"""
        text = (title + " " + description).lower()

        scores = {cat: 0 for cat in self.keywords}

        for category, keywords in self.keywords.items():
            for keyword in keywords:
                if keyword.lower() in text:
                    scores[category] += 1

        max_score = max(scores.values())
        if max_score > 0:
            return max(scores, key=scores.get)
        else:
            return "其他"


def get_classifier(use_llm: bool = True) -> Any:
    """Factory function to get appropriate classifier"""
    if use_llm:
        try:
            return ContentClassifier()
        except ValueError:
            print("⚠️  ANTHROPIC_API_KEY not found, falling back to keyword-based classifier")
            return SimpleClassifier()
    else:
        return SimpleClassifier()
