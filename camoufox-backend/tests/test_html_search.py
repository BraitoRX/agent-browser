import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from input_context import BackendError
from runtime import CamoufoxRuntime, Tab

PAGE_HTML = (
    "<html><head><title>Shop</title></head><body>"
    "<main id=\"catalog\"><p class=\"price\">Total: 42 USD</p>"
    "<p>Total due on delivery</p></main>"
    "<footer>Total items: 7</footer>"
    "</body></html>"
)


class FakeLocator:
    def __init__(self, html):
        self.html = html
        self.first = self

    async def evaluate(self, script):
        if "indexOf" in script:
            return PAGE_HTML.index(self.html)
        if "element.outerHTML" in script:
            return self.html
        return PAGE_HTML.index(self.html)


class SearchFrame:
    def __init__(self):
        self.locator_calls = []

    async def evaluate(self, script):
        return PAGE_HTML

    def locator(self, selector):
        self.locator_calls.append(selector)
        return FakeLocator("<main id=\"catalog\"><p class=\"price\">Total: 42 USD</p><p>Total due on delivery</p></main>")


class FakePage:
    url = "https://example.com/shop"

    def __init__(self):
        self.main_frame = SearchFrame()


class SearchRuntime(CamoufoxRuntime):
    def __init__(self):
        self.page = FakePage()
        self.tab = Tab("t1", self.page)

    def require_active(self):
        return self.page, self.tab

    def active_scope(self):
        return self.page.main_frame

    def _page_frame_metadata(self, page, tab):
        return {"url": page.url, "tabId": tab.tab_id, "frameId": "main"}


class HtmlSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_literal_search_case_insensitive(self):
        runtime = SearchRuntime()
        result = await runtime.page_html_search("total", None, None, 5, 120)
        self.assertEqual(3, result["totalMatches"])
        self.assertFalse(result["truncated"])
        self.assertEqual(len(PAGE_HTML), result["capturedChars"])
        self.assertTrue(all("total" in entry["excerpt"].lower() for entry in result["matches"]))
        self.assertEqual("p.price", result["matches"][0]["cssPath"])

    async def test_regex_overrides_query_and_invalid_regex_fails(self):
        runtime = SearchRuntime()
        result = await runtime.page_html_search("ignored", r"Total\s+\d+", None, 5, 60)
        self.assertTrue(all("Total" in entry["excerpt"] for entry in result["matches"]))

        with self.assertRaises(BackendError) as captured:
            await runtime.page_html_search("q", "([unclosed", None, 5, 60)
        self.assertIn("invalid regex", captured.exception.message)

    async def test_max_results_caps_and_truncates(self):
        runtime = SearchRuntime()
        result = await runtime.page_html_search("total", None, None, 2, 30)
        self.assertEqual(2, len(result["matches"]))
        self.assertEqual(3, result["totalMatches"])
        self.assertTrue(result["truncated"])

    async def test_selector_restricts_search_scope(self):
        runtime = SearchRuntime()
        result = await runtime.page_html_search("total", None, "main#catalog", 5, 60)
        self.assertEqual(2, result["totalMatches"])
        self.assertEqual(["main#catalog"], runtime.page.main_frame.locator_calls)

    async def test_output_budget_truncates_match_list(self):
        runtime = SearchRuntime()
        result = await runtime.page_html_search("t", None, None, 20, 400)
        self.assertLessEqual(
            len(result["matches"]),
            20,
        )
        if result["truncated"]:
            self.assertLess(len(result["matches"]), result["totalMatches"])


if __name__ == "__main__":
    unittest.main()