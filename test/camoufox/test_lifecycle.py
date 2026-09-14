import asyncio
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "camoufox-backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from input_context import (
    CODE_INVALID,
    CODE_NOT_LAUNCHED,
    CODE_NO_ACTIVE_TAB,
    CODE_SESSION_CLOSED,
    BackendError,
)
from runtime import CamoufoxRuntime

FAKE_RUNTIME_DIR = Path(__file__).resolve().parent / "fake-runtime-dir"


class FakeBrowser:
    def __init__(self, connected=True):
        self._connected = connected
        self.contexts = []
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for handler in list(self.handlers.get(event, [])):
            handler(*args)

    def is_connected(self):
        return self._connected

    def disconnect(self):
        self._connected = False
        self.emit("disconnected", self)


class FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self._closed = False
        self.handlers = {}
        browser.contexts.append(self)

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for handler in list(self.handlers.get(event, [])):
            handler(*args)

    def is_closed(self):
        return self._closed

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self in self.browser.contexts:
            self.browser.contexts.remove(self)
        self.emit("close", self)


class FakeFrame:
    def __init__(self, page):
        self.page = page
        self.parent_frame = None
        self.name = ""

    @property
    def url(self):
        return self.page.url

    def is_detached(self):
        return False


class FakePage:
    def __init__(self):
        self._closed = False
        self.handlers = {}
        self.url = "about:blank"
        self.main_frame = FakeFrame(self)
        self.frames = [self.main_frame]

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for handler in list(self.handlers.get(event, [])):
            handler(*args)

    def is_closed(self):
        return self._closed

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self.emit("close", self)


class FakeInstance:
    def __init__(self):
        self.exited = False

    async def __aexit__(self, *args):
        self.exited = True


def make_runtime():
    runtime = CamoufoxRuntime(FAKE_RUNTIME_DIR, "fast")
    browser = FakeBrowser()
    context = FakeContext(browser)
    runtime.browser = browser
    runtime.context = context
    runtime._camoufox = FakeInstance()
    runtime.launched = True
    runtime.headless = True
    browser.on("disconnected", runtime._on_browser_disconnected)
    context.on("close", runtime._on_context_closed)
    context.on("page", runtime._on_context_page)
    page = FakePage()
    tab = runtime._register_tab(page, label=None)
    runtime.active_id = tab.tab_id
    return runtime, browser, context, page, tab


def make_two_tab_runtime():
    runtime, browser, context, first_page, first_tab = make_runtime()
    second_page = FakePage()
    second_tab = runtime._register_tab(second_page, label="second")
    return runtime, browser, context, first_page, first_tab, second_page, second_tab


class LifecycleTests(unittest.TestCase):
    def test_page_close_event_argument_closes_registered_tab(self):
        runtime, _browser, _context, page, tab = make_runtime()
        page.emit("close", page)
        self.assertTrue(tab.closed)
        self.assertIsNone(runtime.active_id)
        listed = runtime.list_tabs()
        self.assertIsNone(listed["activeId"])
        self.assertTrue(listed["tabs"][0]["closed"])

    def test_obsolete_page_close_event_is_ignored(self):
        runtime, _browser, _context, page, tab = make_runtime()
        page.emit("close", FakePage())
        self.assertFalse(tab.closed)
        self.assertEqual(runtime.active_id, tab.tab_id)

    def test_lost_page_close_event_reconciled_from_is_closed(self):
        runtime, _browser, _context, page, tab = make_runtime()
        page._closed = True
        listed = runtime.list_tabs()
        self.assertTrue(listed["tabs"][0]["closed"])
        self.assertIsNone(listed["activeId"])
        with self.assertRaises(BackendError) as raised:
            runtime.require_active()
        self.assertEqual(raised.exception.code, CODE_NO_ACTIVE_TAB)
        with self.assertRaises(BackendError) as raised:
            runtime.switch_tab(tab.tab_id)
        self.assertEqual(raised.exception.code, CODE_INVALID)

    def test_nonactive_close_keeps_active_without_adoption(self):
        runtime, _browser, _context, first_page, first_tab, second_page, second_tab = make_two_tab_runtime()
        runtime.active_id = first_tab.tab_id
        second_page.emit("close", second_page)
        self.assertTrue(second_tab.closed)
        self.assertEqual(runtime.active_id, first_tab.tab_id)
        page, tab = runtime.require_active()
        self.assertIs(page, first_page)
        self.assertEqual(tab.tab_id, first_tab.tab_id)
        self.assertTrue(runtime.launch_info()["launched"])

    def test_active_close_leaves_no_active_and_no_adoption(self):
        runtime, _browser, _context, first_page, first_tab, _second_page, _second_tab = make_two_tab_runtime()
        runtime.active_id = first_tab.tab_id
        first_page.emit("close", first_page)
        self.assertTrue(first_tab.closed)
        self.assertIsNone(runtime.active_id)
        self.assertTrue(runtime.launch_info()["launched"])
        with self.assertRaises(BackendError) as raised:
            runtime.require_active()
        self.assertEqual(raised.exception.code, CODE_NO_ACTIVE_TAB)
        switched = runtime.switch_tab("second")
        self.assertEqual(switched["tabId"], "t2")
        self.assertTrue(switched["active"])

    def test_browser_disconnect_diagnostics_and_invalidation(self):
        runtime, browser, _context, _page, tab = make_runtime()
        tab.refs = {"e1"}
        tab.refs_meta = {"e1": {"role": "button", "name": None, "framePrefix": None}}
        runtime.captures.register({"url": "about:blank"})
        runtime.journal.begin_button_down("left")
        browser.disconnect()
        info = runtime.launch_info()
        self.assertFalse(info["launched"])
        self.assertFalse(info["browserConnected"])
        self.assertTrue(info["recoveryRequired"])
        self.assertEqual(info["closeReason"], "browser_disconnected")
        session = runtime.session_info(["click"])
        self.assertFalse(session["launched"])
        self.assertTrue(session["recoveryRequired"])
        self.assertEqual(session["closeReason"], "browser_disconnected")
        self.assertTrue(tab.closed)
        self.assertIsNone(runtime.active_id)
        self.assertEqual(tab.refs, set())
        self.assertEqual(tab.refs_meta, {})
        self.assertEqual(runtime.captures.count(), 0)
        self.assertEqual(runtime.journal.pending_buttons(), ["left"])
        asyncio.run(runtime.release_inputs())
        self.assertEqual(runtime.journal.pending_buttons(), ["left"])
        error = runtime.sync_session_state()
        self.assertIsNotNone(error)
        self.assertEqual(error.code, CODE_SESSION_CLOSED)
        self.assertIn("close", error.message)

    def test_context_close_diagnostics_and_invalidation(self):
        runtime, browser, context, _page, tab = make_runtime()
        tab.refs = {"e1"}
        runtime.captures.register({"url": "about:blank"})
        asyncio.run(context.close())
        info = runtime.launch_info()
        self.assertFalse(info["launched"])
        self.assertTrue(info["browserConnected"])
        self.assertTrue(info["recoveryRequired"])
        self.assertEqual(info["closeReason"], "context_closed")
        self.assertFalse(browser.contexts)
        self.assertTrue(tab.closed)
        self.assertEqual(tab.refs, set())
        self.assertEqual(runtime.captures.count(), 0)
        error = runtime.sync_session_state()
        self.assertIsNotNone(error)
        self.assertEqual(error.code, CODE_SESSION_CLOSED)

    def test_browser_shutdown_upgrades_context_close_reason(self):
        runtime, browser, context, _page, _tab = make_runtime()
        asyncio.run(context.close())
        browser.disconnect()
        info = runtime.session_info([])
        self.assertEqual(info["closeReason"], "browser_disconnected")
        self.assertFalse(info["browserConnected"])
        self.assertFalse(info["launched"])
        self.assertTrue(info["recoveryRequired"])

    def test_missing_disconnect_event_upgrades_context_close_reason(self):
        runtime, browser, context, _page, _tab = make_runtime()
        asyncio.run(context.close())
        browser._connected = False
        self.assertEqual(runtime.launch_info()["closeReason"], "browser_disconnected")

    def test_is_live_reconciles_a_missed_disconnect(self):
        runtime, browser, _context, _page, _tab = make_runtime()
        browser._connected = False
        self.assertFalse(runtime._is_live())
        self.assertTrue(runtime.recovery_required())

    def test_session_info_reconciles_active_tab_before_serializing(self):
        runtime, _browser, _context, page, _tab = make_runtime()
        page._closed = True
        info = runtime.session_info([])
        self.assertIsNone(info["activeTab"])
        self.assertIsNone(info["tabs"]["activeId"])

    def test_page_close_does_not_discard_pending_input(self):
        runtime, _browser, _context, page, _tab = make_runtime()
        runtime.journal.begin_button_down("left")
        runtime.journal.begin_key_down("Shift")
        asyncio.run(page.close())
        result = asyncio.run(runtime.release_inputs())
        self.assertFalse(result["released"])
        self.assertEqual(runtime.journal.pending_buttons(), ["left"])
        self.assertEqual(runtime.journal.pending_keys(), ["Shift"])

    def test_missing_events_reconciled_from_connection_and_membership(self):
        runtime, browser, context, _page, _tab = make_runtime()
        context._closed = True
        browser.contexts.remove(context)
        runtime.sync_session_state()
        self.assertEqual(runtime.launch_info()["closeReason"], "context_closed")

        runtime, browser, _context, _page, _tab = make_runtime()
        browser._connected = False
        runtime.sync_session_state()
        self.assertEqual(runtime.launch_info()["closeReason"], "browser_disconnected")

    def test_foreign_lifecycle_callbacks_are_ignored(self):
        runtime, _browser, context, _page, tab = make_runtime()
        context.emit("close", FakeContext(FakeBrowser()))
        self.assertIsNone(runtime.launch_info()["closeReason"])
        self.assertFalse(tab.closed)

    def test_intentional_close_callback_does_not_latch_while_closing(self):
        runtime, browser, context, _page, tab = make_runtime()
        runtime._closing = True
        context._closed = True
        context.emit("close", context)
        browser._connected = False
        browser.emit("disconnected", browser)
        self.assertFalse(runtime.recovery_required())
        self.assertFalse(tab.closed)

    def test_latched_session_refuses_launch_without_replacement(self):
        runtime, browser, context, _page, _tab = make_runtime()
        browser.disconnect()
        instance = runtime._camoufox
        with self.assertRaises(BackendError) as raised:
            asyncio.run(runtime.launch(False))
        self.assertEqual(raised.exception.code, CODE_SESSION_CLOSED)
        self.assertIn("close", raised.exception.message)
        self.assertIs(runtime.browser, browser)
        self.assertIs(runtime.context, context)
        self.assertIs(runtime._camoufox, instance)
        self.assertFalse(runtime._is_live())
        with self.assertRaises(BackendError) as raised:
            asyncio.run(runtime.new_tab(None, None))
        self.assertEqual(raised.exception.code, CODE_SESSION_CLOSED)
        with self.assertRaises(BackendError) as raised:
            runtime.switch_tab("t1")
        self.assertEqual(raised.exception.code, CODE_SESSION_CLOSED)
        with self.assertRaises(BackendError) as raised:
            runtime.require_active()
        self.assertEqual(raised.exception.code, CODE_SESSION_CLOSED)

    def test_explicit_close_clears_the_latch_and_release_path_resumes(self):
        runtime, browser, _context, _page, _tab = make_runtime()
        instance = runtime._camoufox
        browser.disconnect()
        self.assertTrue(runtime.recovery_required())
        asyncio.run(runtime.close())
        self.assertIsNone(runtime.launch_info()["closeReason"])
        self.assertFalse(runtime.recovery_required())
        self.assertFalse(runtime.launched)
        self.assertIsNone(runtime.browser)
        self.assertIsNone(runtime.sync_session_state())
        self.assertTrue(instance.exited)
        with self.assertRaises(BackendError) as raised:
            asyncio.run(runtime.launch(False))
        self.assertEqual(raised.exception.code, CODE_NOT_LAUNCHED)

    def test_healthy_session_is_usable_and_not_recovering(self):
        runtime, _browser, _context, page, tab = make_runtime()
        self.assertIsNone(runtime.sync_session_state())
        active_page, active_tab = runtime.require_active()
        self.assertIs(active_page, page)
        self.assertEqual(active_tab.tab_id, tab.tab_id)
        info = runtime.launch_info()
        self.assertTrue(info["launched"])
        self.assertTrue(info["browserConnected"])
        self.assertFalse(info["recoveryRequired"])
        self.assertIsNone(info["closeReason"])
        self.assertEqual(asyncio.run(runtime.launch(True)), info)
        self.assertIs(runtime.active_page(), page)

    def test_never_launched_launch_attempt_and_info_stay_normal(self):
        runtime = CamoufoxRuntime(FAKE_RUNTIME_DIR, "fast")
        info = runtime.launch_info()
        self.assertFalse(info["launched"])
        self.assertFalse(info["browserConnected"])
        self.assertFalse(info["recoveryRequired"])
        self.assertIsNone(info["closeReason"])
        with self.assertRaises(BackendError) as raised:
            asyncio.run(runtime.launch(False))
        self.assertEqual(raised.exception.code, CODE_NOT_LAUNCHED)

    def test_context_default_timeout_stays_below_the_worker_deadline(self):
        import input_context as ic

        runtime = CamoufoxRuntime(FAKE_RUNTIME_DIR, "fast")
        recorded = []

        class RecordingContext:
            def set_default_timeout(self, timeout):
                recorded.append(timeout)

        runtime._configure_default_timeout(RecordingContext())
        deadline_ms = ic.action_deadline_ms()
        self.assertEqual(1, len(recorded))
        self.assertEqual(max(500, deadline_ms - 1000), recorded[0])
        self.assertGreater(recorded[0], 0)
        self.assertLess(recorded[0], deadline_ms)

    def test_context_default_timeout_failure_is_ignored(self):
        runtime = CamoufoxRuntime(FAKE_RUNTIME_DIR, "fast")

        class BrokenContext:
            def set_default_timeout(self, timeout):
                raise RuntimeError("context already closed")

        runtime._configure_default_timeout(BrokenContext())


if __name__ == "__main__":
    unittest.main()
