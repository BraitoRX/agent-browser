from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Dict, List, Optional, Tuple

from input_context import (
    CODE_INVALID,
    BackendError,
    CODE_TIMEOUT,
    CODE_UNSUPPORTED,
    TargetSpec,
    css_point,
    optional_int,
    parse_selector,
    parse_target,
    require_dict,
)

STEP_TIMEOUT_MS = 10_000


async def bounded(ctx: Any, awaitable: Awaitable[Any], what: str) -> Any:
    remaining = ctx.remaining_seconds()
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise BackendError(CODE_TIMEOUT, f"action deadline exceeded before {what}", deadline_exceeded=True)
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError:
        raise BackendError(CODE_TIMEOUT, f"{what} exceeded the action deadline", deadline_exceeded=True) from None


def require_main_frame(spec: TargetSpec, action: str) -> None:
    if spec.kind == "selector" and spec.frame_prefix is not None:
        raise BackendError(
            CODE_UNSUPPORTED,
            f"{action} supports main-frame targets only; ref '{spec.ref}' belongs to an iframe",
        )


def resolved_target(ctx: Any, params: Dict[str, Any], field_name: str = "target") -> TargetSpec:
    return parse_target(params.get(field_name), field_name)


async def locator_for(ctx: Any, spec: TargetSpec) -> Tuple[Any, str]:
    if spec.kind != "selector" or spec.selector is None:
        raise BackendError(CODE_INVALID, "expected a selector target")
    if spec.ref is not None:
        ctx.require_exposed_ref(spec.ref)
        return ctx.page, f"aria-ref={spec.ref}"
    return ctx.page, spec.selector


async def element_box(locator: Any) -> Optional[Dict[str, float]]:
    box = await locator.bounding_box(timeout=STEP_TIMEOUT_MS)
    if box is None:
        return None
    return {
        "x": float(box["x"]),
        "y": float(box["y"]),
        "width": float(box["width"]),
        "height": float(box["height"]),
    }


def box_center(box: Dict[str, float]) -> Tuple[float, float]:
    return (box["x"] + box["width"] / 2.0, box["y"] + box["height"] / 2.0)


async def inside_viewport(ctx: Any, box: Dict[str, float]) -> bool:
    viewport = await bounded(ctx, ctx.viewport_size(), "measure viewport")
    return (
        box["x"] >= 0
        and box["y"] >= 0
        and box["x"] + box["width"] <= viewport["width"] + 0.5
        and box["y"] + box["height"] <= viewport["height"] + 0.5
    )


async def require_in_viewport(ctx: Any, locator: Any, label: str) -> Dict[str, float]:
    await bounded(ctx, locator.scroll_into_view_if_needed(timeout=STEP_TIMEOUT_MS), f"scroll {label} into view")
    box = await element_box(locator)
    if box is None:
        raise BackendError(CODE_INVALID, f"{label} has no layout box")
    if not await inside_viewport(ctx, box):
        raise BackendError(
            CODE_INVALID,
            f"{label} does not fit fully inside the current viewport after scrolling",
        )
    return box


async def require_in_viewport_no_scroll(ctx: Any, locator: Any, label: str) -> Dict[str, float]:
    box = await element_box(locator)
    if box is None:
        raise BackendError(CODE_INVALID, f"{label} has no layout box")
    if not await inside_viewport(ctx, box):
        raise BackendError(
            CODE_INVALID,
            f"{label} is outside the viewport; scrolling is disabled when a coordinate endpoint is used",
        )
    return box


async def trial_hover(ctx: Any, locator: Any, label: str) -> None:
    await bounded(ctx, locator.hover(trial=True, timeout=STEP_TIMEOUT_MS), f"hit test for {label}")


def parse_reveal_steps(value: Any) -> List[Tuple[str, int]]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise BackendError(CODE_INVALID, "'reveal' must be a non-empty array with at most 4 steps")
    if len(value) > 4:
        raise BackendError(CODE_INVALID, "'reveal' accepts at most 4 steps")
    steps: List[Tuple[str, int]] = []
    for index, item in enumerate(value):
        obj = require_dict(item, f"reveal[{index}]")
        extra = set(obj.keys()) - {"selector", "settleMs"}
        if extra:
            raise BackendError(CODE_INVALID,
                               f"'reveal[{index}]' has unexpected keys: {', '.join(sorted(extra))}")
        spec = parse_selector(obj.get("selector"), f"reveal[{index}].selector")
        if spec.ref is not None:
            raise BackendError(
                CODE_INVALID,
                f"'reveal[{index}].selector' must be a CSS selector; refs are not accepted in reveal steps",
            )
        settle_ms = optional_int(obj.get("settleMs"), f"reveal[{index}].settleMs", 0, 1000, 100)
        assert settle_ms is not None
        steps.append((spec.selector or "", settle_ms))
    return steps


async def resolve_capture_point(ctx: Any, spec: TargetSpec, capture_id_used: Optional[str]) -> Tuple[float, float, str]:
    if spec.capture_id is None or spec.x is None or spec.y is None:
        raise BackendError(CODE_INVALID, "coordinate target is incomplete")
    if capture_id_used is not None and spec.capture_id != capture_id_used:
        raise BackendError(CODE_INVALID, "both drag endpoints must use the same captureId")
    capture = await ctx.require_capture(spec.capture_id)
    x, y = css_point(capture, spec)
    return x, y, spec.capture_id


async def mouse_click_at(ctx: Any, x: float, y: float, button: str, count: int) -> None:
    journal = ctx.journal
    ctx.set_stage("move")
    ctx.note_input_dispatched()
    await bounded(ctx, ctx.page.mouse.move(x, y), "mouse move")
    for index in range(count):
        ctx.check_deadline()
        ctx.set_stage("down")
        journal.begin_button_down(button)
        ctx.note_input_dispatched()
        try:
            await bounded(ctx, ctx.page.mouse.down(button=button, click_count=index + 1), "mouse down")
            ctx.check_deadline()
            ctx.set_stage("up")
            ctx.note_input_dispatched()
            await bounded(ctx, ctx.page.mouse.up(button=button, click_count=index + 1), "mouse up")
            journal.finish_button_up(button)
        finally:
            if journal.buttons.get(button):
                try:
                    await asyncio.wait_for(ctx.page.mouse.up(button=button), timeout=1.5)
                    journal.finish_button_up(button)
                except Exception:
                    pass


async def locator_click(ctx: Any, locator: Any, button: str = "left", count: int = 1) -> None:
    ctx.journal.begin_button_down(button)
    ctx.note_input_dispatched()
    try:
        action = locator.dblclick(button=button) if count == 2 else locator.click(button=button)
        await bounded(ctx, action, "locator click")
        ctx.journal.finish_button_up(button)
    finally:
        if ctx.journal.buttons.get(button):
            try:
                await asyncio.wait_for(ctx.page.mouse.up(button=button), timeout=1.5)
                ctx.journal.finish_button_up(button)
            except Exception:
                pass


async def keyboard_press(ctx: Any, key: str) -> None:
    keys = key.split("+")
    for item in keys:
        if item == "ControlOrMeta":
            ctx.journal.begin_key_down("Control")
            ctx.journal.begin_key_down("Meta")
        else:
            ctx.journal.begin_key_down(item)
    ctx.note_input_dispatched()
    try:
        await bounded(ctx, ctx.page.keyboard.press(key), "keyboard press")
    finally:
        for item in ctx.journal.pending_keys():
            try:
                await asyncio.wait_for(ctx.page.keyboard.up(item), timeout=0.5)
                ctx.journal.finish_key_up(item)
            except Exception:
                pass
