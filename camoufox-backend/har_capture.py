from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from input_context import BackendError, CODE_ERROR, CODE_INVALID, CODE_NOT_LAUNCHED
from network_observer import (
    MAX_BODY_BYTES,
    MAX_METADATA_BYTES,
    MAX_REQUESTS,
    NetworkObserver,
    _bound_text,
    _decode_body,
    _is_textual,
    _lookup_header,
    _normalize_headers,
    _parse_content_length,
)

CONTENT_MODES = ("all", "text", "none")
DEFAULT_CONTENT = "text"
MAX_PATH_LENGTH = 4096
ENRICH_BUDGET_SECONDS = 6.0
MAX_ENRICH_CONCURRENCY = 4
MAX_AGGREGATE_BODY_BYTES = 8 * 1024 * 1024
BODY_API_TIMEOUT_SECONDS = 4.0

_LIMITATION_LABELS = {
    "enrichment_failed": "entries failed enrichment",
    "metadata_timeout": "entries were not enriched within the internal budget",
    "metadata_unavailable": "entry metadata (headers, POST data, timing) was unavailable",
    "request_failed": "requests failed",
    "response_pending": "responses were still pending when the capture stopped",
    "response_unavailable": "responses were unavailable",
    "request_headers_unavailable": "request headers were unavailable",
    "request_headers_truncated": "request headers were truncated",
    "response_headers_unavailable": "response headers were unavailable",
    "response_headers_truncated": "response headers were truncated",
    "post_data_unavailable": "POST bodies were unavailable",
    "post_data_truncated": "POST bodies were truncated",
    "timing_unavailable": "timings were unavailable",
    "body_pending": "response bodies were still pending when the capture stopped",
    "body_request_failed": "response bodies belong to failed requests",
    "body_unavailable": "response bodies were unavailable",
    "body_not_textual": "response bodies were not embedded (not textual for the selected content mode)",
    "body_oversized": f"response bodies exceeded the {MAX_BODY_BYTES} byte per-body limit",
    "body_budget_exhausted": "response bodies were skipped after the aggregate limit",
    "body_timeout": "response bodies were not read within the internal budget",
    "body_truncated": "embedded response bodies were truncated",
    "url_truncated": "URLs were truncated",
    "failure_truncated": "failure text was truncated",
}

_UNAVAILABLE_REASONS = ("metadata_unavailable", "metadata_timeout", "enrichment_failed")

_RAW_TIMING_PHASES = ("blocked", "dns", "connect", "ssl", "send", "wait", "receive")
_REQUIRED_TIMING_PHASES = ("send", "wait", "receive")
_TIMED_PHASES = ("blocked", "dns", "connect", "send", "wait", "receive")


class _Budget:
    def __init__(self, seconds: float):
        self._deadline = time.monotonic() + max(0.0, seconds)

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())


class _StoppedCapture:
    def __init__(
        self,
        observer: NetworkObserver,
        records: List[Any],
        dropped: int,
        content: str,
        started_at_ms: int,
        stopped_at_ms: int,
    ):
        self.observer = observer
        self.records = records
        self.dropped = dropped
        self.content = content
        self.started_at_ms = started_at_ms
        self.stopped_at_ms = stopped_at_ms
        self.har_bytes: Optional[bytes] = None
        self.summary: Optional[Dict[str, Any]] = None


def _iso_from_ms(value: int) -> str:
    seconds, millis = divmod(int(value), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{millis:03d}Z"


def _query_string(url: str) -> List[Dict[str, str]]:
    try:
        query = urlsplit(url).query
    except ValueError:
        return []
    if not query:
        return []
    try:
        return [{"name": name, "value": value} for name, value in parse_qsl(query, keep_blank_values=True)]
    except ValueError:
        return []


def _response_header_map(response: Any) -> Optional[Dict[str, Any]]:
    try:
        headers = response.headers
    except Exception:
        return None
    return headers if isinstance(headers, dict) else None


async def _response_header_entries(response: Any) -> Optional[List[Dict[str, str]]]:
    try:
        headers = response.headers
    except Exception:
        return None
    if isinstance(headers, dict):
        normalized = _normalize_headers(headers)
        return normalized[0] if normalized is not None else None
    array_method = getattr(response, "headers_array", None)
    if callable(array_method):
        try:
            raw = await asyncio.wait_for(array_method(), timeout=2.0)
        except Exception:
            return None
        normalized = _normalize_headers(raw)
        return normalized[0] if normalized is not None else None
    return None


def _timing_value(timing: Dict[str, Any], key: str) -> float:
    value = timing.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return -1.0
    number = float(value)
    if not math.isfinite(number):
        return -1.0
    return number


def _timing_delta(start: float, end: float) -> float:
    if start < 0.0 or end < 0.0 or end < start:
        return -1.0
    return round(end - start, 3)


def _raw_timings_from(timing: Dict[str, Any]) -> Dict[str, float]:
    dns_start = _timing_value(timing, "domainLookupStart")
    dns_end = _timing_value(timing, "domainLookupEnd")
    connect_start = _timing_value(timing, "connectStart")
    connect_end = _timing_value(timing, "connectEnd")
    secure_start = _timing_value(timing, "secureConnectionStart")
    request_start = _timing_value(timing, "requestStart")
    response_start = _timing_value(timing, "responseStart")
    response_end = _timing_value(timing, "responseEnd")

    blocked = -1.0
    for candidate in (dns_start, connect_start, request_start):
        if candidate >= 0.0:
            blocked = round(candidate, 3)
            break

    return {
        "blocked": blocked,
        "dns": _timing_delta(dns_start, dns_end),
        "connect": _timing_delta(connect_start, connect_end),
        "ssl": _timing_delta(secure_start, connect_end),
        "send": -1.0,
        "wait": _timing_delta(request_start, response_start),
        "receive": _timing_delta(response_start, response_end),
    }


def _finalize_timings(raw: Dict[str, float]) -> Tuple[Dict[str, float], List[str]]:
    timings = dict(raw)
    unavailable: List[str] = []
    for phase in _REQUIRED_TIMING_PHASES:
        if timings.get(phase, -1.0) < 0.0:
            timings[phase] = 0.0
            unavailable.append(phase)
    return timings, unavailable


def _entry_time(timings: Dict[str, float]) -> float:
    total = 0.0
    for phase in _TIMED_PHASES:
        value = timings.get(phase, -1.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value >= 0.0:
            total += float(value)
    return round(total, 3)


def _entry_status(record: Any, reasons: List[str]) -> str:
    if record.failure is not None:
        return "failed"
    if not record.finished:
        return "pending"
    if any(reason in _UNAVAILABLE_REASONS for reason in reasons):
        return "unavailable"
    if reasons:
        return "partial"
    return "captured"


def _limitation_messages(counts: Dict[str, int], dropped: int, entry_count: int) -> List[str]:
    messages: List[str] = []
    if entry_count == 0:
        messages.append("no requests were captured between start and stop")
    if dropped:
        messages.append(f"{dropped} requests were dropped after the {MAX_REQUESTS}-entry observer limit")
    for code in sorted(counts, key=lambda item: (-counts[item], item)):
        label = _LIMITATION_LABELS.get(code)
        if label is not None:
            messages.append(f"{counts[code]} x {label}")
    messages.append("cookies are not collected; HAR cookie arrays are empty by design")
    messages.append(
        "measured request.timing cannot separate send from wait, so required send, wait and receive phases "
        "without measurements export 0 placeholders listed per entry in _agentBrowser.unavailableTimings with "
        "original values in _agentBrowser.rawTimings; 0 is not a measured zero"
    )
    return messages


async def _gather_tasks(coros: List[Any]) -> List[Any]:
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        return await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _write_exclusive(destination: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise BackendError(CODE_INVALID, "HAR destination already exists; choose a new path") from exc
    except OSError as exc:
        raise BackendError(CODE_ERROR, f"could not create HAR destination: {type(exc).__name__}") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise BackendError(CODE_ERROR, "could not write the HAR file completely")
            view = view[written:]
    except BackendError:
        raise
    except OSError as exc:
        raise BackendError(CODE_ERROR, f"could not write the HAR file: {type(exc).__name__}") from exc
    finally:
        os.close(fd)


class HarCapture:
    """Opt-in, bounded diagnostic HAR export without restarting the browser.

    Unmeasured required timings use marked zero placeholders, not fabricated
    measurements. Body caps bound saved output, not Playwright's body fetch.
    A failed write retains the stopped capture for export to a new path.
    """

    def __init__(self, runtime: Any):
        self._runtime = runtime
        self._observer: Optional[NetworkObserver] = None
        self._pending: Optional[_StoppedCapture] = None
        self._content: str = DEFAULT_CONTENT
        self._started_at_ms: Optional[int] = None

    @property
    def active(self) -> bool:
        return self._observer is not None

    @property
    def pending(self) -> bool:
        return self._pending is not None

    async def start(self, content: Optional[str] = None) -> Dict[str, Any]:
        mode = DEFAULT_CONTENT if content is None else content
        if not isinstance(mode, str) or mode not in CONTENT_MODES:
            raise BackendError(CODE_INVALID, "'content' must be one of: all, text, none")
        if self._observer is not None:
            raise BackendError(CODE_INVALID, "a HAR capture is already active; stop it before starting another")
        if self._pending is not None:
            raise BackendError(
                CODE_INVALID,
                "a stopped HAR capture is awaiting export; call stop with a new path, or reset to discard it",
            )
        runtime = self._runtime
        runtime.require_open_session()
        context = runtime.context
        if context is None:
            raise BackendError(CODE_NOT_LAUNCHED, "browser context is not launched")
        observer = NetworkObserver(runtime)
        try:
            observer.attach(context)
        except Exception as exc:
            raise BackendError(CODE_ERROR, f"could not attach the HAR capture: {type(exc).__name__}") from exc
        self._observer = observer
        self._content = mode
        self._started_at_ms = int(time.time() * 1000)
        return {
            "started": True,
            "content": mode,
            "startedAt": _iso_from_ms(self._started_at_ms),
            "scope": "context-wide",
        }

    async def stop(self, path: Optional[str] = None) -> Dict[str, Any]:
        pending = self._pending
        if pending is None:
            observer = self._observer
            if observer is None:
                raise BackendError(CODE_INVALID, "no HAR capture is active or awaiting export")
            observer._detach()
            self._observer = None
            pending = _StoppedCapture(
                observer=observer,
                records=list(observer._records.values()),
                dropped=observer._dropped,
                content=self._content,
                started_at_ms=self._started_at_ms if self._started_at_ms is not None else int(time.time() * 1000),
                stopped_at_ms=int(time.time() * 1000),
            )
            self._pending = pending

        destination = self._destination(path)
        if pending.har_bytes is None:
            try:
                document, summary = await self._build(pending)
                pending.har_bytes = json.dumps(document, ensure_ascii=True).encode("utf-8")
                pending.summary = summary
            except Exception as exc:
                raise BackendError(CODE_ERROR, f"HAR export failed during serialization: {type(exc).__name__}") from exc
        _write_exclusive(destination, pending.har_bytes)
        response = dict(pending.summary or {})
        response["path"] = str(destination)
        self._pending = None
        self._content = DEFAULT_CONTENT
        self._started_at_ms = None
        return response

    def reset(self) -> None:
        observer = self._observer
        self._observer = None
        self._pending = None
        self._content = DEFAULT_CONTENT
        self._started_at_ms = None
        if observer is not None:
            observer.reset()

    def _destination(self, path: Optional[str]) -> Path:
        if path is None:
            return self._default_destination()
        if not isinstance(path, str) or path == "" or len(path) > MAX_PATH_LENGTH:
            raise BackendError(CODE_INVALID, f"'path' must be a non-empty string of at most {MAX_PATH_LENGTH} characters")
        destination = Path(path).expanduser()
        if destination.exists():
            raise BackendError(CODE_INVALID, "HAR destination already exists; choose a new path")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError(CODE_ERROR, f"could not create the HAR destination directory: {type(exc).__name__}") from exc
        return destination

    def _default_destination(self) -> Path:
        directory = Path(self._runtime.runtime_dir) / "tmp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError(CODE_ERROR, f"could not create the capture directory: {type(exc).__name__}") from exc
        return directory / f"capture-{uuid.uuid4().hex}.har"

    async def _build(self, pending: _StoppedCapture) -> Any:
        budget = _Budget(ENRICH_BUDGET_SECONDS)
        semaphore = asyncio.Semaphore(MAX_ENRICH_CONCURRENCY)
        state: Dict[str, int] = {"body_bytes": 0}
        url_by_id = {record.request_id: record.url for record in pending.records}
        results = await _gather_tasks([
            self._enrich(pending, record, url_by_id, budget, semaphore, state)
            for record in pending.records
        ])
        entries: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {}
        statuses: Dict[str, int] = {}
        for record, result in zip(pending.records, results):
            if isinstance(result, dict):
                entry = result
            else:
                entry = self._skeleton(record, url_by_id)
                entry["_agentBrowser"]["reasons"] = sorted(
                    set(entry["_agentBrowser"]["reasons"]) | {"enrichment_failed"}
                )
                entry["_agentBrowser"]["status"] = "unavailable"
            meta = entry["_agentBrowser"]
            statuses[meta["status"]] = statuses.get(meta["status"], 0) + 1
            for reason in meta["reasons"]:
                counts[reason] = counts.get(reason, 0) + 1
            entries.append(entry)
        limitations = _limitation_messages(counts, pending.dropped, len(entries))
        extension = {
            "capture": {
                "content": pending.content,
                "startedAt": _iso_from_ms(pending.started_at_ms),
                "stoppedAt": _iso_from_ms(pending.stopped_at_ms),
                "scope": "context-wide",
                "requestCount": len(entries),
                "dropped": pending.dropped,
                "observerLimit": MAX_REQUESTS,
                "cookies": "not_collected",
                "timings": "derived from request.timing; time is the sum of the nonnegative blocked, dns, connect, "
                "send, wait and receive phases (ssl is excluded); -1 marks unavailable optional phases; required "
                "send, wait and receive phases without measurements use 0 placeholders listed per entry in "
                "_agentBrowser.unavailableTimings with original values in _agentBrowser.rawTimings",
                "entriesByStatus": statuses,
                "limitations": counts,
                "notes": limitations,
            }
        }
        document = {
            "log": {
                "version": "1.2",
                "creator": {"name": "agent-browser", "version": "camoufox-v1"},
                "entries": entries,
                "_agentBrowser": extension,
            }
        }
        summary = {
            "requestCount": len(entries),
            "dropped": pending.dropped,
            "content": pending.content,
            "limitations": limitations,
        }
        return document, summary

    async def _enrich(
        self,
        pending: _StoppedCapture,
        record: Any,
        url_by_id: Dict[str, str],
        budget: _Budget,
        semaphore: asyncio.Semaphore,
        state: Dict[str, int],
    ) -> Dict[str, Any]:
        try:
            async with semaphore:
                return await self._enrich_inner(pending, record, url_by_id, budget, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            entry = self._skeleton(record, url_by_id)
            entry["_agentBrowser"]["reasons"] = sorted(
                set(entry["_agentBrowser"]["reasons"]) | {"enrichment_failed"}
            )
            entry["_agentBrowser"]["status"] = "unavailable"
            return entry

    async def _enrich_inner(
        self,
        pending: _StoppedCapture,
        record: Any,
        url_by_id: Dict[str, str],
        budget: _Budget,
        state: Dict[str, int],
    ) -> Dict[str, Any]:
        entry = self._skeleton(record, url_by_id)
        reasons = list(entry["_agentBrowser"]["reasons"])
        if record.failure is not None:
            reasons.append("request_failed")
        if record.response is None:
            reasons.append("response_pending" if not record.finished else "response_unavailable")

        detail: Optional[Dict[str, Any]] = None
        remaining = budget.remaining()
        if remaining <= 0:
            reasons.append("metadata_timeout")
        else:
            try:
                detail = await asyncio.wait_for(
                    pending.observer.request_detail(record.request_id, include_body=False),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                reasons.append("metadata_timeout")
            except Exception:
                reasons.append("metadata_unavailable")

        if detail is not None:
            self._apply_detail(entry, detail, reasons)
        response_headers = entry["response"]["headers"]
        if record.response is not None and not isinstance(response_headers, list):
            bounded = await _response_header_entries(record.response)
            if bounded is not None:
                entry["response"]["headers"] = bounded
                reasons = [reason for reason in reasons if reason != "response_headers_unavailable"]
        header_map = _response_header_map(record.response) if record.response is not None else None
        content_type = _lookup_header(entry["response"]["headers"], "content-type")
        if content_type is None:
            content_type = _lookup_header(header_map, "content-type")
        if content_type is not None:
            mime, truncated = _bound_text(content_type)
            entry["response"]["content"]["mimeType"] = mime
            if truncated:
                reasons.append("response_headers_truncated")
        location = _lookup_header(entry["response"]["headers"], "location")
        if location is None:
            location = _lookup_header(header_map, "location")
        if location:
            redirect, truncated = _bound_text(location)
            entry["response"]["redirectURL"] = redirect
            if truncated:
                reasons.append("response_headers_truncated")
        if pending.content != "none":
            await self._apply_body(record, entry, pending.content, budget, state, reasons)

        meta = entry["_agentBrowser"]
        merged = sorted(set(meta["reasons"]) | set(reasons))
        meta["reasons"] = merged
        meta["status"] = _entry_status(record, merged)
        return entry

    def _apply_detail(self, entry: Dict[str, Any], detail: Dict[str, Any], reasons: List[str]) -> None:
        request_headers = detail.get("requestHeaders")
        if isinstance(request_headers, list):
            entry["request"]["headers"] = request_headers
        if detail.get("requestHeadersError") is not None or not isinstance(request_headers, list):
            reasons.append("request_headers_unavailable")
        if detail.get("requestHeadersTruncated"):
            reasons.append("request_headers_truncated")

        response_headers = detail.get("responseHeaders")
        if isinstance(response_headers, list):
            entry["response"]["headers"] = response_headers
        if detail.get("responseHeadersError") is not None or not isinstance(response_headers, list):
            reasons.append("response_headers_unavailable")
        if detail.get("responseHeadersTruncated"):
            reasons.append("response_headers_truncated")

        post_data = detail.get("postData")
        if isinstance(post_data, str) and post_data != "":
            mime = _lookup_header(entry["request"]["headers"], "content-type") or ""
            entry["request"]["postData"] = {"mimeType": mime, "text": post_data}
            if detail.get("postDataTruncated"):
                reasons.append("post_data_truncated")
        elif detail.get("postDataError") is not None:
            reasons.append("post_data_unavailable")

        timing = detail.get("timing")
        if isinstance(timing, dict) and timing:
            raw = _raw_timings_from(timing)
            finalized, unavailable = _finalize_timings(raw)
            entry["timings"] = finalized
            entry["time"] = _entry_time(finalized)
            meta = entry["_agentBrowser"]
            meta["rawTimings"] = {phase: raw[phase] for phase in _RAW_TIMING_PHASES}
            meta["unavailableTimings"] = unavailable
        else:
            reasons.append("timing_unavailable")
        if detail.get("timingError") is not None:
            reasons.append("timing_unavailable")

    async def _apply_body(
        self,
        record: Any,
        entry: Dict[str, Any],
        content_mode: str,
        budget: _Budget,
        state: Dict[str, int],
        reasons: List[str],
    ) -> None:
        if record.failure is not None:
            reasons.append("body_request_failed")
            return
        if not record.finished:
            reasons.append("body_pending")
            return
        response = record.response
        if response is None:
            reasons.append("body_unavailable")
            return

        headers = entry["response"]["headers"] if isinstance(entry["response"]["headers"], list) else []
        header_map = _response_header_map(response)
        content_type = _lookup_header(headers, "content-type")
        if content_type is None:
            content_type = _lookup_header(header_map, "content-type")
        if content_mode == "text" and not _is_textual(content_type):
            reasons.append("body_not_textual")
            return

        known_size = _parse_content_length(_lookup_header(headers, "content-length"))
        if known_size is None:
            known_size = _parse_content_length(_lookup_header(header_map, "content-length"))
        if known_size is not None and known_size > MAX_BODY_BYTES:
            reasons.append("body_oversized")
            return

        allowed = MAX_AGGREGATE_BODY_BYTES - state["body_bytes"]
        if allowed <= 0:
            reasons.append("body_budget_exhausted")
            return
        remaining = budget.remaining()
        if remaining <= 0:
            reasons.append("body_timeout")
            return

        limit = min(MAX_BODY_BYTES, allowed)
        state["body_bytes"] += limit
        try:
            raw = await asyncio.wait_for(response.body(), timeout=min(remaining, BODY_API_TIMEOUT_SECONDS))
        except asyncio.TimeoutError:
            state["body_bytes"] -= limit
            reasons.append("body_timeout")
            return
        except Exception:
            state["body_bytes"] -= limit
            reasons.append("body_unavailable")
            return
        state["body_bytes"] -= limit
        if not isinstance(raw, (bytes, bytearray)):
            reasons.append("body_unavailable")
            return

        data = bytes(raw)
        slice_bytes = data[:limit]
        state["body_bytes"] += len(slice_bytes)
        truncated = len(data) > limit
        text, is_base64 = _decode_body(slice_bytes, content_type)
        content = entry["response"]["content"]
        content["mimeType"] = _bound_text(content_type or "")[0]
        content["size"] = len(data)
        content["text"] = text
        if is_base64:
            content["encoding"] = "base64"
        if truncated:
            reasons.append("body_truncated")

    def _skeleton(self, record: Any, url_by_id: Dict[str, str]) -> Dict[str, Any]:
        redirect_url = ""
        if record.redirected_to is not None:
            redirect_url = url_by_id.get(record.redirected_to) or ""
        entry: Dict[str, Any] = {
            "startedDateTime": _iso_from_ms(record.timestamp),
            "time": 0.0,
            "request": {
                "method": record.method,
                "url": record.url,
                "httpVersion": "",
                "cookies": [],
                "headers": [],
                "queryString": _query_string(record.url),
                "headersSize": -1,
                "bodySize": -1,
            },
            "response": {
                "status": record.status if record.status is not None else 0,
                "statusText": "",
                "httpVersion": "",
                "cookies": [],
                "headers": [],
                "content": {"size": -1, "mimeType": ""},
                "redirectURL": redirect_url,
                "headersSize": -1,
                "bodySize": -1,
            },
            "cache": {},
            "timings": {
                "blocked": -1.0,
                "dns": -1.0,
                "connect": -1.0,
                "ssl": -1.0,
                "send": 0.0,
                "wait": 0.0,
                "receive": 0.0,
            },
            "_resourceType": record.resource_type,
            "_agentBrowser": {
                "requestId": record.request_id,
                "tabId": record.tab_id,
                "finished": record.finished,
                "status": "captured",
                "reasons": [],
                "rawTimings": {
                    "blocked": -1.0,
                    "dns": -1.0,
                    "connect": -1.0,
                    "ssl": -1.0,
                    "send": -1.0,
                    "wait": -1.0,
                    "receive": -1.0,
                },
                "unavailableTimings": ["send", "wait", "receive"],
            },
        }
        if record.failure is not None:
            text, truncated = _bound_text(record.failure, MAX_METADATA_BYTES)
            entry["_error"] = text
            if truncated or record.failure_truncated:
                entry["_agentBrowser"]["reasons"].append("failure_truncated")
        if record.url_truncated:
            entry["_agentBrowser"]["reasons"].append("url_truncated")
        if record.redirected_from is not None:
            entry["_agentBrowser"]["redirectedFrom"] = record.redirected_from
        if record.redirected_to is not None:
            entry["_agentBrowser"]["redirectedTo"] = record.redirected_to
        return entry
