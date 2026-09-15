import asyncio
import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
LIFECYCLE_TEST_DIR = BACKEND_DIR.parent / "test" / "camoufox"
for directory in (BACKEND_DIR, LIFECYCLE_TEST_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_lifecycle import FAKE_RUNTIME_DIR, FakeFrame, make_runtime
from input_context import BackendError, CODE_INVALID, CODE_UNSUPPORTED
import runtime as runtime_module

with contextlib.redirect_stdout(io.StringIO()):
    import worker


class HoverLocator:
    def __init__(self):
        self.matches = 1
        self.visible = True
        self.box = {"x": 10.0, "y": 20.0, "width": 80.0, "height": 40.0}

    async def count(self):
        return self.matches

    async def is_visible(self):
        return self.visible

    async def bounding_box(self, timeout=None):
        return self.box


class HoverFrame(FakeFrame):
    def __init__(self, page, target):
        super().__init__(page)
        self.target = target
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return self.target


class HoverHoldTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, self.browser, self.context, self.page, self.tab = make_runtime()
        self.locator = HoverLocator()
        self.frame = HoverFrame(self.page, self.locator)
        self.page.main_frame = self.frame
        self.page.frames = [self.frame]
        self.page.locator = self.frame.locator
        self.page.mouse = SimpleNamespace(move=AsyncMock(), up=AsyncMock())
        self.instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        self.instance.runtime = self.runtime

    async def asyncTearDown(self):
        await self.runtime.ambient_hover.stop()

    async def request(self, action, **fields):
        return await self.instance.run_action(action, {"id": "hold", "action": action, **fields})

    def start_ambient(self, max_ms=30000):
        self.runtime.ambient_hover.start(self.page, self.tab, 50.0, 40.0, max_ms)
        return self.runtime.ambient_hover._task

    def test_protocol_fields_and_classification(self):
        self.assertTrue({"hover_hold", "hover_hold_stop"} <= worker.BROWSER_ACTIONS)
        self.assertEqual({"selector", "maxMs"}, worker.ACTION_FIELDS["hover_hold"])
        self.assertEqual(set(), worker.ACTION_FIELDS["hover_hold_stop"])
        self.assertIn("hover_hold", worker.MUTATING_ACTIONS)
        self.assertNotIn("hover_hold_stop", worker.MUTATING_ACTIONS)
        self.assertEqual({
            "click", "dblclick", "fill", "type", "press", "hover", "focus", "check", "uncheck",
            "select", "drag", "scroll", "scrollintoview", "download", "waitfordownload", "dialog",
            "gesture", "hover_hold",
        }, worker.INPUT_AMBIENT_STOP)
        self.assertEqual({
            "navigate", "back", "forward", "reload", "tab_new", "tab_switch", "tab_close", "frame", "mainframe",
        }, worker.LIFECYCLE_AMBIENT_STOP)

    async def test_start_returns_point_default_duration_and_input_diagnostics(self):
        result = await self.request("hover_hold", selector="#player")
        self.assertTrue(result["holding"])
        self.assertEqual({"x": 50.0, "y": 40.0}, result["point"])
        self.assertEqual(30000, result["maxMs"])
        self.assertFalse(result["replaced"])
        self.assertTrue(result["diagnostics"]["inputDispatched"])
        self.assertEqual(1, result["diagnostics"]["inputDispatchCount"])
        self.assertEqual(1, self.runtime._input_attempts)
        self.page.mouse.move.assert_awaited_once_with(50.0, 40.0)
        self.assertIsNotNone(self.runtime.ambient_hover._task)
        self.assertFalse(self.runtime.action_in_flight)

    async def test_start_resolves_selector_in_selected_frame(self):
        selected = HoverFrame(self.page, self.locator)
        self.tab.selected_frame = selected
        self.page.frames.append(selected)
        result = await self.request("hover_hold", selector="xpath=//video", maxMs=120000)
        self.assertEqual(["xpath=//video"], selected.selectors)
        self.assertEqual([], self.frame.selectors)
        self.assertEqual(120000, result["maxMs"])

    async def test_start_resolves_exposed_ref(self):
        self.tab.refs = {"e1"}
        result = await self.request("hover_hold", selector="@e1", maxMs=1000)
        self.assertTrue(result["holding"])
        self.assertEqual(["aria-ref=e1"], self.frame.selectors)

    async def test_replacement_cancels_the_previous_task(self):
        await self.request("hover_hold", selector="#player")
        previous = self.runtime.ambient_hover._task
        self.locator.box["x"] = 30.0
        result = await self.request("hover_hold", selector="#other", maxMs=1000)
        self.assertTrue(result["replaced"])
        self.assertTrue(previous.done())
        self.assertIsNot(previous, self.runtime.ambient_hover._task)
        self.assertEqual({"x": 70.0, "y": 40.0}, result["point"])
        self.assertEqual(1000, result["maxMs"])

    async def test_stop_returns_duration_then_reports_no_active_hold(self):
        await self.request("hover_hold", selector="#player")
        self.runtime.ambient_hover._started_at -= 0.125
        previous = self.runtime.ambient_hover._task
        result = await self.request("hover_hold_stop")
        self.assertTrue(result["stopped"])
        self.assertIsInstance(result["heldMs"], int)
        self.assertGreaterEqual(result["heldMs"], 125)
        self.assertTrue(previous.done())
        self.assertIsNone(self.runtime.ambient_hover._task)
        self.assertEqual({"stopped": False, "heldMs": 0}, await self.request("hover_hold_stop"))

    async def test_stop_works_without_an_active_tab_or_when_poisoned(self):
        self.start_ambient()
        self.runtime.active_id = None
        self.instance.poison("test")
        result = await self.request("hover_hold_stop")
        self.assertTrue(result["stopped"])

    async def test_start_invalidates_captures_and_dom_refs_but_stop_does_not(self):
        self.runtime.captures.register({"url": self.page.url})
        self.tab.dom_refs = {"d1": "#player"}
        self.tab.refs = {"e1"}
        await self.request("hover_hold", selector="#player")
        self.assertEqual(0, self.runtime.captures.count())
        self.assertEqual({}, self.tab.dom_refs)
        self.assertEqual({"e1"}, self.tab.refs)
        self.runtime.captures.register({"url": self.page.url})
        self.tab.dom_refs = {"d2": "#player"}
        await self.request("hover_hold_stop")
        self.assertEqual(1, self.runtime.captures.count())
        self.assertEqual({"d2": "#player"}, self.tab.dom_refs)
        self.assertEqual({"e1"}, self.tab.refs)

    async def test_hidden_missing_ambiguous_and_zero_box_targets_are_rejected(self):
        for matches, visible, box in [
            (0, True, self.locator.box),
            (2, True, self.locator.box),
            (1, False, self.locator.box),
            (1, True, None),
            (1, True, {"x": 0, "y": 0, "width": 0, "height": 10}),
            (1, True, {"x": 0, "y": 0, "width": 10, "height": 0}),
        ]:
            with self.subTest(matches=matches, visible=visible, box=box):
                self.locator.matches = matches
                self.locator.visible = visible
                self.locator.box = box
                with self.assertRaises(BackendError) as raised:
                    await self.request("hover_hold", selector="#player")
                self.assertEqual(CODE_INVALID, raised.exception.code)
                self.assertIsNone(self.runtime.ambient_hover._task)
                self.assertFalse(self.runtime.action_in_flight)
        self.page.mouse.move.assert_not_awaited()
        self.assertEqual(0, self.runtime._input_attempts)

    async def test_start_field_validation(self):
        fields = [{}, {"selector": ""}, {"selector": "  "}, {"selector": 42}]
        fields.extend({"selector": "#player", "maxMs": value} for value in [
            999, 120001, -1, True, "30000", 30000.0, None,
        ])
        for values in fields:
            with self.subTest(values=values):
                with self.assertRaises(BackendError) as raised:
                    await self.request("hover_hold", **values)
                self.assertEqual(CODE_INVALID, raised.exception.code)
                self.assertFalse(self.runtime.action_in_flight)
        self.page.mouse.move.assert_not_awaited()

    async def test_unknown_fields_and_stop_fields_are_rejected(self):
        for action, fields in [
            ("hover_hold", {"selector": "#player", "bogus": True}),
            ("hover_hold", {"selector": "#player", "max-ms": 1000}),
            ("hover_hold_stop", {"selector": "#player"}),
            ("hover_hold_stop", {"maxMs": 1000}),
        ]:
            with self.subTest(action=action, fields=fields):
                with self.assertRaises(BackendError) as raised:
                    await self.request(action, **fields)
                self.assertEqual(CODE_UNSUPPORTED, raised.exception.code)
                self.assertFalse(self.runtime.action_in_flight)

    async def test_input_and_lifecycle_actions_cancel_before_dispatch(self):
        previous = None

        async def dispatch(action, payload):
            self.assertTrue(previous.done(), action)
            self.assertIsNone(self.runtime.ambient_hover._task, action)
            self.assertTrue(self.runtime.action_in_flight, action)
            return {"replaced": False}

        self.instance.dispatch_browser = dispatch
        self.instance.dispatch_local = dispatch
        for action in sorted(worker.INPUT_AMBIENT_STOP | worker.LIFECYCLE_AMBIENT_STOP):
            with self.subTest(action=action):
                previous = self.start_ambient()
                result = await self.request(action)
                if action == "hover_hold":
                    self.assertTrue(result["replaced"])
                self.assertFalse(self.runtime.action_in_flight)

    async def test_find_input_subactions_cancel_before_dispatch(self):
        previous = None

        async def dispatch(action, payload):
            self.assertTrue(previous.done())
            self.assertIsNone(self.runtime.ambient_hover._task)
            return {}

        self.instance.dispatch_browser = dispatch
        for action in sorted(worker.FIND_ACTIONS):
            for subaction in [None, "click", "fill", "check", "hover"]:
                with self.subTest(action=action, subaction=subaction):
                    previous = self.start_ambient()
                    fields = {} if subaction is None else {"subaction": subaction}
                    await self.request(action, **fields)

    async def test_read_actions_preserve_the_hold_and_set_in_flight(self):
        previous = self.start_ambient()

        async def dispatch(action, payload):
            self.assertIs(previous, self.runtime.ambient_hover._task, action)
            self.assertFalse(previous.done(), action)
            self.assertTrue(self.runtime.action_in_flight, action)
            return {}

        self.instance.dispatch_browser = dispatch
        self.instance.dispatch_local = dispatch
        read_actions = {
            "screenshot", "snapshot", "evaluate", "read", "url", "title", "content", "tab_list",
            "session_info", "requests", "request_detail", "workers", "console", "errors",
            "websockets", "cookies_get", "storage_get", "downloads", "gestures", "wait",
            "waitforurl", "waitforloadstate", "waitforfunction", "page_outline", "page_links", "dom_chunk",
        } | worker.QUERY_ACTIONS | worker.FIND_ACTIONS
        for action in sorted(read_actions):
            with self.subTest(action=action):
                fields = {"subaction": "text"} if action in worker.FIND_ACTIONS else {}
                await self.request(action, **fields)
                self.assertFalse(self.runtime.action_in_flight)

    async def test_in_flight_flag_clears_after_dispatch_failure_and_cancellation(self):
        async def failing_dispatch(action, payload):
            self.assertTrue(self.runtime.action_in_flight)
            raise RuntimeError("test")

        self.instance.dispatch_browser = failing_dispatch
        with self.assertRaises(RuntimeError):
            await self.request("screenshot")
        self.assertFalse(self.runtime.action_in_flight)
        entered = asyncio.Event()

        async def waiting_dispatch(action, payload):
            entered.set()
            await asyncio.Event().wait()

        self.instance.dispatch_browser = waiting_dispatch
        task = asyncio.create_task(self.request("screenshot"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        self.assertTrue(self.runtime.action_in_flight)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runtime.action_in_flight)

    async def test_lazily_created_runtime_inherits_in_flight_flag(self):
        self.instance.runtime = None

        async def stop():
            self.assertTrue(self.runtime.action_in_flight)
            return {"stopped": False, "heldMs": 0}

        with patch.object(runtime_module, "CamoufoxRuntime", return_value=self.runtime), \
                patch.object(self.runtime.ambient_hover, "stop", side_effect=stop):
            self.assertEqual({"stopped": False, "heldMs": 0}, await self.request("hover_hold_stop"))
        self.assertFalse(self.runtime.action_in_flight)

    async def test_close_before_launch_remains_lazy(self):
        self.instance.runtime = None
        result = await self.request("close")
        self.assertEqual({"engine": "camoufox", "closed": True, "notLaunched": True}, result)
        self.assertIsNone(self.instance.runtime)
        self.assertFalse(self.instance._action_in_flight)

    async def test_loop_pauses_jitters_expires_and_preserves_observation_state(self):
        clock = SimpleNamespace(now=0.0)
        sleep_intervals = []
        original_sleep = asyncio.sleep
        self.runtime.action_in_flight = True
        self.runtime.captures.register({"url": self.page.url})
        self.tab.refs = {"e1"}
        self.tab.dom_refs = {"d1": "#player"}
        self.runtime.journal.begin_key_down("Shift")

        async def sleep(delay):
            clock.now += delay
            sleep_intervals.append(delay)
            if len(sleep_intervals) == 1:
                self.page.mouse.move.assert_not_awaited()
            elif len(sleep_intervals) == 2:
                self.page.mouse.move.assert_not_awaited()
                self.runtime.action_in_flight = False
            await original_sleep(0)

        uniform = Mock(side_effect=[0.3, 0.3, -1.5, 1.25, 0.3, 2.0, -2.0, 0.3])
        with patch.object(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock.now)), \
                patch.object(runtime_module.random, "uniform", uniform), \
                patch.object(runtime_module.asyncio, "sleep", sleep):
            task = self.start_ambient(1000)
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(4, len(sleep_intervals))
        self.assertAlmostEqual(1.0, sum(sleep_intervals))
        self.assertEqual([call(48.5, 41.25), call(52.0, 38.0)], self.page.mouse.move.await_args_list)
        self.assertEqual([
            call(0.2, 0.4), call(0.2, 0.4), call(-2, 2), call(-2, 2),
            call(0.2, 0.4), call(-2, 2), call(-2, 2), call(0.2, 0.4),
        ], uniform.call_args_list)
        self.assertIsNone(self.runtime.ambient_hover._task)
        self.assertEqual({"stopped": False, "heldMs": 0}, await self.request("hover_hold_stop"))
        self.assertEqual(1, self.runtime.captures.count())
        self.assertEqual({"e1"}, self.tab.refs)
        self.assertEqual({"d1": "#player"}, self.tab.dom_refs)
        self.assertEqual(["Shift"], self.runtime.journal.pending_keys())
        self.assertEqual(0, self.runtime._input_attempts)

    async def test_loop_silently_stops_on_target_or_session_lifecycle_changes(self):
        for change in ["tab", "page", "url", "closing", "disconnected", "removed"]:
            with self.subTest(change=change):
                self.tab.closed = False
                self.page._closed = False
                self.page.url = "about:blank"
                self.runtime._closing = False
                self.runtime._close_reason = None
                self.runtime.tabs[self.tab.tab_id] = self.tab
                task = self.start_ambient()
                if change == "tab":
                    self.tab.closed = True
                elif change == "page":
                    self.page._closed = True
                elif change == "url":
                    self.page.url = "https://example.com"
                elif change == "closing":
                    self.runtime._closing = True
                elif change == "disconnected":
                    self.runtime._close_reason = "browser_disconnected"
                else:
                    self.runtime.tabs.clear()
                with patch.object(runtime_module.asyncio, "sleep", new=AsyncMock()):
                    await asyncio.wait_for(task, timeout=1)
                self.assertIsNone(self.runtime.ambient_hover._task)
        self.page.mouse.move.assert_not_awaited()

    async def test_loop_silently_stops_on_mouse_error(self):
        self.page.mouse.move.side_effect = RuntimeError("target closed")
        with patch.object(runtime_module.asyncio, "sleep", new=AsyncMock()):
            task = self.start_ambient()
            await asyncio.wait_for(task, timeout=1)
        self.assertIsNone(self.runtime.ambient_hover._task)
        self.assertEqual(0, self.runtime._input_attempts)

    async def test_input_waits_for_in_progress_mouse_cancellation(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def move(x, y):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cancelled.set()

        async def dispatch(action, payload):
            self.assertTrue(cancelled.is_set())
            self.assertTrue(previous.done())
            self.assertTrue(self.runtime.action_in_flight)
            return {}

        self.page.mouse.move.side_effect = move
        self.instance.dispatch_browser = dispatch
        with patch.object(runtime_module.random, "uniform", return_value=0.001):
            previous = self.start_ambient()
            await asyncio.wait_for(entered.wait(), timeout=1)
            await self.request("click")
        self.assertIsNone(self.runtime.ambient_hover._task)

    async def test_runtime_close_cancels_before_teardown_even_when_already_closing(self):
        previous = self.start_ambient()

        async def reset():
            self.assertTrue(previous.done())
            self.assertIsNone(self.runtime.ambient_hover._task)
            self.assertTrue(self.runtime.action_in_flight)

        with patch.object(self.runtime.interactions, "reset", side_effect=reset):
            result = await self.request("close")
        self.assertTrue(result["closed"])
        self.assertFalse(self.runtime.action_in_flight)
        previous = self.start_ambient()
        result = await self.runtime.close()
        self.assertTrue(result["alreadyClosing"])
        self.assertTrue(previous.done())
        self.assertIsNone(self.runtime.ambient_hover._task)

    async def test_initial_move_failure_does_not_start_ambient_and_marks_input_ambiguous(self):
        self.page.mouse.move.side_effect = RuntimeError("input failed")
        with self.assertRaises(RuntimeError):
            await self.request("hover_hold", selector="#player")
        self.assertTrue(self.instance.poisoned)
        self.assertEqual(1, self.runtime._input_attempts)
        self.assertIsNone(self.runtime.ambient_hover._task)
        self.assertFalse(self.runtime.action_in_flight)


if __name__ == "__main__":
    unittest.main()
