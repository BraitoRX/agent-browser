import copy
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from input_context import BackendError, CODE_STALE_REF, parse_selector
from runtime import CamoufoxRuntime, Tab, _DOM_CHUNK_SCRIPT
from worker import ACTION_FIELDS, BROWSER_ACTIONS, MUTATING_ACTIONS


class FakePage:
    url = "https://example.com/article"


class FakeHandle:
    def __init__(self, runtime):
        self.runtime = runtime
        self.disposed = False

    async def evaluate(self, script, state):
        if script != _DOM_CHUNK_SCRIPT:
            raise AssertionError("unexpected DOM evaluator")
        return self.runtime.chunk_result(state["start"], state["limit"])

    async def dispose(self):
        self.disposed = True


class PageRuntime(CamoufoxRuntime):
    def __init__(self):
        self.page = FakePage()
        self.tab = Tab("t1", self.page)
        self.tabs = {self.tab.tab_id: self.tab}
        self.active_id = self.tab.tab_id
        self.fingerprint = "2:feedbeef"

    def require_active(self):
        return self.page, self.tab

    def active_scope(self):
        return object()

    def _page_cache_key(self):
        return "t1|https://example.com/article|https://example.com/article"

    def _page_frame_metadata(self, page, tab):
        return {"url": page.url, "tabId": tab.tab_id, "frameId": "main"}

    async def _page_root_handle(self, frame, selector):
        return FakeHandle(self)

    async def _dom_root_handle_for_path(self, css_path):
        if css_path != "html:nth-of-type(1) > body:nth-of-type(1) > main:nth-of-type(1)":
            raise AssertionError("unexpected root path")
        return FakeHandle(self)

    def chunk_result(self, start, limit):
        records = [
            {
                "ref": "d1",
                "parentRef": None,
                "depth": 0,
                "tag": "main",
                "id": "article",
                "role": "main",
                "text": "",
                "textTruncated": False,
                "attributes": [],
                "omittedAttributes": 0,
                "childElementCount": 1,
                "cssPath": "html:nth-of-type(1) > body:nth-of-type(1) > main:nth-of-type(1)",
            },
            {
                "ref": "d2",
                "parentRef": "d1",
                "depth": 1,
                "tag": "p",
                "id": None,
                "role": None,
                "text": "Hello",
                "textTruncated": False,
                "attributes": [],
                "omittedAttributes": 0,
                "childElementCount": 0,
                "cssPath": "html:nth-of-type(1) > body:nth-of-type(1) > main:nth-of-type(1) > p:nth-of-type(1)",
            },
        ]
        root_css = "html:nth-of-type(1) > body:nth-of-type(1) > main:nth-of-type(1)"
        return {
            "nodes": copy.deepcopy(records[start:start + limit]),
            "total": 2,
            "totalElements": 2,
            "omittedNodes": 0,
            "truncated": False,
            "rootCss": root_css,
            "fingerprint": self.fingerprint,
        }


class PageInspectionTests(unittest.IsolatedAsyncioTestCase):
    def test_dom_ref_selector_namespace(self):
        spec = parse_selector("@d12")
        self.assertTrue(spec.dom_ref)
        self.assertEqual("d12", spec.ref)
        self.assertIsNone(spec.frame_prefix)
        with self.assertRaises(BackendError):
            parse_selector("@f1d12")

    def test_link_inventory_keeps_heading_context_and_urls(self):
        snapshot = """- heading \"Section\" [level=2] [ref=e1]
- paragraph:
  - link \"Details\" [ref=e2]:
    - /url: /details
"""
        refs = CamoufoxRuntime.extract_refs(snapshot)
        runtime = object.__new__(CamoufoxRuntime)
        links = runtime._build_link_inventory(refs, "https://example.com/article")
        self.assertEqual(
            [{
                "ref": "@e2",
                "text": "Details",
                "textTruncated": False,
                "rawUrl": "/details",
                "rawUrlTruncated": False,
                "url": "https://example.com/details",
                "urlTruncated": False,
                "section": {
                    "level": 2,
                    "text": "Section",
                    "ref": "@e1",
                    "textTruncated": False,
                },
            }],
            links,
        )

    async def test_dom_chunk_cursor_replay_and_revision_invalidation(self):
        runtime = PageRuntime()
        first = await runtime.page_dom_chunk(None, None, 1)
        self.assertEqual("@d1", first["nodes"][0]["ref"])
        self.assertEqual(2, first["total"])
        self.assertFalse(first["done"])
        cursor = first["nextCursor"]
        self.assertEqual(
            "html:nth-of-type(1) > body:nth-of-type(1) > main:nth-of-type(1)",
            runtime.tab.dom_refs["d1"],
        )

        second = await runtime.page_dom_chunk(None, cursor, 1)
        replay = await runtime.page_dom_chunk(None, cursor, 1)
        self.assertEqual(second["nodes"], replay["nodes"])
        self.assertEqual("@d2", second["nodes"][0]["ref"])
        self.assertEqual("@d1", second["nodes"][0]["parentRef"])
        self.assertTrue(second["done"])

        with self.assertRaises(BackendError):
            await runtime.page_dom_chunk(None, cursor, 2)

        runtime.fingerprint = "2:changed"
        with self.assertRaises(BackendError) as captured:
            await runtime.page_dom_chunk(None, cursor, 1)
        self.assertEqual(CODE_STALE_REF, captured.exception.code)
        self.assertEqual({}, runtime.tab.dom_refs)

    def test_worker_protocol_exposes_read_only_page_actions(self):
        self.assertTrue({"page_outline", "page_links", "dom_chunk"} <= BROWSER_ACTIONS)
        self.assertEqual({"selector"}, ACTION_FIELDS["page_outline"])
        self.assertEqual({"selector", "cursor", "limit"}, ACTION_FIELDS["page_links"])
        self.assertEqual({"selector", "cursor", "limit"}, ACTION_FIELDS["dom_chunk"])
        self.assertNotIn("page_outline", MUTATING_ACTIONS)
        self.assertNotIn("page_links", MUTATING_ACTIONS)
        self.assertNotIn("dom_chunk", MUTATING_ACTIONS)


if __name__ == "__main__":
    unittest.main()
