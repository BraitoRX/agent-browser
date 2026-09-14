from __future__ import annotations

import asyncio
from typing import Any, Dict

from input_context import optional_int

from gestures._common import (
    box_center,
    bounded,
    locator_for,
    require_in_viewport,
    resolved_target,
)

NAME = "hover"
DESCRIPTION = "Move the native mouse over a selector or captured coordinate and dwell."
EXAMPLES = [
    {"target": {"selector": "nav .menu-item"}, "settleMs": 300},
]
SCHEMA = {
    "type": "object",
    "properties": {
        "target": {
            "description": "selector string target or coordinate point from a screenshot capture",
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
        "settleMs": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 300},
    },
    "required": ["target"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    spec = resolved_target(ctx, params)
    settle_ms = optional_int(params.get("settleMs"), "settleMs", 0, 1000, 300)
    assert settle_ms is not None

    if spec.kind == "selector":
        _scope, locator = await locator_for(ctx, spec)
        ctx.set_stage("resolve")
        box = await require_in_viewport(ctx, locator, "target")
        ctx.set_stage("hover")
        ctx.note_input_dispatched()
        await bounded(ctx, locator.hover(timeout=10_000), "hover")
        x, y = box_center(box)
    else:
        x, y, _capture = await ctx.resolve_capture_point(spec, None)
        ctx.set_stage("dispatch")
        ctx.note_input_dispatched()
        await bounded(ctx, ctx.page.mouse.move(x, y), "mouse move")

    if settle_ms > 0:
        ctx.set_stage("settle")
        await asyncio.sleep(settle_ms / 1000.0)
    return {
        "action": "hover",
        "settleMs": settle_ms,
        "targetKind": spec.kind,
        "point": {"x": round(x, 2), "y": round(y, 2)},
        "diagnostics": ctx.diagnostics(),
    }
