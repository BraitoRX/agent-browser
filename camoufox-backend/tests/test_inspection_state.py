import base64
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from browser_inspector import (
    MAX_CONSOLE_ENTRIES,
    MAX_ERROR_ENTRIES,
    MAX_PAYLOAD_BYTES,
    MAX_WEBSOCKET_EVENTS,
    BrowserInspector,
    _STORAGE_GET_ALL,
    _STORAGE_GET_ONE,
    _STORAGE_REMOVE,
    _STORAGE_SET,
)
from input_context import (
    CODE_INVALID,
    CODE_INVALID_REQUEST,
    BackendError,
)
from network_control import NetworkControl


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


class FakeMessage:
    def __init__(self, text, type="log", location=None):
        self.text = text
        self.type = type
        if location is None:
            location = {"url": "https://example.com/app.js", "lineNumber": 3, "columnNumber": 7}
        self.location = location


class FakePageError:
    def __init__(self, message=None, stack=""):
        self.message = message
        self.stack = stack

    def __str__(self):
        return "unavailable message"


class FakePage(FakeEmitter):
    def __init__(self, url="https://example.com/start"):
        super().__init__()
        self.url = url
        self.evaluate_calls = []
        self.evaluate_result = None

    async def evaluate(self, script, arg=None):
        self.evaluate_calls.append((script, arg))
        return self.evaluate_result


class FakeSocket(FakeEmitter):
    def __init__(self, url="wss://example.com/socket"):
        super().__init__()
        self.url = url


class FakeContext(FakeEmitter):
    def __init__(self):
        super().__init__()
        self.cookies_calls = []
        self.cookies_result = []
        self.add_cookies_calls = []
        self.clear_cookies_calls = 0
        self.offline_calls = []
        self.header_calls = []
        self.route_calls = []
        self.unroute_calls = []

    async def cookies(self, urls=None):
        self.cookies_calls.append(urls)
        return list(self.cookies_result)

    async def add_cookies(self, cookies):
        self.add_cookies_calls.append(cookies)

    async def clear_cookies(self):
        self.clear_cookies_calls += 1

    async def set_offline(self, value):
        self.offline_calls.append(value)

    async def set_extra_http_headers(self, headers):
        self.header_calls.append(headers)

    async def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    async def unroute(self, pattern, handler):
        self.unroute_calls.append((pattern, handler))


class FakeRuntime:
    def __init__(self, page, context):
        self.page = page
        self.context = context

    def require_open_session(self):
        return None

    def _is_live(self):
        return True

    def require_active(self):
        return self.page, None


class FakeRequest:
    def __init__(self, url, resource_type="document"):
        self.url = url
        self.resource_type = resource_type


class FakeRoute:
    def __init__(self, url, resource_type="document"):
        self.request = FakeRequest(url, resource_type)
        self.aborted = 0
        self.continued = 0
        self.fulfilled = []

    async def abort(self):
        self.aborted += 1

    async def continue_(self):
        self.continued += 1

    async def fulfill(self, **kwargs):
        self.fulfilled.append(kwargs)


class BrokenRoute:
    def __init__(self):
        self.aborted = 0

    @property
    def request(self):
        raise RuntimeError("request unavailable")

    async def abort(self):
        self.aborted += 1


def make_inspector():
    page = FakePage()
    context = FakeContext()
    inspector = BrowserInspector(FakeRuntime(page, context))
    return inspector, page, context


def make_control():
    page = FakePage()
    context = FakeContext()
    control = NetworkControl(FakeRuntime(page, context))
    return control, context


class InspectionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_truncation_cap_and_clear(self):
        inspector, page, _context = make_inspector()
        inspector.attach_page(page, "t1")

        page.emit("console", FakeMessage("x" * (MAX_PAYLOAD_BYTES + 25)))
        result = inspector.console()
        self.assertEqual(1, len(result["messages"]))
        entry = result["messages"][0]
        self.assertTrue(entry["textTruncated"])
        self.assertEqual(MAX_PAYLOAD_BYTES, len(entry["text"].encode("utf-8")))
        self.assertEqual("t1", entry["tabId"])
        self.assertEqual("log", entry["type"])
        self.assertEqual(3, entry["location"]["lineNumber"])

        for index in range(MAX_CONSOLE_ENTRIES):
            page.emit("console", FakeMessage(f"message {index}"))
        result = inspector.console()
        self.assertEqual(MAX_CONSOLE_ENTRIES, len(result["messages"]))
        self.assertEqual(MAX_CONSOLE_ENTRIES, result["limit"])
        self.assertEqual(1, result["dropped"])
        self.assertEqual("message 0", result["messages"][0]["text"])
        self.assertEqual(f"message {MAX_CONSOLE_ENTRIES - 1}", result["messages"][-1]["text"])

        with self.assertRaises(BackendError) as captured:
            inspector.console({"clear": "yes"})
        self.assertEqual(CODE_INVALID_REQUEST, captured.exception.code)

        self.assertEqual({"cleared": True}, inspector.console({"clear": True}))
        result = inspector.console()
        self.assertEqual([], result["messages"])
        self.assertEqual(0, result["dropped"])

    async def test_errors_truncation_and_clear(self):
        inspector, page, _context = make_inspector()
        inspector.attach_page(page, "t2")

        page.emit("pageerror", FakePageError("boom", stack="s" * (MAX_PAYLOAD_BYTES + 5)))
        page.emit("pageerror", FakePageError(None))
        result = inspector.errors()
        self.assertEqual(2, len(result["errors"]))
        first = result["errors"][0]
        self.assertEqual("boom", first["message"])
        self.assertTrue(first["stackTruncated"])
        self.assertEqual(MAX_PAYLOAD_BYTES, len(first["stack"].encode("utf-8")))
        self.assertEqual("t2", first["tabId"])
        self.assertEqual("unavailable message", result["errors"][1]["message"])

        with self.assertRaises(BackendError) as captured:
            inspector.errors({"clear": "yes"})
        self.assertEqual(CODE_INVALID_REQUEST, captured.exception.code)
        self.assertEqual({"cleared": True}, inspector.errors({"clear": True}))

        for index in range(MAX_ERROR_ENTRIES + 1):
            page.emit("pageerror", FakePageError(f"error {index}"))
        result = inspector.errors()
        self.assertEqual(MAX_ERROR_ENTRIES, len(result["errors"]))
        self.assertEqual(MAX_ERROR_ENTRIES, result["limit"])
        self.assertEqual(1, result["dropped"])
        self.assertEqual("error 1", result["errors"][0]["message"])

    async def test_websocket_frames_binary_close_detach(self):
        inspector, page, _context = make_inspector()
        inspector.attach_page(page, "t1")
        socket = FakeSocket()
        page.emit("websocket", socket)

        socket.emit("framesent", "hello")
        binary = bytes(range(256)) * 40
        socket.emit("framereceived", binary)
        socket.emit("socketerror", "boom")

        result = inspector.websockets()
        events = result["websockets"]
        self.assertEqual(["open", "sent", "received", "error"], [item["event"] for item in events])
        self.assertEqual(MAX_WEBSOCKET_EVENTS, result["limit"])
        open_event = events[0]
        self.assertEqual("w1", open_event["webSocketId"])
        self.assertEqual("t1", open_event["tabId"])
        self.assertEqual("wss://example.com/socket", open_event["url"])
        received = events[2]
        self.assertTrue(received["base64Encoded"])
        self.assertTrue(received["truncated"])
        self.assertEqual(
            base64.b64encode(binary[:MAX_PAYLOAD_BYTES]).decode("ascii"),
            received["payload"],
        )
        self.assertEqual("boom", events[3]["payload"])

        socket.emit("close")
        result = inspector.websockets()
        self.assertEqual("close", result["websockets"][-1]["event"])
        self.assertEqual([], socket.listeners["framesent"])
        self.assertEqual([], socket.listeners["framereceived"])
        self.assertEqual([], socket.listeners["socketerror"])
        self.assertEqual([], socket.listeners["close"])
        self.assertFalse(inspector._sockets)
        self.assertFalse(inspector._sockets_by_id)

    async def test_websocket_clear_keeps_quiet_socket_and_caps_connections(self):
        inspector, page, _context = make_inspector()
        inspector.attach_page(page, "t1")
        quiet = FakeSocket(url="wss://quiet.example.com")
        page.emit("websocket", quiet)
        others = [
            FakeSocket(url=f"wss://busy{index}.example.com")
            for index in range(MAX_WEBSOCKET_EVENTS - 1)
        ]
        for socket in others:
            page.emit("websocket", socket)
        busy = others[-1]
        for index in range(MAX_WEBSOCKET_EVENTS):
            busy.emit("framesent", f"payload {index}")

        evicted = inspector.websockets()
        self.assertEqual(MAX_WEBSOCKET_EVENTS, len(evicted["websockets"]))
        self.assertEqual(MAX_WEBSOCKET_EVENTS, evicted["dropped"])
        self.assertEqual(0, inspector._sockets_by_id["w1"]["events"])

        for _ in range(3):
            cleared = inspector.websockets({"clear": True})
            self.assertEqual({"cleared": True, "connectionsDropped": 0}, cleared)

        self.assertEqual(MAX_WEBSOCKET_EVENTS, len(inspector._sockets))
        self.assertIn("w1", inspector._sockets_by_id)
        self.assertEqual(1, len(quiet.listeners["framesent"]))

        quiet.emit("framesent", "after clear")
        after = inspector.websockets()
        self.assertEqual(1, len(after["websockets"]))
        self.assertEqual("w1", after["websockets"][0]["webSocketId"])
        self.assertEqual("after clear", after["websockets"][0]["payload"])

        extra = FakeSocket(url="wss://extra.example.com")
        page.emit("websocket", extra)
        self.assertEqual(MAX_WEBSOCKET_EVENTS, len(inspector._sockets))
        capped = inspector.websockets()
        self.assertEqual(1, capped["connectionsDropped"])
        self.assertNotIn("w1", inspector._sockets_by_id)
        self.assertEqual([], quiet.listeners["framesent"])

    async def test_reset_detaches_listeners_and_clears_events(self):
        inspector, page, _context = make_inspector()
        inspector.attach_page(page, "t1")
        socket = FakeSocket()
        page.emit("websocket", socket)
        page.emit("console", FakeMessage("hello"))
        page.emit("pageerror", FakePageError("boom"))

        inspector.reset()

        for event in ("console", "pageerror", "websocket"):
            self.assertEqual([], page.listeners[event])
        for event in ("framesent", "framereceived", "socketerror", "close"):
            self.assertEqual([], socket.listeners[event])
        self.assertFalse(inspector._sockets)
        self.assertFalse(inspector._sockets_by_id)
        self.assertEqual([], inspector.console()["messages"])
        self.assertEqual([], inspector.errors()["errors"])
        self.assertEqual([], inspector.websockets()["websockets"])

    async def test_cookie_defaults_and_url_normalization(self):
        page = FakePage(url="https://example.com/start?q=1")
        context = FakeContext()
        inspector = BrowserInspector(FakeRuntime(page, context))

        result = await inspector.state("cookies_set", {"cookies": [
            {"name": "session", "value": "abc"},
            {"name": "scoped", "value": "xyz", "domain": "example.com"},
            {"name": "pathed", "value": "p", "url": "https://example.com/app?x=1", "path": "/app"},
            {"name": "plain", "value": "v", "url": "https://example.com/only"},
        ]})

        self.assertEqual({"set": True, "count": 4}, result)
        self.assertEqual([[
            {"name": "session", "value": "abc", "url": "https://example.com/start?q=1"},
            {"name": "scoped", "value": "xyz", "domain": "example.com", "path": "/"},
            {"name": "pathed", "value": "p", "domain": "example.com", "path": "/app", "secure": True},
            {"name": "plain", "value": "v", "url": "https://example.com/only"},
        ]], context.add_cookies_calls)

        page.url = "about:blank"
        with self.assertRaises(BackendError) as captured:
            await inspector.state("cookies_set", {"cookies": [{"name": "scoped", "value": "x"}]})
        self.assertEqual(CODE_INVALID, captured.exception.code)
        self.assertEqual(1, len(context.add_cookies_calls))

    async def test_cookie_rejects_unknown_fields_and_conflicts_before_add(self):
        page = FakePage(url="https://example.com/")
        context = FakeContext()
        inspector = BrowserInspector(FakeRuntime(page, context))

        with self.assertRaises(BackendError) as captured:
            await inspector.state("cookies_set", {"cookies": [
                {"name": "ok", "value": "1"},
                {"name": "bad", "value": "2", "bogus": True},
            ]})
        self.assertEqual(CODE_INVALID, captured.exception.code)
        self.assertIn("bogus", captured.exception.message)
        self.assertEqual([], context.add_cookies_calls)

        with self.assertRaises(BackendError):
            await inspector.state("cookies_set", {"cookies": [
                {"name": "conflict", "value": "1", "url": "https://example.com/", "domain": "example.com"},
            ]})
        self.assertEqual([], context.add_cookies_calls)

    async def test_storage_get_set_empty_key_and_argument_safety(self):
        page = FakePage(url="https://example.com/")
        context = FakeContext()
        inspector = BrowserInspector(FakeRuntime(page, context))

        page.evaluate_result = "stored"
        got = await inspector.state("storage_get", {"type": "local", "key": ""})
        self.assertEqual({"key": "", "value": "stored"}, got)
        self.assertEqual((_STORAGE_GET_ONE, {"type": "local", "key": ""}), page.evaluate_calls[-1])

        hostile = "</script><script>window.pwned=1</script>"
        set_result = await inspector.state("storage_set", {"type": "session", "key": "", "value": hostile})
        self.assertEqual({"set": True}, set_result)
        self.assertEqual((_STORAGE_SET, {"type": "session", "key": "", "value": hostile}), page.evaluate_calls[-1])

        removed = await inspector.state("storage_clear", {"type": "local", "key": ""})
        self.assertEqual({"cleared": True}, removed)
        self.assertEqual((_STORAGE_REMOVE, {"type": "local", "key": ""}), page.evaluate_calls[-1])

        page.evaluate_result = {"first": "1", "second": ""}
        all_values = await inspector.state("storage_get", {"type": "session"})
        self.assertEqual({"data": {"first": "1", "second": ""}}, all_values)
        self.assertEqual((_STORAGE_GET_ALL, {"type": "session"}), page.evaluate_calls[-1])


class NetworkControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_newest_rule_and_owned_handler_removal(self):
        control, context = make_control()
        await control.dispatch("route", {"url": "https://api.example.com/*", "abort": True})
        await control.dispatch("route", {"url": "https://api.example.com/*"})
        await control.dispatch("route", {"url": "https://other.example.com/*", "response": {"status": 201}})
        self.assertEqual(1, len(context.route_calls))
        pattern, handler = context.route_calls[0]
        self.assertEqual("**/*", pattern)

        newest = FakeRoute("https://api.example.com/items")
        await handler(newest)
        self.assertEqual(0, newest.aborted)
        self.assertEqual(1, newest.continued)

        other = FakeRoute("https://other.example.com/data")
        await handler(other)
        self.assertEqual(
            [{"status": 201, "body": "", "headers": {}, "content_type": "application/json"}],
            other.fulfilled,
        )

        unmatched = FakeRoute("https://unknown.example.com/x")
        await handler(unmatched)
        self.assertEqual(1, unmatched.continued)
        self.assertEqual(0, unmatched.aborted)

        await control.dispatch("unroute", {"url": "https://api.example.com/*"})
        self.assertEqual([], context.unroute_calls)
        self.assertEqual(1, len(control.rules))
        self.assertTrue(control.attached)

        await control.dispatch("unroute", {"url": "https://other.example.com/*"})
        self.assertEqual(1, len(context.unroute_calls))
        unroute_pattern, unroute_handler = context.unroute_calls[0]
        self.assertEqual("**/*", unroute_pattern)
        self.assertEqual(handler, unroute_handler)
        self.assertFalse(control.attached)
        self.assertEqual([], control.rules)

    async def test_route_resource_filter_and_rejection_before_install(self):
        control, context = make_control()
        await control.dispatch(
            "route",
            {"url": "https://cdn.example.com/*", "abort": True, "resourceType": "image, stylesheet"},
        )
        self.assertEqual(1, len(context.route_calls))
        handler = context.route_calls[0][1]

        script_route = FakeRoute("https://cdn.example.com/app.js", resource_type="script")
        await handler(script_route)
        self.assertEqual(1, script_route.continued)
        self.assertEqual(0, script_route.aborted)

        image_route = FakeRoute("https://cdn.example.com/logo.png", resource_type="image")
        await handler(image_route)
        self.assertEqual(1, image_route.aborted)
        self.assertEqual(0, image_route.continued)

        for payload in (
            {"url": ""},
            {"url": "https://x.example.com/", "abort": "yes"},
            {"url": "https://x.example.com/", "response": {"status": 99}},
            {"url": "https://x.example.com/", "response": {"status": 200, "headers": "nope"}},
            {"url": "https://x.example.com/", "abort": True, "response": {"status": 200}},
            {"url": "https://x.example.com/", "resourceType": "invalid"},
        ):
            with self.assertRaises(BackendError):
                await control.dispatch("route", payload)

        self.assertEqual(1, len(context.route_calls))
        self.assertEqual(1, len(control.rules))
        self.assertTrue(control.attached)

    async def test_headers_offline_and_route_failure_diagnostic(self):
        control, context = make_control()

        self.assertEqual({"offline": True}, await control.dispatch("offline", {"offline": True}))
        self.assertEqual([True], context.offline_calls)

        self.assertEqual({"set": True}, await control.dispatch("headers", {"headers": {"Authorization": "Bearer token"}}))
        self.assertEqual([{"Authorization": "Bearer token"}], context.header_calls)
        self.assertEqual({"Authorization": "Bearer token"}, control.headers)

        self.assertEqual({"set": True}, await control.dispatch("credentials", {"username": "user", "password": "p@ss"}))
        token = base64.b64encode(b"user:p@ss").decode("ascii")
        self.assertEqual({"Authorization": f"Basic {token}"}, context.header_calls[1])

        with self.assertRaises(BackendError):
            await control.dispatch("headers", {"headers": {"X-Bad": "line\nbreak"}})
        with self.assertRaises(BackendError):
            await control.dispatch("credentials", {"username": "a:b", "password": "c"})
        self.assertEqual(2, len(context.header_calls))

        await control.dispatch("route", {"url": "https://api.example.com/*", "abort": True})
        handler = context.route_calls[0][1]
        broken = BrokenRoute()
        await handler(broken)
        self.assertEqual({"error": "RuntimeError", "outcome": "unconfirmed"}, control.last_error)
        self.assertEqual(1, broken.aborted)


if __name__ == "__main__":
    unittest.main()
