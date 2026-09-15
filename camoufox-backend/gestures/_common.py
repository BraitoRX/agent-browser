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
    is_playwright_timeout,
    optional_int,
    parse_selector,
    parse_target,
    playwright_timeout_error,
    require_dict,
)

STEP_TIMEOUT_MS = 2_000
BOX_STABILITY_DELAY_MS = 150


async def bounded(ctx: Any, awaitable: Awaitable[Any], what: str, timeout_ms: Optional[int] = None) -> Any:
    remaining = ctx.remaining_seconds()
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise BackendError(CODE_TIMEOUT, f"action deadline exceeded before {what}", deadline_exceeded=True)
    watchdog = min(remaining, (timeout_ms + 250) / 1000.0) if timeout_ms is not None else remaining
    try:
        return await asyncio.wait_for(awaitable, timeout=watchdog)
    except asyncio.TimeoutError:
        raise BackendError(
            CODE_TIMEOUT, f"{what} exceeded its {int(watchdog * 1000)}ms watchdog; operation did not complete",
            deadline_exceeded=True,
        ) from None
    except Exception as exc:
        if is_playwright_timeout(exc) and not isinstance(exc, BackendError):
            raise playwright_timeout_error(exc, what, timeout_ms) from exc
        raise


def step_timeout_ms(ctx: Any) -> int:
    remaining = ctx.remaining_seconds()
    if remaining <= 0:
        raise BackendError(CODE_TIMEOUT, "action deadline exceeded before locator operation", deadline_exceeded=True)
    return max(1, min(STEP_TIMEOUT_MS, int(remaining * 1000) - 250))


def require_main_frame(spec: TargetSpec, action: str) -> None:
    if spec.kind == "selector" and spec.frame_prefix is not None:
        raise BackendError(
            CODE_UNSUPPORTED,
            f"{action} supports main-frame targets only; ref '{spec.ref}' belongs to an iframe",
        )


def resolved_target(ctx: Any, params: Dict[str, Any], field_name: str = "target") -> TargetSpec:
    return parse_target(params.get(field_name), field_name)


async def locator_for(ctx: Any, spec: TargetSpec) -> Tuple[Any, Any]:
    if spec.kind != "selector" or spec.selector is None:
        raise BackendError(CODE_INVALID, "expected a selector target")
    if hasattr(ctx, "action_locator"):
        return await ctx.action_locator(spec)
    if hasattr(ctx, "locator_scope"):
        scope, resolved = ctx.locator_scope(spec)
        return scope, scope.locator(resolved)
    if spec.ref is not None:
        ctx.require_exposed_ref(spec.ref)
        return ctx.page, ctx.page.locator(f"aria-ref={spec.ref}")
    return ctx.page, ctx.page.locator(spec.selector)


async def require_visible(ctx: Any, locator: Any, label: str) -> None:
    matches = await bounded(ctx, locator.count(), f"resolve {label}", STEP_TIMEOUT_MS)
    if matches != 1:
        raise BackendError(
            CODE_INVALID, f"{label}: selector resolved to {matches} elements; expected exactly one",
        )
    if not await bounded(ctx, locator.is_visible(), f"check visibility of {label}", STEP_TIMEOUT_MS):
        raise BackendError(CODE_INVALID, f"{label}: element is hidden; it has no bounding box")


async def element_box(ctx: Any, locator: Any, label: str) -> Dict[str, float]:
    await require_visible(ctx, locator, label)
    timeout_ms = step_timeout_ms(ctx)
    box = await bounded(ctx, locator.bounding_box(timeout=timeout_ms), f"measure bounding box of {label}", timeout_ms)
    if box is None or box["width"] <= 0 or box["height"] <= 0:
        raise BackendError(CODE_INVALID, f"{label}: element is hidden or has no layout box; it has no bounding box")
    return {
        "x": float(box["x"]),
        "y": float(box["y"]),
        "width": float(box["width"]),
        "height": float(box["height"]),
    }


def box_center(box: Dict[str, float]) -> Tuple[float, float]:
    return (box["x"] + box["width"] / 2.0, box["y"] + box["height"] / 2.0)


async def inside_viewport(ctx: Any, box: Dict[str, float], label: str) -> bool:
    viewport = await bounded(ctx, ctx.viewport_size(), f"measure viewport for {label}", STEP_TIMEOUT_MS)
    return (
        box["x"] >= 0
        and box["y"] >= 0
        and box["x"] + box["width"] <= viewport["width"] + 0.5
        and box["y"] + box["height"] <= viewport["height"] + 0.5
    )


async def require_in_viewport(ctx: Any, locator: Any, label: str) -> Dict[str, float]:
    box = await element_box(ctx, locator, label)
    if await inside_viewport(ctx, box, label):
        return box
    timeout_ms = step_timeout_ms(ctx)
    await bounded(ctx, locator.scroll_into_view_if_needed(timeout=timeout_ms), f"scroll {label} into view", timeout_ms)
    box = await element_box(ctx, locator, label)
    if not await inside_viewport(ctx, box, label):
        raise BackendError(
            CODE_INVALID,
            f"{label} does not fit fully inside the current viewport after scrolling",
        )
    return box


async def require_in_viewport_no_scroll(ctx: Any, locator: Any, label: str) -> Dict[str, float]:
    box = await element_box(ctx, locator, label)
    if not await inside_viewport(ctx, box, label):
        raise BackendError(
            CODE_INVALID,
            f"{label} is outside the viewport; scrolling is disabled when a coordinate endpoint is used",
        )
    return box


async def require_stable_box(ctx: Any, locator: Any, label: str) -> Dict[str, float]:
    box = await element_box(ctx, locator, label)
    delay_seconds = min(BOX_STABILITY_DELAY_MS / 1000.0, max(0.0, ctx.remaining_seconds() - 0.5))
    if delay_seconds <= 0:
        return box
    await asyncio.sleep(delay_seconds)
    fresh = await element_box(ctx, locator, label)
    if (abs(fresh["x"] - box["x"]) > 1.0 or abs(fresh["y"] - box["y"]) > 1.0
            or abs(fresh["width"] - box["width"]) > 1.0 or abs(fresh["height"] - box["height"]) > 1.0):
        raise BackendError(CODE_TIMEOUT, f"{label} is unstable (its layout is still changing)")
    return fresh


async def trial_hover(ctx: Any, locator: Any, label: str) -> None:
    dispatch = ctx.input_dispatch()
    if getattr(dispatch, "osnative", False):
        box = await require_stable_box(ctx, locator, label)
        if not await inside_viewport(ctx, box, label):
            raise BackendError(CODE_TIMEOUT, f"hit test for {label}: element is outside the viewport")
        x, y = box_center(box)
        ctx.note_input_dispatched()
        await bounded(ctx, dispatch.move(x, y), f"hit test move for {label}")
        return
    timeout_ms = step_timeout_ms(ctx)
    await bounded(ctx, locator.hover(trial=True, timeout=timeout_ms), f"hit test for {label}", timeout_ms)


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
    dispatch = ctx.input_dispatch()
    ctx.set_stage("move")
    ctx.note_input_dispatched()
    await bounded(ctx, dispatch.move(x, y), "mouse move")
    for index in range(count):
        ctx.check_deadline()
        ctx.set_stage("down")
        journal.begin_button_down(button)
        ctx.note_input_dispatched()
        try:
            await bounded(ctx, dispatch.down(button=button, click_count=index + 1), "mouse down")
            ctx.check_deadline()
            ctx.set_stage("up")
            ctx.note_input_dispatched()
            await bounded(ctx, dispatch.up(button=button, click_count=index + 1), "mouse up")
            journal.finish_button_up(button)
        finally:
            if journal.buttons.get(button):
                try:
                    await asyncio.wait_for(dispatch.up(button=button), timeout=1.5)
                    journal.finish_button_up(button)
                except Exception:
                    pass


async def locator_click(ctx: Any, locator: Any, button: str = "left", count: int = 1,
                        label: Optional[str] = None) -> None:
    label = label or str(locator)
    dispatch = ctx.input_dispatch()
    if getattr(dispatch, "osnative", False):
        await osnative_locator_click(ctx, dispatch, locator, button, count, label)
        return
    timeout_ms = step_timeout_ms(ctx)
    ctx.journal.begin_button_down(button)
    ctx.note_input_dispatched()
    try:
        action = locator.dblclick(button=button, timeout=timeout_ms) if count == 2 else locator.click(button=button, timeout=timeout_ms)
        try:
            await bounded(ctx, action, f"click {label}", timeout_ms)
        except BackendError as exc:
            if exc.code == CODE_TIMEOUT:
                raise BackendError(
                    CODE_TIMEOUT,
                    exc.message + "; inspect a fresh screenshot and consider a coordinate gesture or keyboard interaction; "
                    "input may have been dispatched, so do not automatically retry",
                    deadline_exceeded=exc.deadline_exceeded,
                ) from exc
            raise
        ctx.journal.finish_button_up(button)
    finally:
        if ctx.journal.buttons.get(button):
            try:
                await asyncio.wait_for(dispatch.up(button=button), timeout=1.5)
                ctx.journal.finish_button_up(button)
            except Exception:
                pass


async def osnative_locator_click(ctx: Any, dispatch: Any, locator: Any, button: str,
                                 count: int, label: str) -> None:
    timeout_ms = step_timeout_ms(ctx)
    await bounded(ctx, locator.scroll_into_view_if_needed(timeout=timeout_ms), f"scroll {label} into view", timeout_ms)
    box = await require_stable_box(ctx, locator, label)
    if not await inside_viewport(ctx, box, label):
        raise BackendError(
            CODE_INVALID,
            f"{label} does not fit fully inside the current viewport after scrolling",
        )
    x, y = box_center(box)
    ctx.set_stage("move")
    ctx.note_input_dispatched()
    await bounded(ctx, dispatch.move(x, y), f"move to {label}")
    journal = ctx.journal
    for index in range(count):
        ctx.check_deadline()
        ctx.set_stage("down")
        journal.begin_button_down(button)
        ctx.note_input_dispatched()
        try:
            await bounded(ctx, dispatch.down(button=button, click_count=index + 1), f"mouse down on {label}")
            ctx.check_deadline()
            ctx.set_stage("up")
            ctx.note_input_dispatched()
            await bounded(ctx, dispatch.up(button=button, click_count=index + 1), f"mouse up on {label}")
            journal.finish_button_up(button)
        finally:
            if journal.buttons.get(button):
                try:
                    await asyncio.wait_for(dispatch.up(button=button), timeout=1.5)
                    journal.finish_button_up(button)
                except Exception:
                    pass


async def keyboard_press(ctx: Any, key: str) -> None:
    ctx.input_dispatch().validate_press(key)
    keys = key.split("+")
    for item in keys:
        if item == "ControlOrMeta":
            ctx.journal.begin_key_down("Control")
            ctx.journal.begin_key_down("Meta")
        else:
            ctx.journal.begin_key_down(item)
    ctx.note_input_dispatched()
    try:
        await bounded(ctx, ctx.input_dispatch().press(key), "keyboard press")
    finally:
        for item in ctx.journal.pending_keys():
            try:
                await asyncio.wait_for(ctx.input_dispatch().key_up(item), timeout=0.5)
                ctx.journal.finish_key_up(item)
            except Exception:
                pass
