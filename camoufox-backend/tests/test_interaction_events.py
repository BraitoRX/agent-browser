from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import interaction_events
from input_context import (
    CODE_ERROR,
    CODE_INVALID,
    CODE_NO_ACTIVE_TAB,
    CODE_SESSION_CLOSED,
    MAX_ACTION_DEADLINE_MS,
    BackendError,
)
from interaction_events import (
    FROM_CLICK_TIMEOUT_MS,
    MAX_DOWNLOADS,
    MAX_METADATA_BYTES,
    InteractionEvents,
)

_REAL_STDOUT = sys.stdout
import worker as worker_module

sys.stdout = _REAL_STDOUT

APPROVED_TMP_ROOT = "/private/var/folders/2h/mpgrdlg95f915rrnl3l848rc0000gn/T/opencode"


class FakeEmitter:
    def __init__(self) -> None:
        self.listeners: Dict[str, List[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        handlers = self.listeners.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def emit(self, event: str, *args: Any) -> None:
        for handler in list(self.listeners.get(event, [])):
            handler(*args)

    def count(self) -> int:
        return sum(len(items) for items in self.listeners.values())


class FakeInfo:
    def __init__(self, value: Any) -> None:
        self.value = value


class FakeExpectManager:
    def __init__(self, page: "FakePage", download: Any) -> None:
        self.page = page
        self.download = download

    async def __aenter__(self) -> FakeInfo:
        self.page.event_log.append("expect_enter")
        future = asyncio.get_running_loop().create_future()
        self.page.expect_future = future
        if not self.page.pending_expectation:
            future.set_result(self.download)
        return FakeInfo(future)

    async def __aexit__(self, *exc: Any) -> bool:
        self.page.event_log.append("expect_exit")
        return False


class FakePage(FakeEmitter):
    def __init__(self) -> None:
        super().__init__()
        self.main_frame = object()
        self.next_download: Optional[FakeDownload] = None
        self.pending_expectation = False
        self.expect_future: Optional[Any] = None
        self.expect_timeouts: List[Any] = []
        self.event_log: List[str] = []

    def expect_download(self, timeout: Any = None) -> FakeExpectManager:
        self.expect_timeouts.append(timeout)
        self.event_log.append("expect")
        return FakeExpectManager(self, self.next_download)


class FakeDialog:
    def __init__(self, dialog_type: str = "alert", message: str = "hello", default_value: str = "") -> None:
        self.type = dialog_type
        self.message = message
        self.default_value = default_value
        self.accept_calls: List[Any] = []
        self.dismiss_calls = 0
        self.gate: Optional[asyncio.Event] = None

    async def accept(self, prompt_text: Any = None) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.accept_calls.append(prompt_text)

    async def dismiss(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.dismiss_calls += 1


class FakeDownload:
    def __init__(
        self,
        url: str = "https://example.test/file.bin",
        suggested_filename: str = "file.bin",
        content: bytes = b"payload",
        failure: Optional[str] = None,
    ) -> None:
        self.url = url
        self.suggested_filename = suggested_filename
        self.content = content
        self._failure = failure
        self.save_error: Optional[BaseException] = None
        self.save_paths: List[str] = []
        self.failure_calls = 0

    async def failure(self) -> Optional[str]:
        self.failure_calls += 1
        return self._failure

    async def save_as(self, path: str) -> None:
        self.save_paths.append(path)
        if self.save_error is not None:
            raise self.save_error
        Path(path).write_bytes(self.content)


class FakeTab:
    def __init__(self, tab_id: str) -> None:
        self.tab_id = tab_id


class FakeCaptures:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate_all(self) -> None:
        self.invalidations += 1


class FakeRuntime:
    def __init__(self, page: FakePage, tab_id: str = "t1", runtime_dir: Optional[Path] = None) -> None:
        self.page = page
        self.tab = FakeTab(tab_id)
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else BACKEND_DIR / "tests"
        self.captures = FakeCaptures()
        self.interactions = InteractionEvents(self)

    def require_open_session(self) -> None:
        return None

    def require_active(self) -> Any:
        return self.page, self.tab


class InteractionEventsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        root = Path(APPROVED_TMP_ROOT) if Path(APPROVED_TMP_ROOT).is_dir() else BACKEND_DIR / "tests"
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ab-interaction-", dir=root))
        self._events: List[InteractionEvents] = []

    async def asyncTearDown(self) -> None:
        for events in self._events:
            await events.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def make_runtime(self, page: Optional[FakePage] = None, tab_id: str = "t1") -> FakeRuntime:
        resolved = page if page is not None else FakePage()
        runtime = FakeRuntime(resolved, tab_id=tab_id, runtime_dir=self.tmp_dir)
        self._events.append(runtime.interactions)
        return runtime

    def make_worker(self, runtime: FakeRuntime) -> Any:
        worker = worker_module.Worker(self.tmp_dir, "fast")
        worker.runtime = runtime
        return worker

    async def worker_call(self, worker: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        transport = io.StringIO()
        with mock.patch.object(worker_module, "TRANSPORT", transport):
            await worker.handle_line(json.dumps(payload).encode("utf-8"))
        lines = [line for line in transport.getvalue().splitlines() if line.strip()]
        return json.loads(lines[-1])

    async def wait_until(self, predicate: Any, limit: int = 200) -> None:
        for _ in range(limit):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail("condition was not reached")

    async def settle(self, events: InteractionEvents, limit: int = 200) -> None:
        for _ in range(limit):
            if not events._tasks:
                return
            await asyncio.sleep(0)
        self.fail("dialog handling tasks did not settle")

    async def test_default_dialog_dismissal_and_pending_status(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")
        self.assertEqual(4, page.count())

        dialog = FakeDialog(dialog_type="confirm", message="m" * (MAX_METADATA_BYTES + 10))
        page.emit("dialog", dialog)
        pending = events.dialog({"response": "status"})
        self.assertTrue(pending["hasDialog"])
        self.assertIsNone(pending["lastDialog"])

        await self.settle(events)
        settled = events.dialog({"response": "status"})
        self.assertFalse(settled["hasDialog"])
        self.assertEqual(1, dialog.dismiss_calls)
        self.assertEqual([], dialog.accept_calls)
        last = settled["lastDialog"]
        self.assertEqual("confirm", last["type"])
        self.assertEqual("t1", last["tabId"])
        self.assertIs(last["accepted"], False)
        self.assertEqual("auto_dismiss", last["disposition"])
        self.assertTrue(last["messageTruncated"])
        self.assertEqual(MAX_METADATA_BYTES, len(last["message"].encode("utf-8")))
        self.assertEqual(100, settled["limit"])
        self.assertEqual(0, settled["dropped"])
        self.assertIsNone(settled["lastError"])
        self.assertIn("note", settled)

    async def test_dialog_arm_scope_single_use_and_prompt_text(self) -> None:
        page_one = FakePage()
        runtime = self.make_runtime(page_one)
        events = runtime.interactions
        page_two = FakePage()
        events.attach_page(page_one, "t1")
        events.attach_page(page_two, "t2")
        worker = self.make_worker(runtime)

        armed = await self.worker_call(
            worker,
            {"id": "arm-1", "action": "dialog", "response": "accept", "promptText": "answer"},
        )
        self.assertTrue(armed["success"])
        self.assertTrue(armed["data"]["armed"])
        self.assertTrue(armed["data"]["accepted"])
        self.assertEqual("t1", armed["data"]["tabId"])
        self.assertEqual(30000, armed["data"]["expiresInMs"])

        wrong_tab = FakeDialog(dialog_type="prompt")
        page_two.emit("dialog", wrong_tab)
        await self.settle(events)
        self.assertEqual(1, wrong_tab.dismiss_calls)
        self.assertEqual([], wrong_tab.accept_calls)
        self.assertIsNotNone(events._armed)

        prompt = FakeDialog(dialog_type="prompt")
        page_one.emit("dialog", prompt)
        await self.settle(events)
        self.assertEqual(["answer"], prompt.accept_calls)
        self.assertEqual(0, prompt.dismiss_calls)
        self.assertIsNone(events._armed)

        late = FakeDialog()
        page_one.emit("dialog", late)
        await self.settle(events)
        self.assertEqual(1, late.dismiss_calls)
        self.assertEqual([], late.accept_calls)

    async def test_dialog_arm_expiry_navigation_and_close(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")

        events.dialog({"response": "dismiss"})
        events._armed["deadline"] = time.monotonic() - 0.01
        expired = FakeDialog()
        page.emit("dialog", expired)
        await self.settle(events)
        self.assertEqual(1, expired.dismiss_calls)
        self.assertIsNone(events._armed)

        events.dialog({"response": "accept"})
        page.emit("framenavigated", object())
        self.assertIsNotNone(events._armed)
        page.emit("framenavigated", page.main_frame)
        self.assertIsNone(events._armed)

        events.dialog({"response": "accept"})
        page.emit("close")
        self.assertIsNone(events._armed)
        self.assertEqual(0, page.count())
        self.assertEqual({}, events._pages)

    async def test_dialog_arm_captured_at_event_arrival(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")

        events.dialog({"response": "accept"})
        first = FakeDialog()
        page.emit("dialog", first)
        events.dialog({"response": "dismiss"})
        await self.settle(events)
        self.assertEqual([None], first.accept_calls)
        self.assertEqual(0, first.dismiss_calls)

        second = FakeDialog()
        page.emit("dialog", second)
        await self.settle(events)
        self.assertEqual(1, second.dismiss_calls)

        entries = list(events._dialogs)
        self.assertEqual([True, False], [entry["accepted"] for entry in entries])
        self.assertEqual(["armed", "armed"], [entry["disposition"] for entry in entries])

    async def test_worker_download_guard_ordering_and_diagnostics(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        worker = self.make_worker(runtime)

        download = FakeDownload(
            url="https://example.test/report.bin",
            suggested_filename="report.bin",
            content=b"report",
        )
        page.next_download = download
        destination = self.tmp_dir / "report.bin"
        clicks: List[Dict[str, Any]] = []

        async def fake_click(target_runtime: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
            page.event_log.append("click")
            clicks.append(payload)
            return {"clicked": True, "diagnostics": {"captureId": "cap-1"}}

        with mock.patch.object(worker, "do_click", new=fake_click):
            response = await self.worker_call(
                worker,
                {"id": "req-1", "action": "download", "selector": "#report", "path": str(destination)},
            )

        self.assertTrue(response["success"])
        data = response["data"]
        self.assertEqual(str(destination), data["path"])
        self.assertEqual("https://example.test/report.bin", data["url"])
        self.assertEqual("report.bin", data["suggestedFilename"])
        self.assertEqual("d1", data["downloadId"])
        self.assertEqual("t1", data["tabId"])
        self.assertEqual({"captureId": "cap-1"}, data["diagnostics"])
        self.assertEqual([{"selector": "#report"}], clicks)
        self.assertEqual(["expect", "expect_enter", "click", "expect_exit"], page.event_log)
        self.assertEqual([MAX_ACTION_DEADLINE_MS], page.expect_timeouts)
        self.assertEqual(b"report", destination.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(destination).st_mode))
        self.assertEqual(1, download.failure_calls)
        metadata = events.downloads()["downloads"]
        self.assertEqual(1, len(metadata))
        self.assertTrue(metadata[0]["consumed"])
        self.assertEqual(str(destination), metadata[0]["savedPath"])
        self.assertEqual(1, runtime.captures.invalidations)

    async def test_download_destination_rejected_before_click(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions

        existing = self.tmp_dir / "existing.bin"
        existing.write_bytes(b"keep")
        dangling: Optional[Path] = self.tmp_dir / "dangling.bin"
        try:
            os.symlink("missing-target.bin", dangling)
        except (OSError, NotImplementedError):
            dangling = None

        clicks: List[str] = []

        async def click_once() -> None:
            clicks.append("clicked")

        cases = [(str(existing), "already exists as a file")]
        if dangling is not None:
            cases.append((str(dangling), "already exists as a file"))
        for path, fragment in cases:
            with self.assertRaises(BackendError) as captured:
                await events.from_click(path, click_once)
            self.assertEqual(CODE_INVALID, captured.exception.code)
            self.assertIn(fragment, captured.exception.message)

        with self.assertRaises(BackendError) as null_byte:
            await events.from_click("\x00bad.bin", click_once)
        self.assertEqual(CODE_INVALID, null_byte.exception.code)
        self.assertIn("null byte", null_byte.exception.message)

        with self.assertRaises(BackendError) as missing_callback:
            await events.from_click(None, None)
        self.assertEqual(CODE_INVALID, missing_callback.exception.code)

        with self.assertRaises(BackendError) as wait_existing:
            await events.wait_for_download(str(existing), 50)
        self.assertEqual(CODE_INVALID, wait_existing.exception.code)

        self.assertEqual([], clicks)
        self.assertEqual([], page.expect_timeouts)

    async def test_download_publish_failure_leaves_no_target_or_temp(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        clicks: List[str] = []

        async def click_once() -> None:
            clicks.append("clicked")

        page.next_download = FakeDownload(content=b"payload")
        linked = self.tmp_dir / "linked.bin"
        with mock.patch("interaction_events.os.link", side_effect=OSError("hard links unavailable")):
            with self.assertRaises(BackendError) as unsupported:
                await events.from_click(str(linked), click_once)
        self.assertEqual(CODE_ERROR, unsupported.exception.code)
        self.assertIn("could not be published", unsupported.exception.message)
        self.assertFalse(os.path.lexists(linked))
        self.assertEqual([], list(self.tmp_dir.glob(".agent-browser-download-*")))
        self.assertEqual(1, len(clicks))
        retained = events.downloads()["downloads"]
        self.assertEqual(1, len(retained))
        self.assertFalse(retained[0]["consumed"])
        self.assertIsNone(retained[0]["savedPath"])

        retried = await events.wait_for_download(str(linked))
        self.assertEqual(str(linked), retried["path"])
        self.assertEqual(b"payload", linked.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(linked).st_mode))
        self.assertEqual(1, len(clicks))
        self.assertEqual(2, runtime.interactions._downloads["d1"].handle.failure_calls)

        race_destination = self.tmp_dir / "race.bin"

        async def racing_click() -> None:
            clicks.append("clicked")
            race_destination.write_bytes(b"winner")

        page.next_download = FakeDownload(content=b"loser")
        with self.assertRaises(BackendError) as raced:
            await events.from_click(str(race_destination), racing_click)
        self.assertEqual(CODE_INVALID, raced.exception.code)
        self.assertIn("already exists", raced.exception.message)
        self.assertEqual(b"winner", race_destination.read_bytes())
        self.assertEqual([], list(self.tmp_dir.glob(".agent-browser-download-*")))
        self.assertEqual(2, len(clicks))

    async def test_download_save_failure_retains_handle_for_retry(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        clicks: List[str] = []

        async def click_once() -> None:
            clicks.append("clicked")

        download = FakeDownload(content=b"retry-me")
        download.save_error = OSError("disk full")
        page.next_download = download
        first_destination = self.tmp_dir / "first.bin"
        with self.assertRaises(BackendError) as failed:
            await events.from_click(str(first_destination), click_once)
        self.assertEqual(CODE_ERROR, failed.exception.code)
        self.assertIn("could not be saved", failed.exception.message)
        self.assertFalse(os.path.lexists(first_destination))
        self.assertEqual(1, len(clicks))
        self.assertFalse(events.downloads()["downloads"][0]["consumed"])

        download.save_error = None
        second_destination = self.tmp_dir / "second.bin"
        retried = await events.wait_for_download(str(second_destination))
        self.assertEqual("d1", retried["downloadId"])
        self.assertEqual(b"retry-me", second_destination.read_bytes())
        self.assertEqual(1, len(clicks))
        self.assertEqual(2, len(download.save_paths))

    async def test_terminal_failed_download_does_not_block_next(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")

        failed_download = FakeDownload(failure="network reset", content=b"nope")
        page.next_download = failed_download
        destination = self.tmp_dir / "failed.bin"

        async def click_once() -> None:
            return None

        with self.assertRaises(BackendError) as failed:
            await events.from_click(str(destination), click_once)
        self.assertEqual(CODE_ERROR, failed.exception.code)
        self.assertIn("network reset", failed.exception.message)
        self.assertEqual([], failed_download.save_paths)
        self.assertFalse(os.path.lexists(destination))

        next_download = FakeDownload(suggested_filename="next.bin", content=b"next")
        page.emit("download", next_download)
        saved = await events.wait_for_download(str(self.tmp_dir / "next.bin"))
        self.assertEqual("d2", saved["downloadId"])
        self.assertEqual([], failed_download.save_paths)
        self.assertEqual(1, len(next_download.save_paths))
        metadata = events.downloads()["downloads"]
        self.assertEqual("network reset", metadata[0]["failure"])
        self.assertEqual("next.bin", metadata[1]["suggestedFilename"])

    async def test_wait_for_download_pending_event_tab_close_and_timeout(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")

        destination = self.tmp_dir / "pending.bin"
        pending = asyncio.create_task(events.wait_for_download(str(destination)))
        await self.wait_until(lambda: len(events._download_waiters) == 1)
        arrived = FakeDownload(content=b"arrived")
        page.emit("download", arrived)
        saved = await pending
        self.assertEqual(str(destination), saved["path"])
        self.assertEqual(b"arrived", destination.read_bytes())
        self.assertEqual({}, events._download_waiters)
        self.assertEqual(1, len(arrived.save_paths))

        closed_task = asyncio.create_task(events.wait_for_download(str(self.tmp_dir / "closed.bin")))
        await self.wait_until(lambda: len(events._download_waiters) == 1)
        page.emit("close")
        with self.assertRaises(BackendError) as closed:
            await closed_task
        self.assertEqual(CODE_NO_ACTIVE_TAB, closed.exception.code)
        self.assertEqual({}, events._download_waiters)

        worker = self.make_worker(runtime)
        response = await self.worker_call(
            worker,
            {"id": "wait-1", "action": "waitfordownload", "path": str(self.tmp_dir / "timeout.bin"), "timeout": 50},
        )
        self.assertFalse(response["success"])
        self.assertEqual(CODE_ERROR, response["code"])
        self.assertIn("no download arrived", response["error"])
        self.assertNotIn("inputAmbiguous", response)
        self.assertFalse(worker.poisoned)
        self.assertEqual({}, events._download_waiters)

    async def test_downloads_cap_truncation_and_clear(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")

        long_url = "https://example.test/" + "u" * (MAX_METADATA_BYTES + 100)
        long_name = "n" * (MAX_METADATA_BYTES + 100)
        page.emit("download", FakeDownload(url=long_url, suggested_filename=long_name))
        bounded = events.downloads()
        record = bounded["downloads"][0]
        self.assertEqual(MAX_METADATA_BYTES, len(record["url"].encode("utf-8")))
        self.assertTrue(record["urlTruncated"])
        self.assertEqual(MAX_METADATA_BYTES, len(record["suggestedFilename"].encode("utf-8")))
        self.assertTrue(record["suggestedFilenameTruncated"])
        self.assertEqual(MAX_DOWNLOADS, bounded["limit"])
        self.assertEqual(0, bounded["dropped"])
        self.assertFalse(bounded["truncated"])
        self.assertEqual(0, bounded["omitted"])

        page_two = FakePage()
        runtime_two = self.make_runtime(page_two)
        events_two = runtime_two.interactions
        events_two.attach_page(page_two, "t1")
        kept = self.tmp_dir / "kept.bin"
        page_two.emit("download", FakeDownload(content=b"kept"))
        saved = await events_two.wait_for_download(str(kept))
        self.assertEqual("d1", saved["downloadId"])
        for index in range(MAX_DOWNLOADS):
            page_two.emit("download", FakeDownload(url=f"https://example.test/{index}"))
        listing = events_two.downloads()
        self.assertEqual(MAX_DOWNLOADS, len(listing["downloads"]))
        self.assertEqual(1, listing["dropped"])
        self.assertEqual("d2", listing["downloads"][0]["downloadId"])
        self.assertEqual("d33", listing["downloads"][-1]["downloadId"])

        worker = self.make_worker(runtime_two)
        cleared = await self.worker_call(worker, {"id": "clear-1", "action": "downloads", "clear": True})
        self.assertTrue(cleared["success"])
        self.assertEqual({"cleared": True}, cleared["data"])
        self.assertEqual([], events_two.downloads()["downloads"])
        self.assertEqual(0, events_two.downloads()["dropped"])
        self.assertTrue(kept.exists())
        self.assertEqual(b"kept", kept.read_bytes())

    async def test_close_detaches_listeners_cancels_tasks_and_waiters(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")
        events.dialog({"response": "accept"})

        gate = asyncio.Event()
        slow_dialog = FakeDialog()
        slow_dialog.gate = gate
        page.emit("dialog", slow_dialog)
        await self.wait_until(lambda: len(events._tasks) == 1)
        events.dialog({"response": "accept"})

        waiting = asyncio.create_task(events.wait_for_download(str(self.tmp_dir / "never.bin")))
        await self.wait_until(lambda: len(events._download_waiters) == 1)

        await events.close()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(0, page.count())
        self.assertEqual({}, events._pages)
        self.assertEqual(set(), events._tasks)
        self.assertEqual({}, events._download_waiters)
        self.assertEqual(set(), events._pending_dialogs)
        self.assertEqual(0, len(events._dialogs))
        self.assertIsNone(events._armed)
        self.assertIsNone(events._loop)
        self.assertEqual(0, slow_dialog.dismiss_calls)

    async def test_worker_dispatch_dialog_status_and_downloads_validation(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        worker = self.make_worker(runtime)

        status = await self.worker_call(worker, {"id": "status-1", "action": "dialog", "response": "status"})
        self.assertTrue(status["success"])
        self.assertFalse(status["data"]["hasDialog"])
        self.assertIsNone(status["data"]["armed"])
        self.assertEqual(100, status["data"]["limit"])
        self.assertIsNone(status["data"]["lastError"])

        listing = await self.worker_call(worker, {"id": "list-1", "action": "downloads"})
        self.assertTrue(listing["success"])
        self.assertEqual([], listing["data"]["downloads"])
        self.assertEqual(MAX_DOWNLOADS, listing["data"]["limit"])
        self.assertEqual(0, listing["data"]["dropped"])

        invalid_clear = await self.worker_call(worker, {"id": "list-2", "action": "downloads", "clear": "yes"})
        self.assertFalse(invalid_clear["success"])
        self.assertEqual(CODE_INVALID, invalid_clear["code"])

        invalid_dialog = await self.worker_call(worker, {"id": "status-2", "action": "dialog", "response": "later"})
        self.assertFalse(invalid_dialog["success"])
        self.assertEqual(CODE_INVALID, invalid_dialog["code"])

    async def test_review_from_click_wait_starts_after_callback_uses_full_deadline(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        page.pending_expectation = True
        download = FakeDownload(suggested_filename="review.bin", content=b"review")
        page.next_download = download
        destination = self.tmp_dir / "review.bin"
        observed: List[Dict[str, Any]] = []
        real_wait_for = asyncio.wait_for

        async def recording_wait_for(awaitable: Any, timeout: Any = None, **kwargs: Any) -> Any:
            if awaitable is not page.expect_future:
                return await real_wait_for(awaitable, timeout, **kwargs)
            observed.append({"timeout": timeout, "log": list(page.event_log), "done": awaitable.done()})
            page.event_log.append("wait_for")
            if not awaitable.done():
                awaitable.set_result(download)
            return await real_wait_for(awaitable, timeout, **kwargs)

        async def slow_click() -> None:
            self.assertFalse(page.expect_future.done())
            for _ in range(3):
                await asyncio.sleep(0)
            page.event_log.append("click")

        with mock.patch.object(interaction_events.asyncio, "wait_for", new=recording_wait_for):
            saved = await events.from_click(str(destination), slow_click)

        self.assertEqual(str(destination), saved["path"])
        self.assertEqual(b"review", destination.read_bytes())
        self.assertEqual(["expect", "expect_enter", "click", "wait_for", "expect_exit"], page.event_log)
        self.assertEqual(
            [MAX_ACTION_DEADLINE_MS],
            page.expect_timeouts,
            "FakeExpectManager cannot expire real Playwright timers; assert the registered expectation deadline instead",
        )
        self.assertEqual(
            FROM_CLICK_TIMEOUT_MS / 1000.0,
            observed[0]["timeout"],
            "FakeExpectManager cannot expire real Playwright timers; assert the post-click wait deadline by argument",
        )
        self.assertFalse(observed[0]["done"])
        self.assertEqual(["expect", "expect_enter", "click"], observed[0]["log"])
        self.assertEqual(1, len(download.save_paths))

    async def test_review_no_event_keeps_the_session_usable_and_later_event_saves(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")
        worker = self.make_worker(runtime)
        page.pending_expectation = True
        timeouts: List[Any] = []
        real_wait_for = asyncio.wait_for

        async def raising_wait_for(awaitable: Any, timeout: Any = None, **kwargs: Any) -> Any:
            if awaitable is page.expect_future:
                timeouts.append(timeout)
                raise asyncio.TimeoutError()
            return await real_wait_for(awaitable, timeout, **kwargs)

        clicks: List[str] = []

        async def fake_click(target_runtime: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
            clicks.append("clicked")
            return {"clicked": True}

        missing = self.tmp_dir / "review-missing.bin"
        with mock.patch.object(interaction_events.asyncio, "wait_for", new=raising_wait_for):
            with mock.patch.object(worker, "do_click", new=fake_click):
                response = await self.worker_call(
                    worker,
                    {"id": "review-no-event", "action": "download", "selector": "#review", "path": str(missing)},
                )

        self.assertFalse(response["success"])
        self.assertEqual(CODE_ERROR, response["code"])
        self.assertIn("no download arrived", response["error"])
        self.assertNotIn("inputAmbiguous", response)
        self.assertFalse(worker.poisoned)
        self.assertIsNone(worker.poison_reason)
        self.assertEqual(
            [FROM_CLICK_TIMEOUT_MS / 1000.0],
            timeouts,
            "FakeExpectManager cannot expire real Playwright timers; assert the post-click wait deadline by argument",
        )
        self.assertEqual(["clicked"], clicks)
        self.assertFalse(os.path.lexists(missing))

        later = FakeDownload(suggested_filename="later.bin", content=b"later")
        page.emit("download", later)
        saved = await self.worker_call(
            worker,
            {"id": "review-later-event", "action": "waitfordownload", "path": str(self.tmp_dir / "later.bin")},
        )
        self.assertTrue(saved["success"])
        self.assertEqual(str(self.tmp_dir / "later.bin"), saved["data"]["path"])
        self.assertEqual(b"later", (self.tmp_dir / "later.bin").read_bytes())
        self.assertEqual(["clicked"], clicks)
        self.assertFalse(worker.poisoned)
        metadata = events.downloads()["downloads"]
        self.assertEqual(1, len(metadata))
        self.assertTrue(metadata[0]["consumed"])

    async def test_review_closed_source_save_unavailable_and_session_closed_waits(self) -> None:
        page = FakePage()
        runtime = self.make_runtime(page)
        events = runtime.interactions
        events.attach_page(page, "t1")

        consumed = FakeDownload(suggested_filename="consumed.bin", content=b"consumed")
        page.emit("download", consumed)
        await events.wait_for_download(str(self.tmp_dir / "consumed.bin"))
        retained = FakeDownload(suggested_filename="retained.bin", content=b"retained")
        page.emit("download", retained)
        before_close = events.downloads()["downloads"]
        self.assertNotIn("saveUnavailable", before_close[1])
        page.emit("close")
        self.assertEqual({}, events._pages)
        after_close = events.downloads()["downloads"]
        self.assertNotIn("saveUnavailable", after_close[0])
        self.assertIn("saveUnavailable", after_close[1])
        self.assertIn("source tab closed", after_close[1]["saveUnavailable"])
        self.assertTrue(after_close[0]["consumed"])
        self.assertFalse(after_close[1]["consumed"])
        self.assertIsNone(after_close[1]["savedPath"])

        page_two = FakePage()
        runtime_two = self.make_runtime(page_two)
        events_two = runtime_two.interactions
        events_two.attach_page(page_two, "t1")
        closed_error = BackendError(CODE_SESSION_CLOSED, "session closed during review")
        real_wait_for = asyncio.wait_for

        async def closed_timeout_wait(awaitable: Any, timeout: Any = None, **kwargs: Any) -> Any:
            if isinstance(awaitable, asyncio.Future) and not awaitable.done():
                raise asyncio.TimeoutError()
            return await real_wait_for(awaitable, timeout, **kwargs)

        with mock.patch.object(runtime_two, "require_open_session", side_effect=closed_error):
            with mock.patch.object(interaction_events.asyncio, "wait_for", new=closed_timeout_wait):
                with self.assertRaises(BackendError) as timed_out:
                    await events_two.wait_for_download(str(self.tmp_dir / "closed-timeout.bin"))
        self.assertEqual(CODE_SESSION_CLOSED, timed_out.exception.code)
        self.assertNotEqual(CODE_ERROR, timed_out.exception.code)
        self.assertNotIn("no download arrived", timed_out.exception.message)
        self.assertEqual({}, events_two._download_waiters)

        page_three = FakePage()
        runtime_three = self.make_runtime(page_three)
        events_three = runtime_three.interactions
        events_three.attach_page(page_three, "t1")
        with mock.patch.object(runtime_three, "require_open_session", side_effect=closed_error):
            waiting = asyncio.create_task(events_three.wait_for_download(str(self.tmp_dir / "closed-wake.bin")))
            await self.wait_until(lambda: len(events_three._download_waiters) == 1)
            page_three.emit("close")
            with self.assertRaises(BackendError) as closed_wake:
                await waiting
        self.assertEqual(CODE_SESSION_CLOSED, closed_wake.exception.code)
        self.assertNotEqual(CODE_NO_ACTIVE_TAB, closed_wake.exception.code)
        self.assertEqual({}, events_three._download_waiters)


if __name__ == "__main__":
    unittest.main()
