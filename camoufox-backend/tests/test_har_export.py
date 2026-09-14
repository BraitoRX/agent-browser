import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from har_capture import HarCapture
from input_context import CODE_INVALID, CODE_NOT_LAUNCHED, BackendError
from network_observer import MAX_METADATA_BYTES


class FakeEmitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        handlers = self.listeners.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def emit(self, event, *args):
        for handler in list(self.listeners.get(event, [])):
            handler(*args)


class FakeContext(FakeEmitter):
    pass


class FakeRequest:
    def __init__(self, url, timing=None, failure=None, resource_type="document"):
        self.url = url
        self.method = "GET"
        self.resource_type = resource_type
        self.post_data = None
        self.failure = failure
        self._timing = timing

    @property
    def timing(self):
        if self._timing is None:
            raise AttributeError("timing unavailable")
        return self._timing

    async def headers_array(self):
        return [{"name": "accept", "value": "*/*"}]


class FakeResponse:
    def __init__(self, request, status=200, headers=None):
        self.request = request
        self.status = status
        self.headers = headers or {}
        self.body_calls = 0

    async def headers_array(self):
        return [{"name": name, "value": value} for name, value in self.headers.items()]

    async def body(self):
        self.body_calls += 1
        return b""

class FakeRuntime:
    def __init__(self, context, runtime_dir):
        self.context = context
        self.runtime_dir = Path(runtime_dir)
        self._page_to_tab = {}

    def require_open_session(self):
        return None


class HarExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = FakeContext()
        self.runtime_dir = Path(tempfile.mkdtemp(prefix="har-export-test-"))
        self.addCleanup(shutil.rmtree, self.runtime_dir, ignore_errors=True)
        self.capture = HarCapture(FakeRuntime(self.context, self.runtime_dir))

    def destination(self, name):
        return str(self.runtime_dir / name)

    async def export(self, path):
        summary = await self.capture.stop(path)
        document = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
        return summary, document

    def entries_by_url(self, document):
        return {entry["request"]["url"]: entry for entry in document["log"]["entries"]}

    async def test_timings_placeholders_time_and_failure_truncation(self):
        await self.capture.start("none")
        finished = FakeRequest(
            "https://example.com/finished",
            timing={
                "domainLookupStart": 10.0,
                "domainLookupEnd": 15.0,
                "connectStart": 15.0,
                "connectEnd": 25.0,
                "secureConnectionStart": 20.0,
                "requestStart": 30.0,
                "responseStart": 50.0,
                "responseEnd": 80.0,
            },
        )
        pending = FakeRequest("https://example.com/pending")
        failed = FakeRequest(
            "https://example.com/failed",
            timing={
                "domainLookupStart": 5.0,
                "domainLookupEnd": 8.0,
                "connectStart": -1,
            },
            failure="x" * (MAX_METADATA_BYTES + 64),
        )
        for request in (finished, pending, failed):
            self.context.emit("request", request)
        self.context.emit("response", FakeResponse(finished))
        self.context.emit("requestfinished", finished)
        self.context.emit("requestfailed", failed)

        _summary, document = await self.export(self.destination("timings.har"))
        entries = self.entries_by_url(document)

        finished_entry = entries["https://example.com/finished"]
        finished_timings = finished_entry["timings"]
        measured_sum = sum(
            finished_timings[phase]
            for phase in ("blocked", "dns", "connect", "send", "wait", "receive")
        )
        self.assertEqual(finished_timings["ssl"], 5.0)
        self.assertEqual(measured_sum, 75.0)
        self.assertEqual(finished_entry["time"], round(measured_sum, 3))
        self.assertEqual(finished_timings["send"], 0.0)
        self.assertEqual(finished_timings["wait"], 20.0)
        self.assertEqual(finished_timings["receive"], 30.0)
        self.assertEqual(finished_entry["_agentBrowser"]["unavailableTimings"], ["send"])
        self.assertEqual(finished_entry["_agentBrowser"]["rawTimings"]["send"], -1.0)
        self.assertEqual(finished_entry["_agentBrowser"]["rawTimings"]["wait"], 20.0)
        self.assertEqual(finished_entry["_agentBrowser"]["status"], "captured")

        pending_entry = entries["https://example.com/pending"]
        self.assertEqual(pending_entry["_agentBrowser"]["status"], "pending")
        self.assertEqual(pending_entry["time"], 0.0)
        self.assertEqual(pending_entry["timings"]["blocked"], -1.0)
        self.assertEqual(
            pending_entry["_agentBrowser"]["unavailableTimings"], ["send", "wait", "receive"]
        )
        self.assertEqual(pending_entry["_agentBrowser"]["rawTimings"]["wait"], -1.0)

        failed_entry = entries["https://example.com/failed"]
        self.assertEqual(failed_entry["_agentBrowser"]["status"], "failed")
        self.assertIn("failure_truncated", failed_entry["_agentBrowser"]["reasons"])
        self.assertLessEqual(len(failed_entry["_error"]), MAX_METADATA_BYTES)
        self.assertEqual(
            failed_entry["_agentBrowser"]["unavailableTimings"], ["send", "wait", "receive"]
        )
        self.assertEqual(failed_entry["_agentBrowser"]["rawTimings"]["receive"], -1.0)
        self.assertEqual(failed_entry["time"], 8.0)

        notes = document["log"]["_agentBrowser"]["capture"]["notes"]
        self.assertTrue(any("cannot separate send from wait" in note for note in notes))
        self.assertTrue(
            any("placeholders" in note and "unavailableTimings" in note for note in notes)
        )

    async def test_content_none_skips_body_and_maps_mime_and_location(self):
        await self.capture.start("none")
        request = FakeRequest("https://example.com/pdf")
        response = FakeResponse(
            request,
            headers={
                "content-type": "application/pdf",
                "location": "https://example.com/next",
            },
        )
        self.context.emit("request", request)
        self.context.emit("response", response)
        self.context.emit("requestfinished", request)

        _summary, document = await self.export(self.destination("none.har"))
        entry = self.entries_by_url(document)["https://example.com/pdf"]
        self.assertEqual(entry["response"]["content"]["mimeType"], "application/pdf")
        self.assertNotIn("text", entry["response"]["content"])
        self.assertEqual(entry["response"]["redirectURL"], "https://example.com/next")
        self.assertEqual(response.body_calls, 0)

        await self.capture.start("text")
        binary_request = FakeRequest("https://example.com/binary")
        binary_response = FakeResponse(
            binary_request,
            headers={"content-type": "application/octet-stream"},
        )
        self.context.emit("request", binary_request)
        self.context.emit("response", binary_response)
        self.context.emit("requestfinished", binary_request)

        _summary, document = await self.export(self.destination("text.har"))
        entry = self.entries_by_url(document)["https://example.com/binary"]
        self.assertEqual(entry["response"]["content"]["mimeType"], "application/octet-stream")
        self.assertIn("body_not_textual", entry["_agentBrowser"]["reasons"])
        self.assertEqual(binary_response.body_calls, 0)

    async def test_destination_refusal_and_retry(self):
        await self.capture.start("none")
        request = FakeRequest("https://example.com/ok")
        response = FakeResponse(request)
        self.context.emit("request", request)
        self.context.emit("response", response)
        self.context.emit("requestfinished", request)

        existing = self.runtime_dir / "existing.har"
        existing.write_text("existing", encoding="utf-8")
        with self.assertRaises(BackendError) as caught:
            await self.capture.stop(str(existing))
        self.assertEqual(caught.exception.code, CODE_INVALID)
        self.assertEqual(existing.read_text(encoding="utf-8"), "existing")
        self.assertTrue(self.capture.pending)

        retry_path = self.destination("retry.har")
        summary, document = await self.export(retry_path)
        self.assertFalse(self.capture.pending)
        self.assertEqual(summary["path"], retry_path)
        self.assertEqual(summary["requestCount"], 1)
        self.assertEqual(len(document["log"]["entries"]), 1)

    async def test_boundary_missing_context_reports_not_launched(self):
        capture = HarCapture(FakeRuntime(None, self.runtime_dir))
        with self.assertRaises(BackendError) as caught:
            await capture.start()
        self.assertEqual(caught.exception.code, CODE_NOT_LAUNCHED)
        self.assertFalse(capture.active)

    async def test_boundary_fallback_headers_remain_bounded(self):
        await self.capture.start("all")
        request = FakeRequest("https://example.com/fallback")
        response = FakeResponse(request, headers={
            "content-type": "application/octet-stream; " + "x" * MAX_METADATA_BYTES,
            "location": "https://example.com/" + "y" * MAX_METADATA_BYTES,
        })

        async def empty_headers():
            return []

        response.headers_array = empty_headers
        self.context.emit("request", request)
        self.context.emit("response", response)
        self.context.emit("requestfinished", request)
        _summary, document = await self.export(self.destination("bounded.har"))
        entry = document["log"]["entries"][0]
        self.assertEqual(len(entry["response"]["content"]["mimeType"].encode("utf-8")), MAX_METADATA_BYTES)
        self.assertEqual(len(entry["response"]["redirectURL"].encode("utf-8")), MAX_METADATA_BYTES)
        self.assertIn("response_headers_truncated", entry["_agentBrowser"]["reasons"])
        self.assertEqual(response.body_calls, 1)


if __name__ == "__main__":
    unittest.main()
