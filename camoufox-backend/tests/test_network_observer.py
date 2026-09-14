from __future__ import annotations

import asyncio
import base64
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import network_observer
from input_context import BackendError, CODE_INVALID_REQUEST


class FakeContext:
    def __init__(self) -> None:
        self.handlers: Dict[str, List[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self.handlers.get(event, []).remove(handler)

    def emit(self, event: str, *args: Any) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(*args)

    def count(self) -> int:
        return sum(len(items) for items in self.handlers.values())


class FakeFrame:
    def __init__(self, page: Any) -> None:
        self.page = page


class FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        resource_type: str = "document",
        frame: Any = None,
        post_data: Optional[str] = None,
        timing: Optional[Dict[str, Any]] = None,
        headers: Optional[List[Dict[str, str]]] = None,
        failure: Optional[str] = None,
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.frame = frame
        self.redirected_from = None
        self._post_data = post_data
        self._timing = timing if timing is not None else {}
        self._headers = headers
        self._failure = failure

    @property
    def post_data(self) -> Optional[str]:
        return self._post_data

    @property
    def timing(self) -> Dict[str, Any]:
        return self._timing

    @property
    def failure(self) -> Optional[str]:
        return self._failure

    async def headers_array(self) -> Optional[List[Dict[str, str]]]:
        return None if self._headers is None else list(self._headers)

    async def all_headers(self) -> Optional[List[Dict[str, str]]]:
        return None if self._headers is None else list(self._headers)


class FakeResponse:
    def __init__(
        self,
        request: FakeRequest,
        *,
        status: int = 200,
        headers: Optional[Dict[str, str]] = None,
        headers_array: Optional[List[Dict[str, str]]] = None,
        body: bytes = b"",
    ) -> None:
        self.request = request
        self.status = status
        self._headers = headers if headers is not None else {}
        self._headers_array = headers_array
        self._body = body
        self.body_calls = 0

    @property
    def headers(self) -> Dict[str, str]:
        return self._headers

    async def headers_array(self) -> Optional[List[Dict[str, str]]]:
        return None if self._headers_array is None else list(self._headers_array)

    async def all_headers(self) -> Optional[List[Dict[str, str]]]:
        if self._headers_array is not None:
            return list(self._headers_array)
        return [{"name": name, "value": value} for name, value in self._headers.items()]

    async def body(self) -> bytes:
        self.body_calls += 1
        return self._body


class FakeRuntime:
    def __init__(self) -> None:
        self._page_to_tab: Dict[int, str] = {}


class NetworkObserverTests(unittest.IsolatedAsyncioTestCase):
    def make_observer(self) -> Any:
        runtime = FakeRuntime()
        observer = network_observer.NetworkObserver(runtime)
        context = FakeContext()
        observer.attach(context)
        return observer, context, runtime

    async def test_metadata_events_do_not_read_bodies_until_detail_requested(self) -> None:
        observer, context, runtime = self.make_observer()
        page = object()
        runtime._page_to_tab[id(page)] = "t1"

        request = FakeRequest(
            "https://example.test/api/items",
            method="GET",
            resource_type="fetch",
            frame=FakeFrame(page),
        )
        response = FakeResponse(
            request,
            status=200,
            headers={"Content-Type": "application/json"},
            body=b'{"ok":true}',
        )
        context.emit("request", request)
        context.emit("response", response)
        context.emit("requestfinished", request)

        self.assertEqual(response.body_calls, 0)
        listing = observer.requests()
        self.assertEqual(response.body_calls, 0)
        self.assertEqual(len(listing["requests"]), 1)
        metadata = listing["requests"][0]
        self.assertEqual(metadata["requestId"], "n1")
        self.assertEqual(metadata["tabId"], "t1")
        self.assertEqual(metadata["url"], "https://example.test/api/items")
        self.assertEqual(metadata["method"], "GET")
        self.assertEqual(metadata["resourceType"], "fetch")
        self.assertEqual(metadata["status"], 200)
        self.assertIs(metadata["finished"], True)
        self.assertGreater(metadata["timestamp"], 0)
        self.assertEqual(listing["dropped"], 0)

        detail = await observer.request_detail("n1")
        self.assertEqual(response.body_calls, 1)
        self.assertEqual(detail["responseBody"], '{"ok":true}')
        self.assertIs(detail["base64Encoded"], False)

    async def test_detail_preserves_duplicate_headers_post_data_timing_and_binary_body(self) -> None:
        observer, context, _runtime = self.make_observer()
        request = FakeRequest(
            "https://example.test/upload",
            method="POST",
            resource_type="xhr",
            post_data='{"payload":1}',
            timing={"startTime": 1.5, "requestStart": 3.25, "ignored": "n/a"},
            headers=[
                {"name": "Accept", "value": "application/json"},
                {"name": "X-Trace", "value": "one"},
                {"name": "X-Trace", "value": "two"},
            ],
        )
        response = FakeResponse(
            request,
            status=201,
            headers={"Content-Type": "application/octet-stream"},
            headers_array=[
                {"name": "Content-Type", "value": "application/octet-stream"},
                {"name": "Content-Length", "value": "3"},
            ],
            body=b"\x00\x01\xff",
        )
        context.emit("request", request)
        context.emit("response", response)
        context.emit("requestfinished", request)

        detail = await observer.request_detail("n1")
        traces = [entry for entry in detail["requestHeaders"] if entry["name"] == "X-Trace"]
        self.assertEqual([entry["value"] for entry in traces], ["one", "two"])
        self.assertEqual(detail["postData"], '{"payload":1}')
        self.assertEqual(detail["timing"], {"startTime": 1.5, "requestStart": 3.25})
        self.assertEqual(detail["responseBody"], base64.b64encode(b"\x00\x01\xff").decode("ascii"))
        self.assertIs(detail["base64Encoded"], True)

    async def test_body_omission_for_pending_failed_and_known_oversized(self) -> None:
        observer, context, _runtime = self.make_observer()

        pending_request = FakeRequest("https://example.test/slow")
        pending_response = FakeResponse(pending_request, status=200, body=b"late")
        context.emit("request", pending_request)
        context.emit("response", pending_response)

        failed_request = FakeRequest("https://example.test/broken", failure="net::ERR_FAILED")
        failed_response = FakeResponse(failed_request, status=200, body=b"never")
        context.emit("request", failed_request)
        context.emit("response", failed_response)
        context.emit("requestfailed", failed_request)

        oversized_request = FakeRequest("https://example.test/huge")
        oversized_response = FakeResponse(
            oversized_request,
            status=200,
            headers_array=[
                {"name": "Content-Type", "value": "application/octet-stream"},
                {"name": "Content-Length", "value": "2097152"},
            ],
            body=b"ignored",
        )
        context.emit("request", oversized_request)
        context.emit("response", oversized_response)
        context.emit("requestfinished", oversized_request)

        pending_detail = await observer.request_detail("n1")
        self.assertEqual(pending_detail["responseBodyUnavailable"]["reason"], "pending")

        failed_detail = await observer.request_detail("n2")
        self.assertEqual(failed_detail["responseBodyUnavailable"]["reason"], "failed")
        self.assertIn("net::ERR_FAILED", failed_detail["responseBodyUnavailable"]["failure"])
        self.assertIs(failed_detail["finished"], True)

        oversized_detail = await observer.request_detail("n3")
        unavailable = oversized_detail["responseBodyUnavailable"]
        self.assertEqual(unavailable["reason"], "oversized")
        self.assertEqual(unavailable["contentLength"], 2097152)
        self.assertEqual(unavailable["limit"], 1024 * 1024)

        self.assertEqual(pending_response.body_calls, 0)
        self.assertEqual(failed_response.body_calls, 0)
        self.assertEqual(oversized_response.body_calls, 0)

    async def test_detail_survives_eviction_during_header_read(self) -> None:
        observer, context, _runtime = self.make_observer()
        request = FakeRequest("https://example.test/evicted")
        response = FakeResponse(request, headers={"Content-Type": "text/plain"}, body=b"retained")
        context.emit("request", request)
        context.emit("response", response)
        context.emit("requestfinished", request)
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_headers():
            started.set()
            await release.wait()
            return []

        request.headers_array = delayed_headers
        detail_task = asyncio.create_task(observer.request_detail("n1"))
        await asyncio.wait_for(started.wait(), timeout=1)
        try:
            for index in range(500):
                context.emit("request", FakeRequest(f"https://example.test/new/{index}"))
        finally:
            release.set()
        detail = await detail_task
        self.assertEqual(observer.requests()["dropped"], 1)
        self.assertEqual(detail["responseBody"], "retained")
        self.assertTrue(detail["finished"])
        self.assertEqual(response.body_calls, 1)

    async def test_unknown_size_body_truncates_at_one_mib_with_marker(self) -> None:
        observer, context, _runtime = self.make_observer()
        payload = b"a" * (1024 * 1024 + 5)
        request = FakeRequest("https://example.test/stream", resource_type="fetch")
        response = FakeResponse(
            request,
            status=200,
            headers={"Content-Type": "application/octet-stream"},
            body=payload,
        )
        context.emit("request", request)
        context.emit("response", response)
        context.emit("requestfinished", request)

        detail = await observer.request_detail("n1")
        self.assertIs(detail["responseBodyTruncated"], True)
        self.assertEqual(detail["responseBodyBytes"], len(payload))
        self.assertIs(detail["base64Encoded"], True)
        self.assertEqual(len(base64.b64decode(detail["responseBody"])), 1024 * 1024)

    async def test_capacity_cap_clear_monotonic_ids_and_late_events(self) -> None:
        observer, context, _runtime = self.make_observer()
        requests = [FakeRequest(f"https://example.test/{index}") for index in range(502)]
        for request in requests:
            context.emit("request", request)

        listing = observer.requests()
        self.assertEqual(len(listing["requests"]), 500)
        self.assertEqual(listing["dropped"], 2)
        self.assertEqual(listing["requests"][0]["requestId"], "n3")
        self.assertEqual(listing["requests"][-1]["requestId"], "n502")

        self.assertEqual(observer.requests({"clear": True}), {"cleared": True})
        self.assertEqual(observer.requests()["requests"], [])
        self.assertEqual(observer.requests()["dropped"], 0)

        context.emit("requestfinished", requests[0])
        context.emit("requestfailed", requests[1])
        self.assertEqual(observer.requests()["requests"], [])
        with self.assertRaises(BackendError) as unknown:
            await observer.request_detail("n3")
        self.assertEqual(unknown.exception.code, CODE_INVALID_REQUEST)

        fresh = FakeRequest("https://example.test/fresh")
        context.emit("request", fresh)
        self.assertEqual(observer.requests()["requests"][0]["requestId"], "n503")

    async def test_filters_validation_and_reset_detaches_listeners(self) -> None:
        observer, context, _runtime = self.make_observer()
        self.assertEqual(context.count(), 4)

        for url, method, resource_type, status in (
            ("https://example.test/page", "GET", "document", 200),
            ("https://example.test/api/users", "POST", "xhr", 404),
            ("https://cdn.example.test/font.woff2", "GET", "font", 500),
        ):
            request = FakeRequest(url, method=method, resource_type=resource_type)
            response = FakeResponse(request, status=status, body=b"x")
            context.emit("request", request)
            context.emit("response", response)
            context.emit("requestfinished", request)

        by_url = observer.requests({"filter": "api/users"})
        self.assertEqual([entry["requestId"] for entry in by_url["requests"]], ["n2"])

        by_type = observer.requests({"type": "XHR, Font"})
        self.assertEqual(sorted(entry["requestId"] for entry in by_type["requests"]), ["n2", "n3"])

        by_method = observer.requests({"method": "post"})
        self.assertEqual([entry["requestId"] for entry in by_method["requests"]], ["n2"])

        by_class = observer.requests({"status": "4xx"})
        self.assertEqual([entry["requestId"] for entry in by_class["requests"]], ["n2"])

        by_range = observer.requests({"status": "200-499"})
        self.assertEqual(sorted(entry["requestId"] for entry in by_range["requests"]), ["n1", "n2"])

        for bad_payload in (
            {"filter": 7},
            {"filter": ""},
            {"status": "soon"},
            {"type": ", ,"},
            {"clear": "yes"},
        ):
            with self.assertRaises(BackendError) as invalid:
                observer.requests(bad_payload)
            self.assertEqual(invalid.exception.code, CODE_INVALID_REQUEST)

        observer.reset()
        self.assertEqual(context.count(), 0)
        context.emit("request", FakeRequest("https://example.test/after-reset"))
        self.assertEqual(observer.requests()["requests"], [])

        second = FakeContext()
        observer.attach(second)
        context.emit("request", FakeRequest("https://example.test/late"))
        self.assertEqual(observer.requests()["requests"], [])
        second.emit("request", FakeRequest("https://example.test/second"))
        self.assertEqual(observer.requests()["requests"][0]["requestId"], "n1")
