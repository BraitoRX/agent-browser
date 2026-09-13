from __future__ import annotations

import asyncio
import json
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import input_context as ic
from input_context import (
    BackendError,
    CLOSE_REASON_BROWSER_DISCONNECTED,
    CLOSE_REASON_CONTEXT_CLOSED,
    CODE_ERROR,
    CODE_INVALID,
    CODE_NO_ACTIVE_TAB,
    CODE_NOT_LAUNCHED,
    CODE_SESSION_CLOSED,
    CODE_STALE_CAPTURE,
    CODE_STALE_REF,
    Captures,
    InputJournal,
    MOTION_HUMANIZE,
    TargetSpec,
    css_point,
    iso_now,
    json_safe,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REF_TOKEN_RE = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")


def png_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


class Tab:
    def __init__(self, tab_id: str, page: Any, label: Optional[str] = None):
        self.tab_id = tab_id
        self.page = page
        self.label = label
        self.refs: Set[str] = set()
        self.refs_meta: Dict[str, Dict[str, Any]] = {}
        self.closed = False


class GestureContext:
    def __init__(self, runtime: "CamoufoxRuntime", deadline_ms: int):
        page, tab = runtime.require_active()
        self.runtime = runtime
        self.page = page
        self.tab_id = tab.tab_id
        self.journal = runtime.journal
        self.captures = runtime.captures
        self.motion = runtime.motion
        self._started = time.monotonic()
        self._deadline_s = max(0.05, deadline_ms / 1000.0)
        self._stages: List[Dict[str, Any]] = []
        self._input_dispatched = 0

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def remaining_seconds(self) -> float:
        return self._deadline_s - (time.monotonic() - self._started)

    def remaining_ms(self) -> int:
        return max(0, int(self.remaining_seconds() * 1000))

    def check_deadline(self) -> None:
        if self.remaining_seconds() <= 0:
            raise BackendError(ic.CODE_TIMEOUT, "action deadline exceeded", deadline_exceeded=True)

    def set_stage(self, name: str) -> None:
        self._stages.append({"stage": name, "atMs": self.elapsed_ms()})

    def note_input_dispatched(self) -> None:
        self._input_dispatched += 1
        self.runtime.note_input()

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "stages": list(self._stages),
            "elapsedMs": self.elapsed_ms(),
            "inputDispatched": self._input_dispatched > 0,
            "inputDispatchCount": self._input_dispatched,
            "semanticSuccess": "not_asserted",
            "motion": self.motion,
        }

    def require_exposed_ref(self, ref: str) -> None:
        self.runtime.require_exposed_ref(ref)

    async def require_capture(self, capture_id: str) -> Dict[str, Any]:
        return await self.runtime.require_capture(capture_id)

    async def capture_identity(self) -> Dict[str, Any]:
        return await self.runtime.capture_identity()

    async def viewport_size(self) -> Dict[str, float]:
        return await self.runtime.viewport_size()

    async def resolve_capture_point(self, spec: TargetSpec, capture_id_used: Optional[str]) -> Tuple[float, float, str]:
        return await self.runtime.resolve_capture_point(spec, capture_id_used)


class CamoufoxRuntime:
    engine = "camoufox"
    GestureContextClass = GestureContext

    def __init__(self, runtime_dir: Path, motion: str):
        self.runtime_dir = Path(runtime_dir)
        self.motion = motion
        self.humanize = MOTION_HUMANIZE[motion]
        self.captures = Captures()
        self.journal = InputJournal()
        self._camoufox: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.tabs: Dict[str, Tab] = {}
        self._page_to_tab: Dict[int, str] = {}
        self._tab_counter = 0
        self.active_id: Optional[str] = None
        self.launched = False
        self.headless: Optional[bool] = None
        self._closing = False
        self._input_attempts = 0
        self._close_reason: Optional[str] = None

    async def launch(self, headless: bool) -> Dict[str, Any]:
        error = self.sync_session_state()
        if error is not None:
            raise error
        if self._is_live():
            if self.headless != headless:
                raise BackendError(
                    CODE_ERROR,
                    "browser is already running with a different headless setting; close the session first",
                )
            return self.launch_info()
        try:
            record = json.loads((self.runtime_dir / "runtime.json").read_text("utf-8"))
            executable = Path(record["browser"]["executablePath"])
            if not executable.is_absolute():
                raise ValueError("installed browser executable must be absolute")
            resolved_executable = executable.resolve()
            resolved_executable.relative_to((self.runtime_dir / "home").resolve())
            if not executable.is_file():
                raise ValueError("installed browser executable is missing")
            asset_anchor = executable
            if (resolved_executable.parent.name == "MacOS"
                    and resolved_executable.parent.parent.name == "Contents"
                    and resolved_executable.parent.parent.parent.suffix == ".app"):
                resources = resolved_executable.parent.parent / "Resources"
                if not (resources / "properties.json").is_file():
                    raise ValueError("macOS bundle resources are missing")
                asset_anchor = resources / resolved_executable.name
            from importlib.metadata import version
            if version("camoufox") != "0.5.6" or version("playwright") != "1.61.0":
                raise ValueError("runtime package pins do not match")
            from camoufox.async_api import AsyncCamoufox
            from camoufox.addons import DefaultAddons
            from camoufox.utils import launch_options
        except Exception as exc:
            raise BackendError(
                CODE_NOT_LAUNCHED,
                "camoufox runtime is missing or incompatible; run agent-browser --engine camoufox install "
                f"(runtime error: {type(exc).__name__})",
            ) from exc
        self._closing = False
        try:
            # Camoufox 0.5.6 resolves assets beside executable_path. macOS stores them in Resources.
            options = await asyncio.to_thread(
                launch_options,
                headless=headless,
                humanize=self.humanize,
                enable_cache=True,
                executable_path=str(asset_anchor),
                exclude_addons=list(DefaultAddons),
            )
            options["executable_path"] = str(executable)
            instance = AsyncCamoufox(from_options=options, persistent_context=False)
            self._camoufox = instance
            browser = await instance.__aenter__()
            self.browser = browser
            self.context = await browser.new_context()
            self._close_reason = None
            browser.on("disconnected", self._on_browser_disconnected)
            self.context.on("close", self._on_context_closed)
            self.context.on("page", self._on_context_page)
            self.launched = True
            self.headless = headless
            page = await self.context.new_page()
            self.active_id = self._register_tab(page, label=None).tab_id
        except BaseException:
            await self.close()
            raise
        return self.launch_info()

    def _is_live(self) -> bool:
        self.sync_session_state()
        if self._close_reason is not None or self._closing:
            return False
        return (
            self.launched
            and self.browser is not None
            and self.context is not None
            and self._camoufox is not None
        )

    def session_closed_error(self) -> Optional[BackendError]:
        if self._close_reason is None:
            return None
        return BackendError(
            CODE_SESSION_CLOSED,
            f"browser session ended ({self._close_reason}); close the session, then open a new one "
            "before continuing (no implicit relaunch)",
        )

    def _observed_close_reason(self) -> Optional[str]:
        browser = self.browser
        if browser is not None and not browser.is_connected():
            return CLOSE_REASON_BROWSER_DISCONNECTED
        context = self.context
        if context is not None and (
            context.is_closed()
            or (browser is not None and context not in browser.contexts)
        ):
            return CLOSE_REASON_CONTEXT_CLOSED
        return None

    def sync_session_state(self) -> Optional[BackendError]:
        """Reconcile public liveness signals without restarting or discarding input evidence."""
        if self.launched and not self._closing:
            reason = self._observed_close_reason()
            if reason is not None:
                self._mark_session_dead(reason)
        return self.session_closed_error()

    def _mark_session_dead(self, reason: str) -> None:
        if self._close_reason in (reason, CLOSE_REASON_BROWSER_DISCONNECTED):
            return
        # Context-close can precede disconnect during browser shutdown. Upgrade
        # the diagnostic, but never clear the explicit-close requirement here.
        self._close_reason = reason
        self.active_id = None
        for tab in self.tabs.values():
            tab.closed = True
            tab.refs.clear()
            tab.refs_meta.clear()
        self.captures.invalidate_all()

    def require_open_session(self) -> None:
        error = self.sync_session_state()
        if error is not None:
            raise error

    def _on_browser_disconnected(self, browser: Any) -> None:
        if self._closing or browser is not self.browser:
            return
        self._mark_session_dead(CLOSE_REASON_BROWSER_DISCONNECTED)

    def _on_context_closed(self, context: Any) -> None:
        if self._closing or context is not self.context:
            return
        self._mark_session_dead(CLOSE_REASON_CONTEXT_CLOSED)

    def launch_info(self) -> Dict[str, Any]:
        self.sync_session_state()
        return {
            "launched": self._is_live(),
            "browserConnected": self.browser_connected(),
            "recoveryRequired": self.recovery_required(),
            "closeReason": self._close_reason,
            "engine": self.engine,
            "headless": self.headless,
            "motion": self.motion,
            "humanize": self.humanize,
            "runtimeDir": str(self.runtime_dir),
        }

    def browser_connected(self) -> bool:
        browser = self.browser
        return browser is not None and browser.is_connected()

    def recovery_required(self) -> bool:
        return self._close_reason is not None

    async def close(self) -> Dict[str, Any]:
        if self._closing:
            return {"closed": True, "alreadyClosing": True}
        self._closing = True
        try:
            await asyncio.wait_for(self.release_inputs(), timeout=2)
        except Exception:
            pass
        context = self.context
        self.context = None
        if context is not None:
            try:
                await asyncio.wait_for(context.close(), timeout=5)
            except Exception:
                pass
        instance = self._camoufox
        self._camoufox = None
        if instance is not None:
            try:
                await asyncio.wait_for(instance.__aexit__(None, None, None), timeout=8)
            except Exception:
                pass
        self.browser = None
        self.launched = False
        self.active_id = None
        self.tabs.clear()
        self._page_to_tab.clear()
        self.journal.clear()
        self.captures.invalidate_all()
        self._close_reason = None
        return {"closed": True}

    async def release_inputs(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"released": True, "buttons": [], "keys": []}
        page = self._active_page_or_none()
        if page is None:
            result["released"] = not (self.journal.pending_buttons() or self.journal.pending_keys())
            return result
        for button in self.journal.pending_buttons():
            try:
                await asyncio.wait_for(page.mouse.up(button=button), timeout=1.5)
                self.journal.finish_button_up(button)
                result["buttons"].append(button)
            except Exception:
                result["released"] = False
        for key in self.journal.pending_keys():
            try:
                await asyncio.wait_for(page.keyboard.up(key), timeout=1.5)
                self.journal.finish_key_up(key)
                result["keys"].append(key)
            except Exception:
                result["released"] = False
        return result

    def _register_tab(self, page: Any, label: Optional[str]) -> Tab:
        existing_id = self._page_to_tab.get(id(page))
        if existing_id is not None:
            tab = self.tabs[existing_id]
            if label is not None:
                tab.label = label
            return tab
        self._tab_counter += 1
        tab = Tab(f"t{self._tab_counter}", page, label=label)
        self.tabs[tab.tab_id] = tab
        self._page_to_tab[id(page)] = tab.tab_id
        page.on("framenavigated", lambda _frame, tab_id=tab.tab_id: self._clear_refs(tab_id))
        page.on("close", lambda closed_page, tab_id=tab.tab_id: self._on_page_closed(tab_id, closed_page))
        return tab

    def _on_context_page(self, page: Any) -> None:
        if self._close_reason is not None or self._closing:
            return
        if id(page) in self._page_to_tab:
            return
        self._register_tab(page, label=None)

    def _on_page_closed(self, tab_id: str, page: Any = None) -> None:
        if self._closing:
            return
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        if page is not None and page is not tab.page:
            return
        tab.closed = True
        self.captures.invalidate_all()
        self._clear_refs(tab_id)
        if self.active_id == tab_id:
            self.active_id = None

    def _clear_refs(self, tab_id: str) -> None:
        self.captures.invalidate_all()
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        tab.refs.clear()
        tab.refs_meta.clear()

    def find_tab(self, ident: Optional[str]) -> Optional[Tab]:
        if ident is None:
            return None
        if ident in self.tabs:
            return self.tabs[ident]
        for tab in self.tabs.values():
            if tab.label is not None and tab.label == ident:
                return tab
        return None

    def _page_closed_evidence(self, page: Any) -> bool:
        return page.is_closed()

    def _reconcile_tab(self, tab: Tab) -> bool:
        if tab.closed:
            return True
        if not self._page_closed_evidence(tab.page):
            return False
        tab.closed = True
        self._clear_refs(tab.tab_id)
        if self.active_id == tab.tab_id:
            self.active_id = None
        return True

    def reconcile_session(self) -> None:
        self.sync_session_state()
        for tab in self.tabs.values():
            self._reconcile_tab(tab)

    def require_tab(self, ident: Optional[str], *, live: bool = True) -> Tab:
        if live:
            self.require_open_session()
        tab = self.find_tab(ident)
        if tab is None:
            raise BackendError(CODE_INVALID, "unknown tab id or label")
        closed = self._reconcile_tab(tab)
        if live and closed:
            raise BackendError(CODE_INVALID, f"tab {tab.tab_id} is closed")
        return tab

    def require_active(self) -> Tuple[Any, Tab]:
        self.require_open_session()
        if not self._is_live():
            raise BackendError(CODE_NOT_LAUNCHED, "browser is not launched; send a launch command first")
        self.reconcile_session()
        tab = self.tabs.get(self.active_id) if self.active_id else None
        if tab is None or tab.closed:
            raise BackendError(
                CODE_NO_ACTIVE_TAB,
                "no active tab; call tab_switch or tab_new (tab_list, tab_close, and session_info still work)",
            )
        return tab.page, tab

    def _active_page_or_none(self) -> Optional[Any]:
        self.reconcile_session()
        tab = self.tabs.get(self.active_id) if self.active_id else None
        if tab is None or tab.closed:
            return None
        return tab.page

    def active_page(self) -> Any:
        page, _tab = self.require_active()
        return page

    def page_keyboard(self) -> Any:
        return self.active_page().keyboard

    def page_waiter(self) -> Any:
        return self.active_page()

    def page_url(self) -> str:
        return self.active_page().url

    def note_input(self) -> None:
        self._input_attempts += 1

    def list_tabs(self) -> Dict[str, Any]:
        self.reconcile_session()
        return {
            "tabs": [
                {
                    "tabId": tab.tab_id,
                    "label": tab.label,
                    "url": None if tab.closed else tab.page.url,
                    "active": tab.tab_id == self.active_id,
                    "closed": tab.closed,
                }
                for tab in self.tabs.values()
            ],
            "activeId": self.active_id,
        }

    async def new_tab(self, url: Optional[str], label: Optional[str]) -> Dict[str, Any]:
        self.require_open_session()
        if not self._is_live():
            raise BackendError(CODE_NOT_LAUNCHED, "browser is not launched; send a launch command first")
        if label is not None and self.find_tab(label) is not None:
            raise BackendError(CODE_INVALID, "tab label is already in use")
        page = await self.context.new_page()
        tab = self._register_tab(page, label=label)
        self.active_id = tab.tab_id
        if url is not None:
            await page.goto(url, wait_until="load")
        return {
            "tabId": tab.tab_id,
            "label": tab.label,
            "url": page.url,
            "active": True,
            "tabs": self.list_tabs(),
        }

    def switch_tab(self, ident: Optional[str]) -> Dict[str, Any]:
        self.require_open_session()
        if not self._is_live():
            raise BackendError(CODE_NOT_LAUNCHED, "browser is not launched; send a launch command first")
        if ident is None:
            raise BackendError(CODE_INVALID, "'tabId' is required (stable tab id such as t1 or a label)")
        tab = self.require_tab(ident, live=True)
        self.captures.invalidate_all()
        self.active_id = tab.tab_id
        return {"tabId": tab.tab_id, "label": tab.label, "url": tab.page.url, "active": True}

    async def close_tab(self, ident: Optional[str]) -> Dict[str, Any]:
        self.require_open_session()
        if not self._is_live():
            raise BackendError(CODE_NOT_LAUNCHED, "browser is not launched; send a launch command first")
        target = ident if ident is not None else self.active_id
        if target is None:
            raise BackendError(CODE_NO_ACTIVE_TAB, "no active tab to close")
        tab = self.require_tab(target, live=True)
        was_active = tab.tab_id == self.active_id
        try:
            await asyncio.wait_for(tab.page.close(), timeout=5)
        except Exception as exc:
            raise BackendError(CODE_ERROR, f"failed to close tab: {type(exc).__name__}") from exc
        tab.closed = True
        if was_active:
            self.active_id = None
        self.captures.invalidate_all()
        return {
            "tabId": tab.tab_id,
            "closed": True,
            "activeId": self.active_id,
            "adoptedTab": None,
        }

    async def navigate(self, url: str, wait_until: Optional[str]) -> Dict[str, Any]:
        page, _tab = self.require_active()
        self.captures.invalidate_all()
        await page.goto(url, wait_until=wait_until or "load")
        return {"url": page.url, "title": await page.title()}

    async def go_history(self, direction: str) -> Dict[str, Any]:
        page, _tab = self.require_active()
        self.captures.invalidate_all()
        if direction == "back":
            await page.go_back()
        elif direction == "forward":
            await page.go_forward()
        else:
            await page.reload()
        return {"url": page.url, "title": await page.title()}

    async def current_url(self) -> Dict[str, Any]:
        page, _tab = self.require_active()
        return {"url": page.url}

    async def current_title(self) -> Dict[str, Any]:
        page, _tab = self.require_active()
        return {"title": await page.title()}

    async def content(self) -> Dict[str, Any]:
        page, _tab = self.require_active()
        return {"content": await page.content()}

    async def evaluate(self, script: str) -> Dict[str, Any]:
        page, _tab = self.require_active()
        self.captures.invalidate_all()
        result = await page.evaluate(script)
        return {"result": json_safe(result)}

    async def read(self) -> Dict[str, Any]:
        page, _tab = self.require_active()
        text = await page.evaluate("() => document.body ? (document.body.innerText || '') : ''")
        return {"content": text, "url": page.url, "title": await page.title(), "source": "rendered"}

    @staticmethod
    def extract_refs(snapshot_text: str) -> Dict[str, Dict[str, Any]]:
        refs: Dict[str, Dict[str, Any]] = {}
        for line in snapshot_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            remainder = stripped[2:]
            role_match = re.match(r"[\w-]+", remainder)
            if role_match is None:
                continue
            role = role_match.group(0)
            rest = remainder[len(role):].lstrip()
            name = None
            if rest.startswith('"'):
                try:
                    name, end = json.JSONDecoder().raw_decode(rest)
                except ValueError:
                    continue
                rest = rest[end:].lstrip()
            annotations = rest.split(":", 1)[0]
            for match in REF_TOKEN_RE.finditer(annotations):
                ref = match.group(1)
                ref_match = ic.REF_NAME_RE.match(ref)
                meta = {"role": role, "name": name}
                meta["framePrefix"] = ref_match.group(1) if ref_match else None
                refs[ref] = meta
        return refs

    async def snapshot(
        self,
        *,
        selector: Optional[str],
        max_depth: Optional[int],
    ) -> Dict[str, Any]:
        page, tab = self.require_active()
        kwargs: Dict[str, Any] = {"mode": "ai"}
        if max_depth is not None:
            kwargs["depth"] = max_depth
        if selector is not None:
            spec = ic.parse_selector(selector, "selector")
            scope, resolved = self.locator_scope(spec)
            text = await scope.locator(resolved).aria_snapshot(**kwargs)
        else:
            text = await page.aria_snapshot(**kwargs)
        refs_meta = self.extract_refs(text)
        tab.refs = set(refs_meta.keys())
        tab.refs_meta = refs_meta
        return {
            "snapshot": text,
            "refs": refs_meta,
            "refCount": len(refs_meta),
            "url": page.url,
            "tabId": tab.tab_id,
        }

    def locator_scope(self, spec: TargetSpec) -> Tuple[Any, str]:
        page, tab = self.require_active()
        if spec.ref is not None:
            if spec.ref not in tab.refs:
                raise BackendError(
                    CODE_STALE_REF,
                    f"ref '@{spec.ref}' was not exposed by the latest snapshot of tab {tab.tab_id}; "
                    "take a fresh snapshot",
                )
            return page, f"aria-ref={spec.ref}"
        if spec.selector is None:
            raise BackendError(CODE_INVALID, "selector is required")
        return page, spec.selector

    def require_exposed_ref(self, ref: str) -> None:
        _page, tab = self.require_active()
        if ref not in tab.refs:
            raise BackendError(
                CODE_STALE_REF,
                f"ref '@{ref}' was not exposed by the latest snapshot of tab {tab.tab_id}; "
                "take a fresh snapshot",
            )

    def _screenshots_dir(self) -> Path:
        return self.runtime_dir / "screenshots"

    async def capture_identity(self) -> Dict[str, Any]:
        page, tab = self.require_active()
        metrics = await page.evaluate(
            "() => ({x: window.scrollX, y: window.scrollY, w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio})"
        )
        return {
            "url": page.url,
            "tabId": tab.tab_id,
            "scrollX": float(metrics.get("x") or 0),
            "scrollY": float(metrics.get("y") or 0),
            "viewportWidth": float(metrics.get("w") or 0),
            "viewportHeight": float(metrics.get("h") or 0),
            "devicePixelRatio": float(metrics.get("dpr") or 1),
        }

    async def viewport_size(self) -> Dict[str, float]:
        # Camoufox may disable Playwright's fixed viewport to preserve its fingerprint dimensions.
        identity = await self.capture_identity()
        width, height = identity["viewportWidth"], identity["viewportHeight"]
        if width <= 0 or height <= 0:
            raise BackendError(CODE_INVALID, "the current viewport could not be measured")
        return {"width": width, "height": height}

    async def require_capture(self, capture_id: str) -> Dict[str, Any]:
        capture = self.captures.get(capture_id)
        if capture is None:
            raise BackendError(
                CODE_STALE_CAPTURE,
                "screenshot capture is missing, expired, or invalidated; take a new screenshot",
            )
        identity = await self.capture_identity()
        changed = [
            field
            for field in ("url", "tabId", "scrollX", "scrollY", "viewportWidth", "viewportHeight", "devicePixelRatio")
            if capture.get(field) != identity.get(field)
        ]
        if changed:
            self.captures.invalidate(capture_id)
            raise BackendError(
                CODE_STALE_CAPTURE,
                "screenshot capture is stale because " + ", ".join(changed) + " changed; take a new screenshot",
            )
        return capture

    async def resolve_capture_point(self, spec: TargetSpec, capture_id_used: Optional[str]) -> Tuple[float, float, str]:
        if spec.capture_id is None:
            raise BackendError(CODE_INVALID, "coordinate target is missing captureId")
        if capture_id_used is not None and spec.capture_id != capture_id_used:
            raise BackendError(CODE_INVALID, "both endpoints must use the same captureId")
        capture = await self.require_capture(spec.capture_id)
        x, y = css_point(capture, spec)
        return x, y, spec.capture_id

    async def screenshot(
        self,
        *,
        path: Optional[str],
        screenshot_dir: Optional[str],
    ) -> Dict[str, Any]:
        page, tab = self.require_active()
        before = await self.capture_identity()
        try:
            data = await page.screenshot(scale="css")
        except TypeError as exc:
            raise BackendError(
                CODE_ERROR,
                "installed Playwright does not support CSS-scale screenshots; "
                "re-run bootstrap.py with the pinned playwright version",
            ) from exc
        if not isinstance(data, (bytes, bytearray)):
            raise BackendError(CODE_ERROR, "screenshot did not return PNG bytes")
        dimensions = png_dimensions(bytes(data))
        if dimensions is None:
            raise BackendError(CODE_ERROR, "screenshot bytes are not a valid PNG")
        image_width, image_height = dimensions
        identity = await self.capture_identity()
        if identity != before:
            raise BackendError(CODE_STALE_CAPTURE, "page moved during screenshot; take a fresh screenshot")
        viewport_width = identity["viewportWidth"]
        viewport_height = identity["viewportHeight"]
        if abs(image_width - viewport_width) > 1 or abs(image_height - viewport_height) > 1:
            raise BackendError(CODE_STALE_CAPTURE, "screenshot dimensions do not match the CSS viewport")
        device_pixel_ratio = identity["devicePixelRatio"]
        self.captures.invalidate_all()
        capture_id = self.captures.register(
            {
                "imageWidth": image_width,
                "imageHeight": image_height,
                "viewportWidth": viewport_width,
                "viewportHeight": viewport_height,
                "devicePixelRatio": device_pixel_ratio,
                "scrollX": identity["scrollX"],
                "scrollY": identity["scrollY"],
                "url": identity["url"],
                "tabId": tab.tab_id,
                "capturedAt": iso_now(),
            }
        )
        if path is not None:
            destination = Path(path).expanduser()
        elif screenshot_dir is not None:
            destination = Path(screenshot_dir).expanduser() / f"{uuid.uuid4()}.png"
        else:
            destination = self._screenshots_dir() / f"{uuid.uuid4()}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bytes(data))
        capture = self.captures.get(capture_id) or {}
        return {
            "path": str(destination),
            "format": "png",
            "scale": "css",
            "visualCapture": {
                key: capture.get(key)
                for key in (
                    "captureId",
                    "imageWidth",
                    "imageHeight",
                    "viewportWidth",
                    "viewportHeight",
                    "devicePixelRatio",
                    "scrollX",
                    "scrollY",
                    "url",
                    "capturedAt",
                )
            },
        }

    def session_info(self, registry_names: List[str]) -> Dict[str, Any]:
        self.reconcile_session()
        pins = None
        runtime_json = self.runtime_dir / "runtime.json"
        try:
            if runtime_json.is_file():
                raw = json.loads(runtime_json.read_text("utf-8"))
                pins = {"pins": raw.get("pins"), "browser": raw.get("browser")}
        except Exception:
            pins = None
        return {
            "engine": self.engine,
            "launched": self._is_live(),
            "browserConnected": self.browser_connected(),
            "recoveryRequired": self.recovery_required(),
            "closeReason": self._close_reason,
            "headless": self.headless,
            "motion": self.motion,
            "humanize": self.humanize,
            "activeTab": self.active_id,
            "tabs": self.list_tabs(),
            "capabilities": {
                "snapshotFormat": "aria-ai",
                "refs": "aria-ref (element-backed, per-tab, cleared on navigation and replaced by new snapshots)",
                "screenshots": {"format": "png", "scale": "css", "captureTtlSeconds": ic.CAPTURE_TTL_SECONDS},
                "gestures": sorted(registry_names),
                "holdMaxMs": ic.HOLD_MAX_MS,
                "actionDeadlineMs": ic.DEFAULT_ACTION_DEADLINE_MS,
                "maxActionDeadlineMs": ic.MAX_ACTION_DEADLINE_MS,
                "coordinates": True,
                "iframes": "native aria-ref routing via prefixed refs (fNeN)",
            },
            "runtimeDir": str(self.runtime_dir),
            "buildMetadata": pins,
        }
