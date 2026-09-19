import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

BACKEND_DIR = Path(__file__).resolve().parents[1]
LIFECYCLE_TEST_DIR = BACKEND_DIR.parent / "test" / "camoufox"
for directory in (BACKEND_DIR, LIFECYCLE_TEST_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from input_context import BackendError, CODE_ERROR, CODE_INVALID, CODE_STALE_REF, CODE_UNSUPPORTED
from element_registry import (
    _FIELDS,
    _REGISTRY_INSTALL_SCRIPT,
    _REGISTRY_OP_SCRIPT,
    _RELATIONS,
    ElementCatalog,
)

with contextlib.redirect_stdout(io.StringIO()):
    import worker

from test_lifecycle import FAKE_RUNTIME_DIR, FakeFrame, FakePage, make_runtime


class FindPage(FakePage):
    def __init__(self):
        super().__init__()
        self.role_locator = None
        self.text_locator = None
        self.selector_locator = None


def card(element_id="el_abc_1"):
    return {
        "kind": "element_card",
        "elementId": element_id,
        "node": {"tag": "button", "id": None, "classes": "btn", "role": "button", "name": "Guardar"},
        "text": {"direct": "Guardar", "full": "Guardar"},
        "candidates": [
            {
                "strategy": "testid",
                "path": '[data-testid="save"]',
                "resolver": "native-root-css",
                "documentMatches": 1,
                "sameNode": True,
                "verified": True,
                "actionEligible": True,
                "stability": "observed-key",
                "flags": [],
                "length": 21,
            }
        ],
        "relations": {"parent": None, "component": {"elementId": "el_abc_9", "key": "Panel", "source": "data-component"}},
        "geometry": None,
        "inShadowRoot": False,
        "observation": {"documentEpoch": "el_abc", "sameNode": True, "verified": True, "queriesUsed": 1, "budgetExhausted": False},
    }


def expansion(element_id="el_abc_1"):
    return {
        "kind": "element_expansion",
        "elementId": element_id,
        "relation": "ancestors",
        "nodes": [
            {"elementId": "el_abc_2", "tag": "form", "role": None, "name": None, "directText": None,
             "isComponentBoundary": True, "geometry": None, "depth": 1}
        ],
        "omitted": 0,
        "truncated": False,
        "budgetExhausted": False,
    }


class FakeFrame:
    def __init__(self, responses=None):
        self.url = "https://example.com/app"
        self.evaluate = AsyncMock(side_effect=responses if responses is not None else [])
        self.scripts = []

    def installs(self):
        return [call.args[0] for call in self.evaluate.await_args_list]


class ElementCatalogInspectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = SimpleNamespace()
        self.catalog = ElementCatalog(self.runtime)

    async def test_inspect_requires_exactly_one_target(self):
        frame = FakeFrame([{"installed": True, "nonce": "abc", "version": 1}, card()])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.inspect(frame, selector=None, element_id=None)
        self.assertEqual(CODE_INVALID, raised.exception.code)
        frame.evaluate.assert_not_awaited()

        frame2 = FakeFrame([{"installed": True, "nonce": "abc", "version": 1}, card()])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.inspect(frame2, selector="#a", element_id="el_abc_1")
        self.assertEqual(CODE_INVALID, raised.exception.code)
        frame2.evaluate.assert_not_awaited()

    async def test_inspect_installs_controller_then_runs_op(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            card(),
        ])
        result = await self.catalog.inspect(frame, selector="#save", fields=["geometry"])
        self.assertEqual("element_card", result["kind"])
        self.assertEqual("el_abc_1", result["elementId"])
        scripts = frame.installs()
        self.assertEqual(_REGISTRY_INSTALL_SCRIPT, scripts[0])
        self.assertEqual(_REGISTRY_OP_SCRIPT, scripts[1])
        payload = frame.evaluate.await_args_list[1].args[1]
        self.assertEqual(
            {"op": "inspect", "selector": "#save", "fields": ["geometry"]},
            payload,
        )

    async def test_controller_installed_once_across_calls(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            card(),
            card(),
        ])
        await self.catalog.inspect(frame, selector="#save")
        await self.catalog.inspect(frame, selector="#save")
        installs = [s for s in frame.installs() if s == _REGISTRY_INSTALL_SCRIPT]
        self.assertEqual(1, len(installs))

    async def test_controller_reinstalled_after_navigation(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            card(),
            {"installed": True, "nonce": "def", "version": 1},
            card(),
        ])
        await self.catalog.inspect(frame, selector="#save")
        frame.url = "https://example.com/other"
        await self.catalog.inspect(frame, selector="#save")
        installs = [s for s in frame.installs() if s == _REGISTRY_INSTALL_SCRIPT]
        self.assertEqual(2, len(installs))

    async def test_missing_controller_is_reinstalled_once(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            {"__abMissing": True},
            {"installed": True, "nonce": "abc", "version": 1},
            card(),
        ])
        result = await self.catalog.inspect(frame, selector="#save")
        self.assertEqual("element_card", result["kind"])

    async def test_missing_controller_twice_is_an_error(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            {"__abMissing": True},
            {"installed": True, "nonce": "abc", "version": 1},
            {"__abMissing": True},
        ])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.inspect(frame, selector="#save")
        self.assertEqual(CODE_ERROR, raised.exception.code)

    async def test_controller_install_without_nonce_fails(self):
        frame = FakeFrame([{"installed": True}])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.inspect(frame, selector="#save")
        self.assertEqual(CODE_ERROR, raised.exception.code)

    async def test_unsupported_field_is_rejected_before_evaluating_ops(self):
        frame = FakeFrame([{"installed": True, "nonce": "abc", "version": 1}])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.inspect(frame, selector="#save", fields=["html"])
        self.assertEqual(CODE_INVALID, raised.exception.code)
        self.assertEqual(["geometry"], list(_FIELDS))

    async def test_selector_length_is_bounded(self):
        frame = FakeFrame([{"installed": True, "nonce": "abc", "version": 1}])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.inspect(frame, selector="a" * 5000)
        self.assertEqual(CODE_INVALID, raised.exception.code)

    async def test_worker_error_codes_are_translated(self):
        cases = [
            ("ambiguous", CODE_INVALID),
            ("not_found", CODE_INVALID),
            ("expired", CODE_STALE_REF),
            ("internal", CODE_ERROR),
            ("something_unknown", CODE_ERROR),
        ]
        for worker_code, expected in cases:
            with self.subTest(worker_code=worker_code):
                frame = FakeFrame([
                    {"installed": True, "nonce": "abc", "version": 1},
                    {"error": {"code": worker_code, "message": "boom"}},
                ])
                with self.assertRaises(BackendError) as raised:
                    await self.catalog.inspect(frame, selector="#save")
                self.assertEqual(expected, raised.exception.code)

    async def test_unexpected_kind_is_rejected(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            {"kind": "element_expansion"},
        ])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.inspect(frame, selector="#save")
        self.assertEqual(CODE_ERROR, raised.exception.code)

    async def test_non_object_response_is_rejected(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            "nope",
        ])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.inspect(frame, selector="#save")
        self.assertEqual(CODE_ERROR, raised.exception.code)


class ElementCatalogExpandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = ElementCatalog(SimpleNamespace())

    async def test_expand_rejects_unknown_relation(self):
        frame = FakeFrame([{"installed": True, "nonce": "abc", "version": 1}])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.expand(frame, element_id="el_abc_1", relation="cousins")
        self.assertEqual(CODE_INVALID, raised.exception.code)
        self.assertEqual(("parent", "ancestors", "siblings", "subtree"), _RELATIONS)
        frame.evaluate.assert_not_awaited()

    async def test_expand_defaults_relation_to_parent(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            expansion(),
        ])
        result = await self.catalog.expand(frame, element_id="el_abc_1")
        self.assertEqual("element_expansion", result["kind"])
        payload = frame.evaluate.await_args_list[1].args[1]
        self.assertEqual("parent", payload["relation"])
        self.assertNotIn("limit", payload)

    async def test_expand_passes_limit_and_budget(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            expansion(),
        ])
        await self.catalog.expand(
            frame, element_id="el_abc_1", relation="subtree", limit=25, budget={"maxQueries": 3},
        )
        payload = frame.evaluate.await_args_list[1].args[1]
        self.assertEqual(
            {"op": "expand", "elementId": "el_abc_1", "relation": "subtree", "limit": 25,
             "budget": {"maxQueries": 3}},
            payload,
        )

    async def test_expand_expired_id_is_stale(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            {"error": {"code": "expired", "message": "gone"}},
        ])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.expand(frame, element_id="el_abc_1", relation="parent")
        self.assertEqual(CODE_STALE_REF, raised.exception.code)


class ElementCatalogScriptTests(unittest.TestCase):
    def test_install_script_never_injects_into_the_application_dom(self):
        for forbidden in ("createElement", "appendChild", "innerHTML", "setAttribute", "insertAdjacent"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, _REGISTRY_INSTALL_SCRIPT)

    def test_identity_generator_is_geometry_independent(self):
        start = _REGISTRY_INSTALL_SCRIPT.index("const idFor = ")
        end = _REGISTRY_INSTALL_SCRIPT.index("const nodeFor = ")
        generator = _REGISTRY_INSTALL_SCRIPT[start:end]
        for forbidden in ("getBoundingClientRect", "scrollX", "scrollY", "innerWidth", "innerHeight"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, generator)
        self.assertIn('"el_" + nonce + "_" + sequence', generator)

    def test_install_script_is_idempotent_by_version(self):
        self.assertIn("__abRegistry", _REGISTRY_INSTALL_SCRIPT)
        self.assertIn("existing.version === VERSION", _REGISTRY_INSTALL_SCRIPT)

    def test_op_script_guards_against_a_missing_controller(self):
        self.assertIn("__abMissing", _REGISTRY_OP_SCRIPT)

    def test_candidate_eligibility_requires_same_node(self):
        self.assertIn("matches[0] === target", _REGISTRY_INSTALL_SCRIPT)

    def test_unknown_candidates_are_never_reported_as_matching(self):
        self.assertIn("budget.queries >= budget.maxQueries", _REGISTRY_INSTALL_SCRIPT)


class WorkerDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        self.runtime = SimpleNamespace()
        self.runtime.require_active = lambda: None
        self.instance.runtime = self.runtime
        self.instance.require_available = lambda action: None
        self.instance.get_runtime = lambda: self.runtime

    async def request(self, action, **fields):
        return await self.instance.run_action(action, {"id": "req", "action": action, **fields})

    def test_actions_are_registered_and_read_only(self):
        self.assertIn("element_inspect", worker.BROWSER_ACTIONS)
        self.assertIn("element_expand", worker.BROWSER_ACTIONS)
        self.assertEqual(
            {"selector", "elementId", "ref", "fields", "maxQueries"},
            worker.ACTION_FIELDS["element_inspect"],
        )
        self.assertEqual(
            {"elementId", "relation", "limit", "maxQueries"},
            worker.ACTION_FIELDS["element_expand"],
        )
        for action in ("element_inspect", "element_expand"):
            with self.subTest(action=action):
                self.assertNotIn(action, worker.MUTATING_ACTIONS)
                self.assertNotIn(action, worker.INPUT_AMBIENT_STOP)
                self.assertNotIn(action, worker.LIFECYCLE_AMBIENT_STOP)
                self.assertNotIn(action, worker.FIND_ACTIONS)

    async def test_inspect_dispatch_forwards_validated_arguments(self):
        calls = {}

        async def fake(selector, element_id, ref, fields, max_queries):
            calls.update(selector=selector, element_id=element_id, ref=ref,
                         fields=fields, max_queries=max_queries)
            return {"kind": "element_card", "elementId": "el_x"}

        self.runtime.element_inspect = fake
        result = await self.request(
            "element_inspect", selector="#save", fields=["geometry"], maxQueries=12,
        )
        self.assertEqual("element_card", result["kind"])
        self.assertEqual(
            {"selector": "#save", "element_id": None, "ref": None,
             "fields": ["geometry"], "max_queries": 12},
            calls,
        )

    async def test_inspect_rejects_unsupported_field(self):
        async def fake(*args):
            raise AssertionError("must not reach the runtime")

        self.runtime.element_inspect = fake
        with self.assertRaises(BackendError) as raised:
            await self.request("element_inspect", selector="#save", fields=["html"])
        self.assertEqual(CODE_INVALID, raised.exception.code)

    async def test_expand_dispatch_forwards_validated_arguments(self):
        calls = {}

        async def fake(element_id, relation, limit, max_queries):
            calls.update(element_id=element_id, relation=relation, limit=limit, max_queries=max_queries)
            return {"kind": "element_expansion", "elementId": element_id}

        self.runtime.element_expand = fake
        result = await self.request(
            "element_expand", elementId="el_x", relation="subtree", limit=25,
        )
        self.assertEqual("element_expansion", result["kind"])
        self.assertEqual(
            {"element_id": "el_x", "relation": "subtree", "limit": 25, "max_queries": None},
            calls,
        )

    async def test_expand_defaults_relation_and_requires_element_id(self):
        calls = {}

        async def fake(element_id, relation, limit, max_queries):
            calls.update(element_id=element_id, relation=relation)
            return {"kind": "element_expansion", "elementId": element_id}

        self.runtime.element_expand = fake
        await self.request("element_expand", elementId="el_x")
        self.assertEqual("parent", calls["relation"])
        with self.assertRaises(BackendError) as raised:
            await self.request("element_expand", relation="parent")
        self.assertEqual(CODE_INVALID, raised.exception.code)

    async def test_expand_rejects_unknown_relation(self):
        async def fake(*args):
            raise AssertionError("must not reach the runtime")

        self.runtime.element_expand = fake
        with self.assertRaises(BackendError) as raised:
            await self.request("element_expand", elementId="el_x", relation="cousins")
        self.assertEqual(CODE_INVALID, raised.exception.code)

    async def test_unknown_field_is_rejected_before_dispatch(self):
        async def fake(*args, **kwargs):
            raise AssertionError("must not reach the runtime")

        self.runtime.element_inspect = fake
        with self.assertRaises(BackendError) as raised:
            await self.request("element_inspect", selector="#save", bogus=True)
        self.assertEqual(CODE_UNSUPPORTED, raised.exception.code)


class HitTestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = ElementCatalog(SimpleNamespace())

    async def test_hit_test_shape_is_translated(self):
        payload = {
            "kind": "hit_test",
            "point": {"x": 10.0, "y": 20.0},
            "layers": [{"elementId": "el_abc_1", "tag": "button", "role": "button", "name": "Save",
                        "directText": "Save", "isComponentBoundary": False, "pointerEvents": "auto",
                        "disabled": False, "box": {"x": 0.0, "y": 0.0, "width": 40.0, "height": 20.0}}],
            "omittedLayers": 0,
            "hit": True,
            "topElementId": "el_abc_1",
        }
        frame = FakeFrame([{"installed": True, "nonce": "abc", "version": 1}, payload])
        result = await self.catalog.hit_test(frame, x=10.0, y=20.0)
        self.assertEqual("hit_test", result["kind"])
        self.assertEqual("el_abc_1", result["topElementId"])
        request = frame.evaluate.await_args_list[1].args[1]
        self.assertEqual({"op": "hit_test", "x": 10.0, "y": 20.0}, request)

    async def test_hit_test_rejects_non_finite_point(self):
        frame = FakeFrame([
            {"installed": True, "nonce": "abc", "version": 1},
            {"error": {"code": "invalid_request", "message": "bad point"}},
        ])
        with self.assertRaises(BackendError) as raised:
            await self.catalog.hit_test(frame, x=float("nan"), y=0.0)
        self.assertEqual(CODE_INVALID, raised.exception.code)


class VisualTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, self.browser, self.context, page, self.tab = make_runtime()
        self.page = FindPage()
        self.tab.page = self.page
        self.instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        self.instance.runtime = self.runtime

    def stub_capture(self):
        return self.runtime.captures.register({
            "imageWidth": 800, "imageHeight": 600,
            "viewportWidth": 800.0, "viewportHeight": 600.0,
            "devicePixelRatio": 1.0, "scrollX": 0.0, "scrollY": 0.0,
            "url": "about:blank", "tabId": self.tab.tab_id, "capturedAt": "now",
        })

    def stub_identity(self, width=800.0, height=600.0):
        async def identity():
            return {"viewportWidth": width, "viewportHeight": height, "scrollX": 0.0,
                    "scrollY": 0.0, "devicePixelRatio": 1.0, "url": "about:blank",
                    "tabId": self.tab.tab_id}
        self.runtime.capture_identity = identity

    def stub_hit(self, layers):
        async def hit(frame, *, x, y):
            return {"kind": "hit_test", "point": {"x": x, "y": y}, "layers": layers,
                    "omittedLayers": 0, "hit": bool(layers),
                    "topElementId": layers[0]["elementId"] if layers else None}
        self.runtime.element_catalog.hit_test = hit

    def layer(self, element_id, x=10.0, y=20.0, width=80.0, height=30.0):
        return {"elementId": element_id, "tag": "button", "role": "button", "name": "Save",
                "directText": "Save", "isComponentBoundary": False, "pointerEvents": "auto",
                "disabled": False, "box": {"x": x, "y": y, "width": width, "height": height}}

    async def test_snaps_to_element_centre_not_the_raw_point(self):
        capture_id = self.stub_capture()
        self.stub_identity()
        self.stub_hit([self.layer("el_top")])
        result = await self.runtime.visual_target(capture_id, 15.0, 25.0, None)
        self.assertEqual("visual_target", result["kind"])
        self.assertEqual("el_top", result["elementId"])
        self.assertEqual({"x": 15.0, "y": 25.0}, result["imagePoint"])
        self.assertEqual({"x": 50.0, "y": 35.0}, result["snappedPoint"])

    async def test_expected_element_must_be_in_the_stack(self):
        capture_id = self.stub_capture()
        self.stub_identity()
        self.stub_hit([self.layer("el_top"), self.layer("el_below")])
        result = await self.runtime.visual_target(capture_id, 15.0, 25.0, "el_below")
        self.assertTrue(result["matchedExpected"])
        self.assertEqual("el_below", result["elementId"])

        with self.assertRaises(BackendError) as raised:
            await self.runtime.visual_target(capture_id, 15.0, 25.0, "el_missing")
        self.assertEqual(CODE_STALE_REF, raised.exception.code)

    async def test_unknown_capture_is_rejected(self):
        self.stub_identity()
        self.stub_hit([self.layer("el_top")])
        with self.assertRaises(BackendError):
            await self.runtime.visual_target("nope", 1.0, 1.0, None)

    async def test_empty_stack_reports_no_element(self):
        capture_id = self.stub_capture()
        self.stub_identity()
        self.stub_hit([])
        with self.assertRaises(BackendError) as raised:
            await self.runtime.visual_target(capture_id, 1.0, 1.0, None)
        self.assertEqual(CODE_INVALID, raised.exception.code)

    async def test_point_outside_the_captured_image_is_rejected(self):
        capture_id = self.stub_capture()
        self.stub_identity(width=800.0, height=600.0)
        self.stub_hit([self.layer("el_top")])
        with self.assertRaises(BackendError) as raised:
            await self.runtime.visual_target(capture_id, 900.0, 25.0, None)
        self.assertEqual(CODE_INVALID, raised.exception.code)
        self.assertIn("captured image", raised.exception.message)

    async def test_visual_target_dispatches_through_the_worker(self):
        calls = {}

        async def fake(capture_id, x, y, expect_element_id):
            calls.update(capture_id=capture_id, x=x, y=y, expect=expect_element_id)
            return {"kind": "visual_target", "elementId": "el_top"}

        self.runtime.visual_target = fake
        self.instance.require_available = lambda action: None
        self.instance.get_runtime = lambda: self.runtime
        result = await self.instance.run_action(
            "visual_target",
            {"id": "req", "action": "visual_target", "captureId": "cap_1",
             "x": 12.5, "y": 30, "expectElementId": "el_top"},
        )
        self.assertEqual("visual_target", result["kind"])
        self.assertEqual(
            {"capture_id": "cap_1", "x": 12.5, "y": 30.0, "expect": "el_top"},
            calls,
        )

    async def test_visual_target_requires_a_capture_id(self):
        calls = {}

        async def fake(capture_id, x, y, expect_element_id):
            calls.update(capture_id=capture_id)
            return {"kind": "visual_target"}

        self.runtime.visual_target = fake
        with self.assertRaises(BackendError) as raised:
            await self.instance.run_action(
                "visual_target",
                {"id": "req", "action": "visual_target", "x": 1, "y": 1},
            )
        self.assertEqual(CODE_INVALID, raised.exception.code)

    async def test_visual_target_rejects_a_non_finite_point(self):
        async def fake(*args):
            raise AssertionError("must not reach the runtime")

        self.runtime.visual_target = fake
        with self.assertRaises(BackendError) as raised:
            await self.instance.run_action(
                "visual_target",
                {"id": "req", "action": "visual_target", "captureId": "cap_1", "x": 1},
            )
        self.assertEqual(CODE_INVALID, raised.exception.code)

    async def test_visual_target_rejects_a_non_main_frame(self):
        capture_id = self.stub_capture()
        self.stub_identity()
        self.stub_hit([self.layer("el_top")])

        class SelectedFrame:
            def is_detached(self):
                return False
            url = "about:blank"

        self.tab.selected_frame = SelectedFrame()
        with self.assertRaises(BackendError) as raised:
            await self.runtime.visual_target(capture_id, 10.0, 10.0, None)
        self.assertEqual(CODE_INVALID, raised.exception.code)
        self.assertIn("non-main frame", raised.exception.message)

    async def test_visual_target_is_registered_read_only(self):
        self.assertIn("visual_target", worker.BROWSER_ACTIONS)
        self.assertEqual(
            {"captureId", "x", "y", "expectElementId"},
            worker.ACTION_FIELDS["visual_target"],
        )
        self.assertNotIn("visual_target", worker.MUTATING_ACTIONS)


if __name__ == "__main__":
    unittest.main()
