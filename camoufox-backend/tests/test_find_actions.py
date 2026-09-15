import contextlib
import io
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
LIFECYCLE_TEST_DIR = BACKEND_DIR.parent / "test" / "camoufox"
for directory in (BACKEND_DIR, LIFECYCLE_TEST_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_lifecycle import FAKE_RUNTIME_DIR, FakeFrame, FakePage, make_runtime
from input_context import BackendError, CODE_INVALID

with contextlib.redirect_stdout(io.StringIO()):
    import worker

from worker import ACTION_FIELDS, BROWSER_ACTIONS, FIND_ACTIONS, MUTATING_ACTIONS


FIND_CSS_SCRIPT_PREFIX = "(element) => { const path = [];"


class FindLocator:
    def __init__(self, matches=1, text="Continue"):
        self.matches = matches
        self.text = text
        self.calls = []
        self.nth_index = None

    @property
    def first(self):
        return self

    async def count(self):
        self.calls.append("count")
        return self.matches

    async def evaluate(self, script):
        self.calls.append(("evaluate", script))
        if script.startswith(FIND_CSS_SCRIPT_PREFIX):
            return "#submit"
        return self.text

    def nth(self, index):
        self.nth_index = index
        return self


class FindFrame(FakeFrame):
    def __init__(self, page):
        super().__init__(page)
        self.role_queries = []
        self.text_queries = []
        self.selectors = []

    def get_by_role(self, role, name=None, exact=False):
        self.role_queries.append((role, name, exact))
        return self.page.role_locator

    def get_by_text(self, text, exact=False):
        self.text_queries.append((text, exact))
        return self.page.text_locator

    def locator(self, selector):
        self.selectors.append(selector)
        return self.page.selector_locator


class FindPage(FakePage):
    def __init__(self):
        super().__init__()
        self.role_locator = None
        self.text_locator = None
        self.selector_locator = None
        self.main_frame = FindFrame(self)
        self.frames = [self.main_frame]


class FindActionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, _browser, _context, _page, self.tab = make_runtime()
        self.page = FindPage()
        self.tab.page = self.page
        self.instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        self.instance.runtime = self.runtime
        self.instance.load_gestures()

    async def do_find(self, action, **fields):
        return await self.instance.do_find(
            self.runtime, action, {"id": "find-1", "action": action, **fields}
        )

    def test_protocol_exposes_find_actions_without_mutation_flags(self):
        self.assertTrue(FIND_ACTIONS <= BROWSER_ACTIONS)
        self.assertEqual(
            {
                "getbyrole", "getbytext", "getbylabel", "getbyplaceholder",
                "getbyalttext", "getbytitle", "getbytestid", "nth",
            },
            FIND_ACTIONS,
        )
        self.assertEqual({"role", "subaction", "name", "exact", "value"}, ACTION_FIELDS["getbyrole"])
        self.assertEqual({"text", "subaction", "exact", "value"}, ACTION_FIELDS["getbytext"])
        self.assertEqual({"label", "subaction", "exact", "value"}, ACTION_FIELDS["getbylabel"])
        self.assertEqual({"placeholder", "subaction", "exact", "value"}, ACTION_FIELDS["getbyplaceholder"])
        self.assertEqual({"text", "subaction", "exact", "value"}, ACTION_FIELDS["getbyalttext"])
        self.assertEqual({"text", "subaction", "exact", "value"}, ACTION_FIELDS["getbytitle"])
        self.assertEqual({"testId", "subaction", "value"}, ACTION_FIELDS["getbytestid"])
        self.assertEqual({"selector", "index", "subaction", "value"}, ACTION_FIELDS["nth"])
        for action in FIND_ACTIONS:
            self.assertNotIn(action, MUTATING_ACTIONS)

    async def test_text_subaction_returns_found_count_selector_and_text(self):
        self.page.role_locator = FindLocator(matches=1, text="Continue")
        result = await self.do_find(
            "getbyrole", role="button", subaction="text", name="Continue", exact=True
        )
        self.assertEqual(
            {"found": True, "count": 1, "selector": "#submit", "text": "Continue"}, result
        )
        self.assertEqual([("button", "Continue", True)], self.page.main_frame.role_queries)

    async def test_role_lookup_without_name_omits_the_name_argument(self):
        self.page.role_locator = FindLocator(matches=1)
        result = await self.do_find("getbyrole", role="main", subaction="text", name=None, exact=False)
        self.assertEqual({"found": True, "count": 1, "selector": "#submit", "text": "Continue"}, result)
        self.assertEqual([("main", None, False)], self.page.main_frame.role_queries)

    async def test_text_locator_passes_exact_flag(self):
        self.page.text_locator = FindLocator(matches=1, text="hello")
        result = await self.do_find("getbytext", text="hello", subaction="text", exact=True)
        self.assertEqual({"found": True, "count": 1, "selector": "#submit", "text": "hello"}, result)
        self.assertEqual([("hello", True)], self.page.main_frame.text_queries)

    async def test_zero_matches_is_an_invalid_params_error(self):
        self.page.role_locator = FindLocator(matches=0)
        with self.assertRaises(BackendError) as captured:
            await self.do_find("getbyrole", role="button", subaction="click", name=None, exact=False)
        self.assertEqual(CODE_INVALID, captured.exception.code)
        self.assertIn("matched 0 elements", captured.exception.message)

    async def test_ambiguous_match_for_an_acting_subaction_is_rejected(self):
        self.page.role_locator = FindLocator(matches=2)
        with self.assertRaises(BackendError) as captured:
            await self.do_find("getbyrole", role="button", subaction="click", name=None, exact=False)
        self.assertEqual(CODE_INVALID, captured.exception.code)
        self.assertIn("matched 2 elements", captured.exception.message)

    async def test_ambiguous_match_for_text_subaction_reads_the_first_match(self):
        self.page.role_locator = FindLocator(matches=2, text="first")
        result = await self.do_find("getbyrole", role="button", subaction="text", name=None, exact=False)
        self.assertEqual({"found": True, "count": 2, "selector": "#submit", "text": "first"}, result)

    async def test_acting_subaction_delegates_with_the_derived_selector(self):
        self.page.role_locator = FindLocator(matches=1)
        delegated = {}

        async def fake_dispatch(action, payload):
            delegated.update(action=action, payload=payload)
            return {"action": "click"}

        self.instance.dispatch_browser = fake_dispatch
        result = await self.do_find("getbyrole", role="button", subaction="click", name=None, exact=False)
        self.assertEqual("click", delegated["action"])
        self.assertEqual("#submit", delegated["payload"]["selector"])
        self.assertEqual(
            {"found": True, "count": 1, "selector": "#submit", "action": "click"}, result
        )

    async def test_fill_subaction_requires_a_value(self):
        with self.assertRaises(BackendError) as captured:
            await self.do_find("getbytestid", testId="search", subaction="fill")
        self.assertEqual(CODE_INVALID, captured.exception.code)
        self.assertIn("fill subaction requires a value", captured.exception.message)

    async def test_nth_negative_index_resolves_through_the_selector_machinery(self):
        locator = FindLocator(matches=1, text="last")
        self.page.selector_locator = locator
        result = await self.do_find("nth", selector="nav a", index=-1, subaction="text")
        self.assertEqual({"found": True, "count": 1, "selector": "#submit", "text": "last"}, result)
        self.assertEqual(["nav a"], self.page.main_frame.selectors)
        self.assertEqual(-1, locator.nth_index)


if __name__ == "__main__":
    unittest.main()