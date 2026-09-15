from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from input_context import CODE_INVALID, BackendError, optional_int, require_dict, require_int

from gestures._common import (
    box_center,
    bounded,
    locator_for,
    parse_reveal_steps,
    require_in_viewport,
    require_in_viewport_no_scroll,
    require_main_frame,
    resolved_target,
    trial_hover,
)

NAME = "drag"
DESCRIPTION = "Drag between main-frame targets with native mouse input (HTML5-compatible sequence)."
EXAMPLES = [
    {"source": {"selector": "#card"}, "target": {"selector": "#column"}, "steps": 12},
    {
        "source": {"selector": "#slider"},
        "target": {"selector": "#panel"},
        "reveal": [{"selector": "#advanced-toggle", "settleMs": 100}],
    },
]
SCHEMA = {
    "type": "object",
    "properties": {
        "source": {
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
        "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        "steps": {"type": "integer", "minimum": 1, "maximum": 60, "default": 1},
        "holdBeforeDropMs": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 0},
        "reveal": {
            "description": "optional ordered hover steps (1..4) to reveal a hidden endpoint before resolving",
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "settleMs": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 100},
                },
                "required": ["selector"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["source", "target"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    from input_context import parse_selector

    require_dict(params, "params")
    source = resolved_target(ctx, params, "source")
    target = resolved_target(ctx, params, "target")
    require_main_frame(source, "drag")
    require_main_frame(target, "drag")
    button = params.get("button", "left")
    if button not in ("left", "right", "middle"):
        raise BackendError(CODE_INVALID, "'button' must be one of: left, right, middle")
    steps = require_int(params.get("steps", 1), "steps", 1, 60)
    hold_ms = optional_int(params.get("holdBeforeDropMs"), "holdBeforeDropMs", 0, 5000, 0)
    assert hold_ms is not None
    reveal_steps = parse_reveal_steps(params.get("reveal"))

    if source.kind == "coordinates" and target.kind == "coordinates":
        if source.capture_id != target.capture_id:
            raise BackendError(CODE_INVALID, "both drag endpoints must use the same captureId")
    any_coordinate = source.kind == "coordinates" or target.kind == "coordinates"
    if any_coordinate and (source.kind != target.kind or reveal_steps):
        raise BackendError(CODE_INVALID, "coordinate drags require two coordinates from the same capture and no reveal steps")

    dispatch = ctx.input_dispatch()
    osnative = getattr(dispatch, "osnative", False)
    for selector, settle_ms in reveal_steps:
        ctx.set_stage("reveal")
        reveal_spec = parse_selector(selector, "reveal.selector")
        _scope, reveal_locator = await locator_for(ctx, reveal_spec)
        if osnative:
            box = await require_in_viewport(ctx, reveal_locator, "reveal target")
            ctx.note_input_dispatched()
            await bounded(ctx, dispatch.move(*box_center(box)), "reveal hover")
        else:
            ctx.note_input_dispatched()
            await bounded(ctx, reveal_locator.hover(timeout=10_000), "reveal hover")
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)

    ctx.set_stage("resolve-source")
    source_box: Optional[Dict[str, float]] = None
    source_point: Optional[Tuple[float, float]] = None
    if source.kind == "selector":
        _scope, source_locator = await locator_for(ctx, source)
        if any_coordinate:
            source_box = await require_in_viewport_no_scroll(ctx, source_locator, "drag source")
        else:
            source_box = await require_in_viewport(ctx, source_locator, "drag source")
    else:
        src_x, src_y, _capture = await ctx.resolve_capture_point(source, None)
        source_point = (src_x, src_y)

    ctx.set_stage("resolve-target")
    target_box: Optional[Dict[str, float]] = None
    target_point: Optional[Tuple[float, float]] = None
    if target.kind == "selector":
        _scope, target_locator = await locator_for(ctx, target)
        if any_coordinate:
            target_box = await require_in_viewport_no_scroll(ctx, target_locator, "drag target")
        else:
            target_box = await require_in_viewport(ctx, target_locator, "drag target")
    else:
        tgt_x, tgt_y, _capture = await ctx.resolve_capture_point(target, source.capture_id)
        target_point = (tgt_x, tgt_y)

    press_x, press_y = source_point if source_point is not None else box_center(source_box)
    drop_x, drop_y = target_point if target_point is not None else box_center(target_box)

    if source.kind == "selector":
        ctx.set_stage("precheck-source")
        await trial_hover(ctx, source_locator, "drag source")
        fresh = await (
            require_in_viewport_no_scroll(ctx, source_locator, "drag source")
            if any_coordinate
            else require_in_viewport(ctx, source_locator, "drag source")
        )
        source_box = fresh
        press_x, press_y = box_center(fresh)
    if target.kind == "selector":
        ctx.set_stage("precheck-target")
        await trial_hover(ctx, target_locator, "drag target")
        fresh = await (
            require_in_viewport_no_scroll(ctx, target_locator, "drag target")
            if any_coordinate
            else require_in_viewport(ctx, target_locator, "drag target")
        )
        target_box = fresh
        drop_x, drop_y = box_center(fresh)

    if source.kind == "selector":
        source_box = await require_in_viewport_no_scroll(ctx, source_locator, "drag source")
        target_box = await require_in_viewport_no_scroll(ctx, target_locator, "drag target")
        press_x, press_y = box_center(source_box)
        drop_x, drop_y = box_center(target_box)
    else:
        await ctx.require_capture(source.capture_id)
    if ctx.remaining_ms() < hold_ms + 750:
        raise BackendError(CODE_INVALID, "remaining deadline cannot cover the requested drop dwell and release")

    journal = ctx.journal
    ctx.set_stage("move-source")
    ctx.note_input_dispatched()
    await bounded(ctx, dispatch.move(press_x, press_y), "mouse move to source")
    if source.kind == "selector":
        if (await require_in_viewport_no_scroll(ctx, source_locator, "drag source") != source_box
                or await require_in_viewport_no_scroll(ctx, target_locator, "drag target") != target_box):
            raise BackendError(CODE_INVALID, "drag endpoints moved while approaching; observe again before retrying")
    ctx.set_stage("down")
    journal.begin_button_down(button)
    ctx.note_input_dispatched()
    try:
        await bounded(ctx, dispatch.down(button=button), "mouse down")
        ctx.check_deadline()
        ctx.set_stage("move-target")
        if steps > 1:
            await bounded(ctx, dispatch.move(drop_x, drop_y, steps=steps), "mouse move to target")
        else:
            await bounded(ctx, dispatch.move(drop_x, drop_y), "mouse move to target")
        ctx.set_stage("move-target-again")
        await bounded(ctx, dispatch.move(drop_x, drop_y), "mouse move to target (settle)")
        if hold_ms > 0:
            ctx.set_stage("hold-before-drop")
            await asyncio.sleep(hold_ms / 1000.0)
        ctx.check_deadline()
        ctx.set_stage("up")
        ctx.note_input_dispatched()
        await bounded(ctx, dispatch.up(button=button), "mouse up")
        journal.finish_button_up(button)
    finally:
        if journal.buttons.get(button):
            ctx.set_stage("cleanup-release")
            try:
                await asyncio.wait_for(dispatch.up(button=button), timeout=1.5)
                journal.finish_button_up(button)
            except Exception:
                pass

    return {
        "action": "drag",
        "button": button,
        "steps": steps,
        "holdBeforeDropMs": hold_ms,
        "revealSteps": len(reveal_steps),
        "sourceKind": source.kind,
        "targetKind": target.kind,
        "pressPoint": {"x": round(press_x, 2), "y": round(press_y, 2)},
        "dropPoint": {"x": round(drop_x, 2), "y": round(drop_y, 2)},
        "releaseDispatched": True,
        "diagnostics": ctx.diagnostics(),
    }
