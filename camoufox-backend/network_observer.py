from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

from input_context import BackendError, CODE_INVALID_REQUEST

MAX_REQUESTS = 500
MAX_METADATA_BYTES = 8 * 1024
MAX_BODY_BYTES = 1024 * 1024
MAX_HEADER_ENTRIES = 256
MAX_HEADER_TOTAL_BYTES = 64 * 1024
DETAIL_API_TIMEOUT_SECONDS = 2.0
MAX_FILTER_LENGTH = 4096

_STATUS_EXACT_RE = re.compile(r"^[1-5]\d\d$")
_STATUS_CLASS_RE = re.compile(r"^([1-5])xx$")
_STATUS_RANGE_RE = re.compile(r"^([1-5]\d\d)-([1-5]\d\d)$")

_TEXTUAL_CONTENT_HINTS = (
    "text/",
    "application/json",
    "application/ld+json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/xhtml+xml",
    "application/x-www-form-urlencoded",
    "application/graphql",
    "image/svg+xml",
)


@dataclass
class _Record:
    request_id: str
    tab_id: Optional[str]
    url: str
    url_truncated: bool
    method: str
    resource_type: str
    timestamp: int
    request: Any = None
    response: Any = None
    request_key: int = 0
    status: Optional[int] = None
    failure: Optional[str] = None
    failure_truncated: bool = False
    finished: bool = False
    redirected_from: Optional[str] = None
    redirected_to: Optional[str] = None


def _bound_text(value: str, limit: int = MAX_METADATA_BYTES) -> Tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _exc_name(exc: BaseException) -> str:
    return type(exc).__name__


def _safe_property(target: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(target, name)
    except Exception:
        return default
    return default if value is None else value


def _normalize_headers(raw: Any) -> Optional[Tuple[List[Dict[str, str]], bool]]:
    if not isinstance(raw, (list, dict)):
        return None
    entries: List[Dict[str, str]] = []
    truncated = False
    total_bytes = 0
    items = raw if isinstance(raw, list) else list(raw.items())
    for item in items:
        if len(entries) >= MAX_HEADER_ENTRIES or total_bytes >= MAX_HEADER_TOTAL_BYTES:
            truncated = True
            break
        if isinstance(raw, dict):
            name, value = item
        elif isinstance(item, dict):
            name = item.get("name")
            value = item.get("value")
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            name, value = item[0], item[1]
        else:
            name = getattr(item, "name", None)
            value = getattr(item, "value", None)
        if name is None:
            continue
        header_name, name_truncated = _bound_text(str(name))
        text, value_truncated = _bound_text("" if value is None else str(value))
        entry = {"name": header_name, "value": text}
        entry_bytes = len(json.dumps(entry, ensure_ascii=True).encode("utf-8"))
        if total_bytes + entry_bytes > MAX_HEADER_TOTAL_BYTES:
            truncated = True
            break
        if name_truncated:
            entry["nameTruncated"] = True
        if value_truncated:
            entry["valueTruncated"] = True
        truncated = truncated or name_truncated or value_truncated
        entries.append(entry)
        total_bytes += entry_bytes
    if total_bytes > MAX_HEADER_TOTAL_BYTES:
        truncated = True
    return entries, truncated


def _lookup_header(headers: Any, name: str) -> Optional[str]:
    wanted = name.lower()
    if isinstance(headers, list):
        for entry in headers:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name", "")).lower() == wanted:
                value = entry.get("value")
                return value if isinstance(value, str) else None
        return None
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == wanted and isinstance(value, str):
                return value
    return None


def _parse_content_length(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _is_textual(content_type: Optional[str]) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    return any(hint in lowered for hint in _TEXTUAL_CONTENT_HINTS)


def _decode_body(data: bytes, content_type: Optional[str]) -> Tuple[str, bool]:
    if _is_textual(content_type) or content_type is None:
        try:
            return data.decode("utf-8"), False
        except UnicodeDecodeError:
            return base64.b64encode(data).decode("ascii"), True
    return base64.b64encode(data).decode("ascii"), True


def _filter_text(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BackendError(CODE_INVALID_REQUEST, f"'{name}' filter must be a string")
    if value == "":
        raise BackendError(CODE_INVALID_REQUEST, f"'{name}' filter must not be empty")
    if len(value) > MAX_FILTER_LENGTH:
        raise BackendError(CODE_INVALID_REQUEST, f"'{name}' filter exceeds {MAX_FILTER_LENGTH} characters")
    return value


def _status_matcher(value: Any):
    if not isinstance(value, str):
        raise BackendError(CODE_INVALID_REQUEST, "'status' filter must be a string")
    text = value.strip().lower()
    if _STATUS_EXACT_RE.match(text):
        expected = int(text)
        return lambda status: status == expected
    class_match = _STATUS_CLASS_RE.match(text)
    if class_match:
        lower = int(class_match.group(1)) * 100
        return lambda status: status is not None and lower <= status <= lower + 99
    range_match = _STATUS_RANGE_RE.match(text)
    if range_match:
        lower, upper = int(range_match.group(1)), int(range_match.group(2))
        if lower > upper:
            raise BackendError(CODE_INVALID_REQUEST, "'status' filter range start must not exceed its end")
        return lambda status: status is not None and lower <= status <= upper
    raise BackendError(
        CODE_INVALID_REQUEST,
        "'status' filter must be an exact code (200), a class (4xx), or a range (400-499)",
    )


class NetworkObserver:
    def __init__(self, runtime: Any):
        self._runtime = runtime
        self._context: Any = None
        self._records: OrderedDict[str, _Record] = OrderedDict()
        self._by_key: Dict[int, str] = {}
        self._counter = 0
        self._dropped = 0

    def attach(self, context: Any) -> None:
        if context is None:
            return
        self._detach()
        self._context = context
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfinished", self._on_request_finished)
        context.on("requestfailed", self._on_request_failed)

    def reset(self) -> None:
        self._detach()
        self.clear()
        self._counter = 0

    def clear(self) -> None:
        for record in self._records.values():
            self._drop_handles(record)
        self._records.clear()
        self._by_key.clear()
        self._dropped = 0

    def requests(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        clear = payload.get("clear")
        if clear is not None and not isinstance(clear, bool):
            raise BackendError(CODE_INVALID_REQUEST, "'clear' must be a boolean")
        if clear:
            self.clear()
            return {"cleared": True}

        url_filter = _filter_text(payload.get("filter"), "filter")
        type_filter = _filter_text(payload.get("type"), "type")
        method_filter = _filter_text(payload.get("method"), "method")
        status_matcher = None
        status_value = payload.get("status")
        if status_value is not None:
            status_matcher = _status_matcher(status_value)

        type_values: Optional[List[str]] = None
        if type_filter is not None:
            type_values = [part.strip().lower() for part in type_filter.split(",") if part.strip()]
            if not type_values:
                raise BackendError(CODE_INVALID_REQUEST, "'type' filter must contain at least one resource type")

        method_value = method_filter.upper() if method_filter is not None else None

        matches: List[Dict[str, Any]] = []
        output_bytes = 0
        omitted = 0
        for record in self._records.values():
            if url_filter is not None and url_filter not in record.url:
                continue
            if type_values is not None and record.resource_type.lower() not in type_values:
                continue
            if method_value is not None and record.method.upper() != method_value:
                continue
            if status_matcher is not None and not status_matcher(record.status):
                continue
            metadata = self._metadata(record)
            size = len(json.dumps(metadata, ensure_ascii=True).encode("utf-8"))
            if output_bytes + size > 4 * MAX_BODY_BYTES:
                omitted += 1
                continue
            matches.append(metadata)
            output_bytes += size
        return {"requests": matches, "dropped": self._dropped, "limit": MAX_REQUESTS, "omitted": omitted}

    async def request_detail(self, request_id: str, include_body: bool = True) -> Dict[str, Any]:
        if not isinstance(request_id, str) or request_id == "":
            raise BackendError(CODE_INVALID_REQUEST, "'requestId' must be a non-empty string")
        record = self._records.get(request_id)
        if record is None:
            raise BackendError(CODE_INVALID_REQUEST, f"unknown or expired requestId '{request_id}'")

        record = replace(record)
        detail = self._metadata(record)
        request = record.request
        response = record.response

        if response is None:
            detail["responseHeadersUnavailable"] = {"reason": self._response_absence_reason(record)}

        request_headers, request_headers_error, request_headers_truncated = await self._headers(request)
        detail["requestHeaders"] = request_headers
        if request_headers_error is not None:
            detail["requestHeadersError"] = request_headers_error
        if request_headers_truncated:
            detail["requestHeadersTruncated"] = True

        post_data, post_data_truncated, post_data_error = self._post_data(request)
        detail["postData"] = post_data
        if post_data_truncated:
            detail["postDataTruncated"] = True
        if post_data_error is not None:
            detail["postDataError"] = post_data_error

        response_headers, response_headers_error, response_headers_truncated = await self._headers(response)
        detail["responseHeaders"] = response_headers
        if response_headers_error is not None:
            detail["responseHeadersError"] = response_headers_error
        if response_headers_truncated:
            detail["responseHeadersTruncated"] = True

        timing, timing_error = self._timing(request)
        detail["timing"] = timing
        if timing_error is not None:
            detail["timingError"] = timing_error

        detail["base64Encoded"] = False
        if include_body:
            await self._attach_body(detail, record, response_headers)
        else:
            detail["responseBodyUnavailable"] = {"reason": "not_requested"}
        return detail

    def _metadata(self, record: _Record) -> Dict[str, Any]:
        metadata = {
            "requestId": record.request_id,
            "tabId": record.tab_id,
            "url": record.url,
            "method": record.method,
            "resourceType": record.resource_type,
            "timestamp": record.timestamp,
            "status": record.status,
            "failure": record.failure,
            "finished": record.finished,
            "redirectedFrom": record.redirected_from,
            "redirectedTo": record.redirected_to,
        }
        if record.url_truncated:
            metadata["urlTruncated"] = True
        if record.failure_truncated:
            metadata["failureTruncated"] = True
        return metadata

    def _response_absence_reason(self, record: _Record) -> str:
        if record.failure is not None:
            return "failed"
        if not record.finished:
            return "pending"
        return "unavailable"

    async def _headers(self, target: Any) -> Tuple[Optional[List[Dict[str, str]]], Optional[str], bool]:
        if target is None:
            return None, None, False
        array_error: Optional[str] = None
        array_method = getattr(target, "headers_array", None)
        if callable(array_method):
            try:
                raw = await asyncio.wait_for(array_method(), timeout=DETAIL_API_TIMEOUT_SECONDS)
            except Exception as exc:
                array_error = _exc_name(exc)
            else:
                normalized = _normalize_headers(raw)
                if normalized is not None:
                    entries, truncated = normalized
                    return entries, None, truncated
        all_method = getattr(target, "all_headers", None)
        if callable(all_method):
            try:
                raw = await asyncio.wait_for(all_method(), timeout=DETAIL_API_TIMEOUT_SECONDS)
            except Exception as exc:
                return None, _exc_name(exc), False
            normalized = _normalize_headers(raw)
            if normalized is not None:
                entries, truncated = normalized
                return entries, None, truncated
        return None, array_error, False

    def _post_data(self, request: Any) -> Tuple[Optional[str], bool, Optional[str]]:
        if request is None:
            return None, False, None
        try:
            raw = request.post_data
        except Exception as exc:
            return None, False, _exc_name(exc)
        if not isinstance(raw, str):
            return None, False, None
        text, truncated = _bound_text(raw)
        return text, truncated, None

    def _timing(self, request: Any) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
        if request is None:
            return None, None
        try:
            timing = request.timing
        except Exception as exc:
            return None, _exc_name(exc)
        if not isinstance(timing, dict):
            return None, "unexpected timing payload"
        bounded: Dict[str, float] = {}
        for key, value in timing.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            bounded[str(key)] = float(value)
        return bounded, None

    async def _attach_body(
        self,
        detail: Dict[str, Any],
        record: _Record,
        response_headers: Optional[List[Dict[str, str]]],
    ) -> None:
        response = record.response
        if record.failure is not None:
            detail["responseBodyUnavailable"] = {"reason": "failed", "failure": record.failure}
            return
        if not record.finished:
            detail["responseBodyUnavailable"] = {"reason": "pending"}
            return
        if response is None:
            detail["responseBodyUnavailable"] = {"reason": "unavailable"}
            return

        header_map = _safe_property(response, "headers", None)
        content_type = _lookup_header(response_headers, "content-type")
        if content_type is None:
            content_type = _lookup_header(header_map, "content-type")
        raw_length = _lookup_header(response_headers, "content-length")
        if raw_length is None:
            raw_length = _lookup_header(header_map, "content-length")
        known_size = _parse_content_length(raw_length)
        if known_size is not None and known_size > MAX_BODY_BYTES:
            detail["responseBodyUnavailable"] = {
                "reason": "oversized",
                "contentLength": known_size,
                "limit": MAX_BODY_BYTES,
            }
            return

        try:
            raw_body = await asyncio.wait_for(response.body(), timeout=DETAIL_API_TIMEOUT_SECONDS)
        except Exception as exc:
            reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else "error"
            detail["responseBodyUnavailable"] = {"reason": reason, "error": _exc_name(exc)}
            return
        if not isinstance(raw_body, (bytes, bytearray)):
            detail["responseBodyUnavailable"] = {"reason": "unexpected_payload"}
            return

        data = bytes(raw_body)
        truncated = len(data) > MAX_BODY_BYTES
        if truncated:
            data = data[:MAX_BODY_BYTES]
        text, is_base64 = _decode_body(data, content_type)
        detail["responseBody"] = text
        detail["base64Encoded"] = is_base64
        if truncated:
            detail["responseBodyTruncated"] = True
            detail["responseBodyBytes"] = len(bytes(raw_body))

    def _detach(self) -> None:
        context = self._context
        self._context = None
        if context is None:
            return
        for event, handler in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfinished", self._on_request_finished),
            ("requestfailed", self._on_request_failed),
        ):
            try:
                context.remove_listener(event, handler)
            except Exception:
                pass

    def _drop_handles(self, record: _Record) -> None:
        record.request = None
        record.response = None

    def _evict_oldest(self) -> None:
        _request_id, record = self._records.popitem(last=False)
        self._by_key.pop(record.request_key, None)
        self._drop_handles(record)
        self._dropped += 1

    def _tab_id(self, request: Any) -> Optional[str]:
        try:
            frame = request.frame
            page = None if frame is None else frame.page
        except Exception:
            return None
        if page is None:
            return None
        mapping = getattr(self._runtime, "_page_to_tab", None)
        if not isinstance(mapping, dict):
            return None
        return mapping.get(id(page))

    def _record_for(self, request: Any) -> Optional[_Record]:
        request_id = self._by_key.get(id(request))
        if request_id is None:
            return None
        return self._records.get(request_id)

    def _on_request(self, request: Any) -> None:
        try:
            self._handle_request(request)
        except Exception:
            pass

    def _on_response(self, response: Any) -> None:
        try:
            self._handle_response(response)
        except Exception:
            pass

    def _on_request_finished(self, request: Any) -> None:
        try:
            self._handle_request_finished(request)
        except Exception:
            pass

    def _on_request_failed(self, request: Any) -> None:
        try:
            self._handle_request_failed(request)
        except Exception:
            pass

    def _handle_request(self, request: Any) -> None:
        if self._context is None:
            return
        key = id(request)
        if key in self._by_key:
            return
        url, url_truncated = _bound_text(str(_safe_property(request, "url", "")))
        self._counter += 1
        record = _Record(
            request_id=f"n{self._counter}",
            tab_id=self._tab_id(request),
            url=url,
            url_truncated=url_truncated,
            method=_bound_text(str(_safe_property(request, "method", "")))[0],
            resource_type=_bound_text(str(_safe_property(request, "resource_type", "")))[0],
            timestamp=int(time.time() * 1000),
            request=request,
            request_key=key,
        )
        redirected_from = _safe_property(request, "redirected_from")
        if redirected_from is not None:
            previous_id = self._by_key.get(id(redirected_from))
            if previous_id is not None:
                previous = self._records.get(previous_id)
                if previous is not None:
                    record.redirected_from = previous_id
                    previous.redirected_to = record.request_id
        self._records[record.request_id] = record
        self._by_key[key] = record.request_id
        while len(self._records) > MAX_REQUESTS:
            self._evict_oldest()

    def _handle_response(self, response: Any) -> None:
        if self._context is None:
            return
        request = _safe_property(response, "request")
        if request is None:
            return
        record = self._record_for(request)
        if record is None:
            return
        record.response = response
        status = _safe_property(response, "status")
        if isinstance(status, int) and not isinstance(status, bool):
            record.status = status

    def _handle_request_finished(self, request: Any) -> None:
        if self._context is None:
            return
        record = self._record_for(request)
        if record is None:
            return
        record.finished = True

    def _handle_request_failed(self, request: Any) -> None:
        if self._context is None:
            return
        record = self._record_for(request)
        if record is None:
            return
        failure = _safe_property(request, "failure", "request failed without a diagnostic")
        text, truncated = _bound_text(str(failure))
        record.failure = text
        record.failure_truncated = truncated
        record.finished = True
