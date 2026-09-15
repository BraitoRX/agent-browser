#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import input_context as ic
from input_context import (
    BackendError,
    CODE_ERROR,
    CODE_INTERNAL,
    CODE_INVALID,
    CODE_INVALID_REQUEST,
    CODE_OUTPUT_TOO_LARGE,
    CODE_POISONED,
    CODE_REGISTRY,
    CODE_TIMEOUT,
    CODE_UNKNOWN_GESTURE,
    CODE_UNSUPPORTED,
    MAX_OUTPUT_BYTES,
    optional_bool,
    optional_enum,
    optional_int,
    optional_str,
    require_int,
    require_str,
    require_str_list,
)
from registry import (
    GestureSpec,
    RegistryError,
    discover,
    schema_description,
    schema_summary,
    validate_params,
)
from gestures._common import box_center, bounded, element_box, require_visible, step_timeout_ms

TRANSPORT = sys.stdout
sys.stdout = sys.stderr


def write_response(response: Dict[str, Any]) -> None:
    try:
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        encoded = json.dumps(
            {
                "id": response.get("id", "unknown"),
                "success": False,
                "error": "worker could not serialize the response",
                "code": CODE_INTERNAL,
            }
        ).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = json.dumps(
            {
                "id": response.get("id", "unknown"),
                "success": False,
                "error": f"response exceeds the {MAX_OUTPUT_BYTES} byte output limit",
                "code": CODE_OUTPUT_TOO_LARGE,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    TRANSPORT.write(encoded.decode("utf-8") + "\n")
    TRANSPORT.flush()


UNSUPPORTED_LAUNCH_OPTIONS = {
    "allowedDomains": "network containment is not implemented by this backend",
    "provider": "cloud providers are not supported",
    "cdp": "CDP endpoints are not supported",
    "auth": "authenticated sessions are not supported",
    "storageState": "storage state restore is not supported",
    "restoreKey": "session restore is not supported",
    "extensions": "extensions are not supported",
    "initScripts": "init scripts are not supported",
    "args": "custom browser args are not supported",
    "autoConnect": "auto-connect is not supported",
}
KNOWN_LAUNCH_OPTIONS = {
    "headless", "engine", "webmcp", "noXvfb", "metadata", "profile", "adblock",
}


def check_launch_options(payload: Dict[str, Any]) -> None:
    """Profiles select private persistent storage; auth-vault and state-file replay remain unsupported."""
    for key in payload:
        if key in ("id", "action"):
            continue
        if key in UNSUPPORTED_LAUNCH_OPTIONS:
            raise BackendError(
                CODE_UNSUPPORTED,
                f"launch option '{key}' is not supported by the camoufox backend "
                f"({UNSUPPORTED_LAUNCH_OPTIONS[key]})",
            )
        if key in KNOWN_LAUNCH_OPTIONS:
            continue
        raise BackendError(CODE_UNSUPPORTED, f"unknown launch option '{key}'")
    metadata = payload.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise BackendError(CODE_INVALID, "'metadata' must be an object")
        if set(metadata) - {"policyActive"}:
            raise BackendError(CODE_UNSUPPORTED, "unknown launch metadata")
        policy_active = metadata.get("policyActive")
        if policy_active is not None and not isinstance(policy_active, bool):
            raise BackendError(CODE_INVALID, "'metadata.policyActive' must be a boolean")
    engine = payload.get("engine")
    if engine is not None and engine != "camoufox":
        raise BackendError(CODE_UNSUPPORTED, f"engine '{engine}' is not supported by this worker")
    profile = payload.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip() or not Path(profile).is_absolute()):
        raise BackendError(CODE_INVALID, "'profile' must be a non-empty absolute directory path")
    adblock = payload.get("adblock")
    if adblock is not None and not isinstance(adblock, bool):
        raise BackendError(CODE_INVALID, "'adblock' must be a boolean")
    no_xvfb = payload.get("noXvfb")
    if no_xvfb is not None and no_xvfb is not False:
        raise BackendError(CODE_UNSUPPORTED, "'noXvfb' true is not supported")
    webmcp = payload.get("webmcp")
    if webmcp is not None and not isinstance(webmcp, bool):
        raise BackendError(CODE_INVALID, "'webmcp' must be a boolean")
    if webmcp is True:
        raise BackendError(CODE_UNSUPPORTED, "WebMCP is not supported by the Camoufox backend")


BROWSER_ACTIONS = {
    "launch", "navigate", "back", "forward", "reload", "url", "title", "content",
    "evaluate", "read", "snapshot", "screenshot", "click", "dblclick", "fill",
    "type", "press", "hover", "hover_hold", "hover_hold_stop", "focus", "check", "uncheck", "select", "drag",
    "scroll", "scrollintoview", "wait", "waitforurl", "waitforloadstate", "waitforfunction",
    "tab_new", "tab_list", "tab_switch", "tab_close", "frame", "mainframe",
    "gettext", "getattribute", "innerhtml", "inputvalue", "count", "boundingbox",
    "isvisible", "isenabled", "ischecked", "requests", "request_detail", "workers",
    "console", "errors", "websockets", "cookies_get", "cookies_set", "cookies_clear",
    "storage_get", "storage_set", "storage_clear", "route", "unroute", "headers", "offline", "credentials",
    "har_start", "har_stop", "dialog", "download", "waitfordownload", "downloads",
    "page_outline", "page_links", "dom_chunk",
    "getbyrole", "getbytext", "getbylabel", "getbyplaceholder", "getbyalttext",
    "getbytitle", "getbytestid", "nth",
}
LOCAL_ACTIONS = {"gestures", "gesture", "session_info"}
FIND_ACTIONS = {
    "getbyrole", "getbytext", "getbylabel", "getbyplaceholder", "getbyalttext",
    "getbytitle", "getbytestid", "nth",
}
QUERY_ACTIONS = {
    "gettext", "getattribute", "innerhtml", "inputvalue", "count", "boundingbox",
    "isvisible", "isenabled", "ischecked",
}
MUTATING_ACTIONS = {
    "launch", "navigate", "back", "forward", "reload", "evaluate",
    "click", "dblclick", "fill", "type", "press", "hover", "hover_hold", "focus",
    "check", "uncheck", "select", "drag", "scroll", "scrollintoview",
    "tab_new", "tab_switch", "tab_close",
    "cookies_set", "cookies_clear", "storage_set", "storage_clear", "route", "unroute", "headers", "offline", "credentials",
    "har_start", "har_stop", "dialog", "download", "waitfordownload",
}
INPUT_AMBIENT_STOP = {
    "click", "dblclick", "fill", "type", "press", "hover", "focus", "check", "uncheck",
    "select", "drag", "scroll", "scrollintoview", "download", "waitfordownload", "dialog",
    "gesture", "hover_hold",
}
LIFECYCLE_AMBIENT_STOP = {
    "navigate", "back", "forward", "reload", "tab_new", "tab_switch", "tab_close", "frame", "mainframe",
}

ACTION_FIELDS = {
    "navigate": {"url", "waitUntil"},
    "evaluate": {"script"},
    "frame": {"selector"},
    "mainframe": set(),
    "snapshot": {"selector", "maxDepth", "interactive", "compact", "urls", "cursor", "quiet"},
    "screenshot": {"path", "screenshotDir", "selector", "fullPage", "annotate", "format", "quality"},
    "click": {"selector", "target", "button", "count", "newTab"},
    "dblclick": {"selector", "button"},
    "fill": {"selector", "value"},
    "type": {"selector", "text", "clear", "delay"},
    "press": {"key"},
    "hover": {"selector", "settleMs"},
    "hover_hold": {"selector", "maxMs"},
    "hover_hold_stop": set(),
    "select": {"selector", "values"},
    "drag": {"source", "target", "button", "steps", "holdBeforeDropMs", "reveal"},
    "scroll": {"direction", "amount", "selector", "chunkSize", "settleMs"},
    "wait": {"selector", "text", "timeout"},
    "waitforurl": {"url", "timeout"},
    "waitforloadstate": {"state", "timeout"},
    "waitforfunction": {"expression", "timeout"},
    "tab_new": {"url", "label"},
    "tab_switch": {"tabId"},
    "tab_close": {"tabId"},
    "getattribute": {"selector", "attribute"},
    "gestures": {"name"},
    "gesture": {"name", "params", "observe"},
    "requests": {"clear", "filter", "type", "method", "status"},
    "request_detail": {"requestId"},
    "workers": set(),
    "console": {"clear"},
    "errors": {"clear"},
    "websockets": {"clear", "filter"},
    "cookies_get": {"urls"},
    "cookies_set": {"cookies"},
    "cookies_clear": set(),
    "storage_get": {"type", "key"},
    "storage_set": {"type", "key", "value"},
    "storage_clear": {"type"},
    "route": {"url", "abort", "response", "resourceType"},
    "unroute": {"url"},
    "headers": {"headers"},
    "offline": {"offline"},
    "credentials": {"username", "password"},
    "har_start": {"content"},
    "har_stop": {"path"},
    "dialog": {"response", "promptText"},
    "download": {"selector", "path"},
    "waitfordownload": {"path", "timeout"},
    "downloads": {"clear"},
    "page_outline": {"selector"},
    "page_links": {"selector", "cursor", "limit"},
    "dom_chunk": {"selector", "cursor", "limit"},
    "getbyrole": {"role", "subaction", "name", "exact", "value"},
    "getbytext": {"text", "subaction", "exact", "value"},
    "getbylabel": {"label", "subaction", "exact", "value"},
    "getbyplaceholder": {"placeholder", "subaction", "exact", "value"},
    "getbyalttext": {"text", "subaction", "exact", "value"},
    "getbytitle": {"text", "subaction", "exact", "value"},
    "getbytestid": {"testId", "subaction", "value"},
    "nth": {"selector", "index", "subaction", "value"},
}
for _action in QUERY_ACTIONS | {"focus", "check", "uncheck", "scrollintoview"}:
    ACTION_FIELDS.setdefault(_action, {"selector"})
for _action in {"back", "forward", "reload", "url", "title", "content", "read", "tab_list", "session_info", "close"}:
    ACTION_FIELDS[_action] = set()


def validate_request_fields(action: str, payload: Dict[str, Any]) -> None:
    if action == "launch":
        check_launch_options(payload)
        return
    fields = ACTION_FIELDS.get(action)
    if fields is None:
        raise BackendError(CODE_UNSUPPORTED, f"action '{action}' is not supported by the camoufox backend")
    unknown = set(payload) - fields - {"id", "action"}
    if unknown:
        raise BackendError(CODE_UNSUPPORTED, f"unsupported fields for '{action}': {', '.join(sorted(unknown))}")


class Worker:
    def __init__(self, runtime_dir: Path, motion: str):
        self.runtime_dir = runtime_dir
        self.motion = motion
        self.gestures: Dict[str, GestureSpec] = {}
        self.registry_error: Optional[str] = None
        self.poisoned = False
        self.poison_reason: Optional[str] = None
        self.stopping = False
        self.runtime: Optional[Any] = None
        self._action_in_flight = False
        self.deadline_ms = ic.action_deadline_ms()
        self.policy_active = False

    def configure_runtime_env(self) -> None:
        home = self.runtime_dir / "home"
        cache = home / ".cache"
        os.environ["HOME"] = str(home)
        os.environ["XDG_CACHE_HOME"] = str(cache)
        if os.name == "nt":
            os.environ["USERPROFILE"] = str(home)
            os.environ["LOCALAPPDATA"] = str(home / "AppData" / "Local")
            os.environ["APPDATA"] = str(home / "AppData" / "Roaming")
        for key in list(os.environ):
            upper = key.upper()
            if upper in {"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "GH_PAT"} or upper.endswith("_GITHUB_TOKEN"):
                os.environ.pop(key, None)

    def load_gestures(self) -> None:
        directories: List[Tuple[Any, str]] = [(SCRIPT_DIR / "gestures", "builtin")]
        external = os.environ.get("AGENT_BROWSER_GESTURES_DIR")
        if external:
            for entry in external.split(os.pathsep):
                if entry:
                    directories.append((entry, "external"))
        try:
            self.gestures = discover(directories)
            self.registry_error = None
        except RegistryError as exc:
            self.gestures = {}
            self.registry_error = str(exc)

    def get_runtime(self):
        if self.runtime is None:
            from runtime import CamoufoxRuntime

            self.runtime = CamoufoxRuntime(self.runtime_dir, self.motion)
            self.runtime.action_in_flight = self._action_in_flight
        return self.runtime

    def poison(self, reason: str) -> None:
        self.poisoned = True
        self.poison_reason = reason

    def require_available(self, action: str) -> None:
        if not self.poisoned:
            return
        if action in ("close", "session_info", "tab_list", "gestures", "hover_hold_stop"):
            return
        raise BackendError(
            CODE_POISONED,
            "the session needs a reset after timed-out or ambiguous input; close it to recover "
            "(no automatic replay or restart)",
        )

    async def handle_line(self, raw: bytes) -> Optional[str]:
        if len(raw) > ic.MAX_INPUT_BYTES:
            write_response(
                {
                    "id": ic.best_effort_id(raw),
                    "success": False,
                    "error": f"request exceeds the {ic.MAX_INPUT_BYTES} byte input limit",
                    "code": CODE_INVALID_REQUEST,
                }
            )
            return None
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=ic.reject_json_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            write_response(
                {
                    "id": ic.best_effort_id(raw),
                    "success": False,
                    "error": f"invalid JSON request: {type(exc).__name__}",
                    "code": CODE_INVALID_REQUEST,
                }
            )
            return None
        if not isinstance(payload, dict):
            write_response(
                {
                    "id": "unknown",
                    "success": False,
                    "error": "request must be a JSON object",
                    "code": CODE_INVALID_REQUEST,
                }
            )
            return None
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id:
            write_response(
                {
                    "id": "unknown",
                    "success": False,
                    "error": "request 'id' must be a non-empty string",
                    "code": CODE_INVALID_REQUEST,
                }
            )
            return None
        action = payload.get("action")
        if not isinstance(action, str) or not action:
            write_response(
                {
                    "id": request_id,
                    "success": False,
                    "error": "request 'action' must be a non-empty string",
                    "code": CODE_INVALID_REQUEST,
                }
            )
            return None
        attempts_before = int(getattr(self.runtime, "_input_attempts", 0))
        try:
            data = await self.run_action(action, payload)
        except BackendError as exc:
            operation_timeout = _is_playwright_timeout(exc)
            deadline_exceeded = exc.deadline_exceeded or (exc.code == CODE_TIMEOUT and not operation_timeout)
            if deadline_exceeded:
                await self._handle_worker_deadline(action, attempts_before)
            if _is_playwright_target_closed(exc.__cause__):
                exc = self._closed_target_error()
            if exc.code in (ic.CODE_SESSION_CLOSED, ic.CODE_TARGET_CLOSED, ic.CODE_NO_ACTIVE_TAB):
                write_response(self._lifecycle_failure(request_id, exc))
                return None
            response: Dict[str, Any] = {
                "id": request_id,
                "success": False,
                "error": exc.message,
                "code": exc.code,
            }
            if deadline_exceeded:
                response["data"] = {"timeoutKind": "deadline"}
            elif operation_timeout:
                response["data"] = {"timeoutKind": "operation"}
            if self.poisoned:
                response["inputAmbiguous"] = True
            write_response(response)
            return None
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await self._handle_worker_deadline(action, attempts_before)
            response = {
                "id": request_id,
                "success": False,
                "error": "action exceeded its deadline",
                "code": CODE_TIMEOUT,
                "data": {"timeoutKind": "deadline"},
            }
            if self.poisoned:
                response["inputAmbiguous"] = True
            write_response(response)
            return None
        except Exception as exc:
            if _is_playwright_timeout(exc):
                response = {
                    "id": request_id,
                    "success": False,
                    "error": _playwright_error_text(action, exc, payload, timed_out=True),
                    "code": CODE_TIMEOUT,
                    "data": {"timeoutKind": "operation"},
                }
                if self.poisoned:
                    response["inputAmbiguous"] = True
                write_response(response)
                return None
            if _is_playwright_target_closed(exc):
                write_response(self._lifecycle_failure(request_id, self._closed_target_error()))
                return None
            if _is_playwright_error(exc):
                write_response(
                    {
                        "id": request_id,
                        "success": False,
                        "error": _playwright_error_text(action, exc, payload),
                        "code": CODE_ERROR,
                        "inputAmbiguous": True if self.poisoned else None,
                    }
                )
                return None
            detail = f" (missing module {exc.name!r})" if isinstance(exc, ModuleNotFoundError) else ""
            write_response(
                {
                    "id": request_id,
                    "success": False,
                    "error": f"internal worker error: {type(exc).__name__}{detail}",
                    "code": CODE_INTERNAL,
                    "inputAmbiguous": True if self.poisoned else None,
                }
            )
            return None
        if isinstance(data, dict) and self.runtime is not None and action in MUTATING_ACTIONS:
            tab = self.runtime.tabs.get(self.runtime.active_id) if self.runtime.active_id else None
            if tab is not None and not tab.closed and tab.last_ref_remap:
                data.setdefault("remapped", True)
                data.setdefault("newRef", "@" + tab.last_ref_remap["to"])
                tab.last_ref_remap = None
        response = {"id": request_id, "success": True, "data": ic.json_safe(data)}
        if self.poisoned:
            response["inputAmbiguous"] = True
        if action == "close" and isinstance(data, dict) and data.get("closed"):
            write_response(response)
            self.stopping = True
            return "stop"
        write_response(response)
        return None

    def _closed_target_error(self) -> BackendError:
        """Classify a closure race by live state; the exception alone does not identify its scope."""
        runtime = self.get_runtime()
        runtime.reconcile_session()
        for tab in runtime.tabs.values():
            runtime._clear_refs(tab.tab_id)
        runtime.captures.invalidate_all()
        error = runtime.session_closed_error()
        if error is not None:
            return error
        if runtime._is_live() and runtime.active_id is None:
            return BackendError(
                ic.CODE_NO_ACTIVE_TAB,
                "the active tab closed; inspect tab_list, then explicitly tab_switch to a live task tab "
                "or tab_new (do not restart a healthy browser)",
            )
        return BackendError(
            ic.CODE_TARGET_CLOSED,
            "a browser target closed during the action; inspect session_info and tab_list before continuing. "
            "Closure scope is unconfirmed; do not assume the whole browser is dead or replay input",
        )

    def _lifecycle_failure(self, request_id: str, error: BackendError) -> Dict[str, Any]:
        runtime = self.get_runtime()
        runtime.reconcile_session()
        response = {
            "id": request_id,
            "success": False,
            "error": error.message,
            "code": error.code,
            "data": {**runtime.launch_info(), "activeTab": runtime.active_id, "tabs": runtime.list_tabs()},
        }
        if self.poisoned:
            response["inputAmbiguous"] = True
            response["error"] += (
                ". Input may already have been dispatched; close and inspect application state "
                "without retrying the input"
            )
        return response

    async def _handle_worker_deadline(self, action: str, attempts_before: int) -> None:
        """Reconcile input state after the worker's own deadline cancelled an action.

        The cancel is not by itself a reason to discard the session. Require a reset only when the
        cancelled action may have left native input, a half-started browser, or another
        resource in an indeterminate state: pending journaled buttons/keys after a bounded
        release, native input attempted during this action, or a cancelled launch/close. A
        cancelled read-only action has no input state to reconcile, so the session stays
        usable and the next command runs under its own deadline.
        """
        runtime = self.runtime
        if runtime is not None:
            try:
                await asyncio.wait_for(runtime.release_inputs(), timeout=3)
            except Exception:
                pass
            pending = runtime.journal.pending_buttons() + runtime.journal.pending_keys()
            if pending or self._input_attempted_since(runtime, attempts_before):
                self.poison("action deadline exceeded during input dispatch")
                return
        if action in ("launch", "close"):
            self.poison(f"{action} deadline exceeded before the browser reached a stable state")

    async def run_action(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._action_in_flight = True
        if self.runtime is not None:
            self.runtime.action_in_flight = True
        try:
            ambient = getattr(self.runtime, "ambient_hover", None)
            find_input = action in FIND_ACTIONS and payload.get("subaction", "click") != "text"
            replaced = False
            if ambient is not None and (action in INPUT_AMBIENT_STOP | LIFECYCLE_AMBIENT_STOP or find_input):
                replaced = (await ambient.stop())["stopped"]
            self.require_available(action)
            validate_request_fields(action, payload)
            if action == "close":
                return await self.do_close()
            if action == "gesture" and self.policy_active:
                raise BackendError(
                    CODE_UNSUPPORTED,
                    "generic gesture execution is prohibited while policyActive is true; "
                    "only typed actions are permitted",
                )
            if self.registry_error is not None and action in ("gesture", "gestures"):
                raise BackendError(CODE_REGISTRY, f"gesture registry failed to load: {self.registry_error}")
            if action in BROWSER_ACTIONS:
                mutating = action in MUTATING_ACTIONS or find_input
                try:
                    result = await self.deadline(self.dispatch_browser(action, payload), what=f"browser action '{action}'")
                    if action == "hover_hold":
                        result["replaced"] = replaced or result["replaced"]
                    return result
                finally:
                    if mutating and self.runtime is not None:
                        self.runtime.captures.invalidate_all()
                        self.runtime.clear_dom_refs()
            if action in LOCAL_ACTIONS:
                return await self.dispatch_local(action, payload)
            raise BackendError(CODE_UNSUPPORTED, f"action '{action}' is not supported by the camoufox backend")
        finally:
            self._action_in_flight = False
            if self.runtime is not None:
                self.runtime.action_in_flight = False

    async def deadline(self, coro, timeout_ms: Optional[int] = None, *, what: str = "action"):
        effective_ms = min(timeout_ms or self.deadline_ms, ic.MAX_ACTION_DEADLINE_MS)
        effective_ms = max(250, effective_ms)
        try:
            return await asyncio.wait_for(coro, timeout=effective_ms / 1000.0)
        except asyncio.TimeoutError:
            raise BackendError(
                CODE_TIMEOUT,
                f"{what} exceeded the {effective_ms}ms deadline",
                deadline_exceeded=True,
            ) from None
        except Exception as exc:
            if _is_playwright_timeout(exc) and not isinstance(exc, BackendError):
                raise ic.playwright_timeout_error(exc, what) from exc
            raise

    async def _budgeted(self, budget: _ActionBudget, awaitable: Awaitable[Any], what: str) -> Any:
        remaining = budget.remaining_seconds()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise BackendError(
                CODE_TIMEOUT,
                f"action deadline exceeded before {what}",
                deadline_exceeded=True,
            )
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except asyncio.TimeoutError:
            raise BackendError(
                CODE_TIMEOUT,
                f"{what} exceeded the action deadline",
                deadline_exceeded=True,
            ) from None

    async def _release_inputs_bounded(self, runtime) -> List[str]:
        try:
            await asyncio.wait_for(runtime.release_inputs(), timeout=3)
        except Exception:
            pass
        return runtime.journal.pending_buttons() + runtime.journal.pending_keys()

    def _input_attempted_since(self, runtime, attempts_before: int) -> bool:
        return int(getattr(runtime, "_input_attempts", 0)) > attempts_before

    def _poison_failed_input(self, runtime, attempts_before: int, exc: BaseException,
                             pending: List[str], what: str) -> None:
        """A completed Playwright timeout is recoverable after release; a worker deadline is
        recoverable only when no input was attempted during the action."""
        if _is_playwright_timeout(exc) and not pending:
            return
        if pending or self._input_attempted_since(runtime, attempts_before):
            self.poison(f"{what} failed with ambiguous input state")

    async def _execute_gesture(self, runtime, spec: GestureSpec, params: Dict[str, Any]) -> Tuple[Dict[str, Any], "_ActionBudget", int]:
        budget = _ActionBudget(self.deadline_ms)
        attempts_before = int(getattr(runtime, "_input_attempts", 0))
        context = runtime.GestureContextClass(runtime, max(1, budget.remaining_ms()))
        if spec.name in {"drag", "hold", "path"} and not getattr(context, "frame_is_main", True):
            raise BackendError(CODE_UNSUPPORTED, f"{spec.name} requires frame main; selected-frame targets are not supported")
        failed = False
        failure: Optional[BaseException] = None
        result: Any = None
        try:
            result = await self._budgeted(budget, spec.run(context, params), f"gesture '{spec.name}'")
        except BaseException as exc:
            failed = True
            failure = exc
        finally:
            pending = await self._release_inputs_bounded(runtime)
            runtime.captures.invalidate_all()
        if failed:
            self._poison_failed_input(runtime, attempts_before, failure, pending, f"gesture '{spec.name}'")
            raise failure
        if pending:
            self.poison("journaled input could not be released")
            raise BackendError(
                CODE_POISONED,
                "journaled input could not be released after the gesture; "
                "close the session to recover",
            )
        if not isinstance(result, dict):
            result = {"result": result}
        result.setdefault("gesture", spec.name)
        result["diagnostics"] = context.diagnostics()
        return result, budget, attempts_before

    async def _guarded_input(self, runtime, awaitable: Awaitable[Any], *, buttons: Tuple[str, ...] = (),
                             keys: Tuple[str, ...] = (), mark: bool = False, what: str) -> Any:
        journal = runtime.journal
        attempts_before = int(getattr(runtime, "_input_attempts", 0))
        for button in buttons:
            journal.begin_button_down(button)
        for key in keys:
            journal.begin_key_down(key)
        if mark or buttons or keys:
            runtime.note_input()
        failed = False
        failure: Optional[BaseException] = None
        result: Any = None
        try:
            result = await awaitable
        except BaseException as exc:
            failed = True
            failure = exc
        finally:
            if not failed:
                for button in buttons:
                    journal.finish_button_up(button)
                for key in keys:
                    journal.finish_key_up(key)
            pending = await self._release_inputs_bounded(runtime)
        if failed:
            self._poison_failed_input(runtime, attempts_before, failure, pending, what)
            raise failure
        if pending:
            self.poison("journaled input could not be released")
            raise BackendError(
                CODE_POISONED,
                "journaled input could not be released after the input action; "
                "close the session to recover",
            )
        return result

    async def dispatch_local(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if action == "session_info":
            runtime = self.get_runtime()
            info = runtime.session_info(sorted(self.gestures.keys()))
            info["inputAmbiguous"] = self.poisoned
            info["gestureRegistry"] = {"ok": self.registry_error is None, "error": self.registry_error}
            info["limits"] = {
                "maxInputBytes": ic.MAX_INPUT_BYTES,
                "maxOutputBytes": MAX_OUTPUT_BYTES,
                "actionDeadlineMs": self.deadline_ms,
                "maxActionDeadlineMs": ic.MAX_ACTION_DEADLINE_MS,
                "holdMaxMs": ic.HOLD_MAX_MS,
            }
            return info
        if action == "gestures":
            return self.do_gestures_list(payload)
        if action == "gesture":
            return await self.do_gesture(payload)
        raise BackendError(CODE_UNSUPPORTED, f"action '{action}' is not supported")

    def do_gestures_list(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = payload.get("name")
        if name is None:
            return {
                "gestures": [schema_summary(spec) for spec in self.gestures.values()],
                "count": len(self.gestures),
                "externalDir": os.environ.get("AGENT_BROWSER_GESTURES_DIR"),
            }
        if not isinstance(name, str):
            raise BackendError(CODE_INVALID, "'name' must be a string")
        spec = self.gestures.get(name)
        if spec is None:
            raise BackendError(CODE_UNKNOWN_GESTURE, f"unknown gesture: {name}")
        return schema_description(spec)

    async def do_gesture(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = require_str(payload.get("name"), "name")
        spec = self.gestures.get(name)
        if spec is None:
            raise BackendError(CODE_UNKNOWN_GESTURE, f"unknown gesture: {name}")
        params = payload.get("params", {})
        if not isinstance(params, dict):
            raise BackendError(CODE_INVALID, "'params' must be an object")
        errors = validate_params(spec.schema, params)
        if errors:
            raise BackendError(CODE_INVALID, "; ".join(errors[:5]))
        observe = payload.get("observe", "none")
        if observe not in ("none", "snapshot", "screenshot"):
            raise BackendError(CODE_INVALID, "'observe' must be one of: none, snapshot, screenshot")

        runtime = self.get_runtime()
        result, budget, attempts_before = await self._execute_gesture(runtime, spec, params)
        try:
            if observe == "snapshot":
                result["observation"] = await self._budgeted(
                    budget, runtime.snapshot(selector=None, max_depth=None), "gesture observation"
                )
            elif observe == "screenshot":
                shot = await self._budgeted(
                    budget, runtime.screenshot(path=None, screenshot_dir=None), "gesture screenshot observation"
                )
                result["observation"] = {"path": shot["path"], "visualCapture": shot["visualCapture"]}
                result["path"] = shot["path"]
        except BaseException as exc:
            pending = runtime.journal.pending_buttons() + runtime.journal.pending_keys()
            self._poison_failed_input(runtime, attempts_before, exc, pending, "gesture observation")
            raise
        return result

    async def do_close(self) -> Dict[str, Any]:
        runtime = self.runtime
        if runtime is not None:
            result = await asyncio.wait_for(runtime.close(), timeout=18)
        else:
            result = {"closed": True, "notLaunched": True}
        return {"engine": "camoufox", **result}

    async def do_gesture_call(self, runtime, gesture_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        spec = self.gestures.get(gesture_name)
        if spec is None:
            raise BackendError(CODE_REGISTRY, f"built-in gesture '{gesture_name}' is not registered")
        if gesture_name == "hover":
            target = payload.get("selector")
            if target is None:
                raise BackendError(CODE_INVALID, "'selector' is required")
            params: Dict[str, Any] = {"target": {"selector": target}}
            if payload.get("settleMs") is not None:
                params["settleMs"] = payload["settleMs"]
        elif gesture_name == "drag":
            source = payload.get("source")
            target = payload.get("target")
            if source is None or target is None:
                raise BackendError(CODE_INVALID, "'source' and 'target' are required")
            params = {"source": _as_target(source), "target": _as_target(target)}
            for key in ("button", "steps", "holdBeforeDropMs", "reveal"):
                if payload.get(key) is not None:
                    params[key] = payload[key]
        elif gesture_name == "scroll":
            params = {
                key: payload[key]
                for key in ("direction", "amount", "selector", "chunkSize", "settleMs")
                if payload.get(key) is not None
            }
        elif gesture_name == "click":
            params = {"target": payload.get("target")}
            for key in ("button", "count"):
                if payload.get(key) is not None:
                    params[key] = payload[key]
        else:
            raise BackendError(CODE_INTERNAL, f"no canonical mapping for gesture '{gesture_name}'")
        errors = validate_params(spec.schema, params)
        if errors:
            raise BackendError(CODE_INVALID, "; ".join(errors[:5]))
        result, _budget, _attempts = await self._execute_gesture(runtime, spec, params)
        return result

    async def do_snapshot(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        selector = optional_str(payload.get("selector"), "selector")
        max_depth = optional_int(payload.get("maxDepth"), "maxDepth", 0, 100)
        quiet = optional_bool(payload.get("quiet"), "quiet", False)
        if optional_bool(payload.get("interactive"), "interactive", False):
            raise BackendError(CODE_UNSUPPORTED, "interactive filtering is not supported by the native AI snapshot")
        if optional_bool(payload.get("compact"), "compact", False):
            raise BackendError(CODE_UNSUPPORTED, "compact filtering is not supported by the native AI snapshot")
        optional_bool(payload.get("urls"), "urls", False)
        optional_bool(payload.get("cursor"), "cursor", False)
        if quiet:
            await runtime.wait_for_page_quiet(quiet_ms=250, max_ms=3000)
        return await runtime.snapshot(selector=selector, max_depth=max_depth)

    async def do_screenshot(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        path = optional_str(payload.get("path"), "path", max_len=4096)
        selector = optional_str(payload.get("selector"), "selector")
        screenshot_dir = optional_str(payload.get("screenshotDir"), "screenshotDir", max_len=4096)
        if optional_bool(payload.get("fullPage"), "fullPage", False):
            raise BackendError(CODE_UNSUPPORTED, "fullPage screenshots are not supported by the V1 camoufox backend")
        if optional_bool(payload.get("annotate"), "annotate", False):
            raise BackendError(CODE_UNSUPPORTED, "annotated screenshots are not supported by the V1 camoufox backend")
        if selector is not None:
            raise BackendError(CODE_UNSUPPORTED, "element screenshots are not supported by the V1 camoufox backend")
        fmt = optional_str(payload.get("format"), "format")
        if fmt is not None and fmt != "png":
            raise BackendError(CODE_UNSUPPORTED, "only PNG screenshots are supported by the V1 camoufox backend")
        if payload.get("quality") is not None:
            raise BackendError(CODE_UNSUPPORTED, "PNG quality is not applicable; remove 'quality'")
        return await runtime.screenshot(path=path, screenshot_dir=screenshot_dir)

    async def do_click(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        double = bool(payload.get("_double"))
        if optional_bool(payload.get("newTab"), "newTab", False):
            raise BackendError(CODE_UNSUPPORTED, "newTab clicks are not supported by the V1 camoufox backend")
        button = optional_enum(payload.get("button"), "button", ("left", "right", "middle"), "left")
        assert button is not None
        selector = payload.get("selector")
        if selector is None:
            params: Dict[str, Any] = {"target": _as_target(payload.get("target"))}
        else:
            params = {"target": {"selector": selector}}
        params["button"] = button
        params["count"] = 2 if double else optional_int(payload.get("count"), "count", 1, 2, 1)
        result = await self.do_gesture_call(runtime, "click", params)
        result["action"] = "click"
        return result

    async def do_fill(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        selector = require_str(payload.get("selector"), "selector")
        value = require_str(payload.get("value"), "value", max_len=ic.MAX_TEXT_LENGTH, allow_empty=True)
        _scope, locator = await runtime.resolve_action_locator(ic.parse_selector(selector, "selector"))
        await self._guarded_input(runtime, self.deadline(locator.fill(value)), mark=True, what="fill")
        return {"filled": len(value), "selector": selector}

    async def do_download(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        selector = require_str(payload.get("selector"), "selector")
        path = require_str(payload.get("path"), "path", max_len=4096)
        click_result: Dict[str, Any] = {}

        async def click_once() -> None:
            click_result.update(await self.do_click(runtime, {"selector": selector}))

        result = await runtime.interactions.from_click(path, click_once)
        if "diagnostics" in click_result:
            result["diagnostics"] = click_result["diagnostics"]
        return result

    async def do_type(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        selector = require_str(payload.get("selector"), "selector")
        text = require_str(payload.get("text"), "text", max_len=ic.MAX_TEXT_LENGTH, allow_empty=True)
        clear = optional_bool(payload.get("clear"), "clear", False)
        delay = optional_int(payload.get("delay"), "delay", 0, 1000, 0)
        assert clear is not None and delay is not None
        spec = self.gestures.get("type")
        if spec is None:
            raise BackendError(CODE_REGISTRY, "built-in type gesture is unavailable")
        result, _budget, _attempts = await self._execute_gesture(
            runtime, spec, {"selector": selector, "text": text, "clear": clear, "delayMs": delay}
        )
        return {**result, "typed": len(text), "selector": selector, "clear": clear, "delay": delay}

    async def do_select(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        selector = require_str(payload.get("selector"), "selector")
        values = payload.get("values")
        if values is None:
            raise BackendError(CODE_INVALID, "'values' is required (string or array of strings)")
        items = require_str_list(values, "values")
        _scope, locator = await runtime.resolve_action_locator(ic.parse_selector(selector, "selector"))
        selected = await self._guarded_input(runtime, self.deadline(locator.select_option(items)), mark=True, what="select")
        return {"selected": selected, "count": len(selected)}

    async def do_wait(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        selector = optional_str(payload.get("selector"), "selector")
        text = optional_str(payload.get("text"), "text", max_len=ic.MAX_TEXT_LENGTH)
        timeout_ms = optional_int(payload.get("timeout"), "timeout", 0, ic.MAX_ACTION_DEADLINE_MS, 10_000)
        assert timeout_ms is not None
        if selector is None and text is None:
            await asyncio.sleep(timeout_ms / 1000.0)
            return {"waited": "sleep", "timeout": timeout_ms}
        page = runtime.active_scope()
        if selector is not None:
            spec = ic.parse_selector(selector, "selector")
            _scope, locator = await runtime.resolve_action_locator(spec)
            await self.deadline(locator.wait_for(state="visible", timeout=timeout_ms))
            return {"waited": "selector", "timeout": timeout_ms}
        await self.deadline(page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=timeout_ms))
        return {"waited": "text", "timeout": timeout_ms}

    async def do_query(self, runtime, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        selector = require_str(payload.get("selector"), "selector")
        spec = ic.parse_selector(selector, "selector")
        _scope, locator = await runtime.resolve_action_locator(spec)
        if action == "gettext":
            return {"text": await self.deadline(locator.inner_text())}
        if action == "innerhtml":
            return {"html": await self.deadline(locator.inner_html())}
        if action == "getattribute":
            attribute = require_str(payload.get("attribute"), "attribute")
            return {"attribute": attribute, "value": await self.deadline(locator.get_attribute(attribute))}
        if action == "inputvalue":
            return {"value": await self.deadline(locator.input_value())}
        if action == "count":
            count = await self.deadline(locator.count())
            return {"count": count}
        if action == "boundingbox":
            box = await element_box(_ActionBudget(self.deadline_ms), locator, f"selector {selector!r}")
            return {"boundingBox": ic.json_safe(box)}
        if action == "isvisible":
            return {"visible": await self.deadline(locator.is_visible())}
        if action == "isenabled":
            return {"enabled": await self.deadline(locator.is_enabled())}
        if action == "ischecked":
            return {"checked": await self.deadline(locator.is_checked())}
        raise BackendError(CODE_UNSUPPORTED, f"action '{action}' is not supported")

    async def do_hover_hold(self, runtime, payload: Dict[str, Any]) -> Dict[str, Any]:
        selector = require_str(payload.get("selector"), "selector")
        if not selector.strip():
            raise BackendError(CODE_INVALID, "'selector' must not be empty")
        max_ms = payload.get("maxMs", 30000)
        if not isinstance(max_ms, int) or isinstance(max_ms, bool):
            raise BackendError(CODE_INVALID, "'maxMs' must be an integer")
        max_ms = require_int(max_ms, "maxMs", 1000, 120000)
        replaced = (await runtime.ambient_hover.stop())["stopped"]
        spec = ic.parse_selector(selector, "selector")
        _scope, locator = await runtime.resolve_action_locator(spec)
        page, tab = runtime.require_active()
        context = runtime.GestureContextClass(runtime, self.deadline_ms)
        context.set_stage("resolve")
        box = await element_box(context, locator, f"selector {selector!r}")
        x, y = box_center(box)

        async def move_once() -> None:
            context.set_stage("hover")
            context.note_input_dispatched()
            await bounded(context, page.mouse.move(x, y), "hover hold")

        await self._guarded_input(runtime, move_once(), what="hover hold")
        runtime.ambient_hover.start(page, tab, x, y, max_ms)
        return {
            "holding": True,
            "point": {"x": x, "y": y},
            "maxMs": max_ms,
            "replaced": replaced,
            "diagnostics": context.diagnostics(),
        }

    async def do_find(self, runtime, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        subaction = payload.get("subaction") or "click"
        fill_value = payload.get("value")
        if subaction == "fill" and fill_value is None:
            raise BackendError(CODE_INVALID, "fill subaction requires a value")
        scope = runtime.active_scope()
        if action == "nth":
            selector = require_str(payload.get("selector"), "selector")
            index = require_int(payload.get("index"), "index", -2_147_483_648, 2_147_483_647)
            spec = ic.parse_selector(selector, "selector")
            _scope, locator = await runtime.resolve_action_locator(spec)
            locator = locator.nth(index)
        else:
            exact = bool(optional_bool(payload.get("exact"), "exact", False))
            if action == "getbyrole":
                role = require_str(payload.get("role"), "role")
                name = optional_str(payload.get("name"), "name")
                if name:
                    locator = scope.get_by_role(role, name=name, exact=exact)
                else:
                    locator = scope.get_by_role(role)
            elif action == "getbytext":
                text = require_str(payload.get("text"), "text")
                locator = scope.get_by_text(text, exact=exact)
            elif action == "getbylabel":
                label = require_str(payload.get("label"), "label")
                locator = scope.get_by_label(label, exact=exact)
            elif action == "getbyplaceholder":
                placeholder = require_str(payload.get("placeholder"), "placeholder")
                locator = scope.get_by_placeholder(placeholder, exact=exact)
            elif action == "getbyalttext":
                text = require_str(payload.get("text"), "text")
                locator = scope.get_by_alt_text(text, exact=exact)
            elif action == "getbytitle":
                text = require_str(payload.get("text"), "text")
                locator = scope.get_by_title(text, exact=exact)
            else:
                test_id = require_str(payload.get("testId"), "testId")
                locator = scope.get_by_test_id(test_id)
        count = await self.deadline(locator.count())
        if count == 0:
            raise BackendError(
                CODE_INVALID,
                f"{action} locator matched 0 elements; refine the value, use exact, "
                "or take a fresh snapshot",
            )
        if count > 1 and subaction != "text":
            raise BackendError(
                CODE_INVALID,
                f"{action} locator matched {count} elements; refine with exact/name, "
                "scope, or find nth",
            )
        target = locator.first if count > 1 else locator
        css = await self.deadline(target.evaluate(
            "(element) => { const path = []; let el = element; while (el && el.nodeType === 1 && path.length < 50) "
            "{ if (el.id) { try { if (document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) "
            "{ path.unshift('#' + CSS.escape(el.id)); return path.join(' > '); } } catch (e) {} } "
            "let selector = el.tagName.toLowerCase(); const parent = el.parentElement; if (parent) "
            "{ const sameTag = Array.from(parent.children).filter((c) => c.tagName === el.tagName); "
            "if (sameTag.length > 1) selector += ':nth-of-type(' + (sameTag.indexOf(el) + 1) + ')'; } "
            "path.unshift(selector); el = parent; } return path.join(' > '); }"
        ))
        if not css:
            raise BackendError(CODE_INVALID, "could not derive a unique CSS selector for the matched element")
        if subaction == "text":
            text = await self.deadline(target.evaluate(
                "(element) => (element.textContent || '').trim().substring(0, 4096)"
            ))
            return {"found": True, "count": count, "selector": css, "text": text}
        delegated: Dict[str, Any] = {"id": payload.get("id"), "action": subaction, "selector": css}
        if subaction == "fill" and fill_value is not None:
            delegated["value"] = fill_value
        result = await self.dispatch_browser(subaction, delegated)
        return {"found": True, "count": count, "selector": css, **result}

    async def dispatch_browser(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        runtime = self.get_runtime()
        if action == "hover_hold_stop":
            return await runtime.ambient_hover.stop()
        if action == "launch":
            check_launch_options(payload)
            metadata = payload.get("metadata")
            self.policy_active = isinstance(metadata, dict) and metadata.get("policyActive") is True
            headless = optional_bool(payload.get("headless"), "headless", True)
            assert headless is not None
            adblock = optional_bool(payload.get("adblock"), "adblock", False)
            assert adblock is not None
            data = await runtime.launch(headless, profile=payload.get("profile"), adblock=adblock)
            data["startedAt"] = ic.iso_now()
            return data
        if action == "tab_list":
            return runtime.list_tabs()
        if action == "tab_close":
            return await runtime.close_tab(optional_str(payload.get("tabId"), "tabId"))
        if action == "tab_new":
            url = optional_str(payload.get("url"), "url", max_len=ic.MAX_SELECTOR_LENGTH)
            label = optional_str(payload.get("label"), "label", max_len=256)
            return await runtime.new_tab(url, label)
        if action == "tab_switch":
            return runtime.switch_tab(optional_str(payload.get("tabId"), "tabId"))

        if action in ("console", "errors", "websockets"):
            runtime.require_open_session()
            return getattr(runtime.inspector, action)(payload)
        if action in ("dialog", "downloads"):
            runtime.require_open_session()
            return getattr(runtime.interactions, action)(payload)
        if action == "download":
            return await self.do_download(runtime, payload)
        if action == "waitfordownload":
            return await runtime.interactions.wait_for_download(payload.get("path"), payload.get("timeout"))
        if action in ("cookies_get", "cookies_set", "cookies_clear", "storage_get", "storage_set", "storage_clear"):
            return await runtime.inspector.state(action, payload)
        if action in ("route", "unroute", "headers", "offline", "credentials"):
            return await runtime.network_control.dispatch(action, payload)
        if action == "har_start":
            return await runtime.har.start(payload.get("content"))
        if action == "har_stop":
            return await runtime.har.stop(payload.get("path"))
        if action in ("requests", "request_detail"):
            runtime.require_open_session()
            if action == "requests":
                return runtime.network.requests(payload)
            return await runtime.network.request_detail(require_str(payload.get("requestId"), "requestId"))

        runtime.require_active()
        if action == "workers":
            from worker_visibility import inspect_workers
            return await inspect_workers(runtime)
        if action == "navigate":
            url = require_str(payload.get("url"), "url", max_len=ic.MAX_SELECTOR_LENGTH)
            wait_until = optional_str(payload.get("waitUntil"), "waitUntil")
            if wait_until is not None and wait_until not in ("load", "domcontentloaded", "networkidle", "commit"):
                raise BackendError(CODE_INVALID, "'waitUntil' must be load, domcontentloaded, networkidle, or commit")
            return await runtime.navigate(url, wait_until)
        if action == "back":
            return await runtime.go_history("back")
        if action == "forward":
            return await runtime.go_history("forward")
        if action == "reload":
            return await runtime.go_history("reload")
        if action == "url":
            return await runtime.current_url()
        if action == "title":
            return await runtime.current_title()
        if action == "content":
            return await runtime.content()
        if action == "frame":
            return await runtime.switch_frame(require_str(payload.get("selector"), "selector"))
        if action == "mainframe":
            return await runtime.switch_frame(None)
        if action == "evaluate":
            script = require_str(payload.get("script"), "script", max_len=1_000_000)
            return await runtime.evaluate(script)
        if action == "read":
            if payload.get("url") is not None:
                raise BackendError(
                    CODE_UNSUPPORTED,
                    "explicit-URL read is not supported by this runtime (no network fetch bypass); "
                    "navigate first, then read the rendered page",
                )
            return await runtime.read()
        if action == "snapshot":
            return await self.do_snapshot(runtime, payload)
        if action == "screenshot":
            return await self.do_screenshot(runtime, payload)
        if action == "click":
            return await self.do_click(runtime, payload)
        if action == "dblclick":
            payload = dict(payload)
            payload["_double"] = True
            return await self.do_click(runtime, payload)
        if action == "fill":
            return await self.do_fill(runtime, payload)
        if action == "type":
            return await self.do_type(runtime, payload)
        if action == "press":
            key = require_str(payload.get("key"), "key")
            await self._guarded_input(
                runtime,
                self.deadline(runtime.page_keyboard().press(key)),
                keys=_key_parts(key),
                what="keyboard press",
            )
            return {"pressed": key}
        if action == "hover":
            return await self.do_gesture_call(runtime, "hover", payload)
        if action == "hover_hold":
            return await self.do_hover_hold(runtime, payload)
        if action == "focus":
            selector = require_str(payload.get("selector"), "selector")
            _scope, locator = await runtime.resolve_action_locator(ic.parse_selector(selector, "selector"))
            await self._guarded_input(
                runtime,
                self.deadline(locator.focus()),
                mark=True,
                what="focus",
            )
            return {"focused": selector}
        if action == "check":
            selector = require_str(payload.get("selector"), "selector")
            _scope, locator = await runtime.resolve_action_locator(ic.parse_selector(selector, "selector"))
            await self._guarded_input(
                runtime,
                self.deadline(locator.check()),
                buttons=("left",),
                what="check",
            )
            return {"checked": selector}
        if action == "uncheck":
            selector = require_str(payload.get("selector"), "selector")
            _scope, locator = await runtime.resolve_action_locator(ic.parse_selector(selector, "selector"))
            await self._guarded_input(
                runtime,
                self.deadline(locator.uncheck()),
                buttons=("left",),
                what="uncheck",
            )
            return {"unchecked": selector}
        if action == "select":
            return await self.do_select(runtime, payload)
        if action == "drag":
            return await self.do_gesture_call(runtime, "drag", payload)
        if action == "scroll":
            return await self.do_gesture_call(runtime, "scroll", payload)
        if action == "scrollintoview":
            selector = require_str(payload.get("selector"), "selector")
            _scope, locator = await runtime.resolve_action_locator(ic.parse_selector(selector, "selector"))
            budget = _ActionBudget(self.deadline_ms)
            label = f"selector {selector!r}"
            await require_visible(budget, locator, label)
            timeout_ms = step_timeout_ms(budget)
            await self._guarded_input(
                runtime,
                bounded(
                    budget,
                    locator.evaluate(
                        "el => {"
                        " const r = el.getBoundingClientRect();"
                        " const vw = document.documentElement.clientWidth;"
                        " const vh = document.documentElement.clientHeight;"
                        " if (r.width > 0 && r.height > 0 && r.top >= 0 && r.left >= 0"
                        " && r.bottom <= vh && r.right <= vw) return;"
                        " el.scrollIntoView({ block: 'center', inline: 'center' });"
                        " }",
                        timeout=timeout_ms,
                    ),
                    f"scroll {label} into view",
                    timeout_ms,
                ),
                mark=True,
                what="scroll into view",
            )
            return {"scrolled": True, "selector": selector}
        if action == "wait":
            return await self.do_wait(runtime, payload)
        if action == "waitforurl":
            url = require_str(payload.get("url"), "url")
            timeout_ms = optional_int(payload.get("timeout"), "timeout", 0, ic.MAX_ACTION_DEADLINE_MS, 10_000)
            assert timeout_ms is not None
            await self.deadline(runtime.page_waiter().wait_for_url(url, timeout=timeout_ms))
            return {"waited": "url", "url": runtime.page_url()}
        if action == "waitforloadstate":
            state = require_str(payload.get("state"), "state")
            if state not in ("load", "domcontentloaded", "networkidle"):
                raise BackendError(CODE_INVALID, "'state' must be load, domcontentloaded, or networkidle")
            timeout_ms = optional_int(payload.get("timeout"), "timeout", 0, ic.MAX_ACTION_DEADLINE_MS, 10_000)
            assert timeout_ms is not None
            await self.deadline(runtime.page_waiter().wait_for_load_state(state, timeout=timeout_ms))
            return {"waited": "loadstate", "state": state}
        if action == "waitforfunction":
            expression = require_str(payload.get("expression"), "expression", max_len=1_000_000)
            timeout_ms = optional_int(payload.get("timeout"), "timeout", 0, ic.MAX_ACTION_DEADLINE_MS, 10_000)
            assert timeout_ms is not None
            await self.deadline(runtime.active_scope().wait_for_function(expression, timeout=timeout_ms))
            return {"waited": "function"}
        if action in FIND_ACTIONS:
            return await self.do_find(runtime, action, payload)
        if action in QUERY_ACTIONS:
            return await self.do_query(runtime, action, payload)
        if action == "page_outline":
            selector = optional_str(payload.get("selector"), "selector")
            return await runtime.page_outline(selector)
        if action == "page_links":
            selector = optional_str(payload.get("selector"), "selector")
            cursor = optional_str(payload.get("cursor"), "cursor")
            limit = optional_int(payload.get("limit"), "limit", 1, 200)
            return await runtime.page_links(selector, cursor, limit)
        if action == "dom_chunk":
            selector = optional_str(payload.get("selector"), "selector")
            cursor = optional_str(payload.get("cursor"), "cursor")
            limit = optional_int(payload.get("limit"), "limit", 1, 500)
            return await runtime.page_dom_chunk(selector, cursor, limit)
        raise BackendError(CODE_UNSUPPORTED, f"action '{action}' is not supported by the camoufox backend")


def _as_target(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        return {"selector": value}
    if not isinstance(value, dict):
        raise BackendError(CODE_INVALID, "target must be an object or a selector string")
    return value


def _key_parts(key: str) -> Tuple[str, ...]:
    parts = [part for part in key.split("+") if part]
    if key.endswith("+"):
        parts.append("+")
    expanded = []
    for part in parts or [key]:
        expanded.extend(("Control", "Meta") if part == "ControlOrMeta" else (part,))
    return tuple(dict.fromkeys(expanded))


class _ActionBudget:
    def __init__(self, deadline_ms: int):
        self._duration = max(0.05, deadline_ms / 1000.0)
        self._started = time.monotonic()

    def remaining_seconds(self) -> float:
        return self._duration - (time.monotonic() - self._started)

    def remaining_ms(self) -> int:
        return max(0, int(self.remaining_seconds() * 1000))


def _is_playwright_timeout(exc: BaseException) -> bool:
    return ic.is_playwright_timeout(exc)


def _is_playwright_error(exc: BaseException) -> bool:
    return "playwright" in type(exc).__module__


def _is_playwright_target_closed(exc: Optional[BaseException]) -> bool:
    return exc is not None and type(exc).__name__ == "TargetClosedError" and _is_playwright_error(exc)


_TERMINAL_CONTROL_CATEGORIES = ("Cc", "Cf", "Cs")
_TERMINAL_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ERROR_DIAGNOSTIC_MAX_CHARS = 2000
_ERROR_DIAGNOSTIC_TRUNCATION = " [truncated]"
_ERROR_DIAGNOSTIC_FALLBACK = "no diagnostic detail available"
_REDACTED_PAYLOAD_FIELDS = ("text", "value", "values", "key", "script", "expression")
_REDACTION_PLACEHOLDER = "[redacted]"


def _strip_control_characters(text: str) -> str:
    return "".join(
        ch for ch in _TERMINAL_ESCAPE_RE.sub("", text)
        if unicodedata.category(ch) not in _TERMINAL_CONTROL_CATEGORIES
    )


def _payload_redaction_literals(payload: Any) -> List[str]:
    literals: List[str] = []
    seen = set()

    def add_value(value: str) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        candidates = [value]
        for escaped in (
            json.dumps(value, ensure_ascii=False)[1:-1],
            json.dumps(value, ensure_ascii=True)[1:-1],
            repr(value)[1:-1],
            value.encode("unicode_escape").decode("ascii"),
            value.replace("\\", "\\\\").replace("'", "\\'"),
            value.replace("\\", "\\\\").replace('"', '\\"'),
        ):
            if escaped not in candidates:
                candidates.append(escaped)
        literals.extend(candidates)

    stack: List[Tuple[Any, bool]] = [(payload, False)]
    while stack:
        node, collect = stack.pop()
        if isinstance(node, str):
            if collect:
                add_value(node)
        elif isinstance(node, list):
            for item in node:
                stack.append((item, collect))
        elif isinstance(node, dict):
            for key, value in node.items():
                matched = collect or (isinstance(key, str) and key in _REDACTED_PAYLOAD_FIELDS)
                stack.append((value, matched))

    return literals


def _redact_payload_values(text: str, payload: Any) -> str:
    literals = sorted(set(_payload_redaction_literals(payload)), key=len, reverse=True)
    if not literals:
        return text
    patterns = []
    for literal in literals:
        if literal.isspace():
            patterns.extend(re.escape(quote + literal + quote) for quote in ("'", '"', "`"))
        elif len(literal) == 1:
            patterns.append(r"(?<!\w)" + re.escape(literal) + r"(?!\w)")
        else:
            patterns.append(re.escape(literal))
    return re.sub("|".join(patterns), lambda match: _REDACTION_PLACEHOLDER, text)


def _playwright_error_diagnostic(exc: BaseException, payload: Any) -> str:
    """Keep the browser reason, not call-log dumps; redact literal input/code before truncating."""
    message = getattr(exc, "message", None)
    if not isinstance(message, str) or not message.strip():
        try:
            message = str(exc)
        except Exception:
            message = ""
    if not isinstance(message, str):
        message = ""
    message = _redact_payload_values(message, payload)
    line = ""
    for candidate in message.splitlines():
        candidate = _strip_control_characters(candidate).strip()
        if candidate:
            line = candidate
            break
    if not line:
        return _ERROR_DIAGNOSTIC_FALLBACK
    if len(line) > _ERROR_DIAGNOSTIC_MAX_CHARS:
        keep = _ERROR_DIAGNOSTIC_MAX_CHARS - len(_ERROR_DIAGNOSTIC_TRUNCATION)
        line = line[:keep] + _ERROR_DIAGNOSTIC_TRUNCATION
    return line


def _playwright_error_text(action: str, exc: BaseException, payload: Any, *,
                           timed_out: bool = False) -> str:
    outcome = "timed out" if timed_out else "failed"
    action = _strip_control_characters(action)
    return (
        f"browser action '{action}' {outcome}: "
        f"{type(exc).__name__}: {_playwright_error_diagnostic(exc, payload)}"
    )


class LineAssembler:
    OVERSIZED = object()
    EOF = object()

    def __init__(self, limit: int):
        self.limit = limit
        self.buffer = bytearray()
        self.skip = False

    def feed(self, chunk: bytes) -> List[Any]:
        items: List[Any] = []
        data = chunk
        index = 0
        while True:
            newline = data.find(b"\n", index)
            if newline == -1:
                if not self.skip:
                    self.buffer.extend(data[index:])
                    if len(self.buffer) > self.limit:
                        self.buffer.clear()
                        self.skip = True
                        items.append(self.OVERSIZED)
                break
            if not self.skip:
                self.buffer.extend(data[index:newline])
                line = bytes(self.buffer)
                if line.endswith(b"\r"):
                    line = line[:-1]
                items.append(line)
            self.buffer.clear()
            self.skip = False
            index = newline + 1
        return items

    def finish(self) -> List[Any]:
        items: List[Any] = []
        if not self.skip and self.buffer:
            line = bytes(self.buffer)
            if line.endswith(b"\r"):
                line = line[:-1]
            items.append(line)
        self.buffer.clear()
        items.append(self.EOF)
        return items


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="camoufox backend worker")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--motion", default="human-fast", choices=list(ic.MOTIONS))
    return parser.parse_args(argv)


async def run_worker(runtime_dir: Path, motion: str) -> int:
    worker = Worker(runtime_dir, motion)
    worker.configure_runtime_env()
    worker.load_gestures()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def request_stop():
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGTERM, request_stop)
    except (NotImplementedError, RuntimeError):
        signal.signal(signal.SIGTERM, lambda *_args: request_stop())
    try:
        loop.add_signal_handler(signal.SIGINT, request_stop)
    except (NotImplementedError, RuntimeError):
        signal.signal(signal.SIGINT, lambda *_args: request_stop())

    queue: asyncio.Queue = asyncio.Queue()
    assembler = LineAssembler(ic.MAX_INPUT_BYTES)

    def enqueue(items: List[Any]) -> None:
        for item in items:
            queue.put_nowait(item)

    pipe_started = False
    try:
        reader = asyncio.StreamReader(limit=ic.MAX_INPUT_BYTES + 1)
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        pipe_started = True

        async def pump_pipe() -> None:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    enqueue(assembler.finish())
                    return
                enqueue(assembler.feed(chunk))

        asyncio.ensure_future(pump_pipe())
    except (NotImplementedError, ValueError, OSError):
        pipe_started = False

    if not pipe_started:
        def pump_thread() -> None:
            stream = sys.stdin.buffer
            read_chunk = getattr(stream, "read1", None)
            while True:
                try:
                    if read_chunk is not None:
                        chunk = read_chunk(65536)
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                except Exception:
                    chunk = b""
                if not chunk:
                    items = assembler.finish()
                else:
                    items = assembler.feed(chunk)
                for item in items:
                    loop.call_soon_threadsafe(queue.put_nowait, item)
                if not chunk:
                    return

        threading.Thread(target=pump_thread, daemon=True).start()

    try:
        while not stop_event.is_set():
            get_task = asyncio.ensure_future(queue.get())
            stop_task = asyncio.ensure_future(stop_event.wait())
            done, pending = await asyncio.wait({get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if stop_task in done and get_task not in done:
                continue
            item = get_task.result()
            if item is LineAssembler.EOF:
                break
            if item is LineAssembler.OVERSIZED:
                write_response(
                    {
                        "id": "unknown",
                        "success": False,
                        "error": f"request exceeds the {ic.MAX_INPUT_BYTES} byte input limit",
                        "code": CODE_INVALID_REQUEST,
                    }
                )
                continue
            if not item.strip():
                continue
            outcome = await worker.handle_line(item)
            if outcome == "stop":
                stop_event.set()
                break
    finally:
        if worker.runtime is not None:
            try:
                await asyncio.wait_for(worker.runtime.close(), timeout=10)
            except Exception:
                pass
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    runtime_dir = Path(args.runtime_dir).expanduser()
    if not runtime_dir.is_absolute():
        runtime_dir = (Path.cwd() / runtime_dir).resolve()
    else:
        runtime_dir = runtime_dir.resolve()
    try:
        return asyncio.run(run_worker(runtime_dir, args.motion))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"worker startup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
