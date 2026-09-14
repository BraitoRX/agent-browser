import asyncio
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
from gestures._common import STEP_TIMEOUT_MS, bounded
from input_context import BackendError, CODE_INVALID, CODE_TIMEOUT, is_playwright_timeout

with contextlib.redirect_stdout(io.StringIO()):
    import worker

PlaywrightTimeout = type("TimeoutError", (Exception,), {"__module__": "playwright._impl._errors"})
PlaywrightError = type("Error", (Exception,), {"__module__": "playwright._impl._errors"})


class DiagnosticLocator:
    def __init__(self):
        self.matches = 1
        self.visible = True
        self.box = {"x": 10.0, "y": 10.0, "width": 20.0, "height": 20.0}
        self.calls = []
        self.timeouts = []
        self.failures = {}
        self.scroll_moves = True
        self.snapshot_text = '- button "Continue" [ref=e2]'

    def record(self, stage, timeout=None):
        self.calls.append(stage)
        if timeout is not None:
            self.timeouts.append((stage, timeout))
        if stage in self.failures:
            raise self.failures[stage]

    async def count(self):
        self.record("count")
        return self.matches

    async def is_visible(self):
        self.record("visible")
        return self.visible

    async def bounding_box(self, timeout=None):
        self.record("box", timeout)
        return dict(self.box) if self.box is not None else None

    async def scroll_into_view_if_needed(self, timeout=None):
        self.record("scroll", timeout)
        if self.scroll_moves:
            self.box["y"] = 10.0

    async def click(self, button=None, timeout=None):
        self.record("click", timeout)

    async def dblclick(self, button=None, timeout=None):
        self.record("dblclick", timeout)

    async def evaluate(self, script, timeout=None):
        self.record("evaluate", timeout)

    async def aria_snapshot(self, **kwargs):
        self.record("snapshot", kwargs.get("timeout"))
        return self.snapshot_text


class DiagnosticFrame(FakeFrame):
    def __init__(self, page):
        super().__init__(page)
        self.evaluate_calls = []
        self.evaluate_failure = None

    def locator(self, selector):
        self.page.selectors.append(selector)
        return self.page.target

    async def evaluate(self, script):
        self.evaluate_calls.append(script)
        if self.evaluate_failure is not None:
            raise self.evaluate_failure
        return 42


class DiagnosticPage(FakePage):
    def __init__(self, locator):
        super().__init__()
        self.target = locator
        self.selectors = []
        self.main_frame = DiagnosticFrame(self)
        self.frames = [self.main_frame]
        self.mouse = SimpleNamespace(up=AsyncMock())

    def locator(self, selector):
        return self.main_frame.locator(selector)

    async def evaluate(self, script):
        return {"x": 0, "y": 0, "w": 800, "h": 600, "dpr": 1}


class ActionDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, _browser, _context, _page, self.tab = make_runtime()
        self.locator = DiagnosticLocator()
        self.page = DiagnosticPage(self.locator)
        self.tab.page = self.page
        self.instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        self.instance.runtime = self.runtime
        self.instance.load_gestures()

    async def request(self, action, **fields):
        with patch.object(worker, "write_response") as write:
            await self.instance.handle_line(json.dumps({"id": "diagnostics", "action": action, **fields}).encode())
        write.assert_called_once()
        return write.call_args.args[0]

    async def test_click_multiple_matches_fails_before_visibility_scroll_or_input(self):
        self.locator.matches = 2
        selector = 'a[href="https://es.wikipedia.org/"]'
        response = await self.request("click", selector=selector)
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn(selector, response["error"])
        self.assertIn("selector resolved to 2 elements", response["error"])
        self.assertEqual(["count"], self.locator.calls)
        self.assertEqual(0, self.runtime._input_attempts)
        self.assertFalse(self.instance.poisoned)

    async def test_click_missing_target_fails_before_input(self):
        self.locator.matches = 0
        response = await self.request("click", selector="#missing")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("selector resolved to 0 elements", response["error"])
        self.assertEqual(["count"], self.locator.calls)
        self.assertEqual(0, self.runtime._input_attempts)

    async def test_hidden_click_fails_before_box_scroll_or_input(self):
        self.locator.visible = False
        selector = '.interlanguage-link-target[lang="en"]'
        response = await self.request("click", selector=selector)
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn(selector, response["error"])
        self.assertIn("element is hidden", response["error"])
        self.assertEqual(["count", "visible"], self.locator.calls)
        self.assertEqual(0, self.runtime._input_attempts)

    async def test_click_in_viewport_skips_scroll_and_preserves_result(self):
        response = await self.request("click", selector="#target")
        self.assertTrue(response["success"], response)
        self.assertEqual(["count", "visible", "box", "click"], self.locator.calls)
        self.assertEqual({"x": 20.0, "y": 20.0}, response["data"]["point"])
        self.assertEqual([("box", STEP_TIMEOUT_MS), ("click", STEP_TIMEOUT_MS)], self.locator.timeouts)
        self.assertEqual([], self.runtime.journal.pending_buttons())

    async def test_double_click_uses_one_bounded_native_action(self):
        response = await self.request("dblclick", selector="#target")
        self.assertTrue(response["success"], response)
        self.assertEqual(["count", "visible", "box", "dblclick"], self.locator.calls)
        self.assertEqual(2, response["data"]["count"])
        self.assertIn(("dblclick", STEP_TIMEOUT_MS), self.locator.timeouts)

    async def test_click_outside_viewport_scrolls_once_and_remeasures(self):
        self.locator.box["y"] = 700.0
        response = await self.request("click", selector="#target")
        self.assertTrue(response["success"], response)
        self.assertEqual(["count", "visible", "box", "scroll", "count", "visible", "box", "click"], self.locator.calls)
        self.assertIn(("scroll", STEP_TIMEOUT_MS), self.locator.timeouts)

    async def test_scroll_timeout_reports_unstable_stage_without_dispatch(self):
        self.locator.box["y"] = 700.0
        self.locator.failures["scroll"] = PlaywrightTimeout("Timeout 2000ms exceeded.\nCall log:\n - element is not stable")
        response = await self.request("click", selector="#moving")
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertIn("scroll selector '#moving' into view", response["error"])
        self.assertIn("unstable", response["error"])
        self.assertIn("2000ms", response["error"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertEqual(0, self.runtime._input_attempts)
        self.assertFalse(self.instance.poisoned)

    async def test_click_still_outside_viewport_fails_without_dispatch(self):
        self.locator.box["y"] = 700.0
        self.locator.scroll_moves = False
        response = await self.request("click", selector="#outside")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("'#outside'", response["error"])
        self.assertIn("viewport after scrolling", response["error"])
        self.assertNotIn("click", self.locator.calls)
        self.assertEqual(0, self.runtime._input_attempts)

    async def test_covered_click_reports_pointer_interception_without_retry_or_reset(self):
        self.locator.failures["click"] = PlaywrightTimeout(
            "Locator.click: Timeout 2000ms exceeded.\nCall log:\n - element is not stable\n"
            " - element is visible, enabled and stable\n - <header> intercepts pointer events\n - retrying click action"
        )
        response = await self.request("click", selector="#p-lang-btn-label")
        self.assertEqual(CODE_TIMEOUT, response["code"])
        for text in ("#p-lang-btn-label", "covered by other content", "pointer events", "2000ms", "coordinate", "keyboard", "do not automatically retry"):
            self.assertIn(text, response["error"])
        self.assertNotIn("unstable", response["error"])
        self.assertNotIn("<header>", response["error"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertEqual(1, self.locator.calls.count("click"))
        self.page.mouse.up.assert_awaited_once_with(button="left")
        self.assertFalse(self.instance.poisoned)
        self.assertEqual([], self.runtime.journal.pending_buttons())

    async def test_click_timeout_with_failed_release_requires_a_reset(self):
        self.locator.failures["click"] = PlaywrightTimeout("Timeout 2000ms exceeded")
        self.page.mouse.up.side_effect = RuntimeError("release failed")
        response = await self.request("click", selector="#target")
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertTrue(response["inputAmbiguous"])
        self.assertEqual(["left"], self.runtime.journal.pending_buttons())
        self.assertEqual(1, self.locator.calls.count("click"))

    async def test_click_asyncio_cancellation_remains_a_reset_requiring_deadline(self):
        self.locator.failures["click"] = asyncio.TimeoutError()
        response = await self.request("click", selector="#target")
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "deadline"}, response["data"])
        self.assertTrue(response["inputAmbiguous"])
        self.assertIn("watchdog", response["error"])
        self.assertEqual(1, self.locator.calls.count("click"))

    async def test_click_ref_diagnostic_preserves_original_ref(self):
        self.tab.refs = {"e1"}
        self.locator.visible = False
        response = await self.request("click", selector="@e1")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("'@e1'", response["error"])
        self.assertEqual(["aria-ref=e1"], self.page.selectors)

    async def test_hidden_bounding_box_fails_before_native_measurement(self):
        self.locator.visible = False
        response = await self.request("boundingbox", selector="#hidden")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("selector '#hidden': element is hidden; it has no bounding box", response["error"])
        self.assertEqual(["count", "visible"], self.locator.calls)

    async def test_bounding_box_multiple_matches_fails_before_measurement(self):
        self.locator.matches = 2
        response = await self.request("boundingbox", selector="#duplicate")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("selector resolved to 2 elements", response["error"])
        self.assertEqual(["count"], self.locator.calls)

    async def test_bounding_box_missing_layout_fails_clearly(self):
        self.locator.box = None
        response = await self.request("boundingbox", selector="#vanished")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("#vanished", response["error"])
        self.assertIn("no bounding box", response["error"])

    async def test_bounding_box_zero_area_is_invalid(self):
        self.locator.box["width"] = 0.0
        response = await self.request("click", selector="#collapsed")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("no layout box", response["error"])
        self.assertEqual(["count", "visible", "box"], self.locator.calls)

    async def test_bounding_box_preserves_main_viewport_coordinates_without_scroll(self):
        self.locator.box = {"x": -10.0, "y": 700.0, "width": 40.0, "height": 15.0}
        response = await self.request("boundingbox", selector="#frame-target")
        self.assertTrue(response["success"], response)
        self.assertEqual({"boundingBox": self.locator.box}, response["data"])
        self.assertEqual([("box", STEP_TIMEOUT_MS)], self.locator.timeouts)
        self.assertEqual(["count", "visible", "box"], self.locator.calls)

    async def test_bounding_box_playwright_timeout_is_stage_specific(self):
        self.locator.failures["box"] = PlaywrightTimeout("Locator.bounding_box: Timeout 2000ms exceeded")
        response = await self.request("boundingbox", selector="#changed")
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertIn("measure bounding box of selector '#changed'", response["error"])
        self.assertIn("2000ms", response["error"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertFalse(self.instance.poisoned)

    async def test_typed_scroll_uses_visibility_precheck_and_bounded_rect_evaluation(self):
        response = await self.request("scrollintoview", selector="#target")
        self.assertTrue(response["success"], response)
        self.assertEqual({"scrolled": True, "selector": "#target"}, response["data"])
        self.assertEqual(["count", "visible", "evaluate"], self.locator.calls)
        self.assertEqual([("evaluate", STEP_TIMEOUT_MS)], self.locator.timeouts)

    async def test_typed_scroll_hidden_precheck_has_selector(self):
        self.locator.visible = False
        response = await self.request("scrollintoview", selector="#hidden")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("#hidden", response["error"])
        self.assertEqual(["count", "visible"], self.locator.calls)
        self.assertEqual(0, self.runtime._input_attempts)

    async def test_typed_scroll_playwright_timeout_does_not_become_a_deadline(self):
        self.locator.failures["evaluate"] = PlaywrightTimeout("Timeout 2000ms exceeded")
        response = await self.request("scrollintoview", selector="#target")
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertIn("scroll selector '#target' into view", response["error"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertFalse(self.instance.poisoned)

    async def test_leading_top_level_return_is_rejected_without_evaluation(self):
        for script in ("return document.title", " \ufeff /* note */ // note\n return 42;", "// return\u2028return\n42"):
            with self.subTest(script=script):
                response = await self.request("evaluate", script=script)
                self.assertEqual(CODE_INVALID, response["code"])
                self.assertIn("top-level return", response["error"])
                self.assertIn("IIFE", response["error"])
        self.assertEqual([], self.page.main_frame.evaluate_calls)

    async def test_eval_expressions_functions_and_multistatement_scripts_are_not_rewritten(self):
        scripts = (
            "document.title", "const value = 40; value + 2;", "(() => { return 42; })()",
            "() => { return 42; }", '"return 42"', "/return/.test('return')",
            "/* note */ returnValue", "return$", r"return\u0061", "// return 1",
        )
        for script in scripts:
            with self.subTest(script=script):
                response = await self.request("evaluate", script=script)
                self.assertTrue(response["success"], response)
                self.assertEqual(42, response["data"]["result"])
                self.assertIn("frameId", response["data"])
        self.assertEqual(list(scripts), self.page.main_frame.evaluate_calls)

    async def test_later_top_level_return_syntax_error_is_mapped_without_reexecution(self):
        self.page.main_frame.evaluate_failure = PlaywrightError("Frame.evaluate: SyntaxError: return not in function")
        response = await self.request("evaluate", script="const value = 42; return value;")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("IIFE", response["error"])
        self.assertEqual(1, len(self.page.main_frame.evaluate_calls))

    async def test_eval_timeout_has_stage_and_does_not_reexecute(self):
        self.page.main_frame.evaluate_failure = PlaywrightTimeout("Frame.evaluate: Timeout 21000ms exceeded")
        response = await self.request("evaluate", script="Promise.resolve(42)")
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertIn("browser action 'evaluate' timed out", response["error"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertEqual(1, len(self.page.main_frame.evaluate_calls))

    async def test_missing_scoped_snapshot_is_invalid_before_native_snapshot(self):
        self.locator.matches = 0
        response = await self.request("snapshot", selector="#p-lang")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("snapshot selector '#p-lang' does not match any element", response["error"])
        self.assertEqual(["count"], self.locator.calls)

    async def test_scoped_snapshot_disappearing_after_count_is_invalid(self):
        self.locator.failures["snapshot"] = PlaywrightError('Selector "#p-lang" does not match any element')
        response = await self.request("snapshot", selector="#p-lang")
        self.assertEqual(CODE_INVALID, response["code"])
        self.assertIn("snapshot selector '#p-lang' does not match any element", response["error"])
        self.assertEqual(["count", "snapshot"], self.locator.calls)

    async def test_scoped_snapshot_success_preserves_refs(self):
        response = await self.request("snapshot", selector="#target")
        self.assertTrue(response["success"], response)
        self.assertEqual(1, response["data"]["refCount"])
        self.assertEqual({"e2"}, self.tab.refs)
        self.assertEqual(self.locator.snapshot_text, response["data"]["snapshot"])

    async def test_bounded_and_deadline_convert_playwright_errors_with_operation_cause(self):
        async def failing_operation():
            raise PlaywrightTimeout("Timeout 1234ms exceeded\n - element is not visible")

        for wrapper in (
            lambda: bounded(worker._ActionBudget(22000), failing_operation(), "resolve selector '#hidden'", 1234),
            lambda: self.instance.deadline(failing_operation(), what="resolve selector '#hidden'"),
        ):
            with self.subTest(wrapper=wrapper):
                with self.assertRaises(BackendError) as raised:
                    await wrapper()
                self.assertEqual(CODE_TIMEOUT, raised.exception.code)
                self.assertTrue(is_playwright_timeout(raised.exception))
                self.assertFalse(raised.exception.deadline_exceeded)
                self.assertIn("'#hidden'", raised.exception.message)
                self.assertIn("1234ms", raised.exception.message)

    async def test_locator_timeout_reserves_margin_inside_short_action_budget(self):
        with patch.dict("os.environ", {"AGENT_BROWSER_ACTION_DEADLINE_MS": "1000"}):
            self.instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
            self.instance.runtime = self.runtime
            self.instance.load_gestures()
        response = await self.request("click", selector="#target")
        self.assertTrue(response["success"], response)
        for stage, timeout in self.locator.timeouts:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 750)
