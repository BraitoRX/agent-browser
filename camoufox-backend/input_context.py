from __future__ import annotations

import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
DEFAULT_ACTION_DEADLINE_MS = 22_000
MAX_ACTION_DEADLINE_MS = 25_000
HOLD_MAX_MS = 20_000
CAPTURE_TTL_SECONDS = 120.0
MAX_SELECTOR_LENGTH = 4096
MAX_TEXT_LENGTH = 64 * 1024
MAX_CAPTURE_ID_LENGTH = 128

MOTIONS = ("human-fast", "fast", "precision")
MOTION_HUMANIZE: Dict[str, Any] = {"human-fast": 0.25, "fast": False, "precision": False}

CODE_INVALID = "camoufox_invalid_params"
CODE_INVALID_REQUEST = "camoufox_invalid_request"
CODE_UNSUPPORTED = "camoufox_unsupported"
CODE_NOT_LAUNCHED = "camoufox_not_launched"
CODE_SESSION_CLOSED = "camoufox_session_closed"
CODE_TARGET_CLOSED = "camoufox_target_closed"
CODE_NO_ACTIVE_TAB = "camoufox_no_active_tab"
CODE_STALE_CAPTURE = "stale_visual_capture"
CODE_STALE_REF = "camoufox_stale_ref"
CODE_TIMEOUT = "camoufox_timeout"
CODE_POISONED = "camoufox_poisoned"
CODE_ERROR = "camoufox_error"
CODE_OUTPUT_TOO_LARGE = "camoufox_output_too_large"
CODE_UNKNOWN_GESTURE = "camoufox_unknown_gesture"
CODE_REGISTRY = "camoufox_registry_error"
CODE_INTERNAL = "camoufox_internal_error"

CLOSE_REASON_BROWSER_DISCONNECTED = "browser_disconnected"
CLOSE_REASON_CONTEXT_CLOSED = "context_closed"

REF_NAME_RE = re.compile(r"^(?:(f\d+)?e\d+|d\d+)$")


class BackendError(Exception):
    def __init__(self, code: str, message: str, deadline_exceeded: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.deadline_exceeded = deadline_exceeded


def action_deadline_ms(env: Optional[Dict[str, str]] = None) -> int:
    source = env if env is not None else os.environ
    raw = source.get("AGENT_BROWSER_ACTION_DEADLINE_MS")
    if raw is None:
        return DEFAULT_ACTION_DEADLINE_MS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ACTION_DEADLINE_MS
    return max(1_000, min(MAX_ACTION_DEADLINE_MS, value))


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def require_str(value: Any, field_name: str, *, max_len: int = MAX_SELECTOR_LENGTH,
                allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise BackendError(CODE_INVALID, f"'{field_name}' must be a string")
    if len(value) > max_len:
        raise BackendError(CODE_INVALID, f"'{field_name}' exceeds {max_len} characters")
    if not allow_empty and value == "":
        raise BackendError(CODE_INVALID, f"'{field_name}' must not be empty")
    return value


def optional_str(value: Any, field_name: str, *, max_len: int = MAX_SELECTOR_LENGTH,
                 allow_empty: bool = False) -> Optional[str]:
    if value is None:
        return None
    return require_str(value, field_name, max_len=max_len, allow_empty=allow_empty)


def require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise BackendError(CODE_INVALID, f"'{field_name}' must be a boolean")
    return value


def optional_bool(value: Any, field_name: str, default: Optional[bool] = None) -> Optional[bool]:
    if value is None:
        return default
    return require_bool(value, field_name)


def require_int(value: Any, field_name: str, lo: int, hi: int) -> int:
    parsed = _as_int(value)
    if parsed is None:
        raise BackendError(CODE_INVALID, f"'{field_name}' must be an integer")
    if parsed < lo or parsed > hi:
        raise BackendError(CODE_INVALID, f"'{field_name}' must be between {lo} and {hi}")
    return parsed


def optional_int(value: Any, field_name: str, lo: int, hi: int,
                 default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    return require_int(value, field_name, lo, hi)


def require_number(value: Any, field_name: str, lo: float, hi: float) -> float:
    parsed = _as_number(value)
    if parsed is None:
        raise BackendError(CODE_INVALID, f"'{field_name}' must be a finite number")
    if parsed < lo or parsed > hi:
        raise BackendError(CODE_INVALID, f"'{field_name}' must be between {lo} and {hi}")
    return parsed


def optional_number(value: Any, field_name: str, lo: float, hi: float,
                    default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    return require_number(value, field_name, lo, hi)


def optional_enum(value: Any, field_name: str, allowed: Sequence[str],
                  default: Optional[str] = None) -> Optional[str]:
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise BackendError(CODE_INVALID, f"'{field_name}' must be one of: {', '.join(allowed)}")
    return value


def require_str_list(value: Any, field_name: str, *, max_items: int = 64,
                     max_len: int = MAX_SELECTOR_LENGTH) -> List[str]:
    items = value if isinstance(value, list) else [value]
    if not items:
        raise BackendError(CODE_INVALID, f"'{field_name}' must not be empty")
    if len(items) > max_items:
        raise BackendError(CODE_INVALID, f"'{field_name}' accepts at most {max_items} values")
    return [require_str(item, field_name, max_len=max_len) for item in items]


def require_dict(value: Any, field_name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise BackendError(CODE_INVALID, f"'{field_name}' must be an object")
    return value


@dataclass
class TargetSpec:
    kind: str
    selector: Optional[str] = None
    ref: Optional[str] = None
    frame_prefix: Optional[str] = None
    dom_ref: bool = False
    x: Optional[float] = None
    y: Optional[float] = None
    capture_id: Optional[str] = None


def parse_selector(value: Any, field_name: str = "selector") -> TargetSpec:
    raw = require_str(value, field_name, max_len=MAX_SELECTOR_LENGTH)
    if raw.startswith("@"):
        name = raw[1:]
        match = REF_NAME_RE.match(name)
        if not match:
            raise BackendError(
                CODE_STALE_REF,
                f"'{field_name}' is not a valid ref; expected @eN or @fNeN from a snapshot, or @dN from dom_chunk",
            )
        return TargetSpec(
            kind="selector",
            selector=raw,
            ref=name,
            frame_prefix=match.group(1),
            dom_ref=name.startswith("d"),
        )
    if "aria-ref=" in raw or "internal:" in raw or ">>" in raw:
        raise BackendError(
            CODE_UNSUPPORTED,
            "use CSS, an xpath= selector, or an observed @ref, not internal selector engines",
        )
    return TargetSpec(kind="selector", selector=raw)


def parse_target(value: Any, field_name: str = "target") -> TargetSpec:
    obj = require_dict(value, field_name)
    keys = set(obj.keys())
    if keys == {"selector"}:
        return parse_selector(obj["selector"], f"{field_name}.selector")
    if keys == {"coordinates"}:
        point = require_dict(obj["coordinates"], f"{field_name}.coordinates")
        extra = set(point.keys()) - {"x", "y", "captureId"}
        if extra:
            raise BackendError(CODE_INVALID,
                               f"'{field_name}.coordinates' has unexpected keys: {', '.join(sorted(extra))}")
        missing = {"x", "y", "captureId"} - set(point.keys())
        if missing:
            raise BackendError(CODE_INVALID,
                               f"'{field_name}.coordinates' is missing: {', '.join(sorted(missing))}")
        x = require_number(point["x"], f"{field_name}.coordinates.x", 0.0, 1_000_000.0)
        y = require_number(point["y"], f"{field_name}.coordinates.y", 0.0, 1_000_000.0)
        capture_id = require_str(point["captureId"], f"{field_name}.coordinates.captureId",
                                 max_len=MAX_CAPTURE_ID_LENGTH)
        return TargetSpec(kind="coordinates", x=x, y=y, capture_id=capture_id)
    raise BackendError(
        CODE_INVALID,
        f"'{field_name}' must be exactly one of: {{'selector': string}} or "
        "{'coordinates': {'x': number, 'y': number, 'captureId': string}}",
    )


def css_point(capture: Dict[str, Any], spec: TargetSpec) -> Tuple[float, float]:
    image_width = float(capture["imageWidth"])
    image_height = float(capture["imageHeight"])
    viewport_width = float(capture["viewportWidth"])
    viewport_height = float(capture["viewportHeight"])
    if image_width <= 0 or image_height <= 0 or viewport_width <= 0 or viewport_height <= 0:
        raise BackendError(CODE_STALE_CAPTURE, "screenshot capture has invalid dimensions")
    if spec.x is None or spec.y is None:
        raise BackendError(CODE_INVALID, "coordinate target is missing x/y")
    if spec.x >= image_width or spec.y >= image_height:
        raise BackendError(
            CODE_INVALID,
            f"coordinate ({spec.x}, {spec.y}) is outside the captured image "
            f"({int(image_width)}x{int(image_height)})",
        )
    return (spec.x * viewport_width / image_width, spec.y * viewport_height / image_height)


class Captures:
    def __init__(self, ttl_seconds: float = CAPTURE_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._items: Dict[str, Dict[str, Any]] = {}

    def register(self, meta: Dict[str, Any]) -> str:
        capture_id = uuid.uuid4().hex
        stored = dict(meta)
        stored["captureId"] = capture_id
        stored["_monotonic"] = time.monotonic()
        self._items[capture_id] = stored
        self.prune()
        return capture_id

    def get(self, capture_id: str) -> Optional[Dict[str, Any]]:
        self.prune()
        item = self._items.get(capture_id)
        return dict(item) if item is not None else None

    def invalidate(self, capture_id: str) -> None:
        self._items.pop(capture_id, None)

    def invalidate_all(self) -> None:
        self._items.clear()

    def prune(self) -> None:
        now = time.monotonic()
        stale = [key for key, item in self._items.items()
                 if now - item["_monotonic"] > self.ttl_seconds]
        for key in stale:
            self._items.pop(key, None)

    def count(self) -> int:
        self.prune()
        return len(self._items)


class InputJournal:
    def __init__(self) -> None:
        self.buttons: Dict[str, bool] = {}
        self.keys: Dict[str, bool] = {}
        self.down_attempts = 0

    def begin_button_down(self, button: str) -> None:
        self.buttons[button] = True
        self.down_attempts += 1

    def finish_button_up(self, button: str) -> None:
        self.buttons.pop(button, None)

    def begin_key_down(self, key: str) -> None:
        self.keys[key] = True

    def finish_key_up(self, key: str) -> None:
        self.keys.pop(key, None)

    def pending_buttons(self) -> List[str]:
        return sorted(self.buttons.keys())

    def pending_keys(self) -> List[str]:
        return sorted(self.keys.keys())

    def clear(self) -> None:
        self.buttons.clear()
        self.keys.clear()


def json_safe(value: Any, _depth: int = 0) -> Any:
    if _depth > 60:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [json_safe(item, _depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item, _depth + 1) for key, item in value.items()}
    return str(value)


def best_effort_id(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace")
    match = re.search(r'"id"\s*:\s*"((?:[^"\\]|\\.){0,128})"', text)
    if match:
        return match.group(1)
    return "unknown"


def reject_json_constant(name: str) -> None:
    raise ValueError(f"invalid JSON constant: {name}")
