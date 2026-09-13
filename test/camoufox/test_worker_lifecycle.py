import contextlib
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

from test_lifecycle import FAKE_RUNTIME_DIR, make_runtime
from input_context import BackendError, CODE_ERROR

with contextlib.redirect_stdout(io.StringIO()):
    import worker

TargetClosedError = type("TargetClosedError", (Exception,), {"__module__": "playwright._impl._errors"})


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, self.browser, self.context, self.page, self.tab = make_runtime()
        self.worker = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        self.worker.runtime = self.runtime

    async def request(self, action="navigate", **fields):
        payload = {"id": "lifecycle-test", "action": action, **fields}
        with patch.object(worker, "write_response") as write:
            await self.worker.handle_line(json.dumps(payload).encode())
        write.assert_called_once()
        return write.call_args.args[0]

    async def test_preflight_closure_reports_recovery_state(self):
        self.browser.disconnect()
        response = await self.request(url="about:blank")
        self.assertEqual(response["code"], "camoufox_session_closed")
        self.assertFalse(response["data"]["launched"])
        self.assertFalse(response["data"]["browserConnected"])
        self.assertTrue(response["data"]["recoveryRequired"])
        self.assertNotIn("poisoned", response)

    async def test_target_closed_race_reconciles_browser_loss_without_replay(self):
        async def failed_action(*args):
            self.browser._connected = False
            raise TargetClosedError()

        with patch.object(self.worker, "run_action", new=AsyncMock(side_effect=failed_action)) as action:
            response = await self.request()
        action.assert_awaited_once()
        self.assertEqual(response["code"], "camoufox_session_closed")
        self.assertEqual(response["data"]["closeReason"], "browser_disconnected")
        self.assertTrue(self.tab.closed)
        self.assertIsNone(response["data"]["activeTab"])

    async def test_target_closed_race_keeps_live_browser_after_page_loss(self):
        async def failed_action(*args):
            self.page._closed = True
            raise TargetClosedError()

        with patch.object(self.worker, "run_action", new=AsyncMock(side_effect=failed_action)):
            response = await self.request()
        self.assertEqual(response["code"], "camoufox_no_active_tab")
        self.assertTrue(response["data"]["launched"])
        self.assertFalse(response["data"]["recoveryRequired"])
        self.assertIsNone(response["data"]["activeTab"])

    async def test_unclassified_target_closure_invalidates_refs_without_guessing_scope(self):
        self.tab.refs.add("e1")
        self.runtime.captures.register({"url": "about:blank"})
        with patch.object(self.worker, "run_action", new=AsyncMock(side_effect=TargetClosedError())):
            response = await self.request()
        self.assertEqual(response["code"], "camoufox_target_closed")
        self.assertTrue(response["data"]["browserConnected"])
        self.assertFalse(response["data"]["recoveryRequired"])
        self.assertFalse(self.tab.closed)
        self.assertFalse(self.tab.refs)
        self.assertEqual(self.runtime.captures.count(), 0)

    async def test_wrapped_target_closure_retains_session_recovery_error(self):
        async def failed_action(*args):
            self.browser._connected = False
            try:
                raise TargetClosedError()
            except TargetClosedError as cause:
                raise BackendError(CODE_ERROR, "failed to close tab") from cause

        with patch.object(self.worker, "run_action", new=AsyncMock(side_effect=failed_action)):
            response = await self.request()
        self.assertEqual(response["code"], "camoufox_session_closed")

    async def test_attempted_input_remains_poisoned_after_target_loss(self):
        async def close_during_input():
            self.page._closed = True
            raise TargetClosedError()

        async def failed_action(*args):
            return await self.worker._guarded_input(
                self.runtime, close_during_input(), buttons=("left",), what="test input"
            )

        with patch.object(self.worker, "run_action", new=AsyncMock(side_effect=failed_action)) as action:
            response = await self.request()
        action.assert_awaited_once()
        self.assertTrue(response["poisoned"])
        self.assertEqual(response["code"], "camoufox_no_active_tab")
        self.assertIn("without retrying the input", response["error"])
        self.assertEqual(self.runtime.journal.pending_buttons(), ["left"])
        refused = await self.request(url="about:blank")
        self.assertEqual(refused["code"], "camoufox_poisoned")
        diagnostics = await self.request("session_info")
        self.assertTrue(diagnostics["success"])
        self.assertTrue(diagnostics["data"]["poisoned"])

    async def test_unrelated_playwright_error_is_not_session_loss(self):
        error = type("Error", (Exception,), {"__module__": "playwright._impl._errors"})
        with patch.object(self.worker, "run_action", new=AsyncMock(side_effect=error())):
            response = await self.request()
        self.assertEqual(response["code"], CODE_ERROR)
        self.assertIsNone(self.runtime.session_closed_error())


if __name__ == "__main__":
    unittest.main()
