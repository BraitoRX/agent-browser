from __future__ import annotations

import asyncio
from typing import Any, Dict

from input_context import (
    BackendError,
    CODE_INVALID,
    MAX_TEXT_LENGTH,
    optional_int,
    require_bool,
    require_str,
)

from gestures._common import bounded, keyboard_press, locator_click, locator_for, require_in_viewport, trial_hover

NAME = "type"
DESCRIPTION = "Focus a selector and type real key events with an optional per-key delay."
EXAMPLES = [
    {"selector": "input[name=q]", "text": "hello world", "delayMs": 20},
    {"selector": "textarea", "text": "draft", "clear": True},
]
SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "minLength": 1, "maxLength": 4096},
        "text": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
        "clear": {"type": "boolean", "default": False},
        "delayMs": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 0},
        "settleAfterMs": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 0},
    },
    "required": ["selector", "text"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    from input_context import parse_selector

    spec = parse_selector(params.get("selector"), "selector")
    text = require_str(params.get("text"), "text", max_len=MAX_TEXT_LENGTH, allow_empty=True)
    clear_first = False if params.get("clear") is None else require_bool(params.get("clear"), "clear")
    delay_ms = optional_int(params.get("delayMs"), "delayMs", 0, 1000, 0)
    settle_after = optional_int(params.get("settleAfterMs"), "settleAfterMs", 0, 1000, 0)
    assert delay_ms is not None and settle_after is not None
    if len(text) * delay_ms + settle_after + 500 > ctx.remaining_ms():
        raise BackendError(CODE_INVALID, "requested typing delay cannot fit inside the remaining action deadline")

    scope, selector = await locator_for(ctx, spec)
    locator = scope.locator(selector)
    ctx.set_stage("resolve")
    await require_in_viewport(ctx, locator, "input target")
    await trial_hover(ctx, locator, "input target")
    ctx.set_stage("focus")
    await locator_click(ctx, locator)
    if clear_first:
        ctx.set_stage("clear")
        await keyboard_press(ctx, "ControlOrMeta+A")
        await keyboard_press(ctx, "Backspace")
    ctx.set_stage("type")
    for character in text:
        ctx.journal.begin_key_down(character)
        ctx.note_input_dispatched()
        try:
            await bounded(ctx, ctx.page.keyboard.type(character, delay=delay_ms), "keyboard type")
            ctx.journal.finish_key_up(character)
        finally:
            if ctx.journal.keys.get(character):
                try:
                    await asyncio.wait_for(ctx.page.keyboard.up(character), timeout=0.5)
                    ctx.journal.finish_key_up(character)
                except Exception:
                    pass
    if settle_after > 0:
        ctx.set_stage("settle")
        await asyncio.sleep(settle_after / 1000.0)
    return {
        "action": "type",
        "target": f"@{spec.ref}" if spec.ref else spec.selector,
        "characters": len(text),
        "clear": clear_first,
        "delayMs": delay_ms,
        "diagnostics": ctx.diagnostics(),
    }
