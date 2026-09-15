from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from input_context import (
    BackendError,
    CODE_ERROR,
    CODE_INVALID,
    CODE_NOT_LAUNCHED,
)

CAMOUFOX_WM_CLASS = ("Navigator", "camoufox")
LARGE_WINDOW_MIN_WIDTH = 500
LARGE_WINDOW_MIN_HEIGHT = 400
GEOMETRY_TOLERANCE_PX = 1
NOTCH_CSS_PIXELS = 100.0
SCALE_MIN = 0.2
SCALE_MAX = 5.0
PATH_STEP_SLEEP_SECONDS = 0.02
HUMANIZE_LONG_DISTANCE_PX = 400.0
HUMANIZE_LONG_STEP_COUNT = 24
HUMANIZE_SHORT_STEP_COUNT = 4
HUMANIZE_LONG_SHORT_STEP_COUNT = 8
PATH_JITTER_PX = 2.0
WINDOW_WAIT_TOTAL_SECONDS = 10.0
WINDOW_WAIT_INTERVAL_SECONDS = 0.1

BUTTON_TO_X_DETAIL = {"left": 1, "middle": 2, "right": 3}
WHEEL_BUTTON_UP = 4
WHEEL_BUTTON_DOWN = 5
WHEEL_BUTTON_LEFT = 6
WHEEL_BUTTON_RIGHT = 7
SUPPORTED_KEYMAP_INDEX = 1

LATIN_KEYSYMS = [
    "aacute", "eacute", "iacute", "oacute", "uacute",
    "ntilde", "udiaeresis", "ccedilla",
    "questiondown", "exclamdown", "euro",
]
LATIN_KEYMAP_APPLIED = "latin-remap"

NAMED_KEYSYMS: Dict[str, str] = {
    "Enter": "Return",
    "Backspace": "BackSpace",
    "PageUp": "Prior",
    "PageDown": "Next",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    "Meta": "Super_L",
}
MODIFIER_KEYSYMS: Dict[str, str] = {
    "Control": "Control_L",
    "Shift": "Shift_L",
    "Alt": "Alt_L",
    "Meta": "Super_L",
    "ControlOrMeta": "Control_L",
}
CHARACTER_KEYSYMS: Dict[str, str] = {"\n": "Return", "\r": "Return", "\t": "Tab"}
SHIFT_KEYSYM_NAME = "Shift_L"
for _function_index in range(1, 25):
    NAMED_KEYSYMS[f"F{_function_index}"] = f"F{_function_index}"
for _named in ("Tab", "Escape", "Delete", "Home", "End", "Insert"):
    NAMED_KEYSYMS.setdefault(_named, _named)


def _keysym_for(name: str) -> int:
    from Xlib import XK

    character_name = CHARACTER_KEYSYMS.get(name)
    if character_name is not None:
        return int(XK.string_to_keysym(character_name) or 0)
    if len(name) == 1:
        return ord(name)
    return int(XK.string_to_keysym(name) or 0)


class OsnativePointer:
    def __init__(self, display_name: str):
        from Xlib import X
        from Xlib.ext import xtest
        from Xlib.display import Display

        self.X = X
        self.display = Display(display_name)
        self.root = self.display.screen().root
        self.xtest = xtest
        if not self.display.has_extension(xtest.extname):
            self.display.close()
            raise BackendError(
                CODE_NOT_LAUNCHED,
                "the X server on DISPLAY does not provide the XTEST extension; "
                "the os-native input backend cannot dispatch input",
            )
        self.latin_keymap_status = self.ensure_latin_keymap()
        if self.latin_keymap_status is not None:
            self.display.flush()

    def move(self, x: float, y: float) -> None:
        self.xtest.fake_input(
            self.display, self.X.MotionNotify,
            x=max(0, int(round(x))), y=max(0, int(round(y))),
        )
        self.display.flush()

    def ensure_latin_keymap(self) -> Optional[str]:
        """Bind missing Latin keysyms to unused keycodes.

        The stock Xvfb keymap has no bindings for accented characters, so
        XTEST cannot emit them before this remap. Assigns each unbound Latin
        keysym to one fully-empty keycode, leaving the existing layout intact.
        Returns a diagnostic marker when a remap was applied.
        """
        from Xlib import XK

        missing: List[Tuple[str, int]] = []
        for name in LATIN_KEYSYMS:
            keysym = int(getattr(XK, f"XK_{name}", 0) or 0)
            if keysym and not self.display.keysym_to_keycode(keysym):
                missing.append((name, keysym))
        if not missing:
            return None
        info = self.display.display.info
        first = info.min_keycode
        count = info.max_keycode - info.min_keycode + 1
        mapping = self.display.get_keyboard_mapping(first, count)
        empty_keycodes = [
            index + first
            for index, per_keycode in enumerate(mapping)
            if all(not keysym for keysym in per_keycode)
        ]
        if len(empty_keycodes) < len(missing):
            raise BackendError(
                CODE_ERROR,
                "os-native input cannot provide a Latin keymap: not enough free "
                "X keycodes to bind accented characters",
            )
        try:
            for (_, keysym), keycode in zip(missing, empty_keycodes):
                self.display.change_keyboard_mapping(
                    first_keycode=keycode,
                    keysyms=[(keysym,)],
                )
        except Exception as exc:
            raise BackendError(
                CODE_ERROR,
                f"os-native input could not apply the Latin keymap remap: {type(exc).__name__}",
            ) from exc
        self.display.flush()
        self.display._update_keymap(first, count)
        assigned = {keycode for (_, _), keycode in zip(missing, empty_keycodes)}
        for name, keysym in missing:
            keycode = self.display.keysym_to_keycode(keysym)
            if not keycode or keycode not in assigned:
                raise BackendError(
                    CODE_ERROR,
                    f"os-native input Latin keymap remap did not bind '{name}'",
                )
        return LATIN_KEYMAP_APPLIED

    def button(self, detail: int, press: bool) -> None:
        event_type = self.X.ButtonPress if press else self.X.ButtonRelease
        self.xtest.fake_input(self.display, event_type, detail=detail)
        self.display.flush()

    def wheel_notch(self, button: int) -> None:
        self.xtest.fake_input(self.display, self.X.ButtonPress, detail=button)
        self.xtest.fake_input(self.display, self.X.ButtonRelease, detail=button)
        self.display.flush()

    def key(self, keycode: int, press: bool) -> None:
        event_type = self.X.KeyPress if press else self.X.KeyRelease
        self.xtest.fake_input(self.display, event_type, detail=keycode)
        self.display.flush()

    def focus_window(self, window_id: int) -> None:
        window = self.display.create_resource_object("window", window_id)
        window.set_input_focus(self.X.RevertToPointerRoot, self.X.CurrentTime)
        self.display.flush()

    def pointer_position(self) -> Tuple[int, int]:
        pointer = self.root.query_pointer()
        return int(pointer.root_x), int(pointer.root_y)

    def _camoufox_window(self, window: Any, root_id: int) -> Optional[Dict[str, int]]:
        geometry = window.get_geometry()
        x, y = 0, 0
        top_level = window
        current: Optional[Any] = window
        while current is not None and current.id != root_id:
            parent = current.query_tree().parent
            if parent is None:
                break
            offset = current.get_geometry()
            x += offset.x
            y += offset.y
            top_level = current
            current = parent
        return {
            "x": int(x),
            "y": int(y),
            "width": int(geometry.width),
            "height": int(geometry.height),
            "focusId": int(top_level.id),
        }

    def camoufox_windows(self) -> List[Dict[str, int]]:
        root_id = self.root.id
        matches: List[Dict[str, int]] = []
        stack = [self.root]
        while stack:
            window = stack.pop()
            try:
                attributes = window.get_attributes()
                children = window.query_tree().children
            except Exception:
                continue
            if attributes.map_state == self.X.IsViewable:
                try:
                    window_class = window.get_wm_class()
                except Exception:
                    window_class = None
                if window_class:
                    normalized = tuple(
                        item.decode("utf-8", "ignore") if isinstance(item, bytes) else item
                        for item in window_class
                    )
                    if normalized == CAMOUFOX_WM_CLASS:
                        matches.append(self._camoufox_window(window, root_id))
            stack.extend(children)
        return matches

    def large_window_count(self) -> int:
        total = 0
        stack = [self.root]
        while stack:
            window = stack.pop()
            try:
                attributes = window.get_attributes()
                geometry = window.get_geometry()
            except Exception:
                continue
            if (attributes.map_state == self.X.IsViewable
                    and geometry.width >= LARGE_WINDOW_MIN_WIDTH
                    and geometry.height >= LARGE_WINDOW_MIN_HEIGHT):
                total += 1
            stack.extend(window.query_tree().children)
        return total

    def close(self) -> None:
        try:
            self.display.close()
        except Exception:
            pass


class OsnativeGeometry:
    def __init__(self, window: Dict[str, int], large_windows: int):
        self.window = window
        self.large_windows = large_windows

    def mismatch_reason(self, pointer: OsnativePointer) -> Optional[str]:
        windows = pointer.camoufox_windows()
        if not windows:
            return "the Camoufox browser window is no longer mapped on the X display"
        if len(windows) > 1:
            return (
                f"{len(windows)} Camoufox windows are mapped on the X display; "
                "input would be ambiguous; close the extra windows and reobserve"
            )
        if pointer.large_window_count() > self.large_windows:
            return (
                "another large window appeared on the X display; "
                "input would be ambiguous; close it and reobserve"
            )
        current = windows[0]
        for field in ("x", "y", "width", "height"):
            if abs(current[field] - self.window[field]) > GEOMETRY_TOLERANCE_PX:
                return (
                    f"the Camoufox browser window changed on the X display ({field} "
                    f"{current[field]} instead of {self.window[field]}); input was not "
                    "dispatched; reobserve and take a fresh screenshot before continuing"
                )
        return None


class OsnativeInputDispatch:
    osnative = True

    def __init__(self, page: Any, pointer: OsnativePointer, geometry: OsnativeGeometry,
                 humanize: Any):
        self.page = page
        self.pointer = pointer
        self.geometry = geometry
        self.humanize = humanize
        self._latched_metrics: Optional[Tuple[float, float]] = None

    async def _guard(self) -> None:
        reason = await asyncio.to_thread(self.geometry.mismatch_reason, self.pointer)
        if reason is not None:
            raise BackendError(
                CODE_INVALID,
                f"os-native input refused: {reason}",
            )

    async def _focus(self) -> None:
        await asyncio.to_thread(self.pointer.focus_window, self.geometry.window["focusId"])

    async def _calibrate(self) -> Tuple[float, float]:
        if self._latched_metrics is not None:
            return self._latched_metrics
        metrics = await self.page.evaluate(
            "() => ({w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio})"
        )
        if not isinstance(metrics, dict):
            raise BackendError(CODE_ERROR, "os-native input could not read viewport metrics")
        width = float(metrics.get("w") or 0)
        height = float(metrics.get("h") or 0)
        if width <= 0 or height <= 0:
            raise BackendError(CODE_ERROR, "os-native input could not read viewport metrics")
        self._latched_metrics = (width, height)
        return self._latched_metrics

    def _screen_point(self, css_x: float, css_y: float, window: Dict[str, int],
                      inner_width: float, inner_height: float) -> Tuple[float, float]:
        scale = window["width"] / inner_width
        if not SCALE_MIN <= scale <= SCALE_MAX:
            raise BackendError(
                CODE_INVALID,
                f"os-native input refused: window/viewport scale {scale:.3f} is outside the "
                f"supported {SCALE_MIN}..{SCALE_MAX} range",
            )
        origin_x = window["x"]
        origin_y = window["y"] + window["height"] - inner_height * scale
        screen_x = origin_x + css_x * scale
        screen_y = origin_y + css_y * scale
        if not (window["x"] <= screen_x < window["x"] + window["width"]
                and window["y"] <= screen_y < window["y"] + window["height"]):
            raise BackendError(
                CODE_INVALID,
                f"os-native input refused: target ({css_x:.1f}, {css_y:.1f}) maps outside "
                "the browser window; input was not dispatched; reobserve before continuing",
            )
        return screen_x, screen_y

    def _step_count(self, steps: Optional[int], distance: float) -> int:
        if self.humanize:
            if distance > HUMANIZE_LONG_DISTANCE_PX:
                return HUMANIZE_LONG_STEP_COUNT
            if distance > 0:
                return HUMANIZE_LONG_SHORT_STEP_COUNT
            return HUMANIZE_SHORT_STEP_COUNT
        if steps is not None and steps > 1:
            return steps
        return 0

    async def move(self, x: float, y: float, steps: Optional[int] = None) -> None:
        await self._guard()
        inner_width, inner_height = await self._calibrate()
        window = self.geometry.window
        scale = window["width"] / inner_width
        origin_x = window["x"]
        origin_y = window["y"] + window["height"] - inner_height * scale
        current_x, current_y = await asyncio.to_thread(self.pointer.pointer_position)
        previous_x = (current_x - origin_x) / scale
        previous_y = (current_y - origin_y) / scale
        distance = math.hypot(x - previous_x, y - previous_y)
        step_count = self._step_count(steps, distance)
        waypoints: List[Tuple[float, float]] = []
        for index in range(1, step_count + 1):
            fraction = index / step_count
            step_x = previous_x + (x - previous_x) * fraction
            step_y = previous_y + (y - previous_y) * fraction
            if self.humanize and index < step_count:
                step_x += random.uniform(-PATH_JITTER_PX, PATH_JITTER_PX)
                step_y += random.uniform(-PATH_JITTER_PX, PATH_JITTER_PX)
            waypoints.append((step_x, step_y))
        if not waypoints:
            waypoints = [(x, y)]
        for index, (way_x, way_y) in enumerate(waypoints):
            target_x, target_y = self._screen_point(way_x, way_y, window, inner_width, inner_height)
            await asyncio.to_thread(self.pointer.move, target_x, target_y)
            if index < len(waypoints) - 1:
                await asyncio.sleep(PATH_STEP_SLEEP_SECONDS)

    async def down(self, button: str = "left", click_count: Optional[int] = None) -> None:
        await self._guard()
        detail = BUTTON_TO_X_DETAIL.get(button)
        if detail is None:
            raise BackendError(CODE_INVALID, f"os-native input does not support button '{button}'")
        await asyncio.to_thread(self.pointer.button, detail, True)

    async def up(self, button: str = "left", click_count: Optional[int] = None) -> None:
        await self._guard()
        detail = BUTTON_TO_X_DETAIL.get(button)
        if detail is None:
            raise BackendError(CODE_INVALID, f"os-native input does not support button '{button}'")
        await asyncio.to_thread(self.pointer.button, detail, False)

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self._guard()
        if delta_y < 0:
            button, amount = WHEEL_BUTTON_UP, -delta_y
        elif delta_y > 0:
            button, amount = WHEEL_BUTTON_DOWN, delta_y
        elif delta_x < 0:
            button, amount = WHEEL_BUTTON_LEFT, -delta_x
        elif delta_x > 0:
            button, amount = WHEEL_BUTTON_RIGHT, delta_x
        else:
            return
        notches = max(1, int(round(amount / NOTCH_CSS_PIXELS)))
        for _ in range(notches):
            await asyncio.to_thread(self.pointer.wheel_notch, button)

    def _keycodes(self, key: str) -> List[int]:
        parts = [part for part in key.split("+") if part]
        if key.endswith("+"):
            parts.append("+")
        if not parts:
            raise BackendError(CODE_INVALID, "os-native input cannot map an empty key")
        is_combo = len(parts) > 1
        modifier_codes: List[int] = []
        key_codes: List[int] = []
        for part in parts:
            if is_combo and len(part) == 1 and part not in MODIFIER_KEYSYMS:
                lookup = part.lower()
            else:
                lookup = MODIFIER_KEYSYMS.get(part) or NAMED_KEYSYMS.get(part) or part
            keysym = _keysym_for(lookup)
            if not keysym:
                raise BackendError(
                    CODE_INVALID,
                    f"os-native input cannot map key '{key}': '{part}' has no X keysym",
                )
            bindings = list(self.pointer.display.keysym_to_keycodes(keysym))
            if not bindings:
                bindings = [(self.pointer.display.keysym_to_keycode(keysym), 0)]
            keycode, index = bindings[0]
            if not keycode:
                raise BackendError(
                    CODE_INVALID,
                    f"os-native input cannot map key '{key}': no X keycode is bound to '{part}'",
                )
            if index > SUPPORTED_KEYMAP_INDEX:
                raise BackendError(
                    CODE_INVALID,
                    f"os-native input cannot map key '{key}': '{part}' needs an unsupported "
                    "X keymap level",
                )
            if part in MODIFIER_KEYSYMS and is_combo:
                modifier_codes.append(keycode)
            elif index == SUPPORTED_KEYMAP_INDEX:
                shift_bindings = list(self.pointer.display.keysym_to_keycodes(
                    _keysym_for(SHIFT_KEYSYM_NAME)))
                if not shift_bindings or not shift_bindings[0][0]:
                    raise BackendError(
                        CODE_INVALID,
                        f"os-native input cannot map key '{key}': no X keycode is bound to Shift",
                    )
                key_codes.insert(0, shift_bindings[0][0])
                key_codes.append(keycode)
            else:
                key_codes.append(keycode)
        if not key_codes and modifier_codes:
            key_codes.append(modifier_codes.pop())
        return modifier_codes + key_codes

    async def press(self, key: str) -> None:
        codes = self._keycodes(key)
        await self._guard()
        await self._focus()
        for code in codes[:-1]:
            await asyncio.to_thread(self.pointer.key, code, True)
        try:
            await asyncio.to_thread(self.pointer.key, codes[-1], True)
        finally:
            await asyncio.to_thread(self.pointer.key, codes[-1], False)
            for code in reversed(codes[:-1]):
                await asyncio.to_thread(self.pointer.key, code, False)

    async def type(self, text: str, delay: int = 0) -> None:
        character_codes = [self._keycodes(character) for character in text]
        await self._guard()
        await self._focus()
        for character, codes in zip(text, character_codes):
            for code in codes:
                await asyncio.to_thread(self.pointer.key, code, True)
            for code in reversed(codes):
                await asyncio.to_thread(self.pointer.key, code, False)
            if delay:
                await asyncio.sleep(delay / 1000.0)

    def validate_text(self, text: str) -> None:
        for character in text:
            self._keycodes(character)

    def validate_press(self, key: str) -> None:
        self._keycodes(key)

    async def key_up(self, key: str) -> None:
        await self._guard()
        codes = self._keycodes(key)
        await asyncio.to_thread(self.pointer.key, codes[-1], False)


class JugglerInputDispatch:
    osnative = False

    def __init__(self, page: Any):
        self.page = page

    async def move(self, x: float, y: float, steps: Optional[int] = None) -> None:
        if steps is None:
            await self.page.mouse.move(x, y)
        else:
            await self.page.mouse.move(x, y, steps=steps)

    async def down(self, button: str = "left", click_count: Optional[int] = None) -> None:
        if click_count is None:
            await self.page.mouse.down(button=button)
        else:
            await self.page.mouse.down(button=button, click_count=click_count)

    async def up(self, button: str = "left", click_count: Optional[int] = None) -> None:
        if click_count is None:
            await self.page.mouse.up(button=button)
        else:
            await self.page.mouse.up(button=button, click_count=click_count)

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self.page.mouse.wheel(delta_x, delta_y)

    async def press(self, key: str) -> None:
        await self.page.keyboard.press(key)

    async def type(self, text: str, delay: int = 0) -> None:
        await self.page.keyboard.type(text, delay=delay)

    def validate_text(self, text: str) -> None:
        return None

    def validate_press(self, key: str) -> None:
        return None

    async def key_up(self, key: str) -> None:
        await self.page.keyboard.up(key)


async def discover_osnative_geometry(pointer: OsnativePointer) -> OsnativeGeometry:
    deadline = time.monotonic() + WINDOW_WAIT_TOTAL_SECONDS
    windows: List[Dict[str, int]] = []
    while True:
        windows = await asyncio.to_thread(pointer.camoufox_windows)
        if windows:
            break
        if time.monotonic() >= deadline:
            raise BackendError(
                CODE_NOT_LAUNCHED,
                "the os-native input backend requires a mapped Camoufox browser window on "
                "the X DISPLAY; launch with headless false against a visible X display",
            )
        await asyncio.sleep(WINDOW_WAIT_INTERVAL_SECONDS)
    window = max(windows, key=lambda item: item["width"] * item["height"])
    large_windows = await asyncio.to_thread(pointer.large_window_count)
    return OsnativeGeometry(window, large_windows)