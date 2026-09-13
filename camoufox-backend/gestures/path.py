from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

from input_context import (
    CODE_INVALID,
    CODE_UNSUPPORTED,
    BackendError,
    parse_selector,
    require_dict,
    require_int,
    require_number,
)

from gestures._common import bounded, locator_for, require_in_viewport_no_scroll, require_main_frame

NAME = "path"
DESCRIPTION = "Drag through up to 64 element-relative waypoints (fast/precision motion only)."
EXAMPLES = [
    {
        "source": {"selector": "#canvas"},
        "points": [{"x": 120, "y": 80}, {"x": 240, "y": 160}],
        "durationMs": 800,
    }
]
SCHEMA = {
    "type": "object",
    "properties": {
        "source": {
            "type": "object",
            "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 4096}},
            "required": ["selector"],
            "additionalProperties": False,
        },
        "points": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                },
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        },
        "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        "durationMs": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0},
    },
    "required": ["source", "points"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    if ctx.motion == "human-fast":
        raise BackendError(
            CODE_UNSUPPORTED,
            "the path gesture requires the fast or precision motion; "
            "human-fast enables browser-level humanization that conflicts with exact paths",
        )
    require_dict(params, "params")
    source = require_dict(params.get("source"), "source")
    spec = parse_selector(source.get("selector"), "source.selector")
    require_main_frame(spec, "path")
    raw_points = params.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise BackendError(CODE_INVALID, "'points' must be a non-empty array")
    if len(raw_points) > 64:
        raise BackendError(CODE_INVALID, "'points' accepts at most 64 waypoints")
    points: List[Tuple[float, float]] = []
    for index, item in enumerate(raw_points):
        point = require_dict(item, f"points[{index}]")
        extra = set(point.keys()) - {"x", "y"}
        if extra:
            raise BackendError(CODE_INVALID, f"'points[{index}]' has unexpected keys")
        x = require_number(point.get("x"), f"points[{index}].x", 0.0, 1_000_000.0)
        y = require_number(point.get("y"), f"points[{index}].y", 0.0, 1_000_000.0)
        points.append((x, y))
    button = params.get("button", "left")
    if button not in ("left", "right", "middle"):
        raise BackendError(CODE_INVALID, "'button' must be one of: left, right, middle")
    duration_ms = require_int(params.get("durationMs", 0), "durationMs", 0, 10_000)

    scope, resolved = await locator_for(ctx, spec)
    box = await require_in_viewport_no_scroll(ctx, scope.locator(resolved), "path origin")
    origin_x = box["x"]
    origin_y = box["y"]
    if any(x >= box["width"] or y >= box["height"] for x, y in points):
        raise BackendError(CODE_INVALID, "every path waypoint must be inside the source element")
    if ctx.remaining_ms() < duration_ms + 750:
        raise BackendError(CODE_INVALID, "remaining deadline cannot cover the path and release")

    journal = ctx.journal
    ctx.set_stage("down")
    journal.begin_button_down(button)
    ctx.note_input_dispatched()
    try:
        await bounded(ctx, ctx.page.mouse.move(origin_x, origin_y), "mouse move to origin")
        await bounded(ctx, ctx.page.mouse.down(button=button), "mouse down")
        ctx.set_stage("path")
        per_step_sleep = (duration_ms / 1000.0 / len(points)) if duration_ms > 0 else 0.0
        for x, y in points:
            ctx.check_deadline()
            ctx.note_input_dispatched()
            await bounded(ctx, ctx.page.mouse.move(origin_x + x, origin_y + y), "mouse move waypoint")
            if per_step_sleep > 0:
                await asyncio.sleep(per_step_sleep)
        ctx.set_stage("up")
        ctx.note_input_dispatched()
        await bounded(ctx, ctx.page.mouse.up(button=button), "mouse up")
        journal.finish_button_up(button)
    finally:
        if journal.buttons.get(button):
            ctx.set_stage("cleanup-release")
            try:
                await asyncio.wait_for(ctx.page.mouse.up(button=button), timeout=1.5)
                journal.finish_button_up(button)
            except Exception:
                pass
    return {
        "action": "path",
        "waypoints": len(points),
        "durationMs": duration_ms,
        "button": button,
        "diagnostics": ctx.diagnostics(),
    }
