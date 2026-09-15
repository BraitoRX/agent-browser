import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
LIFECYCLE_TEST_DIR = BACKEND_DIR.parent / "test" / "camoufox"
for directory in (BACKEND_DIR, LIFECYCLE_TEST_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_lifecycle import FAKE_RUNTIME_DIR, FakeFrame, FakePage, make_runtime

with contextlib.redirect_stdout(io.StringIO()):
    import worker


class RefFallbackLocator:
    def __init__(self, matches=1, visible=True):
        self.matches = matches
        self.visible = visible
        self.box = {"x": 10.0, "y": 10.0, "width": 20.0, "height": 20.0}
        self.calls = []

    async def count(self):
        self.calls.append("count")
        return self.matches

    async def is_visible(self):
        self.calls.append("visible")
        return self.visible

    async def bounding_box(self, timeout=None):
        self.calls.append("box")
        return dict(self.box)

    async def scroll_into_view_if_needed(self, timeout=None):
        self.calls.append("scroll")

    async def hover(self, button=None, timeout=None, trial=None):
        self.calls.append("hover")

    async def click(self, button=None, timeout=None):
        self.calls.append("click")


class RefFallbackFrame(FakeFrame):
    def locator(self, selector):
        return self.page.locator_for_selector(selector)

    async def evaluate(self, script, arg=None):
        return 42


class RefFallbackPage(FakePage):
    def __init__(self):
        super().__init__()
        self.selectors = []
        self.aria_locator = None
        self.role_locator = None
        self.role_queries = []
        self.snapshot_text = None
        self.snapshot_ref = None
        self.snapshot_calls = 0
        self.remap_locator = None
        self.main_frame = RefFallbackFrame(self)
        self.frames = [self.main_frame]
        self.mouse = SimpleNamespace(up=AsyncMock())

    def locator_for_selector(self, selector):
        self.selectors.append(selector)
        if self.snapshot_ref is not None and selector == f"aria-ref={self.snapshot_ref}":
            self.remap_locator = RefFallbackLocator(matches=1)
            return self.remap_locator
        return self.aria_locator

    async def aria_snapshot(self, **kwargs):
        self.snapshot_calls += 1
        if self.snapshot_text is None:
            return ""
        return self.snapshot_text

    def locator(self, selector):
        return self.main_frame.locator(selector)

    def get_by_role(self, role, name=None):
        self.role_queries.append((role, name))
        if self.role_locator is None:
            raise AssertionError("unexpected role/name lookup")
        return self.role_locator

    async def evaluate(self, script):
        return {"x": 0, "y": 0, "w": 800, "h": 600, "dpr": 1}


class RefFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, _browser, _context, _page, self.tab = make_runtime()
        self.page = RefFallbackPage()
        self.tab.page = self.page
        self.instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        self.instance.runtime = self.runtime
        self.instance.load_gestures()

    async def request(self, action, **fields):
        with patch.object(worker, "write_response") as write:
            await self.instance.handle_line(json.dumps({"id": "fallback", "action": action, **fields}).encode())
        write.assert_called_once()
        return write.call_args.args[0]

    def expose_main_ref(self, ref, role="button", name="Continue"):
        self.tab.refs = {ref}
        self.tab.refs_meta = {ref: {"role": role, "name": name, "framePrefix": None}}

    async def test_fresh_ref_resolves_through_aria_ref_without_role_fallback(self):
        self.page.aria_locator = RefFallbackLocator(matches=1)
        self.expose_main_ref("e1")
        response = await self.request("click", selector="@e1")
        self.assertTrue(response["success"], response)
        self.assertEqual(["aria-ref=e1"], self.page.selectors)
        self.assertEqual([], self.page.role_queries)
        self.assertIn("count", self.page.aria_locator.calls)
        self.assertIn("click", self.page.aria_locator.calls)

    async def test_stale_ref_with_unique_role_name_match_falls_back_and_clicks(self):
        self.page.aria_locator = RefFallbackLocator(matches=0)
        self.page.role_locator = RefFallbackLocator(matches=1)
        self.expose_main_ref("e2", role="button", name="Continue")
        response = await self.request("click", selector="@e2")
        self.assertTrue(response["success"], response)
        self.assertEqual([("button", "Continue")], self.page.role_queries)
        self.assertGreaterEqual(self.page.role_locator.calls.count("count"), 1)
        self.assertIn("click", self.page.role_locator.calls)
        self.assertNotIn("click", self.page.aria_locator.calls)

    async def test_stale_ref_with_multiple_role_name_matches_is_a_stale_ref(self):
        self.page.aria_locator = RefFallbackLocator(matches=0)
        self.page.role_locator = RefFallbackLocator(matches=2)
        self.expose_main_ref("e3", role="link", name="Docs")
        response = await self.request("click", selector="@e3")
        self.assertEqual("camoufox_stale_ref", response["code"])
        self.assertIn("role/name fallback found 2 candidates", response["error"])
        self.assertIn("take a fresh snapshot", response["error"])
        self.assertEqual(0, self.runtime._input_attempts)
        self.assertFalse(self.instance.poisoned)

    async def test_stale_ref_with_no_role_name_match_is_a_stale_ref(self):
        self.page.aria_locator = RefFallbackLocator(matches=0)
        self.page.role_locator = RefFallbackLocator(matches=0)
        self.expose_main_ref("e4", role="textbox", name="Search")
        response = await self.request("click", selector="@e4")
        self.assertEqual("camoufox_stale_ref", response["code"])
        self.assertIn("role/name fallback found 0 candidates", response["error"])
        self.assertEqual(0, self.runtime._input_attempts)
        self.assertFalse(self.instance.poisoned)

    async def test_unknown_ref_keeps_the_not_exposed_stale_ref_error(self):
        self.page.aria_locator = RefFallbackLocator(matches=1)
        response = await self.request("click", selector="@e9")
        self.assertEqual("camoufox_stale_ref", response["code"])
        self.assertIn("was not exposed by the latest snapshot", response["error"])
        self.assertEqual([], self.page.selectors)
        self.assertEqual([], self.page.role_queries)

    async def test_frame_prefixed_ref_skips_the_role_fallback(self):
        self.page.aria_locator = RefFallbackLocator(matches=1)
        self.expose_main_ref("f1e2", role="button", name="Nested")
        self.tab.refs_meta["f1e2"]["framePrefix"] = "f1"
        response = await self.request("click", selector="@f1e2")
        self.assertTrue(response["success"], response)
        self.assertEqual(["aria-ref=f1e2"], self.page.selectors)
        self.assertEqual([], self.page.role_queries)
        self.assertIn("count", self.page.aria_locator.calls)
        self.assertIn("click", self.page.aria_locator.calls)

    async def test_remap_after_re_render_succeeds_and_rewrites_the_ref(self):
        self.page.aria_locator = RefFallbackLocator(matches=0)
        self.page.role_locator = RefFallbackLocator(matches=0)
        self.page.snapshot_text = '- button "Continue" [ref=e7]\n'
        self.page.snapshot_ref = "e7"
        self.expose_main_ref("e5", role="button", name="Continue")
        response = await self.request("click", selector="@e5")
        self.assertTrue(response["success"], response)
        self.assertIs(True, response["data"].get("remapped"))
        self.assertEqual("@e7", response["data"].get("newRef"))
        self.assertIn("aria-ref=e7", self.page.selectors)
        self.assertIsNotNone(self.page.remap_locator)
        self.assertIn("click", self.page.remap_locator.calls)
        self.assertNotIn("click", self.page.aria_locator.calls)
        self.assertNotIn("e5", self.tab.refs)
        self.assertIn("e7", self.tab.refs)
        self.assertEqual("button", self.tab.refs_meta["e7"]["role"])
        self.assertIsNone(self.tab.last_ref_remap)

    async def test_remap_with_ambiguous_snapshot_keeps_stale_ref(self):
        self.page.aria_locator = RefFallbackLocator(matches=0)
        self.page.role_locator = RefFallbackLocator(matches=0)
        self.page.snapshot_text = (
            '- button "Continue" [ref=e7]\n- button "Continue" [ref=e8]\n'
        )
        self.expose_main_ref("e5", role="button", name="Continue")
        response = await self.request("click", selector="@e5")
        self.assertEqual("camoufox_stale_ref", response["code"])
        self.assertIn("role/name fallback found 0 candidates", response["error"])
        self.assertIn("take a fresh snapshot", response["error"])
        self.assertEqual(0, self.runtime._input_attempts)
        self.assertFalse(self.instance.poisoned)
        self.assertNotIn("remapped", response.get("data", {}))

    async def test_remap_frame_prefixed_never_remapped(self):
        self.page.aria_locator = RefFallbackLocator(matches=0)
        self.expose_main_ref("f1e2", role="button", name="Nested")
        self.tab.refs_meta["f1e2"]["framePrefix"] = "f1"
        response = await self.request("click", selector="@f1e2")
        self.assertFalse(response["success"])
        self.assertEqual(0, self.page.snapshot_calls)
        self.assertEqual(["aria-ref=f1e2"], self.page.selectors)


if __name__ == "__main__":
    unittest.main()