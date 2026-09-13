from __future__ import annotations

import asyncio
from typing import Any, Dict

from input_context import optional_int, parse_selector
from gestures._common import locator_click

NAME = "shift_click"
DESCRIPTION = "Example external gesture: hold Shift and click a selector."
EXAMPLES = [
    {"selector": "li.range-start", "settleMs": 150},
]
SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "minLength": 1, "maxLength": 4096},
        "settleMs": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 150},
    },
    "required": ["selector"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    spec = parse_selector(params.get("selector"), "selector")
    settle_ms = optional_int(params.get("settleMs"), "settleMs", 0, 1000, 150)
    assert settle_ms is not None
    if spec.ref is not None:
        ctx.require_exposed_ref(spec.ref)
        resolved = f"aria-ref={spec.ref}"
    else:
        resolved = spec.selector
    locator = ctx.page.locator(resolved)
    journal = ctx.journal
    ctx.set_stage("down")
    journal.begin_key_down("Shift")
    ctx.note_input_dispatched()
    try:
        await ctx.page.keyboard.down("Shift")
        ctx.check_deadline()
        ctx.set_stage("click")
        await locator_click(ctx, locator)
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)
    finally:
        ctx.set_stage("release")
        try:
            await asyncio.wait_for(ctx.page.keyboard.up("Shift"), timeout=1.5)
            journal.finish_key_up("Shift")
        except Exception:
            pass
    return {"action": "shift_click", "diagnostics": ctx.diagnostics()}
