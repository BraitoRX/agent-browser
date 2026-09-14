from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from input_context import (
    BackendError,
    CODE_ERROR,
    CODE_INVALID,
    CODE_NO_ACTIVE_TAB,
    MAX_ACTION_DEADLINE_MS,
    MAX_TEXT_LENGTH,
    require_str,
)

MAX_DOWNLOADS = 32
MAX_DIALOGS = 100
MAX_METADATA_BYTES = 8 * 1024
MAX_LIST_BYTES = 1024 * 1024
ARM_TTL_SECONDS = 30.0
FROM_CLICK_TIMEOUT_MS = 10_000
DEFAULT_WAIT_TIMEOUT_MS = 10_000
MAX_PATH_LENGTH = 4096
MAX_FILENAME_LENGTH = 128
DEFAULT_FILENAME = "download.bin"
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bound_text(value: str, limit: int = MAX_METADATA_BYTES) -> Tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _safe_attr(target: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(target, name)
    except Exception:
        return default
    return default if value is None else value


def _exception_text(exc: BaseException) -> str:
    try:
        message = str(exc)
    except Exception:
        message = ""
    if not message:
        message = type(exc).__name__
    lines = message.splitlines()
    line = lines[0] if lines else message
    return f"{type(exc).__name__}: {line[:2000]}"


def _sanitize_filename(name: str) -> str:
    text = name.replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    text = _UNSAFE_FILENAME_RE.sub("_", text).strip("._")
    if not text:
        return DEFAULT_FILENAME
    if len(text) > MAX_FILENAME_LENGTH:
        text = text[:MAX_FILENAME_LENGTH].strip("._") or DEFAULT_FILENAME
    return text


def _encoded_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return MAX_LIST_BYTES + 1


def _bound_records(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool, int]:
    if _encoded_size(items) <= MAX_LIST_BYTES:
        return list(items), False, 0
    bounded: List[Dict[str, Any]] = []
    used = 16
    omitted = 0
    for index, item in enumerate(items):
        cost = _encoded_size(item) + 2
        if used + cost > MAX_LIST_BYTES:
            omitted = len(items) - index
            break
        bounded.append(item)
        used += cost
    return bounded, True, omitted


def _publish_file(temp_path: str, destination: Path) -> None:
    os.link(temp_path, destination)


def _bounded_timeout(value: Any) -> int:
    if value is None:
        return DEFAULT_WAIT_TIMEOUT_MS
    parsed = value if isinstance(value, int) and not isinstance(value, bool) else None
    if parsed is None and isinstance(value, float) and value.is_integer():
        parsed = int(value)
    if parsed is None:
        raise BackendError(CODE_INVALID, "'timeout' must be an integer")
    return max(1, min(parsed, MAX_ACTION_DEADLINE_MS))


@dataclass
class _DownloadRecord:
    download_id: str
    tab_id: str
    url: str
    suggested_filename: str
    timestamp: int
    handle: Any = None
    consumed: bool = False
    saved_path: Optional[str] = None
    failure: Optional[str] = None
    error: Optional[str] = None
    url_truncated: bool = False
    suggested_filename_truncated: bool = False
    source_tab_closed: bool = False


class InteractionEvents:
    """Resolve dialogs without blocking the serial worker; retain downloads without replaying input."""

    def __init__(self, runtime: Any):
        self._runtime = runtime
        self._pages: Dict[int, Dict[str, Any]] = {}
        self._loop: Optional[Any] = None
        self._tasks: Set[Any] = set()
        self._pending_dialogs: Set[int] = set()
        self._dialogs: Deque[Dict[str, Any]] = deque()
        self._dialog_dropped = 0
        self._armed: Optional[Dict[str, Any]] = None
        self._downloads: Dict[str, _DownloadRecord] = {}
        self._download_order: Deque[str] = deque()
        self._download_by_handle: Dict[int, str] = {}
        self._download_waiters: Dict[int, Tuple[str, Any]] = {}
        self._download_counter = 0
        self._download_dropped = 0
        self.last_error: Optional[Dict[str, Any]] = None

    def attach_page(self, page: Any, tab_id: str) -> None:
        if page is None:
            return
        key = id(page)
        if key in self._pages:
            return
        handlers = {
            "dialog": lambda dialog, tab_id=tab_id: self._on_dialog_event(tab_id, dialog),
            "download": lambda download, tab_id=tab_id: self._on_download_event(tab_id, download),
            "framenavigated": lambda frame, tab_id=tab_id, page=page: self._on_frame_navigated(tab_id, page, frame),
            "close": lambda closed_page=None, tab_id=tab_id, page=page: self._on_page_closed_event(tab_id, page, closed_page),
        }
        self._pages[key] = {"page": page, "tab_id": tab_id, "handlers": handlers}
        for event, handler in handlers.items():
            try:
                page.on(event, handler)
            except Exception as exc:
                self.last_error = {"error": _exception_text(exc), "timestamp": _now_ms()}

    def detach_page(self, page: Any) -> None:
        if page is None:
            return
        record = self._pages.pop(id(page), None)
        if record is None:
            return
        for event, handler in record["handlers"].items():
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass
        tab_id = record["tab_id"]
        armed = self._armed
        if armed is not None and armed["tab_id"] == tab_id:
            self._armed = None
        for download in self._downloads.values():
            if download.tab_id == tab_id:
                download.source_tab_closed = True
        self._fail_waiters(tab_id)

    async def close(self) -> None:
        self._detach_all()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._pending_dialogs.clear()
        for key in list(self._download_waiters):
            _tab_id, future = self._download_waiters.pop(key)
            if not future.done():
                future.cancel()
        self._drop_all_downloads()
        self._dialogs.clear()
        self._dialog_dropped = 0
        self._armed = None
        self.last_error = None
        self._loop = None

    async def reset(self) -> None:
        await self.close()

    def dialog(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        response = payload.get("response")
        if response not in ("status", "accept", "dismiss"):
            raise BackendError(CODE_INVALID, "'response' must be one of: status, accept, dismiss")
        prompt_text: Optional[str] = None
        if payload.get("promptText") is not None:
            prompt_text = require_str(
                payload["promptText"], "promptText", max_len=MAX_TEXT_LENGTH, allow_empty=True
            )
        if response == "status":
            last = self._dialogs[-1] if self._dialogs else None
            return {
                "hasDialog": bool(self._pending_dialogs),
                "lastDialog": dict(last) if last is not None else None,
                "lastError": self.last_error,
                "armed": self._armed_state(),
                "dropped": self._dialog_dropped,
                "limit": MAX_DIALOGS,
                "note": "dialogs are dismissed unless a decision is armed for the active tab before the triggering action",
            }
        tab_id = self._require_active_tab_id()
        accepted = response == "accept"
        self._armed = {
            "tab_id": tab_id,
            "accepted": accepted,
            "prompt_text": prompt_text,
            "deadline": time.monotonic() + ARM_TTL_SECONDS,
        }
        return {
            "armed": True,
            "accepted": accepted,
            "handled": False,
            "tabId": tab_id,
            "expiresInMs": int(ARM_TTL_SECONDS * 1000),
        }

    def downloads(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        clear = payload.get("clear")
        if clear is not None and not isinstance(clear, bool):
            raise BackendError(CODE_INVALID, "'clear' must be a boolean")
        if clear:
            self._drop_all_downloads()
            return {"cleared": True}
        metadata = [self._metadata(record) for record in self._downloads.values()]
        bounded, truncated, omitted = _bound_records(metadata)
        return {
            "downloads": bounded,
            "dropped": self._download_dropped,
            "limit": MAX_DOWNLOADS,
            "truncated": truncated,
            "omitted": omitted,
        }

    async def from_click(self, path: Optional[str] = None, click_callback: Optional[Any] = None) -> Dict[str, Any]:
        if not callable(click_callback):
            raise BackendError(CODE_INVALID, "a click callback is required to save a download")
        destination = self._explicit_destination(path) if path is not None else None
        page, tab_id = self._active_page_and_tab()
        manager = page.expect_download(timeout=MAX_ACTION_DEADLINE_MS)
        async with manager as info:
            await click_callback()
            try:
                handle = await asyncio.wait_for(info.value, FROM_CLICK_TIMEOUT_MS / 1000.0)
            except asyncio.TimeoutError:
                self._runtime.require_open_session()
                raise BackendError(
                    CODE_ERROR,
                    f"click completed but no download arrived within {FROM_CLICK_TIMEOUT_MS}ms; "
                    "inspect downloads or retry waitfordownload without replaying the click",
                ) from None
        record = self._capture(tab_id, handle)
        self._notify_download_waiters(record)
        target = destination if destination is not None else self._default_destination(record)
        return await self._save_record(record, target)

    async def wait_for_download(self, path: Optional[str] = None,
                                timeout_ms: Any = DEFAULT_WAIT_TIMEOUT_MS) -> Dict[str, Any]:
        timeout = _bounded_timeout(timeout_ms)
        destination = self._explicit_destination(path) if path is not None else None
        _page, tab_id = self._active_page_and_tab()
        record = self._oldest_unconsumed(tab_id)
        if record is None:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            key = id(future)
            self._download_waiters[key] = (tab_id, future)
            try:
                download_id = await asyncio.wait_for(future, timeout / 1000.0)
            except asyncio.TimeoutError:
                self._runtime.require_open_session()
                raise BackendError(
                    CODE_ERROR,
                    f"no download arrived within {timeout}ms; nothing was saved "
                    "(inspect downloads or retry waitfordownload; do not replay the triggering input)",
                ) from None
            finally:
                self._download_waiters.pop(key, None)
                if not future.done():
                    future.cancel()
            self._runtime.require_open_session()
            if download_id is None:
                raise BackendError(CODE_NO_ACTIVE_TAB, "the tab closed while waiting for a download; nothing was saved")
            record = self._downloads.get(download_id)
            if record is None:
                raise BackendError(CODE_ERROR, "the download was dropped before it could be saved; inspect application state without replaying the triggering input")
        target = destination if destination is not None else self._default_destination(record)
        return await self._save_record(record, target)

    def _require_active_tab_id(self) -> str:
        _page, tab = self._runtime.require_active()
        tab_id = getattr(tab, "tab_id", None)
        if not isinstance(tab_id, str) or not tab_id:
            raise BackendError(CODE_NO_ACTIVE_TAB, "no active tab; open or switch to a page before arming a dialog decision")
        return tab_id

    def _active_page_and_tab(self) -> Tuple[Any, str]:
        page, tab = self._runtime.require_active()
        tab_id = getattr(tab, "tab_id", None)
        if not isinstance(tab_id, str) or not tab_id:
            raise BackendError(CODE_NO_ACTIVE_TAB, "no active tab; open or switch to a page before interacting with downloads")
        return page, tab_id

    def _explicit_destination(self, path: str) -> Path:
        raw = require_str(path, "path", max_len=MAX_PATH_LENGTH)
        if "\x00" in raw:
            raise BackendError(CODE_INVALID, "download destination must not contain a null byte")
        destination = Path(raw).expanduser()
        if os.path.lexists(destination):
            kind = "directory" if destination.is_dir() else "file"
            raise BackendError(
                CODE_INVALID,
                f"download destination already exists as a {kind}: {destination}; "
                "choose a new path (downloads never overwrite)",
            )
        parent = destination.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError(CODE_INVALID, f"download destination parent is not usable: {_exception_text(exc)}") from exc
        if not parent.is_dir():
            raise BackendError(CODE_INVALID, "download destination parent is not a directory")
        return destination

    def _default_destination(self, record: _DownloadRecord) -> Path:
        base = Path(self._runtime.runtime_dir) / "tmp"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError(CODE_ERROR, f"download directory is not usable: {_exception_text(exc)}") from exc
        name = _sanitize_filename(record.suggested_filename)
        destination = base / f"download-{uuid.uuid4().hex}-{name}"
        if destination.exists():
            raise BackendError(CODE_INVALID, f"generated download destination already exists: {destination}; retry")
        return destination

    async def _save_record(self, record: _DownloadRecord, destination: Path) -> Dict[str, Any]:
        handle = record.handle
        if handle is None:
            raise BackendError(
                CODE_ERROR,
                "download is no longer retained; it was dropped by the retention limit or the session was reset "
                "(inspect application state without replaying the triggering input)",
            )
        try:
            failure = await handle.failure()
        except Exception as exc:
            self._runtime.require_open_session()
            raise BackendError(
                CODE_ERROR,
                "download capture failed while awaiting completion; inspect downloads or retry "
                "waitfordownload without replaying the triggering input",
            ) from exc
        if failure:
            self._runtime.require_open_session()
            text, _truncated = _bound_text(str(failure))
            record.failure = text
            raise BackendError(CODE_ERROR, f"download failed: {text}; nothing was saved (do not replay the click)")
        if os.path.lexists(destination):
            raise BackendError(
                CODE_INVALID,
                f"download destination already exists: {destination}; choose a new path (downloads never overwrite)",
            )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError(CODE_ERROR, f"download destination parent is not usable: {_exception_text(exc)}") from exc
        fd, temp_name = tempfile.mkstemp(prefix=".agent-browser-download-", dir=str(destination.parent))
        os.close(fd)
        try:
            try:
                await handle.save_as(temp_name)
            except Exception as exc:
                raise BackendError(
                    CODE_ERROR,
                    f"download could not be saved to {destination} ({_exception_text(exc)}); "
                    "the download is retained, retry with waitfordownload (do not replay the click)",
                ) from exc
            try:
                os.chmod(temp_name, 0o600)
                _publish_file(temp_name, destination)
            except FileExistsError as exc:
                raise BackendError(
                    CODE_INVALID,
                    f"download destination already exists: {destination}; choose a new path (downloads never overwrite)",
                ) from exc
            except OSError as exc:
                raise BackendError(
                    CODE_ERROR,
                    f"download could not be published to {destination} ({_exception_text(exc)}); "
                    "the download is retained, retry with waitfordownload (do not replay the click)",
                ) from exc
        finally:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        record.saved_path = str(destination)
        record.consumed = True
        return {
            "path": str(destination),
            "url": record.url,
            "suggestedFilename": record.suggested_filename,
            "downloadId": record.download_id,
            "tabId": record.tab_id,
        }

    def _capture(self, tab_id: str, download: Any) -> _DownloadRecord:
        key = id(download)
        existing_id = self._download_by_handle.get(key)
        if existing_id is not None:
            existing = self._downloads.get(existing_id)
            if existing is not None:
                return existing
        raw_url = _safe_attr(download, "url", "")
        raw_name = _safe_attr(download, "suggested_filename", "")
        url = raw_url if isinstance(raw_url, str) else str(raw_url)
        suggested = raw_name if isinstance(raw_name, str) else str(raw_name)
        url, url_truncated = _bound_text(url)
        suggested, suggested_truncated = _bound_text(suggested)
        self._download_counter += 1
        record = _DownloadRecord(
            download_id=f"d{self._download_counter}",
            tab_id=tab_id,
            url=url,
            suggested_filename=suggested,
            timestamp=_now_ms(),
            handle=download,
            url_truncated=url_truncated,
            suggested_filename_truncated=suggested_truncated,
        )
        self._downloads[record.download_id] = record
        self._download_by_handle[key] = record.download_id
        self._download_order.append(record.download_id)
        while len(self._download_order) > MAX_DOWNLOADS:
            self._evict_download(self._download_order.popleft())
        return record

    def _evict_download(self, download_id: str) -> None:
        record = self._downloads.pop(download_id, None)
        if record is None:
            return
        if record.handle is not None:
            self._download_by_handle.pop(id(record.handle), None)
        record.handle = None
        self._download_dropped += 1

    def _drop_all_downloads(self) -> None:
        for record in self._downloads.values():
            record.handle = None
        self._downloads.clear()
        self._download_by_handle.clear()
        self._download_order.clear()
        self._download_dropped = 0

    def _oldest_unconsumed(self, tab_id: str) -> Optional[_DownloadRecord]:
        for download_id in list(self._download_order):
            record = self._downloads.get(download_id)
            if record is None or record.consumed or record.failure or record.handle is None or record.tab_id != tab_id:
                continue
            return record
        return None

    def _metadata(self, record: _DownloadRecord) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "downloadId": record.download_id,
            "tabId": record.tab_id,
            "url": record.url,
            "suggestedFilename": record.suggested_filename,
            "consumed": record.consumed,
            "savedPath": record.saved_path,
            "failure": record.failure,
            "timestamp": record.timestamp,
        }
        if record.url_truncated:
            metadata["urlTruncated"] = True
        if record.suggested_filename_truncated:
            metadata["suggestedFilenameTruncated"] = True
        if record.error is not None:
            metadata["error"] = record.error
        if record.source_tab_closed and not record.consumed:
            metadata["saveUnavailable"] = "source tab closed; waitfordownload saves only active-tab downloads"
        return metadata

    def _notify_download_waiters(self, record: _DownloadRecord) -> None:
        for key in list(self._download_waiters):
            tab_id, future = self._download_waiters[key]
            if future.done():
                continue
            if tab_id != record.tab_id:
                continue
            self._download_waiters.pop(key, None)
            future.set_result(record.download_id)

    def _fail_waiters(self, tab_id: str) -> None:
        for key in list(self._download_waiters):
            waiter_tab, future = self._download_waiters[key]
            if future.done() or waiter_tab != tab_id:
                continue
            self._download_waiters.pop(key, None)
            future.set_result(None)

    def _armed_state(self) -> Optional[Dict[str, Any]]:
        armed = self._armed
        if armed is None:
            return None
        remaining = armed["deadline"] - time.monotonic()
        if remaining <= 0:
            self._armed = None
            return None
        return {
            "tabId": armed["tab_id"],
            "accepted": armed["accepted"],
            "promptTextArmed": armed["prompt_text"] is not None,
            "expiresInMs": max(0, int(remaining * 1000)),
        }

    def _take_armed(self, tab_id: str) -> Optional[Dict[str, Any]]:
        armed = self._armed
        if armed is None:
            return None
        if time.monotonic() >= armed["deadline"]:
            self._armed = None
            return None
        if armed["tab_id"] != tab_id:
            return None
        self._armed = None
        return armed

    def _on_dialog_event(self, tab_id: str, dialog: Any) -> None:
        self._pending_dialogs.add(id(dialog))
        try:
            armed = self._take_armed(tab_id)
            if self._schedule(self._handle_dialog(tab_id, dialog, armed)) is None:
                self._pending_dialogs.discard(id(dialog))
        except Exception as exc:
            self._pending_dialogs.discard(id(dialog))
            self.last_error = {"error": _exception_text(exc), "timestamp": _now_ms()}

    async def _handle_dialog(self, tab_id: str, dialog: Any, armed: Optional[Dict[str, Any]]) -> None:
        raw_type = _safe_attr(dialog, "type", "unknown")
        raw_message = _safe_attr(dialog, "message", "")
        raw_default = _safe_attr(dialog, "default_value", "")
        dialog_type = raw_type if isinstance(raw_type, str) else str(raw_type)
        message = raw_message if isinstance(raw_message, str) else str(raw_message)
        default_prompt = raw_default if isinstance(raw_default, str) else str(raw_default)
        message, message_truncated = _bound_text(message)
        default_prompt, default_prompt_truncated = _bound_text(default_prompt)
        record: Dict[str, Any] = {
            "type": dialog_type,
            "message": message,
            "defaultPrompt": default_prompt,
            "tabId": tab_id,
            "accepted": None,
            "disposition": None,
            "timestamp": _now_ms(),
        }
        if message_truncated:
            record["messageTruncated"] = True
        if default_prompt_truncated:
            record["defaultPromptTruncated"] = True
        try:
            if armed is None:
                await dialog.dismiss()
                record["accepted"] = False
                record["disposition"] = "auto_dismiss"
            elif armed["accepted"]:
                prompt_text = armed["prompt_text"]
                if prompt_text is None:
                    await dialog.accept()
                else:
                    await dialog.accept(prompt_text)
                record["accepted"] = True
                record["disposition"] = "armed"
            else:
                await dialog.dismiss()
                record["accepted"] = False
                record["disposition"] = "armed"
        except Exception as exc:
            record["error"] = _exception_text(exc)
            self.last_error = {"error": record["error"], "timestamp": _now_ms()}
            record["disposition"] = record["disposition"] or "error"
            try:
                await asyncio.wait_for(dialog.dismiss(), timeout=2)
            except Exception:
                pass
        finally:
            self._pending_dialogs.discard(id(dialog))
            self._append_dialog(record)

    def _append_dialog(self, record: Dict[str, Any]) -> None:
        self._dialogs.append(record)
        while len(self._dialogs) > MAX_DIALOGS:
            self._dialogs.popleft()
            self._dialog_dropped += 1

    def _on_download_event(self, tab_id: str, download: Any) -> None:
        try:
            record = self._capture(tab_id, download)
        except Exception as exc:
            self.last_error = {"error": _exception_text(exc), "timestamp": _now_ms()}
            return
        self._notify_download_waiters(record)

    def _on_frame_navigated(self, tab_id: str, page: Any, frame: Any) -> None:
        try:
            if not self._is_main_frame(page, frame):
                return
            armed = self._armed
            if armed is not None and armed["tab_id"] == tab_id:
                self._armed = None
        except Exception as exc:
            self.last_error = {"error": _exception_text(exc), "timestamp": _now_ms()}

    def _is_main_frame(self, page: Any, frame: Any) -> bool:
        main_frame = getattr(page, "main_frame", None)
        if main_frame is None:
            return False
        if frame is main_frame:
            return True
        frame_impl = getattr(frame, "_impl_obj", None)
        main_impl = getattr(main_frame, "_impl_obj", None)
        return frame_impl is not None and frame_impl is main_impl

    def _on_page_closed_event(self, tab_id: str, page: Any, closed_page: Any = None) -> None:
        try:
            if closed_page is not None and closed_page is not page:
                return
            self.detach_page(page)
        except Exception as exc:
            self.last_error = {"error": _exception_text(exc), "timestamp": _now_ms()}

    def _schedule(self, coro: Any) -> Optional[Any]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            self.last_error = {"error": "event loop unavailable for dialog handling", "timestamp": _now_ms()}
            return None
        self._loop = loop
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task: Any) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.last_error = {"error": _exception_text(exc), "timestamp": _now_ms()}

    def _detach_all(self) -> None:
        for record in list(self._pages.values()):
            page = record["page"]
            for event, handler in record["handlers"].items():
                try:
                    page.remove_listener(event, handler)
                except Exception:
                    pass
        self._pages.clear()
