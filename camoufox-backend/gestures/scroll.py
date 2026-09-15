from __future__ import annotations

import asyncio
from typing import Any, Dict

from input_context import (
    CODE_INVALID,
    BackendError,
    optional_enum,
    optional_int,
    parse_selector,
    require_int,
)

from gestures._common import box_center, bounded, locator_for, require_in_viewport

NAME = "scroll"
DESCRIPTION = "Scroll with native mouse wheel input, optionally centred on a selector."
EXAMPLES = [
    {"direction": "down", "amount": 600},
    {"direction": "up", "amount": 400, "selector": "#results"},
]
SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "amount": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 500},
        "selector": {"type": "string", "minLength": 1, "maxLength": 4096},
        "chunkSize": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 120},
        "settleMs": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 0},
    },
    "required": ["direction"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    direction = optional_enum(
        params.get("direction"), "direction", ["up", "down", "left", "right"]
    )
    if direction is None:
        raise BackendError(
            CODE_INVALID, "'direction' is required and must be up, down, left, or right"
        )
    amount = require_int(params.get("amount", 500), "amount", 1, 100_000)
    chunk = require_int(params.get("chunkSize", 120), "chunkSize", 1, 1000)
    settle_ms = optional_int(params.get("settleMs"), "settleMs", 0, 1000, 0)
    assert settle_ms is not None
    selector = params.get("selector")

    if selector is not None:
        spec = parse_selector(selector, "selector")
        _scope, locator = await locator_for(ctx, spec)
        ctx.set_stage("hover")
        dispatch = ctx.input_dispatch()
        if getattr(dispatch, "osnative", False):
            box = await require_in_viewport(ctx, locator, "scroll target")
            ctx.note_input_dispatched()
            await bounded(ctx, dispatch.move(*box_center(box)), "hover scroll target")
        else:
            ctx.note_input_dispatched()
            await bounded(ctx, locator.hover(timeout=10_000), "hover scroll target")

    vertical = direction in ("up", "down")
    sign = 1 if direction in ("down", "right") else -1
    ctx.set_stage("wheel")
    dispatch = ctx.input_dispatch()
    remaining = amount
    while remaining > 0:
        ctx.check_deadline()
        step_count = min(chunk, remaining)
        step = step_count * sign
        ctx.note_input_dispatched()
        await bounded(
            ctx,
            dispatch.wheel(0 if vertical else step, step if vertical else 0),
            "mouse wheel",
        )
        remaining -= step_count
        if settle_ms > 0 and remaining > 0:
            await asyncio.sleep(settle_ms / 1000.0)
    if settle_ms > 0:
        await asyncio.sleep(settle_ms / 1000.0)
    return {
        "action": "scroll",
        "direction": direction,
        "amount": amount,
        "chunkSize": chunk,
        "selector": selector,
        "diagnostics": ctx.diagnostics(),
    }
