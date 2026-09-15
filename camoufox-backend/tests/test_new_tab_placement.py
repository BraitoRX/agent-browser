import asyncio
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from runtime import CamoufoxRuntime, Tab


class StubPage:
    def __init__(self, url="https://example.com/"):
        self.url = url
        self.opened_urls = []
        self.listeners = []

    def on(self, event, handler):
        self.listeners.append((event, handler))

    async def evaluate(self, script, *args):
        self.opened_urls.append(args[0] if args else None)
        return None


class NewTabRuntime(CamoufoxRuntime):
    def __init__(self, pages, active_page=None, popup_during_evaluate=None):
        self.pages = list(pages)
        self.active_page_value = active_page
        self.new_page_calls = 0
        self.tabs = {}
        self._page_to_tab = {}
        self.active_id = None
        self._tab_counter = 0
        self.popup_during_evaluate = popup_during_evaluate
        self.claimed_ids = set()

    async def _new_page(self):
        self.new_page_calls += 1
        page = StubPage()
        self.pages.append(page)
        return page

    def _active_page_or_none(self):
        return self.active_page_value

    def _register_tab(self, page, label):
        self.claimed_ids.add(id(page))
        tab = Tab(f"t{self._tab_counter + 1}", page, label=label)
        self._tab_counter += 1
        self.tabs[tab.tab_id] = tab
        self._page_to_tab[id(page)] = tab.tab_id
        return tab

    async def _open_tab_page(self):
        page = self._active_page_or_none()
        if page is None:
            return await self._new_page()
        before = {id(candidate) for candidate in self.pages}
        try:
            await asyncio.wait_for(page.evaluate("(url) => window.open(url, '_blank')", "about:blank"), timeout=5)
        except Exception:
            return await self._new_page()
        for _ in range(50):
            await asyncio.sleep(0.05)
            for candidate in self.pages:
                if id(candidate) not in before and id(candidate) != id(page):
                    return candidate
            if self._active_page_or_none() is None:
                break
        return await self._new_page()


class NewTabTests(unittest.IsolatedAsyncioTestCase):
    def test_uses_window_open_from_active_page_and_finds_new_context_page(self):
        active = StubPage()
        popup = StubPage(url="about:blank")
        runtime = NewTabRuntime([active], active_page=active, popup_during_evaluate=popup)

        class OpeningPage(StubPage):
            async def evaluate(self, script, *args):
                await StubPage.evaluate(self, script, *args)
                asyncio.get_event_loop().call_later(0.06, runtime.pages.append, popup)
                return None

        runtime.pages[0] = OpeningPage()
        runtime.active_page_value = runtime.pages[0]

        async def scenario():
            return await runtime._open_tab_page()

        page = asyncio.run(scenario())
        self.assertIs(page, popup)
        self.assertEqual(runtime.pages[0].opened_urls, ["about:blank"])
        self.assertEqual(runtime.new_page_calls, 0)

    def test_skips_popup_already_claimed_by_event_handler(self):
        active = StubPage()
        popup = StubPage(url="about:blank")
        runtime = NewTabRuntime([active], active_page=active, popup_during_evaluate=popup)
        runtime._register_tab(popup, label=None)

        class OpeningPage(StubPage):
            async def evaluate(self, script, *args):
                await StubPage.evaluate(self, script, *args)
                asyncio.get_event_loop().call_later(0.06, runtime.pages.append, popup)
                return None

        runtime.pages[0] = OpeningPage()
        runtime.active_page_value = runtime.pages[0]

        async def scenario():
            return await runtime._open_tab_page()

        page = asyncio.run(scenario())
        self.assertIs(page, popup)
        self.assertEqual(runtime.claimed_ids, {id(popup)})

    def test_falls_back_to_new_page_without_active_page(self):
        runtime = NewTabRuntime([], active_page=None)

        async def scenario():
            return await runtime._open_tab_page()

        page = asyncio.run(scenario())
        self.assertEqual(runtime.new_page_calls, 1)
        self.assertIsNot(page, None)

    def test_falls_back_when_window_open_raises(self):
        class BlockedPage(StubPage):
            async def evaluate(self, script, *args):
                raise RuntimeError("popup blocked")

        runtime = NewTabRuntime([BlockedPage()], active_page=BlockedPage())

        async def scenario():
            return await runtime._open_tab_page()

        page = asyncio.run(scenario())
        self.assertEqual(runtime.new_page_calls, 1)

    def test_falls_back_when_no_new_context_page_appears(self):
        active = StubPage()
        runtime = NewTabRuntime([active], active_page=active)

        async def scenario():
            return await runtime._open_tab_page()

        page = asyncio.run(scenario())
        self.assertEqual(runtime.new_page_calls, 1)
        self.assertEqual(active.opened_urls, ["about:blank"])

    def test_register_tab_tracks_popup_and_new_page(self):
        active = StubPage()
        popup = StubPage(url="about:blank")
        runtime = NewTabRuntime([active, popup], active_page=active)

        class AttachRecorder:
            def __init__(self):
                self.attached = []

            def attach_page(self, page, tab_id):
                self.attached.append((id(page), tab_id))

        runtime.inspector = AttachRecorder()
        runtime.interactions = AttachRecorder()
        CamoufoxRuntime._register_tab(runtime, popup, label=None)

        tab = runtime.tabs[runtime._page_to_tab[id(popup)]]
        self.assertIs(tab.page, popup)
        events = [event for event, _handler in popup.listeners]
        self.assertIn("close", events)
        self.assertIn("framenavigated", events)
        self.assertEqual(len(runtime.inspector.attached), 1)


if __name__ == "__main__":
    unittest.main()