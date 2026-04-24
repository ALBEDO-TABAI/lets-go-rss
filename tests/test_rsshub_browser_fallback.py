import sys
import unittest


sys.path.insert(0, "/Users/maijiahui/.openclaw/workspace/skills/lets-go-rss/scripts")

from scrapers import DouyinScraper, WeiboScraper


class RSSHubBrowserFallbackTests(unittest.TestCase):
    def test_weibo_uses_browser_fallback_after_rsshub_503(self):
        scraper = WeiboScraper()
        scraper.get = lambda *args, **kwargs: (_ for _ in ()).throw(
            Exception("Server error '503 Service Unavailable' for url")
        )
        scraper._fetch_via_browser = lambda url: [
            {
                "item_id": "weibo_1",
                "title": "browser fallback item",
                "description": "",
                "link": "https://weibo.com/1",
                "pub_date": "",
                "metadata": {},
            }
        ]

        items = scraper.fetch_items("https://weibo.com/u/6182606334")

        self.assertEqual([item["title"] for item in items], ["browser fallback item"])

    def test_douyin_uses_browser_fallback_after_rsshub_503(self):
        scraper = DouyinScraper()
        scraper.get = lambda *args, **kwargs: (_ for _ in ()).throw(
            Exception("Server error '503 Service Unavailable' for url")
        )
        scraper._fetch_via_browser = lambda url: [
            {
                "item_id": "douyin_1",
                "title": "douyin browser fallback item",
                "description": "",
                "link": "https://douyin.com/video/1",
                "pub_date": "",
                "metadata": {},
            }
        ]

        items = scraper.fetch_items("https://www.douyin.com/user/MS4wLjABAAAA_test")

        self.assertEqual([item["title"] for item in items], ["douyin browser fallback item"])


if __name__ == "__main__":
    unittest.main()
