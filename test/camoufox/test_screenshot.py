import asyncio
import contextlib
import io
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[2] / "camoufox-backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from input_context import CODE_INVALID, BackendError
from runtime import (
    SCREENSHOT_INLINE_LIMIT,
    SCREENSHOT_MAX_FULL_PAGE_HEIGHT,
    CamoufoxRuntime,
)
from test_lifecycle import FAKE_RUNTIME_DIR, FakeFrame, FakePage, make_runtime

with contextlib.redirect_stdout(io.StringIO()):
    import worker


def one_by_one_png(width: int = 800, height: int = 600) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", 0)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"\x00") + chunk(b"IEND", b"")


class ScreenshotPage(FakePage):
    def __init__(self, data=None, scroll_height=1200):
        super().__init__()
        self._data = one_by_one_png() if data is None else data
        self._scroll_height = scroll_height
        self.screenshot_calls = []

    async def evaluate(self, script):
        if "scrollHeight" in script:
            return self._scroll_height
        return {"x": 0, "y": 0, "w": 800, "h": 600, "dpr": 1}

    async def screenshot(self, scale="css", full_page=False):
        self.screenshot_calls.append({"scale": scale, "full_page": full_page})
        return self._data


class ScreenshotRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def make_runtime(self, page):
        runtime, _browser, _context, _old_page, tab = make_runtime()
        tab.page = page
        return runtime, tab

    async def test_viewport_screenshot_inline_includes_image_and_capture_id(self):
        page = ScreenshotPage()
        runtime, _tab = self.make_runtime(page)
        result = await runtime.screenshot(path=None, screenshot_dir=None, inline=True)
        self.assertEqual(page.screenshot_calls, [{"scale": "css", "full_page": False}])
        self.assertFalse(result["fullPage"])
        self.assertIn("image", result)
        self.assertTrue(result["visualCapture"]["captureId"])
        destination = Path(result["path"])
        self.assertTrue(destination.exists())
        destination.unlink()
        destination.parent.rmdir()

    async def test_viewport_screenshot_without_inline_has_no_image(self):
        page = ScreenshotPage()
        runtime, _tab = self.make_runtime(page)
        result = await runtime.screenshot(path=None, screenshot_dir=None)
        self.assertNotIn("image", result)
        destination = Path(result["path"])
        destination.unlink()
        destination.parent.rmdir()

    async def test_full_page_screenshot_returns_no_capture_id(self):
        page = ScreenshotPage(scroll_height=5000)
        runtime, _tab = self.make_runtime(page)
        result = await runtime.screenshot(path=None, screenshot_dir=None, full_page=True)
        self.assertEqual(page.screenshot_calls, [{"scale": "css", "full_page": True}])
        self.assertTrue(result["fullPage"])
        self.assertIsNone(result["visualCapture"])
        self.assertNotIn("image", result)
        destination = Path(result["path"])
        destination.unlink()
        destination.parent.rmdir()

    async def test_full_page_height_precheck_rejects_tall_documents(self):
        page = ScreenshotPage(scroll_height=SCREENSHOT_MAX_FULL_PAGE_HEIGHT + 1)
        runtime, _tab = self.make_runtime(page)
        with self.assertRaises(BackendError) as raised:
            await runtime.screenshot(path=None, screenshot_dir=None, full_page=True)
        self.assertEqual(raised.exception.code, CODE_INVALID)
        self.assertIn(str(SCREENSHOT_MAX_FULL_PAGE_HEIGHT), raised.exception.message)
        self.assertEqual(page.screenshot_calls, [])

    async def test_full_page_non_numeric_height_is_refused(self):
        page = ScreenshotPage(scroll_height="wide")
        runtime, _tab = self.make_runtime(page)
        with self.assertRaises(BackendError) as raised:
            await runtime.screenshot(path=None, screenshot_dir=None, full_page=True)
        self.assertEqual(raised.exception.code, CODE_INVALID)
        self.assertEqual(page.screenshot_calls, [])

    async def test_full_page_inline_overflow_refuses_without_writing(self):
        page = ScreenshotPage(data=one_by_one_png(800, 60000) + b"\x00" * (SCREENSHOT_INLINE_LIMIT + 1))
        runtime, _tab = self.make_runtime(page)
        with self.assertRaises(BackendError) as raised:
            await runtime.screenshot(path=None, screenshot_dir=None, full_page=True, inline=True)
        self.assertEqual(raised.exception.code, CODE_INVALID)
        self.assertEqual(page.screenshot_calls, [{"scale": "css", "full_page": True}])
        written = sorted((FAKE_RUNTIME_DIR / "screenshots").glob("*.png"))
        self.assertEqual(written, [])


class ScreenshotWorkerTests(unittest.IsolatedAsyncioTestCase):
    def make_worker(self, page):
        runtime, _browser, _context, _old_page, tab = make_runtime()
        tab.page = page
        instance = worker.Worker(FAKE_RUNTIME_DIR, "fast")
        instance.runtime = runtime
        return instance, runtime

    async def request(self, instance, **fields):
        payload = {"id": "screenshot-test", "action": "screenshot", **fields}
        with patch.object(worker, "write_response") as write:
            await instance.handle_line(json.dumps(payload).encode())
        write.assert_called_once()
        return write.call_args.args[0]

    async def test_worker_accepts_full_page_and_inline(self):
        page = ScreenshotPage()
        instance, _runtime = self.make_worker(page)
        response = await self.request(instance, fullPage=True, inline=True)
        self.assertTrue(response["success"])
        self.assertTrue(response["data"]["fullPage"])
        self.assertIsNone(response["data"]["visualCapture"])
        destination = Path(response["data"]["path"])
        destination.unlink()
        destination.parent.rmdir()

    async def test_worker_still_rejects_annotate(self):
        page = ScreenshotPage()
        instance, _runtime = self.make_worker(page)
        response = await self.request(instance, annotate=True)
        self.assertFalse(response["success"])
        self.assertEqual("camoufox_unsupported", response["code"])


if __name__ == "__main__":
    unittest.main()