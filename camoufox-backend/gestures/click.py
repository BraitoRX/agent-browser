from __future__ import annotations

from typing import Any, Dict

from input_context import optional_enum, optional_int

from gestures._common import (
    bounded,
    locator_for,
    locator_click,
    mouse_click_at,
    require_in_viewport,
    resolved_target,
)

NAME = "click"
DESCRIPTION = "Click a selector or captured coordinate with native mouse input."
EXAMPLES = [
    {"target": {"selector": "button#submit"}, "button": "left", "count": 1},
    {"target": {"coordinates": {"x": 100, "y": 200, "captureId": "<from screenshot>"}}},
]
SCHEMA = {
    "type": "object",
    "description": "Native click at exactly one target.",
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
        "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        "count": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1},
    },
    "required": ["target"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    spec = resolved_target(ctx, params)
    button = optional_enum(params.get("button"), "button", ["left", "right", "middle"], "left")
    count = optional_int(params.get("count"), "count", 1, 2, 1)
    assert button is not None and count is not None

    point = None
    if spec.kind == "selector":
        scope, selector = await locator_for(ctx, spec)
        locator = scope.locator(selector)
        ctx.set_stage("resolve")
        box = await require_in_viewport(ctx, locator, "target")
        ctx.set_stage("dispatch")
        await locator_click(ctx, locator, button, count)
        if box is not None:
            point = {"x": round(box["x"] + box["width"] / 2, 2),
                     "y": round(box["y"] + box["height"] / 2, 2)}
    else:
        x, y, _capture = await ctx.resolve_capture_point(spec, None)
        ctx.set_stage("dispatch")
        await mouse_click_at(ctx, x, y, button, count)
        point = {"x": round(x, 2), "y": round(y, 2)}

    return {
        "action": "click",
        "button": button,
        "count": count,
        "targetKind": spec.kind,
        "point": point,
        "diagnostics": ctx.diagnostics(),
    }
