from __future__ import annotations

import base64
import json
import math
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from input_context import (
    BackendError,
    CODE_ERROR,
    CODE_INVALID,
    CODE_INVALID_REQUEST,
    CODE_NOT_LAUNCHED,
    CODE_UNSUPPORTED,
    MAX_INPUT_BYTES,
    MAX_SELECTOR_LENGTH,
    MAX_TEXT_LENGTH,
    json_safe,
    optional_enum,
    require_bool,
    require_str,
    require_str_list,
)

MAX_CONSOLE_ENTRIES = 500
MAX_ERROR_ENTRIES = 500
MAX_WEBSOCKET_EVENTS = 200
MAX_PAYLOAD_BYTES = 8 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_FILTER_LENGTH = 4096

COOKIE_FIELDS = frozenset(
    {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
)
COOKIE_SAME_SITE = ("Strict", "Lax", "None")
STORAGE_TYPES = ("local", "session")

_STORAGE_GET_ONE = (
    "(spec) => { const store = spec.type === 'session' ? window.sessionStorage : window.localStorage; "
    "const value = store.getItem(spec.key); return value === null ? null : value; }"
)
_STORAGE_GET_ALL = (
    "(spec) => { const store = spec.type === 'session' ? window.sessionStorage : window.localStorage; "
    "const data = Object.create(null); for (let index = 0; index < store.length; index += 1) { "
    "const key = store.key(index); if (key !== null) { data[key] = store.getItem(key); } } return data; }"
)
_STORAGE_SET = (
    "(spec) => { const store = spec.type === 'session' ? window.sessionStorage : window.localStorage; "
    "store.setItem(spec.key, spec.value); return true; }"
)
_STORAGE_REMOVE = (
    "(spec) => { const store = spec.type === 'session' ? window.sessionStorage : window.localStorage; "
    "store.removeItem(spec.key); return true; }"
)
_STORAGE_CLEAR = (
    "(spec) => { const store = spec.type === 'session' ? window.sessionStorage : window.localStorage; "
    "store.clear(); return true; }"
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bound_text(value: str, limit: int = MAX_PAYLOAD_BYTES) -> Tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def _encoded_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return MAX_STATE_BYTES + 1


def _safe_attr(target: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(target, name)
    except Exception:
        return default
    return default if value is None else value


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendError(CODE_INVALID, f"'{field}' must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise BackendError(CODE_INVALID, f"'{field}' must be a finite number")
    return number


def _bound_list(items: List[Any]) -> Tuple[List[Any], bool, int]:
    if _encoded_size(items) <= MAX_STATE_BYTES:
        return list(items), False, 0
    bounded: List[Any] = []
    used = 16
    omitted = 0
    for index, item in enumerate(items):
        cost = _encoded_size(item) + 2
        if used + cost > MAX_STATE_BYTES:
            omitted = len(items) - index
            break
        bounded.append(item)
        used += cost
    return bounded, True, omitted


def _bound_storage_data(data: Dict[str, str]) -> Dict[str, Any]:
    if _encoded_size(data) <= MAX_STATE_BYTES:
        return {"data": data}
    bounded: Dict[str, str] = {}
    used = 16
    omitted = 0
    items = list(data.items())
    for index, (key, value) in enumerate(items):
        key_bytes = len(key.encode("utf-8"))
        remaining = MAX_STATE_BYTES - used - key_bytes - 8
        if remaining <= 2:
            omitted = len(items) - index
            break
        text, cut = _bound_text(value, remaining)
        bounded[key] = text
        used += key_bytes + len(text.encode("utf-8")) + 8
        if cut:
            omitted = len(items) - index - 1
            break
    result: Dict[str, Any] = {"data": bounded, "truncated": True}
    if omitted:
        result["omitted"] = omitted
    return result


class BrowserInspector:
    def __init__(self, runtime: Any):
        self._runtime = runtime
        self._context: Any = None
        self._pages: Dict[int, Dict[str, Any]] = {}
        self._console: Deque[Dict[str, Any]] = deque()
        self._console_dropped = 0
        self._errors: Deque[Dict[str, Any]] = deque()
        self._errors_dropped = 0
        self._ws_events: Deque[Dict[str, Any]] = deque()
        self._ws_dropped = 0
        self._ws_counter = 0
        self._ws_connections_dropped = 0
        self._sockets: Dict[int, Dict[str, Any]] = {}
        self._sockets_by_id: Dict[str, Dict[str, Any]] = {}

    def attach_context(self, context: Any) -> None:
        if context is None:
            return
        self._detach_pages()
        self._detach_sockets()
        self._context = context

    def attach_page(self, page: Any, tab_id: str) -> None:
        if page is None:
            return
        key = id(page)
        if key in self._pages:
            return
        handlers = {
            "console": lambda message, tab_id=tab_id: self._on_console(tab_id, message),
            "pageerror": lambda error, tab_id=tab_id: self._on_page_error(tab_id, error),
            "websocket": lambda socket, tab_id=tab_id, page_key=key: self._on_websocket(tab_id, socket, page_key),
        }
        self._pages[key] = {"page": page, "handlers": handlers}
        for event, handler in handlers.items():
            try:
                page.on(event, handler)
            except Exception:
                pass

    def detach(self) -> None:
        self._detach_pages()
        self._detach_sockets()

    def detach_page(self, page: Any) -> None:
        if page is None:
            return
        key = id(page)
        record = self._pages.pop(key, None)
        if record is not None:
            for event, handler in record["handlers"].items():
                try:
                    page.remove_listener(event, handler)
                except Exception:
                    pass
        for socket_record in list(self._sockets.values()):
            if socket_record.get("page_key") == key:
                self._detach_socket(socket_record)

    def reset(self) -> None:
        self.detach()
        self._console.clear()
        self._errors.clear()
        self._ws_events.clear()
        self._console_dropped = 0
        self._errors_dropped = 0
        self._ws_dropped = 0
        self._ws_counter = 0
        self._ws_connections_dropped = 0
        self._context = None

    def _detach_pages(self) -> None:
        for record in self._pages.values():
            page = record["page"]
            for event, handler in record["handlers"].items():
                try:
                    page.remove_listener(event, handler)
                except Exception:
                    pass
        self._pages.clear()

    def _detach_sockets(self) -> None:
        for record in list(self._sockets_by_id.values()):
            self._detach_socket(record)
        self._sockets.clear()
        self._sockets_by_id.clear()

    def _detach_socket(self, record: Dict[str, Any]) -> None:
        socket = record.get("socket")
        handlers = record.get("handlers") or {}
        if socket is not None:
            for event, handler in handlers.items():
                try:
                    socket.remove_listener(event, handler)
                except Exception:
                    pass
        record["handlers"] = {}
        record["socket"] = None
        self._sockets.pop(record.get("key"), None)
        self._sockets_by_id.pop(record.get("id"), None)

    def _on_console(self, tab_id: str, message: Any) -> None:
        try:
            self._record_console(tab_id, message)
        except Exception:
            pass

    def _record_console(self, tab_id: str, message: Any) -> None:
        raw_text = _safe_attr(message, "text", "")
        text = raw_text if isinstance(raw_text, str) else str(raw_text)
        text, text_truncated = _bound_text(text)
        raw_type = _safe_attr(message, "type", "")
        entry = {
            "type": raw_type if isinstance(raw_type, str) else str(raw_type),
            "text": text,
            "location": self._console_location(message),
            "tabId": tab_id,
            "timestamp": _now_ms(),
        }
        if text_truncated:
            entry["textTruncated"] = True
        if len(self._console) >= MAX_CONSOLE_ENTRIES:
            self._console.popleft()
            self._console_dropped += 1
        self._console.append(entry)

    def _console_location(self, message: Any) -> Dict[str, Any]:
        raw = _safe_attr(message, "location")
        if isinstance(raw, dict):
            url = raw.get("url")
            line = raw.get("lineNumber")
            column = raw.get("columnNumber")
        else:
            url = _safe_attr(raw, "url", "")
            line = _safe_attr(raw, "lineNumber", None)
            column = _safe_attr(raw, "columnNumber", None)
        url_text = url if isinstance(url, str) else ("" if url is None else str(url))
        url_text, truncated = _bound_text(url_text)
        location: Dict[str, Any] = {
            "url": url_text,
            "lineNumber": _as_int(line),
            "columnNumber": _as_int(column),
        }
        if truncated:
            location["urlTruncated"] = True
        return location

    def _on_page_error(self, tab_id: str, error: Any) -> None:
        try:
            self._record_error(tab_id, error)
        except Exception:
            pass

    def _record_error(self, tab_id: str, error: Any) -> None:
        raw_message = _safe_attr(error, "message")
        if not isinstance(raw_message, str) or raw_message == "":
            try:
                raw_message = str(error)
            except Exception:
                raw_message = ""
        raw_stack = _safe_attr(error, "stack")
        if not isinstance(raw_stack, str):
            raw_stack = ""
        message, message_truncated = _bound_text(raw_message)
        stack, stack_truncated = _bound_text(raw_stack)
        entry: Dict[str, Any] = {
            "message": message,
            "stack": stack,
            "tabId": tab_id,
            "timestamp": _now_ms(),
        }
        if message_truncated:
            entry["messageTruncated"] = True
        if stack_truncated:
            entry["stackTruncated"] = True
        if len(self._errors) >= MAX_ERROR_ENTRIES:
            self._errors.popleft()
            self._errors_dropped += 1
        self._errors.append(entry)

    def _on_websocket(self, tab_id: str, socket: Any, page_key: Optional[int] = None) -> None:
        try:
            self._attach_socket(tab_id, socket, page_key)
        except Exception:
            pass

    def _attach_socket(self, tab_id: str, socket: Any, page_key: Optional[int] = None) -> None:
        if socket is None:
            return
        key = id(socket)
        if key in self._sockets:
            return
        if len(self._sockets) >= MAX_WEBSOCKET_EVENTS:
            self._detach_socket(next(iter(self._sockets.values())))
            self._ws_connections_dropped += 1
        self._ws_counter += 1
        ws_id = f"w{self._ws_counter}"
        raw_url = _safe_attr(socket, "url", "")
        url = raw_url if isinstance(raw_url, str) else ("" if raw_url is None else str(raw_url))
        url, url_truncated = _bound_text(url)
        record: Dict[str, Any] = {
            "key": key,
            "id": ws_id,
            "tab_id": tab_id,
            "page_key": page_key,
            "url": url,
            "url_truncated": url_truncated,
            "events": 0,
            "closed": False,
            "socket": socket,
            "handlers": {},
        }
        handlers = {
            "framesent": lambda payload, ws_id=ws_id: self._on_ws_frame("sent", ws_id, payload),
            "framereceived": lambda payload, ws_id=ws_id: self._on_ws_frame("received", ws_id, payload),
            "socketerror": lambda *args, ws_id=ws_id: self._on_ws_error(ws_id, args[0] if args else ""),
            "close": lambda *args, ws_id=ws_id: self._on_ws_close(ws_id),
        }
        record["handlers"] = handlers
        self._sockets[key] = record
        self._sockets_by_id[ws_id] = record
        for event, handler in handlers.items():
            try:
                socket.on(event, handler)
            except Exception:
                pass
        entry = {
            "event": "open",
            "webSocketId": ws_id,
            "tabId": tab_id,
            "url": url,
            "timestamp": _now_ms(),
        }
        if url_truncated:
            entry["urlTruncated"] = True
        self._append_ws(entry)

    def _ws_entry(self, event: str, record: Dict[str, Any]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "event": event,
            "webSocketId": record["id"],
            "tabId": record["tab_id"],
            "url": record["url"],
            "timestamp": _now_ms(),
        }
        if record["url_truncated"]:
            entry["urlTruncated"] = True
        return entry

    def _on_ws_frame(self, event: str, ws_id: str, payload: Any) -> None:
        try:
            record = self._sockets_by_id.get(ws_id)
            if record is None or record["closed"]:
                return
            entry = self._ws_entry(event, record)
            if isinstance(payload, (bytes, bytearray)):
                data = bytes(payload)
                truncated = len(data) > MAX_PAYLOAD_BYTES
                if truncated:
                    data = data[:MAX_PAYLOAD_BYTES]
                entry["payload"] = base64.b64encode(data).decode("ascii")
                entry["base64Encoded"] = True
                if truncated:
                    entry["truncated"] = True
            else:
                text = payload if isinstance(payload, str) else str(payload)
                text, truncated = _bound_text(text)
                entry["payload"] = text
                if truncated:
                    entry["truncated"] = True
            self._append_ws(entry)
        except Exception:
            pass

    def _on_ws_error(self, ws_id: str, error: Any) -> None:
        try:
            record = self._sockets_by_id.get(ws_id)
            if record is None or record["closed"]:
                return
            text = error if isinstance(error, str) else ("" if error is None else str(error))
            text, truncated = _bound_text(text)
            entry = self._ws_entry("error", record)
            entry["payload"] = text
            if truncated:
                entry["truncated"] = True
            self._append_ws(entry)
        except Exception:
            pass

    def _on_ws_close(self, ws_id: str) -> None:
        try:
            record = self._sockets_by_id.get(ws_id)
            if record is None or record["closed"]:
                return
            record["closed"] = True
            entry = self._ws_entry("close", record)
            self._append_ws(entry)
            self._detach_socket(record)
        except Exception:
            pass

    def _append_ws(self, entry: Dict[str, Any]) -> None:
        self._ws_events.append(entry)
        record = self._sockets_by_id.get(entry["webSocketId"])
        if record is not None:
            record["events"] += 1
        while len(self._ws_events) > MAX_WEBSOCKET_EVENTS:
            evicted = self._ws_events.popleft()
            self._ws_dropped += 1
            self._release_ws_event(evicted)

    def _release_ws_event(self, evicted: Dict[str, Any]) -> None:
        record = self._sockets_by_id.get(evicted.get("webSocketId"))
        if record is None:
            return
        if record["events"] > 0:
            record["events"] -= 1

    def console(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        clear = payload.get("clear")
        if clear is not None and not isinstance(clear, bool):
            raise BackendError(CODE_INVALID_REQUEST, "'clear' must be a boolean")
        if clear:
            self._console.clear()
            self._console_dropped = 0
            return {"cleared": True}
        entries, truncated, omitted = _bound_list(list(self._console))
        return {
            "messages": entries, "truncated": truncated, "omitted": omitted,
            "dropped": self._console_dropped,
            "limit": MAX_CONSOLE_ENTRIES,
        }

    def errors(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        clear = payload.get("clear")
        if clear is not None and not isinstance(clear, bool):
            raise BackendError(CODE_INVALID_REQUEST, "'clear' must be a boolean")
        if clear:
            self._errors.clear()
            self._errors_dropped = 0
            return {"cleared": True}
        entries, truncated, omitted = _bound_list(list(self._errors))
        return {
            "errors": entries, "truncated": truncated, "omitted": omitted,
            "dropped": self._errors_dropped,
            "limit": MAX_ERROR_ENTRIES,
        }

    def websockets(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        clear = payload.get("clear")
        if clear is not None and not isinstance(clear, bool):
            raise BackendError(CODE_INVALID_REQUEST, "'clear' must be a boolean")
        if clear:
            self._ws_events.clear()
            self._ws_dropped = 0
            for record in self._sockets_by_id.values():
                record["events"] = 0
            return {"cleared": True, "connectionsDropped": self._ws_connections_dropped}
        filter_value = payload.get("filter")
        if filter_value is None:
            events = list(self._ws_events)
        else:
            if not isinstance(filter_value, str) or filter_value == "":
                raise BackendError(CODE_INVALID_REQUEST, "'filter' must be a non-empty string")
            if len(filter_value) > MAX_FILTER_LENGTH:
                raise BackendError(CODE_INVALID_REQUEST, f"'filter' exceeds {MAX_FILTER_LENGTH} characters")
            events = [event for event in self._ws_events if filter_value in str(event.get("url", ""))]
        entries, truncated, omitted = _bound_list(events)
        return {
            "websockets": entries, "truncated": truncated, "omitted": omitted,
            "connectionsDropped": self._ws_connections_dropped,
            "dropped": self._ws_dropped,
            "limit": MAX_WEBSOCKET_EVENTS,
        }

    def _require_live_context(self) -> Any:
        runtime = self._runtime
        runtime.require_open_session()
        if not runtime._is_live() or runtime.context is None:
            raise BackendError(
                CODE_NOT_LAUNCHED,
                "browser is not launched; send a launch command first",
            )
        return runtime.context

    async def state(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        if action == "cookies_get":
            return await self._cookies_get(payload)
        if action == "cookies_set":
            return await self._cookies_set(payload)
        if action == "cookies_clear":
            return await self._cookies_clear()
        if action in ("storage_get", "storage_set", "storage_clear"):
            return await self._storage(action, payload)
        raise BackendError(CODE_UNSUPPORTED, f"state action '{action}' is not supported")

    async def _cookies_get(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        context = self._require_live_context()
        urls = None
        if payload.get("urls") is not None:
            urls = require_str_list(payload["urls"], "urls")
        cookies = await context.cookies(urls) if urls is not None else await context.cookies()
        normalized = [
            json_safe(dict(cookie)) if isinstance(cookie, dict) else json_safe(cookie)
            for cookie in cookies
        ]
        bounded, truncated, omitted = _bound_list(normalized)
        result: Dict[str, Any] = {"cookies": bounded}
        if truncated:
            result["truncated"] = True
            result["omitted"] = omitted
        return result

    async def _cookies_set(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        context = self._require_live_context()
        raw = payload.get("cookies")
        if not isinstance(raw, list) or not raw:
            raise BackendError(CODE_INVALID, "'cookies' must be a non-empty array of cookie objects")
        prepared: List[Dict[str, Any]] = []
        needs_default_url: List[int] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise BackendError(CODE_INVALID, f"'cookies[{index}]' must be an object")
            unknown = set(item) - COOKIE_FIELDS
            if unknown:
                raise BackendError(
                    CODE_INVALID,
                    f"'cookies[{index}]' has unsupported fields: {', '.join(sorted(unknown))}",
                )
            if "name" not in item:
                raise BackendError(CODE_INVALID, f"'cookies[{index}].name' is required")
            if "value" not in item:
                raise BackendError(CODE_INVALID, f"'cookies[{index}].value' is required")
            cookie: Dict[str, Any] = {
                "name": require_str(item["name"], f"cookies[{index}].name"),
                "value": require_str(
                    item["value"],
                    f"cookies[{index}].value",
                    max_len=MAX_TEXT_LENGTH,
                    allow_empty=True,
                ),
            }
            for field in ("url", "domain", "path"):
                if field in item:
                    cookie[field] = require_str(
                        item[field],
                        f"cookies[{index}].{field}",
                        max_len=MAX_SELECTOR_LENGTH,
                    )
            if "expires" in item:
                cookie["expires"] = _finite_number(item["expires"], f"cookies[{index}].expires")
            for field in ("httpOnly", "secure"):
                if field in item:
                    cookie[field] = require_bool(item[field], f"cookies[{index}].{field}")
            if "sameSite" in item:
                same_site = item["sameSite"]
                if not isinstance(same_site, str) or same_site not in COOKIE_SAME_SITE:
                    raise BackendError(
                        CODE_INVALID,
                        f"'cookies[{index}].sameSite' must be one of: {', '.join(COOKIE_SAME_SITE)}",
                    )
                cookie["sameSite"] = same_site
            if "url" not in cookie and "domain" not in cookie:
                needs_default_url.append(len(prepared))
            if "url" in cookie and "domain" in cookie:
                raise BackendError(CODE_INVALID, "cookie url and domain must not be combined")
            if "domain" in cookie:
                cookie.setdefault("path", "/")
            prepared.append(cookie)
        if needs_default_url:
            page, _tab = self._runtime.require_active()
            default_url = page.url
            if not isinstance(default_url, str) or not default_url.lower().startswith(("http://", "https://")):
                raise BackendError(
                    CODE_INVALID,
                    "cannot default cookie scope: the active tab is not on an http(s) URL",
                )
            for index in needs_default_url:
                prepared[index]["url"] = default_url
        for cookie in prepared:
            if "url" in cookie and "path" in cookie:
                parsed = urlsplit(cookie["url"])
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise BackendError(CODE_INVALID, "cookie URL must be http(s) when combined with a path")
                cookie.pop("url")
                cookie["domain"] = parsed.hostname
                cookie.setdefault("secure", parsed.scheme == "https")
        await context.add_cookies(prepared)
        return {"set": True, "count": len(prepared)}

    async def _cookies_clear(self) -> Dict[str, Any]:
        context = self._require_live_context()
        await context.clear_cookies()
        return {"cleared": True}

    async def _storage(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        store_type = optional_enum(payload.get("type"), "type", STORAGE_TYPES)
        if store_type is None:
            raise BackendError(CODE_INVALID, "'type' is required (local or session)")
        key: Optional[str] = None
        if payload.get("key") is not None:
            key = require_str(payload["key"], "key", max_len=MAX_TEXT_LENGTH, allow_empty=True)
        value: Optional[str] = None
        if action == "storage_set":
            if key is None:
                raise BackendError(CODE_INVALID, "'key' is required for storage_set")
            value = require_str(
                payload.get("value"),
                "value",
                max_len=MAX_INPUT_BYTES,
                allow_empty=True,
            )
        page, _tab = self._runtime.require_active()
        if action == "storage_get":
            if key is not None:
                value = await page.evaluate(_STORAGE_GET_ONE, {"type": store_type, "key": key})
                if value is not None and not isinstance(value, str):
                    value = json_safe(value)
                size = _encoded_size(value)
                if size > MAX_STATE_BYTES:
                    return {
                        "key": key,
                        "value": None,
                        "unavailable": {"reason": "oversized", "bytes": size, "limit": MAX_STATE_BYTES},
                    }
                return {"key": key, "value": value}
            data = await page.evaluate(_STORAGE_GET_ALL, {"type": store_type})
            if not isinstance(data, dict):
                raise BackendError(CODE_ERROR, "storage read returned an unexpected payload")
            normalized: Dict[str, str] = {}
            for entry_key, entry_value in data.items():
                if isinstance(entry_value, str):
                    normalized[str(entry_key)] = entry_value
                elif entry_value is None:
                    normalized[str(entry_key)] = ""
                else:
                    normalized[str(entry_key)] = str(json_safe(entry_value))
            return _bound_storage_data(normalized)
        if action == "storage_set":
            assert key is not None and value is not None
            await page.evaluate(_STORAGE_SET, {"type": store_type, "key": key, "value": value})
            return {"set": True}
        if key is not None:
            await page.evaluate(_STORAGE_REMOVE, {"type": store_type, "key": key})
        else:
            await page.evaluate(_STORAGE_CLEAR, {"type": store_type})
        return {"cleared": True}
