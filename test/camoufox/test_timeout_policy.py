import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

from test_lifecycle import FAKE_RUNTIME_DIR, FakeFrame, FakePage, make_runtime
from input_context import CODE_ERROR, CODE_POISONED, CODE_TIMEOUT, BackendError

with contextlib.redirect_stdout(io.StringIO()):
    import worker

PlaywrightTimeout = type("TimeoutError", (Exception,), {"__module__": "playwright._impl._errors"})
PlaywrightError = type("Error", (Exception,), {"__module__": "playwright._impl._errors"})


class FakeLocator:
    def __init__(self, failure=None):
        self.failure = failure
        self.scroll_calls = 0
        self.hover_calls = 0

    async def scroll_into_view_if_needed(self, timeout=None):
        self.scroll_calls += 1
        if self.failure == "resolve":
            raise PlaywrightTimeout("Timeout 10000ms exceeded waiting for target resolution")
        if self.failure == "deadline_resolve":
            raise BackendError(CODE_TIMEOUT, "scroll target into view exceeded the action deadline",
                               deadline_exceeded=True)
        return None

    async def bounding_box(self, timeout=None):
        return {"x": 10.0, "y": 10.0, "width": 20.0, "height": 20.0}

    async def hover(self, timeout=None):
        self.hover_calls += 1
        if self.failure == "hover":
            raise PlaywrightTimeout("Timeout 10000ms exceeded while moving the mouse")
        return None


class FakeMouse:
    def __init__(self):
        self.up_calls = 0

    async def up(self, button=None):
        self.up_calls += 1
        raise RuntimeError("mouse release failed")


class GesturePage(FakePage):
    def __init__(self, locator):
        super().__init__()
        self._locator = locator
        self.main_frame = GestureFrame(self, locator)
        self.frames = [self.main_frame]

    def locator(self, selector):
        return self._locator

    async def evaluate(self, script):
        return {"x": 0, "y": 0, "w": 800, "h": 600, "dpr": 1}


class GestureFrame(FakeFrame):
    def __init__(self, page, locator):
        super().__init__(page)
        self._locator = locator

    def locator(self, selector):
        return self._locator


class TimeoutPolicyTests(unittest.IsolatedAsyncioTestCase):
    def make_worker(self, locator=None):
        runtime, _browser, _context, page, tab = make_runtime()
        if locator is not None:
            page = GesturePage(locator)
            tab.page = page
        instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        instance.runtime = runtime
        return instance, runtime, page, tab

    async def request(self, instance, action="url", **fields):
        payload = {"id": "timeout-policy-test", "action": action, **fields}
        with patch.object(worker, "write_response") as write:
            await instance.handle_line(json.dumps(payload).encode())
        write.assert_called_once()
        return write.call_args.args[0]

    async def test_read_only_playwright_timeout_keeps_session_usable(self):
        instance, runtime, _page, _tab = self.make_worker()
        with patch.object(runtime, "current_title", new=AsyncMock(side_effect=PlaywrightTimeout("Timeout exceeded"))):
            response = await self.request(instance, "title")

        self.assertFalse(response["success"])
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertNotIn("poisoned", response)
        self.assertFalse(instance.poisoned)
        self.assertIsNone(instance.poison_reason)

        followed = await self.request(instance, "url")
        self.assertTrue(followed["success"])
        self.assertEqual({"url": "about:blank"}, followed["data"])

    async def test_gesture_resolution_timeout_before_input_does_not_poison(self):
        locator = FakeLocator(failure="resolve")
        instance, runtime, _page, _tab = self.make_worker(locator=locator)
        instance.load_gestures()

        response = await self.request(instance, "hover", selector="#target")

        self.assertFalse(response["success"])
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertNotIn("poisoned", response)
        self.assertFalse(instance.poisoned)
        self.assertEqual(1, locator.scroll_calls)
        self.assertEqual(0, locator.hover_calls)
        self.assertEqual(0, runtime._input_attempts)
        self.assertEqual([], runtime.journal.pending_buttons())
        self.assertIn("timed out", response["error"])

    async def test_gesture_worker_deadline_before_input_does_not_poison(self):
        locator = FakeLocator(failure="deadline_resolve")
        instance, runtime, _page, _tab = self.make_worker(locator=locator)
        instance.load_gestures()

        response = await self.request(instance, "hover", selector="#target")

        self.assertFalse(response["success"])
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "deadline"}, response["data"])
        self.assertNotIn("poisoned", response)
        self.assertFalse(instance.poisoned)
        self.assertEqual(0, runtime._input_attempts)
        self.assertEqual([], runtime.journal.pending_buttons())
        self.assertEqual([], runtime.journal.pending_keys())

        followed = await self.request(instance, "url")
        self.assertTrue(followed["success"])

    async def test_hover_timeout_after_input_mark_with_clean_release_does_not_poison(self):
        locator = FakeLocator(failure="hover")
        instance, runtime, _page, _tab = self.make_worker(locator=locator)
        instance.load_gestures()

        response = await self.request(instance, "hover", selector="#target")

        self.assertFalse(response["success"])
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertNotIn("poisoned", response)
        self.assertFalse(instance.poisoned)
        self.assertEqual(1, locator.hover_calls)
        self.assertEqual(1, runtime._input_attempts)
        self.assertEqual([], runtime.journal.pending_buttons())
        self.assertEqual([], runtime.journal.pending_keys())

        followed = await self.request(instance, "url")
        self.assertTrue(followed["success"])

    async def test_timeout_with_unreleased_button_still_poisons(self):
        instance, runtime, page, _tab = self.make_worker()
        page.mouse = FakeMouse()

        async def failing_input():
            raise PlaywrightTimeout("Timeout 10000ms exceeded during input")

        async def failed_action(*args):
            return await instance._guarded_input(runtime, failing_input(), buttons=("left",), what="hover")

        with patch.object(instance, "run_action", new=AsyncMock(side_effect=failed_action)):
            response = await self.request(instance)

        self.assertTrue(instance.poisoned)
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "operation"}, response["data"])
        self.assertTrue(response["poisoned"])
        self.assertEqual(["left"], runtime.journal.pending_buttons())
        self.assertEqual(1, page.mouse.up_calls)

        refused = await self.request(instance, "url")
        self.assertEqual(CODE_POISONED, refused["code"])
        diagnostics = await self.request(instance, "session_info")
        self.assertTrue(diagnostics["success"])
        self.assertTrue(diagnostics["data"]["poisoned"])

    async def test_action_deadline_with_attempted_input_still_poisons(self):
        instance, runtime, _page, _tab = self.make_worker()
        error = BackendError(CODE_TIMEOUT, "action exceeded the 22000ms deadline", deadline_exceeded=True)

        async def input_then_deadline(*args):
            runtime._input_attempts += 1
            raise error

        with patch.object(instance, "run_action", new=AsyncMock(side_effect=input_then_deadline)):
            response = await self.request(instance)

        self.assertTrue(instance.poisoned)
        self.assertTrue(response["poisoned"])
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "deadline"}, response["data"])

    async def test_action_deadline_without_input_keeps_session_usable(self):
        instance, _runtime, _page, _tab = self.make_worker()
        error = BackendError(CODE_TIMEOUT, "action exceeded the 22000ms deadline", deadline_exceeded=True)
        with patch.object(instance, "run_action", new=AsyncMock(side_effect=error)):
            response = await self.request(instance)

        self.assertFalse(instance.poisoned)
        self.assertNotIn("poisoned", response)
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "deadline"}, response["data"])

        followed = await self.request(instance, "url")
        self.assertTrue(followed["success"])

    async def test_raw_asyncio_timeout_with_attempted_input_still_poisons(self):
        instance, runtime, _page, _tab = self.make_worker()

        async def input_then_deadline(*args):
            runtime._input_attempts += 1
            raise asyncio.TimeoutError()

        with patch.object(instance, "run_action", new=AsyncMock(side_effect=input_then_deadline)):
            response = await self.request(instance)

        self.assertTrue(instance.poisoned)
        self.assertTrue(response["poisoned"])
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "deadline"}, response["data"])

    async def test_raw_asyncio_timeout_without_input_keeps_session_usable(self):
        instance, _runtime, _page, _tab = self.make_worker()
        with patch.object(instance, "run_action", new=AsyncMock(side_effect=asyncio.TimeoutError())):
            response = await self.request(instance)

        self.assertFalse(instance.poisoned)
        self.assertNotIn("poisoned", response)
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "deadline"}, response["data"])

        followed = await self.request(instance, "url")
        self.assertTrue(followed["success"])

    async def test_launch_deadline_still_poisons(self):
        instance, _runtime, _page, _tab = self.make_worker()
        error = BackendError(CODE_TIMEOUT, "action exceeded the 22000ms deadline", deadline_exceeded=True)
        with patch.object(instance, "run_action", new=AsyncMock(side_effect=error)):
            response = await self.request(instance, "launch")

        self.assertTrue(instance.poisoned)
        self.assertTrue(response["poisoned"])
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "deadline"}, response["data"])
        self.assertIn("launch", instance.poison_reason)

    async def test_close_deadline_still_poisons(self):
        instance, _runtime, _page, _tab = self.make_worker()
        error = BackendError(CODE_TIMEOUT, "action exceeded the 22000ms deadline", deadline_exceeded=True)
        with patch.object(instance, "run_action", new=AsyncMock(side_effect=error)):
            response = await self.request(instance, "close")

        self.assertTrue(instance.poisoned)
        self.assertTrue(response["poisoned"])
        self.assertEqual(CODE_TIMEOUT, response["code"])
        self.assertEqual({"timeoutKind": "deadline"}, response["data"])
        self.assertIn("close", instance.poison_reason)

    async def test_non_timeout_ambiguous_input_still_poisons(self):
        instance, runtime, _page, _tab = self.make_worker()

        async def ambiguous_input():
            raise PlaywrightError("Element is not visible")

        async def failed_action(*args):
            return await instance._guarded_input(runtime, ambiguous_input(), mark=True, what="fill")

        with patch.object(instance, "run_action", new=AsyncMock(side_effect=failed_action)):
            response = await self.request(instance)

        self.assertTrue(instance.poisoned)
        self.assertEqual(CODE_ERROR, response["code"])
        self.assertTrue(response["poisoned"])
        self.assertNotIn("timeoutKind", response.get("data", {}))

    async def test_playwright_timeout_error_text_is_redacted_and_bounded(self):
        instance, runtime, _page, _tab = self.make_worker()
        secret = "await fetch('/secret-endpoint')"
        error = PlaywrightTimeout("Timeout 10000ms exceeded: " + secret + " " + "z" * 2400)
        with patch.object(runtime, "evaluate", new=AsyncMock(side_effect=error)):
            response = await self.request(instance, "evaluate", script=secret)

        self.assertIn("[redacted]", response["error"])
        self.assertNotIn(secret, response["error"])
        self.assertIn("[truncated]", response["error"])
        self.assertTrue(response["error"].startswith("browser action 'evaluate' timed out: TimeoutError: "))


if __name__ == "__main__":
    unittest.main()
