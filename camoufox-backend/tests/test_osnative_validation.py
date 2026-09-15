import asyncio
import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
LIFECYCLE_TEST_DIR = BACKEND_DIR.parent / "test" / "camoufox"
for directory in (BACKEND_DIR, LIFECYCLE_TEST_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_lifecycle import FAKE_RUNTIME_DIR, make_runtime
from input_context import BackendError, CODE_INVALID
from osnative_input import LATIN_KEYSYMS

with contextlib.redirect_stdout(io.StringIO()):
    import worker


class FakeOsnativeDispatch:
    osnative = True

    def __init__(self, mappable_text):
        self.mappable_text = mappable_text
        self.dispatched = []

    def validate_text(self, text):
        for character in text:
            if character not in self.mappable_text:
                raise BackendError(
                    CODE_INVALID,
                    f"os-native input cannot map key '{character}': no X keycode is bound to '{character}'",
                )

    def validate_press(self, key):
        if key not in ("Enter", "Return"):
            raise BackendError(CODE_INVALID, f"os-native input cannot map key '{key}'")

    async def type(self, text, delay=0):
        self.dispatched.append(("type", text))

    async def press(self, key):
        self.dispatched.append(("press", key))

    async def key_up(self, key):
        pass

    async def move(self, x, y, steps=None):
        pass

    async def down(self, button="left", click_count=None):
        pass

    async def up(self, button="left", click_count=None):
        pass

    async def wheel(self, delta_x, delta_y):
        pass

    async def _guard(self):
        pass


class FakeJournal:
    def __init__(self):
        self.keys = {}

    def begin_key_down(self, key):
        self.keys[key] = True

    def finish_key_up(self, key):
        self.keys.pop(key, None)

    def pending_keys(self):
        return sorted(self.keys.keys())

    def pending_buttons(self):
        return []


class UnmappableTypeRegression(unittest.TestCase):
    """A pre-dispatch keymap validation failure must not poison the session.

    The os-native backend cannot map some characters to X keycodes. That
    validation now runs before any journaling or input attempt, and the
    deterministic BackendError(CODE_INVALID) must not mark input ambiguous:
    nothing was dispatched, so the session stays usable.
    """

    def test_unmappable_character_error_is_clean_and_session_survives(self):
        async def scenario():
            runtime = make_runtime()[0]
            runtime._input_attempts = 0
            dispatch = FakeOsnativeDispatch("Bogot")
            journal = FakeJournal()
            runtime.journal = journal
            runtime.GestureContextClass = lambda _runtime, remaining_ms: SimpleNamespace(
                input_dispatch=lambda: dispatch,
                journal=journal,
                set_stage=lambda _stage: None,
                note_input_dispatched=lambda: setattr(
                    runtime, "_input_attempts", getattr(runtime, "_input_attempts", 0) + 1
                ),
                diagnostics=lambda: {},
                remaining_ms=lambda: 20000,
            )
            runtime.release_inputs = AsyncMock(
                return_value={"released": True, "buttons": [], "keys": []}
            )
            backend = worker.Worker.__new__(worker.Worker)
            backend.poisoned = False
            backend.poison = lambda message: setattr(backend, "poisoned", True)
            backend.deadline_ms = 20000
            backend._input_attempted_since = lambda _runtime, before: getattr(
                runtime, "_input_attempts", 0
            ) > before

            spec = worker.GestureSpec(
                name="type",
                description="test",
                schema={},
                examples=[],
                run=_run_type_gesture_with_fakes,
                module="<test>",
                source="<test>",
            )
            with self.assertRaises(BackendError) as caught:
                await backend._execute_gesture(
                    runtime, spec, {"selector": "#searchInput", "text": "Bogotá"}
                )
            self.assertIn("cannot map key 'á'", caught.exception.message)
            self.assertFalse(
                backend.poisoned,
                "a pre-dispatch validation failure must not poison the session",
            )
            self.assertEqual(journal.pending_keys(), [])
            self.assertEqual(dispatch.dispatched, [])

        asyncio.run(scenario())


async def _run_type_gesture_with_fakes(context, params):
    from gestures._common import bounded

    text = params["text"]
    dispatch = context.input_dispatch()
    context.note_input_dispatched()
    dispatch.validate_text(text)
    for character in text:
        context.journal.begin_key_down(character)
        try:
            await bounded(context, dispatch.type(character), "keyboard type")
            context.journal.finish_key_up(character)
        finally:
            if context.journal.keys.get(character):
                context.journal.keys.pop(character, None)


class LatinKeymapContract(unittest.TestCase):
    def test_latin_keysym_list_contains_core_accents(self):
        required = {"aacute", "eacute", "iacute", "oacute", "uacute", "ntilde"}
        self.assertTrue(required.issubset(set(LATIN_KEYSYMS)))


if __name__ == "__main__":
    unittest.main()