from __future__ import annotations

import asyncio
from typing import Any, Dict

from input_context import (
    CODE_TIMEOUT,
    HOLD_MAX_MS,
    BackendError,
    optional_enum,
    optional_int,
    require_int,
)

from gestures._common import (
    box_center,
    bounded,
    locator_for,
    require_in_viewport,
    require_in_viewport_no_scroll,
    require_main_frame,
    resolved_target,
    trial_hover,
)

NAME = "hold"
DESCRIPTION = "Press and hold a main-frame selector or captured coordinate for 100..20000ms."
EXAMPLES = [
    {"target": {"selector": "#captcha-button"}, "durationMs": 12000},
    {"target": {"selector": "#captcha-button"}, "durationMs": 12000, "button": "left"},
]
SCHEMA = {
    "type": "object",
    "properties": {
        "target": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 4096}},
                    "required": ["selector"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "coordinates": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "captureId": {"type": "string", "minLength": 1, "maxLength": 128},
                            },
                            "required": ["x", "y", "captureId"],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["coordinates"],
                    "additionalProperties": False,
                },
            ],
        },
        "durationMs": {"type": "integer", "minimum": 100, "maximum": HOLD_MAX_MS},
        "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        "settleBeforeMs": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 0},
    },
    "required": ["target", "durationMs"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    spec = resolved_target(ctx, params)
    require_main_frame(spec, "hold")
    duration_ms = require_int(params.get("durationMs"), "durationMs", 100, HOLD_MAX_MS)
    button = optional_enum(params.get("button"), "button", ["left", "right", "middle"], "left")
    settle_before = optional_int(params.get("settleBeforeMs"), "settleBeforeMs", 0, 1000, 0)
    assert button is not None and settle_before is not None

    if spec.kind == "selector":
        _scope, locator = await locator_for(ctx, spec)
        ctx.set_stage("resolve")
        box = await require_in_viewport(ctx, locator, "target")
        await trial_hover(ctx, locator, "target")
        box = await require_in_viewport_no_scroll(ctx, locator, "target")
        x, y = box_center(box)
    else:
        x, y, _capture = await ctx.resolve_capture_point(spec, None)

    ctx.set_stage("move")
    ctx.note_input_dispatched()
    await bounded(ctx, ctx.page.mouse.move(x, y), "mouse move")
    if settle_before > 0:
        await asyncio.sleep(settle_before / 1000.0)

    if ctx.remaining_ms() < duration_ms + 500:
        raise BackendError(
            CODE_TIMEOUT,
            f"remaining deadline {ctx.remaining_ms()}ms cannot cover the {duration_ms}ms hold",
        )

    journal = ctx.journal
    ctx.set_stage("down")
    journal.begin_button_down(button)
    ctx.note_input_dispatched()
    remaining_seconds = duration_ms / 1000.0
    timed_out = False
    try:
        await bounded(ctx, ctx.page.mouse.down(button=button), "mouse down")
        ctx.set_stage("hold")
        while remaining_seconds > 0:
            slice_seconds = min(0.25, ctx.remaining_seconds() - 0.25)
            if slice_seconds <= 0:
                timed_out = True
                break
            step = min(slice_seconds, remaining_seconds)
            await asyncio.sleep(step)
            remaining_seconds -= step
    finally:
        ctx.set_stage("release")
        if journal.buttons.get(button):
            try:
                await asyncio.wait_for(ctx.page.mouse.up(button=button), timeout=1.5)
                journal.finish_button_up(button)
            except Exception:
                pass
    if timed_out:
        raise BackendError(CODE_TIMEOUT, f"hold exceeded the action deadline after {duration_ms}ms", deadline_exceeded=True)
    return {
        "action": "hold",
        "durationMs": duration_ms,
        "button": button,
        "targetKind": spec.kind,
        "point": {"x": round(x, 2), "y": round(y, 2)},
        "downDispatched": True,
        "diagnostics": ctx.diagnostics(),
    }
