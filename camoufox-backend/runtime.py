from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import random
import re
import struct
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

import input_context as ic
from input_context import (
    BackendError,
    CLOSE_REASON_BROWSER_DISCONNECTED,
    CLOSE_REASON_CONTEXT_CLOSED,
    CODE_ERROR,
    CODE_INVALID,
    CODE_NO_ACTIVE_TAB,
    CODE_NOT_LAUNCHED,
    CODE_SESSION_CLOSED,
    CODE_STALE_CAPTURE,
    CODE_STALE_REF,
    Captures,
    INPUT_BACKEND_OSNATIVE,
    InputJournal,
    MOTION_HUMANIZE,
    TargetSpec,
    css_point,
    input_backend,
    iso_now,
    json_safe,
)
from browser_inspector import BrowserInspector
from bubble import bubble_enabled, ensure_bubble_stack, stop_bubble_stack
from har_capture import HarCapture
from interaction_events import InteractionEvents
from network_observer import NetworkObserver
from network_control import NetworkControl
from osnative_input import (
    JugglerInputDispatch,
    OsnativeGeometry,
    OsnativeInputDispatch,
    OsnativePointer,
    discover_osnative_geometry,
)
from persistent_profile import PersistentProfile

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SCREENSHOT_INLINE_LIMIT = 8 * 1024 * 1024
SCREENSHOT_MAX_FULL_PAGE_HEIGHT = 30_000
LEADING_JS_TRIVIA_RE = re.compile(r"[\s\ufeff]+|//[^\r\n\u2028\u2029]*|/\*[\s\S]*?\*/")
RETURN_STATEMENT_RE = re.compile(r"return(?![\w$\\\u200c\u200d])")
EVAL_RETURN_ERROR = "eval script contains a top-level return; wrap the script in an IIFE, for example: (() => { return document.title; })()"
REF_TOKEN_RE = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")
HEADING_LEVEL_ANNOTATION_RE = re.compile(r"\[level=(\d+)\]")
MAIN_FALLBACK_SELECTOR = "main"
OUTLINE_HEADING_LIMIT = 500
OUTLINE_LANDMARK_LIMIT = 200
LANDMARK_ROLES = (
    "banner", "complementary", "contentinfo", "form", "main",
    "navigation", "region", "search",
)
IMPLICIT_LANDMARK_TAGS: Dict[str, str] = {
    "header": "banner",
    "nav": "navigation",
    "main": "main",
    "aside": "complementary",
    "footer": "contentinfo",
}
INTERACTIVE_TAGS = frozenset({
    "a", "button", "details", "embed", "iframe", "input", "label",
    "meter", "object", "output", "progress", "select", "textarea",
})
INTERACTIVE_ROLES = frozenset({
    "button", "checkbox", "columnheader", "combobox", "grid", "gridcell", "link",
    "listbox", "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio",
    "option", "progressbar", "radio", "radiogroup", "rowheader", "scrollbar",
    "searchbox", "slider", "spinbutton", "switch", "tab", "tablist", "textbox",
    "timer", "toolbar", "tree", "treegrid", "treeitem",
})
PAGE_INVENTORY_CACHE_LIMIT = 4
LINKS_CURSOR_PREFIX = "l"
DOM_CURSOR_PREFIX = "d"
DOM_NODE_TEXT_LIMIT = 500
DOM_ATTRIBUTE_LIMIT = 30
DOM_ATTRIBUTE_VALUE_LIMIT = 500
DOM_IDENTIFIER_LIMIT = 500
DOM_INVENTORY_NODE_LIMIT = 50_000
DOM_RESPONSE_TARGET_BYTES = 8 * 1024 * 1024
PAGE_LINK_FIELD_LIMIT = 2_048
_OUTLINE_SCRIPT = """
(root) => {
  const LANDMARK_ROLES = new Set(%s);
  const IMPLICIT_LANDMARK_TAGS = new Map(%s);
  const INTERACTIVE_TAGS = new Set(%s);
  const INTERACTIVE_ROLES = new Set(%s);
  const HEADING_LIMIT = %d;
  const LANDMARK_LIMIT = %d;
  const TEXT_LIMIT = 200;
  const IDENTIFIER_LIMIT = %d;
  const headings = [];
  const landmarks = [];
  const counts = { links: 0, forms: 0, interactive: 0 };
  let omittedHeadings = 0;
  let omittedLandmarks = 0;
  const boundedText = (value) => {
    const text = (value || '').trim();
    return { text: text.slice(0, TEXT_LIMIT), textTruncated: text.length > TEXT_LIMIT };
  };
  const boundedId = (element) => ({
    id: element.id ? element.id.slice(0, IDENTIFIER_LIMIT) : null,
    idTruncated: element.id.length > IDENTIFIER_LIMIT,
  });
  const stack = [root];
  while (stack.length) {
    const element = stack.pop();
    const tag = (element.tagName || '').toLowerCase();
    const role = element.getAttribute && element.getAttribute('role');
    const normalizedRole = role ? role.trim().toLowerCase().split(/\\s+/)[0] : '';
    const landmarkRole = normalizedRole || (IMPLICIT_LANDMARK_TAGS.get(tag) || '');
    if (/^h[1-6]$/.test(tag) || normalizedRole === 'heading') {
      const explicitLevel = Number.parseInt(element.getAttribute('aria-level') || '', 10);
      const level = /^h[1-6]$/.test(tag) ? Number.parseInt(tag[1], 10)
        : (explicitLevel >= 1 && explicitLevel <= 6 ? explicitLevel : 2);
      if (headings.length < HEADING_LIMIT) {
        headings.push({ level, ...boundedText(element.textContent), ...boundedId(element) });
      } else {
        omittedHeadings += 1;
      }
    }
    if (landmarkRole && LANDMARK_ROLES.has(landmarkRole)) {
      if (landmarks.length < LANDMARK_LIMIT) {
        landmarks.push({
          role: landmarkRole,
          ...boundedText(element.getAttribute('aria-label') || element.textContent),
          ...boundedId(element),
        });
      } else {
        omittedLandmarks += 1;
      }
    }
    if (tag === 'a') counts.links += 1;
    if (tag === 'form') counts.forms += 1;
    if (INTERACTIVE_TAGS.has(tag) || (normalizedRole && INTERACTIVE_ROLES.has(normalizedRole))) {
      counts.interactive += 1;
    }
    const children = element.children || [];
    for (let index = children.length - 1; index >= 0; index -= 1) {
      stack.push(children[index]);
    }
  }
  return {
    rootTag: (root.tagName || '').toLowerCase().slice(0, IDENTIFIER_LIMIT),
    rootTagTruncated: (root.tagName || '').length > IDENTIFIER_LIMIT,
    rootId: root.id ? root.id.slice(0, IDENTIFIER_LIMIT) : null,
    rootIdTruncated: root.id.length > IDENTIFIER_LIMIT,
    headings,
    landmarks,
    counts,
    omittedHeadings,
    omittedLandmarks,
  };
}
""" % (
    json.dumps(sorted(LANDMARK_ROLES)),
    json.dumps(list(IMPLICIT_LANDMARK_TAGS.items())),
    json.dumps(sorted(INTERACTIVE_TAGS)),
    json.dumps(sorted(INTERACTIVE_ROLES)),
    OUTLINE_HEADING_LIMIT,
    OUTLINE_LANDMARK_LIMIT,
    DOM_IDENTIFIER_LIMIT,
)

_DOM_CHUNK_SCRIPT = """
(root, state) => {
  const START = state.start;
  const LIMIT = state.limit;
  const INVENTORY_LIMIT = %d;
  const TEXT_LIMIT = %d;
  const ATTRIBUTE_LIMIT = %d;
  const VALUE_LIMIT = %d;
  const IDENTIFIER_LIMIT = %d;
  const normalizedTag = (element) => (element.localName || element.tagName || '').toLowerCase();
  const directText = (element) => {
    let text = '';
    for (const node of element.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) text += ' ' + (node.nodeValue || '');
    }
    return text.replace(/\\s+/g, ' ').trim();
  };
  const escapeTag = (tag) => CSS.escape(tag);
  const cssPath = (element) => {
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      const tag = normalizedTag(current);
      if (!tag) return null;
      let position = 1;
      for (let sibling = current.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
        if (normalizedTag(sibling) === tag) position += 1;
      }
      let parent = current.parentElement;
      let separator = ' > ';
      if (!parent) {
        const treeRoot = current.getRootNode();
        if (treeRoot && treeRoot.host) {
          parent = treeRoot.host;
          separator = ' ';
        }
      }
      parts.unshift({ segment: escapeTag(tag) + ':nth-of-type(' + position + ')', separator });
      current = parent;
    }
    if (!parts.length) return null;
    let path = parts[0].segment;
    for (let index = 1; index < parts.length; index += 1) {
      path += parts[index].separator + parts[index].segment;
    }
    return path;
  };
  let hash = 2166136261;
  const hashText = (value) => {
    hash ^= value.length;
    hash = Math.imul(hash, 16777619) >>> 0;
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619) >>> 0;
    }
  };
  const fingerprintRoot = root.ownerDocument && root.ownerDocument.documentElement
    ? root.ownerDocument.documentElement
    : root;
  const fingerprintStack = [fingerprintRoot];
  while (fingerprintStack.length) {
    const element = fingerprintStack.pop();
    hashText(normalizedTag(element));
    hashText(String(element.childElementCount));
    const attributes = Array.from(element.attributes || [])
      .map((attribute) => ({ name: attribute.name, value: attribute.value || '' }))
      .sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));
    for (const attribute of attributes) {
      hashText(attribute.name);
      hashText(attribute.value);
    }
    hashText(directText(element));
    const children = element.children || [];
    for (let index = children.length - 1; index >= 0; index -= 1) {
      fingerprintStack.push(children[index]);
    }
  }
  let totalElements = 0;
  const stack = [{ element: root, depth: 0, parentIndex: null }];
  const nodes = [];
  while (stack.length) {
    const current = stack.pop();
    const element = current.element;
    const position = totalElements;
    totalElements += 1;
    if (position < INVENTORY_LIMIT && position >= START && nodes.length < LIMIT) {
      const tag = normalizedTag(element);
      const textValue = directText(element);
      const allAttributes = Array.from(element.attributes || [])
        .map((attribute) => ({ name: attribute.name, value: attribute.value || '' }))
        .sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));
      const role = element.getAttribute('role');
      const normalizedRole = role ? role.trim().toLowerCase().split(/\\s+/)[0] : '';
      const textTruncated = textValue.length > TEXT_LIMIT;
      const omittedAttributes = Math.max(0, allAttributes.length - ATTRIBUTE_LIMIT);
      const attributes = allAttributes.slice(0, ATTRIBUTE_LIMIT).map((attribute) => ({
        name: attribute.name.slice(0, IDENTIFIER_LIMIT),
        nameTruncated: attribute.name.length > IDENTIFIER_LIMIT,
        value: attribute.value.slice(0, VALUE_LIMIT),
        valueTruncated: attribute.value.length > VALUE_LIMIT,
      }));
      nodes.push({
        ref: 'd' + (position + 1),
        parentRef: current.parentIndex === null ? null : 'd' + (current.parentIndex + 1),
        depth: current.depth,
        tag: tag.slice(0, IDENTIFIER_LIMIT),
        tagTruncated: tag.length > IDENTIFIER_LIMIT,
        id: element.id ? element.id.slice(0, IDENTIFIER_LIMIT) : null,
        idTruncated: element.id.length > IDENTIFIER_LIMIT,
        role: normalizedRole ? normalizedRole.slice(0, IDENTIFIER_LIMIT) : null,
        roleTruncated: normalizedRole.length > IDENTIFIER_LIMIT,
        text: textValue.slice(0, TEXT_LIMIT),
        textTruncated,
        attributes,
        omittedAttributes,
        childElementCount: element.childElementCount,
        cssPath: cssPath(element),
      });
    }
    const children = element.children || [];
    for (let index = children.length - 1; index >= 0; index -= 1) {
      stack.push({ element: children[index], depth: current.depth + 1, parentIndex: position });
    }
  }
  const total = Math.min(totalElements, INVENTORY_LIMIT);
  return {
    nodes,
    total,
    totalElements,
    omittedNodes: Math.max(0, totalElements - INVENTORY_LIMIT),
    truncated: totalElements > INVENTORY_LIMIT,
    rootCss: cssPath(root),
    fingerprint: totalElements + ':' + hash.toString(16).padStart(8, '0'),
  };
}
""" % (
    DOM_INVENTORY_NODE_LIMIT,
    DOM_NODE_TEXT_LIMIT,
    DOM_ATTRIBUTE_LIMIT,
    DOM_ATTRIBUTE_VALUE_LIMIT,
    DOM_IDENTIFIER_LIMIT,
)

def configure_macos_headed_worker(headless: bool) -> None:
    """Keep the Python helper out of the Dock before Camoufox probes NSScreen."""
    if headless or sys.platform != "darwin":
        return
    if threading.current_thread() is not threading.main_thread():
        raise BackendError(
            CODE_NOT_LAUNCHED,
            "macOS headed worker activation policy must be configured on the main thread",
        )

    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    application = NSApplication.sharedApplication()
    if not application.setActivationPolicy_(NSApplicationActivationPolicyAccessory):
        raise BackendError(
            CODE_NOT_LAUNCHED,
            "macOS refused the Python worker's accessory activation policy; browser was not started",
        )


def png_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _absolute_url(base: str, url: str) -> str:
    try:
        return urljoin(base, url)
    except ValueError:
        return url


def _clip_string(value: Any, limit: int) -> Tuple[str, bool]:
    text = "" if value is None else str(value)
    return text[:limit], len(text) > limit


_HTML_SEARCH_OUTPUT_BUDGET = 8000
_HTML_SEARCH_EXCERPT_MARK = " ... "
_HTML_SEARCH_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)((?:\"[^\"]*\"|'[^']*'|[^<>])*)>")
_HTML_SEARCH_ATTR_ID_RE = re.compile(r"\bid\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))")
_HTML_SEARCH_ATTR_CLASS_RE = re.compile(r"\bclass\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))")
_HTML_SEARCH_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
})
_HTML_SEARCH_CSS_PATH_LIMIT = 120


def _html_attr_values(match: Optional[re.Match]) -> List[str]:
    if match is None:
        return []
    return [group for group in match.groups() if group is not None]


def _html_search_excerpt(html: str, start: int, end: int, context_chars: int) -> str:
    left = max(0, start - context_chars)
    right = min(len(html), end + context_chars)
    excerpt = html[left:right]
    prefix = _HTML_SEARCH_EXCERPT_MARK.strip() if left > 0 else ""
    suffix = _HTML_SEARCH_EXCERPT_MARK.strip() if right < len(html) else ""
    return prefix + excerpt + suffix


def _html_search_matches(html: str, query: str, regex: Optional[str]) -> List[Tuple[int, int]]:
    if regex is not None:
        try:
            pattern = re.compile(regex, re.IGNORECASE | re.DOTALL)
        except re.error as error:
            raise BackendError(CODE_INVALID, f"invalid regex: {error}") from error
        return [match.span() for match in pattern.finditer(html) if match.start() != match.end()]
    needle = query.casefold()
    haystack = html.casefold()
    span = len(needle)
    matches = []
    offset = haystack.find(needle)
    while offset != -1:
        matches.append((offset, offset + span))
        offset = haystack.find(needle, offset + 1)
    return matches


async def _html_scope_subtree(frame: Any, html: str, selector: str) -> str:
    scope = frame.locator(selector).first
    try:
        start = await scope.evaluate(
            "(element) => Math.max(document.documentElement.outerHTML.indexOf(element.outerHTML), 0)"
        )
        scoped_html = await scope.evaluate("(element) => element.outerHTML")
    except Exception as error:
        raise BackendError(CODE_INVALID, f"selector scope failed: {error}") from error
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        return html
    end = start + len(scoped_html)
    if end > len(html) or start >= end:
        return html
    return html[start:end]


def _html_css_path_at(html: str, start: int, end: int) -> str:
    path_limit = _HTML_SEARCH_CSS_PATH_LIMIT
    opener = None
    for match in _HTML_SEARCH_TAG_RE.finditer(html, 0, start):
        if match.group(1).lower() in _HTML_SEARCH_VOID_TAGS:
            continue
        opener = match
    if opener is None:
        return "html"
    attrs = opener.group(2) or ""
    tag = opener.group(1).lower()
    id_values = _html_attr_values(_HTML_SEARCH_ATTR_ID_RE.search(attrs))
    class_values = _html_attr_values(_HTML_SEARCH_ATTR_CLASS_RE.search(attrs))
    segments = [tag]
    if id_values:
        segments.append(f"#{id_values[0]}")
    elif class_values:
        segments.append("." + ".".join(class_values[0].split()[:3]))
    path = "".join(segments)
    if len(path) > path_limit:
        path = path[:path_limit]
    return path


def _html_search_envelope_size(captured_chars: int, entries: List[Dict[str, Any]]) -> int:
    try:
        return len(
            json.dumps(
                {
                    "matches": entries,
                    "totalMatches": 1,
                    "capturedChars": captured_chars,
                    "truncated": False,
                },
                ensure_ascii=False,
            )
        )
    except (TypeError, ValueError):
        return _HTML_SEARCH_OUTPUT_BUDGET + 1


class Tab:
    def __init__(self, tab_id: str, page: Any, label: Optional[str] = None):
        self.tab_id = tab_id
        self.page = page
        self.label = label
        self.refs: Set[str] = set()
        self.refs_meta: Dict[str, Dict[str, Any]] = {}
        self.dom_refs: Dict[str, str] = {}
        self.selected_frame: Optional[Any] = None
        self.frame_ids: Dict[Any, str] = {}
        self.next_frame_id = 1
        self.closed = False
        self.inventory_caches: Dict[str, Dict[str, Any]] = {}
        self.last_ref_remap: Optional[Dict[str, str]] = None


class GestureContext:
    def __init__(self, runtime: "CamoufoxRuntime", deadline_ms: int):
        page, tab = runtime.require_active()
        self.runtime = runtime
        self.page = page
        self.tab_id = tab.tab_id
        self.frame_is_main = runtime.active_scope() is page.main_frame
        self.journal = runtime.journal
        self.captures = runtime.captures
        self.motion = runtime.motion
        self._started = time.monotonic()
        self._deadline_s = max(0.05, deadline_ms / 1000.0)
        self._stages: List[Dict[str, Any]] = []
        self._input_dispatched = 0

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def remaining_seconds(self) -> float:
        return self._deadline_s - (time.monotonic() - self._started)

    def remaining_ms(self) -> int:
        return max(0, int(self.remaining_seconds() * 1000))

    def check_deadline(self) -> None:
        if self.remaining_seconds() <= 0:
            raise BackendError(ic.CODE_TIMEOUT, "action deadline exceeded", deadline_exceeded=True)

    def set_stage(self, name: str) -> None:
        self._stages.append({"stage": name, "atMs": self.elapsed_ms()})

    def note_input_dispatched(self) -> None:
        self._input_dispatched += 1
        self.runtime.note_input()

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "stages": list(self._stages),
            "elapsedMs": self.elapsed_ms(),
            "inputDispatched": self._input_dispatched > 0,
            "inputDispatchCount": self._input_dispatched,
            "semanticSuccess": "not_asserted",
            "motion": self.motion,
        }

    def require_exposed_ref(self, ref: str) -> None:
        self.runtime.require_exposed_ref(ref)

    def locator_scope(self, spec: TargetSpec) -> Tuple[Any, str]:
        return self.runtime.locator_scope(spec)

    async def action_locator(self, spec: TargetSpec) -> Tuple[Any, Any]:
        return await self.runtime.resolve_action_locator(spec)

    async def require_capture(self, capture_id: str) -> Dict[str, Any]:
        return await self.runtime.require_capture(capture_id)

    async def capture_identity(self) -> Dict[str, Any]:
        return await self.runtime.capture_identity()

    async def viewport_size(self) -> Dict[str, float]:
        return await self.runtime.viewport_size()

    def input_dispatch(self) -> Any:
        return self.runtime.input_dispatch(self.page)

    async def resolve_capture_point(self, spec: TargetSpec, capture_id_used: Optional[str]) -> Tuple[float, float, str]:
        return await self.runtime.resolve_capture_point(spec, capture_id_used)


_HOST_OS_MAP = {"Darwin": "macos", "Windows": "windows"}
LAUNCH_WINDOW = (1920, 1080)


def _resolve_host_os() -> str:
    """Camoufox OS string matching the host so generated fingerprints stay coherent."""
    return _HOST_OS_MAP.get(platform.system(), "linux")


_LOCALE_CONFIG_KEYS = (
    "locale:language",
    "locale:region",
    "locale:script",
    "locale:all",
    "navigator.language",
)

_CONFIG_CHUNK_SIZE = 32768

_CAMOU_CONFIG_CHUNK_RE = re.compile(r"^CAMOU_CONFIG_([0-9]+)$")


def _sanitize_fingerprint_config(options: Dict[str, Any]) -> None:
    """Remove locale-spoof keys from a generated Camoufox config.

    Camoufox's locale-spoofing patch hooks Locale::Language()/Region()/Script() so every
    internal Locale instance reports the spoofed subtags while MaskConfig carries
    locale:* or navigator.language. That poisons Intl.DisplayNames for every consumer
    (measured: of('US'), of('GB'), of('KY') all resolve to the spoofed region's name), and
    pages that validate locales through DisplayNames — OpenRouter's gt-next is one — throw
    during hydration and replace the document with their own error screen. The
    GetDefaultLocale() override alone is the intended spoof surface; the subtag accessors
    are the collateral. Until the Camoufox patch is narrowed upstream, launching without
    these keys keeps the engine's intl machinery coherent. navigator.language in the page
    then reflects the browser's real build locale and the intl.accept_languages pref, not
    a geo-coherent spoof.

    The fingerprint is serialized into CAMOU_CONFIG_<n> environment chunks, so the keys
    are removed from the decoded JSON and the chunks are rewritten in place.
    """
    config = options.get("config")
    if not isinstance(config, dict):
        config = {}
    removed = False
    for key in _LOCALE_CONFIG_KEYS:
        if key in config:
            config.pop(key, None)
            removed = True
    if removed:
        options["config"] = config
    env = options.get("env")
    if not isinstance(env, dict):
        return
    chunk_keys = sorted(
        (key for key in env if isinstance(key, str) and _CAMOU_CONFIG_CHUNK_RE.match(key)),
        key=lambda key: int(_CAMOU_CONFIG_CHUNK_RE.match(key).group(1)),
    )
    if not chunk_keys:
        return
    raw = "".join(env[key] for key in chunk_keys)
    try:
        config = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(config, dict):
        return
    removed = False
    for key in _LOCALE_CONFIG_KEYS:
        if key in config:
            config.pop(key, None)
            removed = True
    if not removed:
        return
    encoded = json.dumps(config, separators=(",", ":"))
    # Re-chunk at a conservative size so no single env var grows beyond limits.
    chunks = [encoded[i : i + _CONFIG_CHUNK_SIZE] for i in range(0, len(encoded), _CONFIG_CHUNK_SIZE)]
    for old_key in list(env):
        if isinstance(old_key, str) and _CAMOU_CONFIG_CHUNK_RE.match(old_key):
            del env[old_key]
    for index, chunk in enumerate(chunks, start=1):
        env[f"CAMOU_CONFIG_{index}"] = chunk


def _geoip_launch_value() -> Optional[bool]:
    """Enable IP-based geo coherence only when the geoip extra and database exist."""
    try:
        from camoufox.geolocation import ALLOW_GEOIP, MMDB_DIR
    except Exception:
        return None
    if not ALLOW_GEOIP:
        return None
    try:
        return True if any(MMDB_DIR.glob("*.mmdb")) else None
    except OSError:
        return None


class AmbientHover:
    def __init__(self, runtime: "CamoufoxRuntime"):
        self.runtime = runtime
        self._task: Optional[asyncio.Task] = None
        self._started_at = 0.0

    def start(self, page: Any, tab: Tab, x: float, y: float, max_ms: int) -> None:
        self._started_at = time.monotonic()
        self._task = asyncio.create_task(
            self._run(page, tab, page.url, x, y, self._started_at + max_ms / 1000.0)
        )

    async def stop(self) -> Dict[str, Any]:
        task = self._task
        if task is None or task.done():
            self._task = None
            return {"stopped": False, "heldMs": 0}
        held_ms = max(0, int((time.monotonic() - self._started_at) * 1000))
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        finally:
            if self._task is task:
                self._task = None
        return {"stopped": True, "heldMs": held_ms}

    async def _run(self, page: Any, tab: Tab, url: str, x: float, y: float, expires_at: float) -> None:
        try:
            while True:
                remaining = expires_at - time.monotonic()
                if remaining <= 0:
                    return
                await asyncio.sleep(min(random.uniform(0.2, 0.4), remaining))
                remaining = expires_at - time.monotonic()
                if (remaining <= 0 or self.runtime._closing or self.runtime._close_reason is not None
                        or self.runtime.tabs.get(tab.tab_id) is not tab or tab.closed
                        or page.is_closed() or page.url != url):
                    return
                if self.runtime.action_in_flight:
                    continue
                await asyncio.wait_for(
                    self.runtime.input_dispatch(page).move(
                        x + random.uniform(-2, 2), y + random.uniform(-2, 2)
                    ),
                    timeout=remaining,
                )
        except Exception:
            pass
        finally:
            if self._task is asyncio.current_task():
                self._task = None


class CamoufoxRuntime:
    engine = "camoufox"
    GestureContextClass = GestureContext

    def __init__(self, runtime_dir: Path, motion: str):
        self.runtime_dir = Path(runtime_dir)
        self.motion = motion
        self.humanize = MOTION_HUMANIZE[motion]
        self.input_backend = input_backend()
        self.osnative_pointer: Optional[OsnativePointer] = None
        self.osnative_geometry: Optional[OsnativeGeometry] = None
        self.captures = Captures()
        self.journal = InputJournal()
        self.network = NetworkObserver(self)
        self.har = HarCapture(self)
        self.network_control = NetworkControl(self)
        self.inspector = BrowserInspector(self)
        self.interactions = InteractionEvents(self)
        self._camoufox: Any = None
        self.browser: Any = None
        self.context: Any = None
        self._profile: Optional[PersistentProfile] = None
        self.profile_path: Optional[str] = None
        self.adblock = False
        self.tabs: Dict[str, Tab] = {}
        self._page_to_tab: Dict[int, str] = {}
        self._tab_counter = 0
        self.active_id: Optional[str] = None
        self.launched = False
        self.headless: Optional[bool] = None
        self._closing = False
        self.action_in_flight = False
        self.ambient_hover = AmbientHover(self)
        self._input_attempts = 0
        self._close_reason: Optional[str] = None

    async def launch(self, headless: bool, profile: Optional[str] = None, adblock: bool = False) -> Dict[str, Any]:
        """Reuse a private on-disk context only when explicitly selected; never switch a live profile."""
        if bubble_enabled():
            headless = False
            await asyncio.to_thread(ensure_bubble_stack)
        error = self.sync_session_state()
        if error is not None:
            raise error
        try:
            persistent = PersistentProfile(profile) if profile is not None else None
            requested_path = str(persistent.path.resolve()) if persistent is not None else None
        except (OSError, ValueError) as exc:
            raise BackendError(CODE_INVALID, f"invalid Camoufox profile: {exc}") from exc
        if self._is_live():
            if self.profile_path != requested_path:
                raise BackendError(CODE_INVALID, "browser is already running with a different profile; close the session first")
            if self.headless != headless:
                raise BackendError(
                    CODE_ERROR,
                    "browser is already running with a different headless setting; close the session first",
                )
            if self.adblock != adblock:
                raise BackendError(
                    CODE_ERROR,
                    "browser is already running with a different adblock setting; close the session first",
                )
            return self.launch_info()
        if self.input_backend == INPUT_BACKEND_OSNATIVE:
            display = os.environ.get("DISPLAY")
            if not display:
                raise BackendError(
                    CODE_NOT_LAUNCHED,
                    "the os-native input backend requires an X DISPLAY for the browser; "
                    "set the DISPLAY environment variable to the private X display "
                    "(for example an Xvfb screen) before launch",
                )
        try:
            record = json.loads((self.runtime_dir / "runtime.json").read_text("utf-8"))
            executable = Path(record["browser"]["executablePath"])
            if not executable.is_absolute():
                raise ValueError("installed browser executable must be absolute")
            resolved_executable = executable.resolve()
            resolved_executable.relative_to((self.runtime_dir / "home").resolve())
            if not executable.is_file():
                raise ValueError("installed browser executable is missing")
            asset_anchor = executable
            if (resolved_executable.parent.name == "MacOS"
                    and resolved_executable.parent.parent.name == "Contents"
                    and resolved_executable.parent.parent.parent.suffix == ".app"):
                resources = resolved_executable.parent.parent / "Resources"
                if not (resources / "properties.json").is_file():
                    raise ValueError("macOS bundle resources are missing")
                asset_anchor = resources / resolved_executable.name
            from importlib.metadata import version
            if version("camoufox") != "0.5.6" or version("playwright") != "1.61.0":
                raise ValueError("runtime package pins do not match")
            from camoufox.async_api import AsyncCamoufox
            from camoufox.addons import DefaultAddons, get_addon_path
            from camoufox.utils import launch_options
        except Exception as exc:
            raise BackendError(
                CODE_NOT_LAUNCHED,
                "camoufox runtime is missing or incompatible; run agent-browser --engine camoufox install "
                f"(runtime error: {type(exc).__name__})",
            ) from exc
        self._closing = False
        try:
            profile_kwargs: Dict[str, Any] = {}
            if persistent is not None:
                try:
                    persistent.acquire()
                    self._profile = persistent
                    self.profile_path = str(persistent.path)
                    profile_kwargs = persistent.launch_kwargs(str(executable))
                except (OSError, ValueError) as exc:
                    raise BackendError(CODE_INVALID, f"cannot use Camoufox profile: {exc}") from exc
            addon_paths: Optional[List[str]] = None
            if adblock:
                addon_path = get_addon_path(DefaultAddons.UBO.name)
                if not (Path(addon_path) / "manifest.json").is_file():
                    raise BackendError(
                        CODE_NOT_LAUNCHED,
                        "the uBlock Origin addon is missing from the managed runtime; "
                        "run agent-browser --engine camoufox install",
                    )
                addon_paths = [addon_path]
            # Set AppKit policy on the main thread before launch_options probes displays in a thread.
            configure_macos_headed_worker(headless)
            # Camoufox 0.5.6 resolves assets beside executable_path. macOS stores them in Resources.
            # Fingerprint policy mirrors the NBF routine recipe: host-matched OS, fixed window,
            # blocked WebRTC, and IP-based geo coherence when the geoip runtime is available.
            # Camoufox config merges are no-clobber, so a persistent profile's saved identity
            # keeps its fingerprint; only absent values (timezone, locale, geolocation) are filled.
            geoip_value = _geoip_launch_value()
            launch_build: Dict[str, Any] = {
                "headless": headless,
                "humanize": self.humanize,
                "enable_cache": True,
                "executable_path": str(asset_anchor),
                "exclude_addons": list(DefaultAddons),
                "addons": addon_paths,
                "os": _resolve_host_os(),
                "window": LAUNCH_WINDOW,
                "block_webrtc": True,
                "geoip": geoip_value,
            }
            try:
                options = await asyncio.to_thread(launch_options, **launch_build, **profile_kwargs)
            except Exception:
                if geoip_value is None:
                    raise
                # A failed public-IP or GeoIP lookup must not make every launch unusable.
                print(
                    "agent-browser: geoip lookup failed at launch; retrying without geoip",
                    file=sys.stderr,
                )
                launch_build["geoip"] = None
                options = await asyncio.to_thread(launch_options, **launch_build, **profile_kwargs)
            _sanitize_fingerprint_config(options)
            options["executable_path"] = str(executable)
            if persistent is not None:
                if not profile_kwargs:
                    try:
                        persistent.save_identity(options, str(executable))
                    except (OSError, ValueError) as exc:
                        raise BackendError(CODE_INVALID, f"cannot save Camoufox profile identity: {exc}") from exc
                options["user_data_dir"] = str(persistent.path)
            instance = AsyncCamoufox(from_options=options, persistent_context=persistent is not None)
            self._camoufox = instance
            launched = await instance.__aenter__()
            if persistent is not None:
                self.context = launched
                self.browser = launched.browser
                if self.browser is None:
                    raise BackendError(CODE_NOT_LAUNCHED, "persistent Camoufox context has no owning browser")
            else:
                self.browser = launched
                self.context = await launched.new_context()
            self._configure_default_timeout(self.context)
            self._close_reason = None
            self.network.attach(self.context)
            self.inspector.attach_context(self.context)
            self.browser.on("disconnected", self._on_browser_disconnected)
            self.context.on("close", self._on_context_closed)
            self.context.on("page", self._on_context_page)
            self.launched = True
            self.headless = headless
            self.adblock = adblock
            pages = list(self.context.pages)
            if not pages:
                pages = [await self.context.new_page()]
            if self.input_backend == INPUT_BACKEND_OSNATIVE:
                await self._connect_osnative()
            for page in pages:
                tab = self._register_tab(page, label=None)
                if self.active_id is None:
                    self.active_id = tab.tab_id
        except BaseException:
            await self.close()
            raise
        return self.launch_info()

    async def _connect_osnative(self) -> None:
        display = os.environ.get("DISPLAY") or ""
        try:
            pointer = await asyncio.to_thread(OsnativePointer, display)
        except BackendError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise BackendError(
                CODE_NOT_LAUNCHED,
                f"the os-native input backend could not open the X display "
                f"(DISPLAY={display or '<unset>'}): {type(exc).__name__}",
            ) from exc
        self.osnative_pointer = pointer
        try:
            self.osnative_geometry = await discover_osnative_geometry(pointer)
        except BackendError:
            await self.close()
            raise

    def input_dispatch(self, page: Any) -> Any:
        if self.input_backend == INPUT_BACKEND_OSNATIVE:
            if self.osnative_pointer is None or self.osnative_geometry is None:
                raise BackendError(
                    CODE_NOT_LAUNCHED,
                    "the os-native input backend is not connected; the browser must be "
                    "launched again on an X display",
                )
            return OsnativeInputDispatch(
                page, self.osnative_pointer, self.osnative_geometry, self.humanize,
            )
        return JugglerInputDispatch(page)

    def active_input_dispatch(self) -> Any:
        return self.input_dispatch(self.active_page())

    def _configure_default_timeout(self, context: Any) -> None:
        """Keep Playwright's own waits inside the worker's per-action deadline.

        Playwright defaults to a 30s timeout, which is longer than the worker's 22s
        default deadline. A polling call (a locator read or wait for a missing element)
        would then be cancelled by the worker's wait_for before Playwright can return
        its own clean TimeoutError, turning an ordinary operation timeout into a
        cancelled action. Set the context default below the deadline instead, so the
        operation reports `data.timeoutKind: "operation"` and the session stays usable.
        """
        margin_ms = 1000
        effective_ms = max(500, ic.action_deadline_ms() - margin_ms)
        try:
            context.set_default_timeout(effective_ms)
        except Exception:
            pass

    def _is_live(self) -> bool:
        self.sync_session_state()
        if self._close_reason is not None or self._closing:
            return False
        return (
            self.launched
            and self.browser is not None
            and self.context is not None
            and self._camoufox is not None
        )

    def session_closed_error(self) -> Optional[BackendError]:
        if self._close_reason is None:
            return None
        return BackendError(
            CODE_SESSION_CLOSED,
            f"browser session ended ({self._close_reason}); close the session, then open a new one "
            "before continuing (no implicit relaunch)",
        )

    def _observed_close_reason(self) -> Optional[str]:
        browser = self.browser
        if browser is not None and not browser.is_connected():
            return CLOSE_REASON_BROWSER_DISCONNECTED
        context = self.context
        if context is not None and (
            context.is_closed()
            or (browser is not None and context not in browser.contexts)
        ):
            return CLOSE_REASON_CONTEXT_CLOSED
        return None

    def sync_session_state(self) -> Optional[BackendError]:
        """Reconcile public liveness signals without restarting or discarding input evidence."""
        if self.launched and not self._closing:
            reason = self._observed_close_reason()
            if reason is not None:
                self._mark_session_dead(reason)
        return self.session_closed_error()

    def _mark_session_dead(self, reason: str) -> None:
        if self._close_reason in (reason, CLOSE_REASON_BROWSER_DISCONNECTED):
            return
        # Context-close can precede disconnect during browser shutdown. Upgrade
        # the diagnostic, but never clear the explicit-close requirement here.
        self._close_reason = reason
        self.active_id = None
        for tab in self.tabs.values():
            tab.closed = True
            tab.selected_frame = None
            tab.frame_ids.clear()
            tab.refs.clear()
            tab.refs_meta.clear()
            tab.dom_refs.clear()
            tab.inventory_caches.clear()
        self.captures.invalidate_all()

    def require_open_session(self) -> None:
        error = self.sync_session_state()
        if error is not None:
            raise error

    def _on_browser_disconnected(self, browser: Any) -> None:
        if self._closing or browser is not self.browser:
            return
        self._mark_session_dead(CLOSE_REASON_BROWSER_DISCONNECTED)

    def _on_context_closed(self, context: Any) -> None:
        if self._closing or context is not self.context:
            return
        self._mark_session_dead(CLOSE_REASON_CONTEXT_CLOSED)

    def launch_info(self) -> Dict[str, Any]:
        self.sync_session_state()
        return {
            "launched": self._is_live(),
            "browserConnected": self.browser_connected(),
            "recoveryRequired": self.recovery_required(),
            "closeReason": self._close_reason,
            "engine": self.engine,
            "headless": self.headless,
            "adblock": self.adblock,
            "motion": self.motion,
            "humanize": self.humanize,
            "inputBackend": self.input_backend,
            "runtimeDir": str(self.runtime_dir),
            "persistentProfile": self.profile_path is not None,
            "profilePath": self.profile_path,
        }

    def browser_connected(self) -> bool:
        browser = self.browser
        return browser is not None and browser.is_connected()

    def recovery_required(self) -> bool:
        return self._close_reason is not None

    async def close(self) -> Dict[str, Any]:
        await self.ambient_hover.stop()
        if self._closing:
            return {"closed": True, "alreadyClosing": True}
        self._closing = True
        self.har.reset()
        self.network.reset()
        self.network_control.reset()
        self.inspector.reset()
        await self.interactions.reset()
        try:
            await asyncio.wait_for(self.release_inputs(), timeout=2)
        except Exception:
            pass
        context = self.context
        self.context = None
        if context is not None:
            try:
                await asyncio.wait_for(context.close(), timeout=5)
            except Exception:
                pass
        instance = self._camoufox
        self._camoufox = None
        if instance is not None:
            try:
                await asyncio.wait_for(instance.__aexit__(None, None, None), timeout=8)
            except Exception:
                pass
        self.browser = None
        pointer = self.osnative_pointer
        self.osnative_pointer = None
        self.osnative_geometry = None
        if pointer is not None:
            await asyncio.to_thread(pointer.close)
        profile = self._profile
        self._profile = None
        if profile is not None:
            profile.release()
        self.profile_path = None
        self.adblock = False
        self.launched = False
        self.active_id = None
        self.tabs.clear()
        self._page_to_tab.clear()
        self.journal.clear()
        self.captures.invalidate_all()
        self._close_reason = None
        if bubble_enabled():
            await asyncio.to_thread(stop_bubble_stack)
        return {"closed": True}

    async def release_inputs(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"released": True, "buttons": [], "keys": []}
        page = self._active_page_or_none()
        if page is None:
            result["released"] = not (self.journal.pending_buttons() or self.journal.pending_keys())
            return result
        dispatch = self.input_dispatch(page)
        for button in self.journal.pending_buttons():
            try:
                await asyncio.wait_for(dispatch.up(button=button), timeout=1.5)
                self.journal.finish_button_up(button)
                result["buttons"].append(button)
            except Exception:
                result["released"] = False
        for key in self.journal.pending_keys():
            try:
                await asyncio.wait_for(dispatch.key_up(key), timeout=1.5)
                self.journal.finish_key_up(key)
                result["keys"].append(key)
            except Exception:
                result["released"] = False
        return result

    def _register_tab(self, page: Any, label: Optional[str]) -> Tab:
        existing_id = self._page_to_tab.get(id(page))
        if existing_id is not None:
            tab = self.tabs[existing_id]
            if label is not None:
                tab.label = label
            return tab
        self._tab_counter += 1
        tab = Tab(f"t{self._tab_counter}", page, label=label)
        self.tabs[tab.tab_id] = tab
        self._page_to_tab[id(page)] = tab.tab_id
        self.inspector.attach_page(page, tab.tab_id)
        self.interactions.attach_page(page, tab.tab_id)
        page.on("framenavigated", lambda _frame, tab_id=tab.tab_id: self._clear_refs(tab_id))
        page.on("framedetached", lambda _frame, tab_id=tab.tab_id: self._clear_refs(tab_id))
        page.on("close", lambda closed_page, tab_id=tab.tab_id: self._on_page_closed(tab_id, closed_page))
        return tab

    def _on_context_page(self, page: Any) -> None:
        if self._close_reason is not None or self._closing:
            return
        if id(page) in self._page_to_tab:
            return
        self._register_tab(page, label=None)

    def _on_page_closed(self, tab_id: str, page: Any = None) -> None:
        if self._closing:
            return
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        if page is not None and page is not tab.page:
            return
        tab.closed = True
        tab.selected_frame = None
        tab.frame_ids.clear()
        self.inspector.detach_page(tab.page)
        self.interactions.detach_page(tab.page)
        self.captures.invalidate_all()
        self._clear_refs(tab_id)
        if self.active_id == tab_id:
            self.active_id = None

    def _clear_refs(self, tab_id: str) -> None:
        self.captures.invalidate_all()
        tab = self.tabs.get(tab_id)
        if tab is None:
            return
        tab.refs.clear()
        tab.refs_meta.clear()
        tab.dom_refs.clear()
        tab.inventory_caches.clear()

    def clear_dom_refs(self) -> None:
        tab = self.tabs.get(self.active_id) if self.active_id else None
        if tab is None or tab.closed:
            return
        tab.dom_refs.clear()
        tab.inventory_caches.clear()

    def find_tab(self, ident: Optional[str]) -> Optional[Tab]:
        if ident is None:
            return None
        if ident in self.tabs:
            return self.tabs[ident]
        for tab in self.tabs.values():
            if tab.label is not None and tab.label == ident:
                return tab
        return None

    def _page_closed_evidence(self, page: Any) -> bool:
        return page.is_closed()

    def _reconcile_tab(self, tab: Tab) -> bool:
        if tab.closed:
            return True
        if not self._page_closed_evidence(tab.page):
            return False
        tab.closed = True
        tab.selected_frame = None
        tab.frame_ids.clear()
        self.inspector.detach_page(tab.page)
        self.interactions.detach_page(tab.page)
        self._clear_refs(tab.tab_id)
        if self.active_id == tab.tab_id:
            self.active_id = None
        return True

    def reconcile_session(self) -> None:
        self.sync_session_state()
        for tab in self.tabs.values():
            self._reconcile_tab(tab)

    def require_tab(self, ident: Optional[str], *, live: bool = True) -> Tab:
        if live:
            self.require_open_session()
        tab = self.find_tab(ident)
        if tab is None:
            raise BackendError(CODE_INVALID, "unknown tab id or label")
        closed = self._reconcile_tab(tab)
        if live and closed:
            raise BackendError(CODE_INVALID, f"tab {tab.tab_id} is closed")
        return tab

    def require_active(self) -> Tuple[Any, Tab]:
        self.require_open_session()
        if not self._is_live():
            raise BackendError(CODE_NOT_LAUNCHED, "browser is not launched; send a launch command first")
        self.reconcile_session()
        tab = self.tabs.get(self.active_id) if self.active_id else None
        if tab is None or tab.closed:
            raise BackendError(
                CODE_NO_ACTIVE_TAB,
                "no active tab; call tab_switch or tab_new (tab_list, tab_close, and session_info still work)",
            )
        return tab.page, tab

    def _active_page_or_none(self) -> Optional[Any]:
        self.reconcile_session()
        tab = self.tabs.get(self.active_id) if self.active_id else None
        if tab is None or tab.closed:
            return None
        return tab.page

    def active_page(self) -> Any:
        page, _tab = self.require_active()
        return page

    def page_waiter(self) -> Any:
        return self.active_page()

    def active_scope(self) -> Any:
        """Keep detached selection explicit; only frame main or a new selection may recover it."""
        page, tab = self.require_active()
        frame = tab.selected_frame
        if frame is not None and frame.is_detached():
            raise BackendError(CODE_INVALID, "selected frame is detached; use frame main or select an observed frame ID from tab list")
        return frame if frame is not None else page.main_frame

    def frame_metadata(self, tab: Tab) -> Dict[str, Any]:
        page = tab.page
        frames = list(page.frames)
        tab.frame_ids = {frame: ident for frame, ident in tab.frame_ids.items() if frame in frames}
        for frame in frames:
            if frame is page.main_frame:
                tab.frame_ids[frame] = "main"
            elif frame not in tab.frame_ids:
                tab.frame_ids[frame] = f"frame-{tab.next_frame_id}"
                tab.next_frame_id += 1
        selected = tab.selected_frame if tab.selected_frame is not None else page.main_frame
        return {
            "frames": [
                {"frameId": tab.frame_ids[frame], "parentId": tab.frame_ids.get(frame.parent_frame),
                 "name": frame.name[:4096], "url": frame.url[:4096], "main": frame is page.main_frame,
                 "selected": frame is selected}
                for frame in frames[:256]
            ],
            "framesOmitted": max(0, len(frames) - 256),
            "selectedFrameDetached": selected.is_detached(),
        }

    def scope_metadata(self) -> Dict[str, Any]:
        _page, tab = self.require_active()
        frame = self.active_scope()
        self.frame_metadata(tab)
        return {"frameId": tab.frame_ids.get(frame), "frameUrl": frame.url}

    async def switch_frame(self, selector: Optional[str]) -> Dict[str, Any]:
        page, tab = self.require_active()
        if selector is None:
            selected = None
        elif re.fullmatch(r"frame-\d+", selector):
            self.frame_metadata(tab)
            selected = next((frame for frame, ident in tab.frame_ids.items() if ident == selector), None)
            if selected is None:
                raise BackendError(CODE_INVALID, "unknown frame ID for this tab; inspect tab list")
        else:
            spec = ic.parse_selector(selector, "selector")
            scope, resolved = self.locator_scope(spec)
            locator = scope.locator(resolved)
            if await locator.count() != 1:
                raise BackendError(CODE_INVALID, "frame selector must match exactly one iframe element")
            handle = await locator.element_handle()
            if handle is None:
                raise BackendError(CODE_INVALID, "frame element no longer exists")
            try:
                selected = await handle.content_frame()
            finally:
                await handle.dispose()
            if selected is None:
                raise BackendError(CODE_INVALID, "selected element is not an iframe")
        if selected is not None and (selected.is_detached() or selected not in page.frames):
            raise BackendError(CODE_INVALID, "selected iframe is detached")
        tab.selected_frame = selected
        self._clear_refs(tab.tab_id)
        self.captures.invalidate_all()
        return {"tabId": tab.tab_id, **self.scope_metadata(), **self.frame_metadata(tab)}

    def page_url(self) -> str:
        return self.active_page().url

    def note_input(self) -> None:
        self._input_attempts += 1

    def list_tabs(self) -> Dict[str, Any]:
        self.reconcile_session()
        return {
            "tabs": [
                {
                    "tabId": tab.tab_id,
                    "label": tab.label,
                    "url": None if tab.closed else tab.page.url,
                    "active": tab.tab_id == self.active_id,
                    "closed": tab.closed,
                    **({} if tab.closed else self.frame_metadata(tab)),
                }
                for tab in self.tabs.values()
            ],
            "activeId": self.active_id,
        }

    async def new_tab(self, url: Optional[str], label: Optional[str]) -> Dict[str, Any]:
        self.require_open_session()
        if not self._is_live():
            raise BackendError(CODE_NOT_LAUNCHED, "browser is not launched; send a launch command first")
        if label is not None and self.find_tab(label) is not None:
            raise BackendError(CODE_INVALID, "tab label is already in use")
        page = await self._open_tab_page()
        tab = self._register_tab(page, label=label)
        self.active_id = tab.tab_id
        try:
            await asyncio.wait_for(page.bring_to_front(), timeout=5)
        except Exception as exc:
            raise BackendError(CODE_ERROR, f"failed to focus tab: {type(exc).__name__}") from exc
        if url is not None:
            await page.goto(url, wait_until="load")
        return {
            "tabId": tab.tab_id,
            "label": tab.label,
            "url": page.url,
            "active": True,
            "tabs": self.list_tabs(),
        }

    async def _open_tab_page(self) -> Any:
        """Prefer a same-window Firefox tab over a new chrome window.

        Camoufox's Juggler implements context.new_page() with
        Services.ww.openWindow, so every Playwright page is a separate browser
        window. Window.open from a live active page instead lets Firefox apply
        its normal tab-opening behavior and Juggler registers the tab through
        its TabOpen listener. Fall back to new_page() when there is no usable
        active page (fresh launch, closed tab) or the popup is blocked.
        """
        page = self._active_page_or_none()
        if page is None:
            return await self.context.new_page()
        before = {id(candidate) for candidate in self.context.pages}
        try:
            await asyncio.wait_for(page.evaluate("(url) => window.open(url, '_blank')", "about:blank"), timeout=5)
        except Exception:
            return await self.context.new_page()
        for _ in range(50):
            await asyncio.sleep(0.05)
            for candidate in self.context.pages:
                if id(candidate) not in before and id(candidate) != id(page):
                    return candidate
            if self._active_page_or_none() is None:
                break
        return await self.context.new_page()

    async def switch_tab(self, ident: Optional[str]) -> Dict[str, Any]:
        self.require_open_session()
        if not self._is_live():
            raise BackendError(CODE_NOT_LAUNCHED, "browser is not launched; send a launch command first")
        if ident is None:
            raise BackendError(CODE_INVALID, "'tabId' is required (stable tab id such as t1 or a label)")
        tab = self.require_tab(ident, live=True)
        try:
            await asyncio.wait_for(tab.page.bring_to_front(), timeout=5)
        except Exception as exc:
            raise BackendError(CODE_ERROR, f"failed to focus tab: {type(exc).__name__}") from exc
        self.captures.invalidate_all()
        self.active_id = tab.tab_id
        return {"tabId": tab.tab_id, "label": tab.label, "url": tab.page.url, "active": True}

    async def close_tab(self, ident: Optional[str]) -> Dict[str, Any]:
        self.require_open_session()
        if not self._is_live():
            raise BackendError(CODE_NOT_LAUNCHED, "browser is not launched; send a launch command first")
        target = ident if ident is not None else self.active_id
        if target is None:
            raise BackendError(CODE_NO_ACTIVE_TAB, "no active tab to close")
        tab = self.require_tab(target, live=True)
        was_active = tab.tab_id == self.active_id
        try:
            await asyncio.wait_for(tab.page.close(), timeout=5)
        except Exception as exc:
            raise BackendError(CODE_ERROR, f"failed to close tab: {type(exc).__name__}") from exc
        tab.closed = True
        tab.selected_frame = None
        tab.frame_ids.clear()
        self._clear_refs(tab.tab_id)
        if was_active:
            self.active_id = None
        self.captures.invalidate_all()
        return {
            "tabId": tab.tab_id,
            "closed": True,
            "activeId": self.active_id,
            "adoptedTab": None,
        }

    async def navigate(self, url: str, wait_until: Optional[str]) -> Dict[str, Any]:
        page, _tab = self.require_active()
        self.captures.invalidate_all()
        await page.goto(url, wait_until=wait_until or "load")
        return {"url": page.url, "title": await page.title()}

    async def go_history(self, direction: str) -> Dict[str, Any]:
        page, _tab = self.require_active()
        self.captures.invalidate_all()
        if direction == "back":
            await page.go_back()
        elif direction == "forward":
            await page.go_forward()
        else:
            await page.reload()
        return {"url": page.url, "title": await page.title()}

    async def current_url(self) -> Dict[str, Any]:
        page, _tab = self.require_active()
        return {"url": page.url}

    async def current_title(self) -> Dict[str, Any]:
        page, _tab = self.require_active()
        return {"title": await page.title()}

    async def content(self) -> Dict[str, Any]:
        frame = self.active_scope()
        return {"content": await frame.content(), **self.scope_metadata()}

    async def evaluate(self, script: str) -> Dict[str, Any]:
        """Use Camoufox's default isolated evaluation context, without opting into main-world eval."""
        offset = 0
        while trivia := LEADING_JS_TRIVIA_RE.match(script, offset):
            offset = trivia.end()
        if RETURN_STATEMENT_RE.match(script, offset):
            raise BackendError(CODE_INVALID, EVAL_RETURN_ERROR)
        frame = self.active_scope()
        self.captures.invalidate_all()
        try:
            result = await frame.evaluate(script)
        except Exception as exc:
            if "playwright" in type(exc).__module__ and re.search(
                r"SyntaxError: (?:return not in function|Illegal return statement)", str(exc), re.IGNORECASE,
            ):
                raise BackendError(CODE_INVALID, EVAL_RETURN_ERROR) from exc
            raise
        return {"result": json_safe(result), **self.scope_metadata()}

    async def read(self) -> Dict[str, Any]:
        page, _tab = self.require_active()
        frame = self.active_scope()
        text = await frame.evaluate("() => document.body ? (document.body.innerText || '') : ''")
        return {"content": text, "url": page.url, "title": await frame.title(), "source": "rendered", **self.scope_metadata()}

    @staticmethod
    def extract_refs(snapshot_text: str) -> Dict[str, Dict[str, Any]]:
        """Read native AI keys, not inline text; Playwright quotes YAML keys and JSON names separately."""
        refs: Dict[str, Dict[str, Any]] = {}
        parents: List[Tuple[int, List[str]]] = []
        heading_stack: List[Tuple[int, Dict[str, Any]]] = []
        for line in snapshot_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            indent = len(line) - len(line.lstrip())
            while parents and parents[-1][0] >= indent:
                parents.pop()
            remainder = stripped[2:]
            if remainder.startswith("/url: "):
                if parents and parents[-1][0] + 2 == indent:
                    value = remainder[6:]
                    if value.startswith('"'):
                        # YAML's hex escapes are not JSON escapes; preserve escaped backslashes.
                        value = re.sub(r'\\(?:x([0-9a-fA-F]{2})|.)', lambda match: '\\u00' + match.group(1) if match.group(1) else match.group(0), value)
                        try:
                            value = json.loads(value)
                        except ValueError:
                            continue
                    for ref in parents[-1][1]:
                        if refs[ref]["role"] == "link":
                            refs[ref]["url"] = value
                            section = CamoufoxRuntime._nearest_heading(heading_stack)
                            if section is not None:
                                refs[ref]["section"] = section
                continue
            if remainder.startswith("'"):
                quoted = re.match(r"^'((?:[^']|'')*)'(.*)$", remainder)
                if quoted is None:
                    continue
                remainder = quoted.group(1).replace("''", "'") + quoted.group(2)
            role_match = re.match(r"[\w-]+", remainder)
            if role_match is None:
                continue
            role = role_match.group(0)
            rest = remainder[len(role):].lstrip()
            name = None
            if rest.startswith('"'):
                try:
                    name, end = json.JSONDecoder().raw_decode(rest)
                except ValueError:
                    continue
                rest = rest[end:].lstrip()
            annotations = rest.split(":", 1)[0]
            level_match = HEADING_LEVEL_ANNOTATION_RE.search(annotations)
            heading_level = int(level_match.group(1)) if level_match else None
            node_refs = []
            for match in REF_TOKEN_RE.finditer(annotations):
                ref = match.group(1)
                ref_match = ic.REF_NAME_RE.match(ref)
                meta = {"role": role, "name": name}
                meta["framePrefix"] = ref_match.group(1) if ref_match else None
                refs[ref] = meta
                node_refs.append(ref)
            if role == "heading" and heading_level is not None and node_refs:
                heading_stack.append(
                    (heading_level, {"level": heading_level, "text": name or "", "ref": node_refs[0]})
                )
            parents.append((indent, node_refs))
        return refs

    @staticmethod
    def _nearest_heading(
        heading_stack: List[Tuple[int, Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        if not heading_stack:
            return None
        heading = heading_stack[-1][1]
        return {"level": heading["level"], "text": heading["text"], "ref": heading["ref"]}

    async def wait_for_page_quiet(self, *, quiet_ms: int, max_ms: int) -> Dict[str, Any]:
        page, tab = self.require_active()
        frame = self.active_scope()
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                frame.evaluate(
                    """
                    async ([quietMs, maxMs]) => {
                      const deadline = performance.now() + maxMs;
                      const quietFor = () => new Promise((resolve) => {
                        let pending = 0;
                        const done = () => { if (pending === 0) resolve(); };
                        const observer = new MutationObserver((records) => {
                          pending -= records.length;
                          if (pending <= 0) { observer.disconnect(); done(); }
                        });
                        observer.observe(document.documentElement, {
                          childList: true, subtree: true, attributes: true, characterData: true,
                        });
                        setTimeout(() => { observer.disconnect(); done(); }, quietMs);
                      });
                      for (;;) {
                        await quietFor();
                        if (performance.now() >= deadline) return false;
                        return true;
                      }
                    }
                    """,
                    [quiet_ms, max_ms],
                ),
                timeout=(max_ms + 2000) / 1000.0,
            )
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            raise BackendError(CODE_INVALID, f"quiet wait failed: {type(exc).__name__}") from exc
        elapsed = int((time.monotonic() - started) * 1000)
        return {"quiet": True, "quietMs": quiet_ms, "maxMs": max_ms, "elapsedMs": elapsed}

    async def snapshot(
        self,
        *,
        selector: Optional[str],
        max_depth: Optional[int],
    ) -> Dict[str, Any]:
        page, tab = self.require_active()
        kwargs: Dict[str, Any] = {"mode": "ai"}
        if max_depth is not None:
            kwargs["depth"] = max_depth
        if selector is not None:
            spec = ic.parse_selector(selector, "selector")
            scope, resolved = self.locator_scope(spec)
            locator = scope.locator(resolved)
            try:
                if await asyncio.wait_for(locator.count(), timeout=2) == 0:
                    raise BackendError(CODE_INVALID, f"snapshot selector {selector!r} does not match any element")
                timeout_ms = max(500, ic.action_deadline_ms() - 1000)
                text = await locator.aria_snapshot(timeout=timeout_ms, **kwargs)
            except asyncio.TimeoutError:
                raise BackendError(
                    ic.CODE_TIMEOUT, f"resolve snapshot selector {selector!r} exceeded its 2000ms watchdog",
                    deadline_exceeded=True,
                ) from None
            except Exception as exc:
                if ic.is_playwright_timeout(exc) and not isinstance(exc, BackendError):
                    raise ic.playwright_timeout_error(exc, f"snapshot selector {selector!r}") from exc
                if "playwright" in type(exc).__module__ and "does not match any element" in str(exc):
                    raise BackendError(CODE_INVALID, f"snapshot selector {selector!r} does not match any element") from exc
                raise
        elif tab.selected_frame is not None:
            text = await self.active_scope().locator(":root").aria_snapshot(**kwargs)
        else:
            text = await page.aria_snapshot(**kwargs)
        refs_meta = self.extract_refs(text)
        tab.refs = set(refs_meta.keys())
        tab.refs_meta = refs_meta
        tab.dom_refs.clear()
        tab.inventory_caches.clear()
        return {
            "snapshot": text,
            "refs": refs_meta,
            "refCount": len(refs_meta),
            "url": page.url,
            "tabId": tab.tab_id,
            **self.scope_metadata(),
            **self.frame_metadata(tab),
        }

    def _page_cache_key(self) -> str:
        page, tab = self.require_active()
        frame = self.active_scope()
        return f"{tab.tab_id}|{page.url}|{frame.url}"

    def _prune_inventory_caches(self, tab: Tab) -> None:
        while len(tab.inventory_caches) > PAGE_INVENTORY_CACHE_LIMIT:
            oldest = min(
                tab.inventory_caches.items(),
                key=lambda item: item[1].get("_createdAt", 0.0),
            )[0]
            tab.inventory_caches.pop(oldest, None)

    @staticmethod
    def _issue_cursor(token: str, offset: int, prefix: str) -> str:
        return f"{prefix}-{token}-{offset}"

    def _resolve_cursor(
        self,
        value: Any,
        cache: Optional[Dict[str, Any]],
        prefix: str,
        field_name: str,
    ) -> int:
        if not isinstance(value, str) or not value:
            raise BackendError(
                CODE_INVALID,
                f"'{field_name}' must be the opaque nextCursor token issued by the previous page",
            )
        expected = f"{prefix}-"
        if not value.startswith(expected):
            raise BackendError(
                CODE_INVALID,
                f"'{field_name}' is not a valid {prefix} pagination cursor",
            )
        body = value[len(expected):]
        token, _sep, offset_text = body.rpartition("-")
        if not token or not offset_text or not offset_text.isdigit():
            raise BackendError(CODE_INVALID, f"'{field_name}' is not a valid pagination cursor")
        if cache is None:
            raise BackendError(
                CODE_STALE_REF,
                f"{field_name} is unknown, stale, or invalidated; take a fresh first page",
            )
        offsets = cache.get("cursors")
        if token != cache.get("token") or not isinstance(offsets, dict) or value not in offsets:
            raise BackendError(
                CODE_STALE_REF,
                f"{field_name} was not issued for the current page state; take a fresh first page",
            )
        offset = offsets[value]
        if not isinstance(offset, int) or offset < 0:
            raise BackendError(CODE_STALE_REF, f"{field_name} is corrupt or invalid for this cache")
        return offset

    def _page_frame_metadata(self, page: Any, tab: Tab) -> Dict[str, Any]:
        return {"url": page.url, "tabId": tab.tab_id, **self.scope_metadata()}

    async def page_outline(self, selector: Optional[str]) -> Dict[str, Any]:
        """Return a bounded structural summary for the selected document scope."""
        page, tab = self.require_active()
        frame = self.active_scope()
        outline = await self._collect_outline(frame, selector)
        return {**outline, **self._page_frame_metadata(page, tab)}

    async def page_html_search(
        self,
        query: str,
        regex: Optional[str],
        selector: Optional[str],
        max_results: int,
        context_chars: int,
    ) -> Dict[str, Any]:
        page, tab = self.require_active()
        frame = self.active_scope()
        html = await frame.evaluate("() => document.documentElement.outerHTML")
        if not isinstance(html, str):
            raise BackendError(CODE_INVALID, "the selected frame returned no HTML to search")
        if selector is not None:
            html = await _html_scope_subtree(frame, html, selector)
        matches = _html_search_matches(html, query, regex)
        excerpts: List[Dict[str, Any]] = []
        truncated = len(matches) > max_results
        for start, end in matches[:max_results]:
            css_path = _html_css_path_at(html, start, end)
            excerpt = _html_search_excerpt(html, start, end, context_chars)
            entry = {"cssPath": css_path, "excerpt": excerpt}
            if _html_search_envelope_size(len(html), excerpts + [entry]) > _HTML_SEARCH_OUTPUT_BUDGET:
                truncated = True
                break
            excerpts.append(entry)
        return {
            "matches": excerpts,
            "totalMatches": len(matches),
            "capturedChars": len(html),
            "truncated": truncated,
            **self._page_frame_metadata(page, tab),
        }

    async def page_links(
        self,
        selector: Optional[str],
        cursor: Optional[str],
        limit: Optional[int],
    ) -> Dict[str, Any]:
        """Page through one ref-bearing native link inventory."""
        page, tab = self.require_active()
        page_key = self._page_cache_key()
        cache = tab.inventory_caches.get(f"links:{page_key}")
        if cursor is not None:
            if selector is not None:
                raise BackendError(CODE_INVALID, "selector cannot be combined with a continuation cursor")
            start = self._resolve_cursor(cursor, cache, LINKS_CURSOR_PREFIX, "cursor")
            page_size = cache.get("pageSize")
            if not isinstance(page_size, int):
                raise BackendError(CODE_STALE_REF, "cursor pagination state is invalid; take a fresh first page")
            requested_size = ic.optional_int(limit, "limit", 1, 200)
            if requested_size is not None and requested_size != page_size:
                raise BackendError(CODE_INVALID, "limit cannot change while continuing a page-links inventory")
            links = cache["links"]
            snapshot_meta = {}
        else:
            page_size = ic.optional_int(limit, "limit", 1, 200, 50)
            assert page_size is not None
            refs_meta, snapshot_meta = await self._snapshot_for_links(selector)
            links = self._build_link_inventory(refs_meta, self.active_scope().url)
            token = uuid.uuid4().hex[:12]
            cache = {
                "token": token,
                "links": links,
                "pageSize": page_size,
                "cursors": {},
                "_createdAt": time.monotonic(),
            }
            tab.inventory_caches[f"links:{page_key}"] = cache
            self._prune_inventory_caches(tab)
            snapshot_meta = {"snapshotTaken": True, "refCount": len(refs_meta)}
            start = 0
        page_items = links[start:start + page_size]
        done = start + len(page_items) >= len(links)
        next_cursor = None
        if not done:
            next_offset = start + page_size
            next_cursor = self._issue_cursor(cache["token"], next_offset, LINKS_CURSOR_PREFIX)
            cache["cursors"][next_cursor] = next_offset
        return {
            "links": page_items,
            "total": len(links),
            "returned": len(page_items),
            "done": done,
            "pageVersion": cache["token"],
            "nextCursor": next_cursor,
            "url": page.url,
            "tabId": tab.tab_id,
            **snapshot_meta,
            **self._page_frame_metadata(page, tab),
        }

    async def page_dom_chunk(
        self,
        selector: Optional[str],
        cursor: Optional[str],
        limit: Optional[int],
    ) -> Dict[str, Any]:
        """Page through structured DOM records with document-scoped refs."""
        page, tab = self.require_active()
        page_key = self._page_cache_key()
        cache_key = f"dom:{page_key}"
        limit_value = ic.optional_int(limit, "limit", 1, 500, 100)
        assert limit_value is not None
        if cursor is not None:
            if selector is not None:
                raise BackendError(CODE_INVALID, "selector cannot be combined with a continuation cursor")
            cache = tab.inventory_caches.get(cache_key)
            start = self._resolve_cursor(cursor, cache, DOM_CURSOR_PREFIX, "cursor")
            root_css = cache.get("rootCss") if isinstance(cache, dict) else None
            if not isinstance(root_css, str) or not root_css:
                raise BackendError(
                    CODE_STALE_REF,
                    "the paginated root element no longer exists; take a fresh first page",
                )
            page_size = cache.get("pageSize")
            if not isinstance(page_size, int):
                raise BackendError(CODE_STALE_REF, "cursor pagination state is invalid; take a fresh first page")
            requested_size = ic.optional_int(limit, "limit", 1, 500)
            if requested_size is not None and requested_size != page_size:
                raise BackendError(CODE_INVALID, "limit cannot change while continuing a dom-chunk inventory")
            root_handle = await self._dom_root_handle_for_path(root_css)
            try:
                summary = await self._evaluate_dom_chunk(root_handle, start, page_size)
            finally:
                try:
                    await root_handle.dispose()
                except Exception:
                    pass
            if (
                summary.get("rootCss") != cache.get("rootCss")
                or summary.get("fingerprint") != cache.get("fingerprint")
                or summary.get("totalElements") != cache.get("totalElements")
                or summary.get("total") != cache.get("total")
            ):
                tab.dom_refs.clear()
                tab.inventory_caches.pop(cache_key, None)
                raise BackendError(
                    CODE_STALE_REF,
                    "cursor was issued for a document that changed; take a fresh first page",
                )
        else:
            frame = self.active_scope()
            root_handle = await self._page_root_handle(frame, selector)
            tab.dom_refs.clear()
            tab.inventory_caches.pop(cache_key, None)
            try:
                summary = await self._evaluate_dom_chunk(root_handle, 0, limit_value)
            finally:
                try:
                    await root_handle.dispose()
                except Exception:
                    pass
            root_css = summary.get("rootCss")
            if not isinstance(root_css, str) or not root_css:
                raise BackendError(
                    CODE_INVALID,
                    "the selected root cannot be assigned a stable document-scoped reference",
                )
            token = uuid.uuid4().hex[:12]
            cache = {
                "token": token,
                "rootCss": root_css,
                "fingerprint": summary["fingerprint"],
                "totalElements": summary["totalElements"],
                "total": summary["total"],
                "pageSize": limit_value,
                "cursors": {},
                "_createdAt": time.monotonic(),
            }
            tab.inventory_caches[cache_key] = cache
            self._prune_inventory_caches(tab)
            start = 0
        page_items = self._bounded_dom_records(summary["nodes"])
        for record in page_items:
            bare_ref = record["ref"]
            css_path = record.pop("cssPath", None)
            if not isinstance(css_path, str) or not css_path:
                tab.dom_refs.clear()
                tab.inventory_caches.pop(cache_key, None)
                raise BackendError(
                    CODE_STALE_REF,
                    "a DOM node could not be assigned a stable reference; take a fresh first page",
                )
            tab.dom_refs[bare_ref] = css_path
            record["ref"] = f"@{bare_ref}"
            parent_ref = record.get("parentRef")
            if parent_ref is not None:
                record["parentRef"] = f"@{parent_ref}"
        done = start + len(page_items) >= summary["total"]
        next_cursor = None
        if not done:
            next_offset = start + len(page_items)
            next_cursor = self._issue_cursor(cache["token"], next_offset, DOM_CURSOR_PREFIX)
            cache["cursors"][next_cursor] = next_offset
        return {
            "nodes": page_items,
            "total": summary["total"],
            "totalElements": summary["totalElements"],
            "returned": len(page_items),
            "done": done,
            "truncated": summary["truncated"],
            "omittedNodes": summary["omittedNodes"],
            "pageVersion": cache["token"],
            "documentFingerprint": summary["fingerprint"],
            "nextCursor": next_cursor,
            "url": page.url,
            "tabId": tab.tab_id,
            **self._page_frame_metadata(page, tab),
        }

    async def _snapshot_for_links(
        self, selector: Optional[str]
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        page, tab = self.require_active()
        kwargs: Dict[str, Any] = {"mode": "ai"}
        if selector is not None:
            frame = self.active_scope()
            spec = ic.parse_selector(selector, "selector")
            scope, resolved = self.locator_scope(spec)
            locator = scope.locator(resolved)
            count = await locator.count()
            if count != 1:
                raise BackendError(
                    CODE_INVALID,
                    f"selector must match exactly one element (matched {count})",
                )
            handle = await locator.element_handle()
            if handle is None:
                raise BackendError(CODE_INVALID, "selected page root no longer exists")
            try:
                owner_frame = await handle.owner_frame()
            finally:
                await handle.dispose()
            if owner_frame is not frame:
                raise BackendError(
                    CODE_INVALID,
                    "page inspection roots must belong to the selected frame; select that frame first",
                )
            text = await locator.aria_snapshot(**kwargs)
        else:
            frame = self.active_scope()
            main_locator = frame.locator(MAIN_FALLBACK_SELECTOR)
            if await main_locator.count() == 1:
                text = await main_locator.aria_snapshot(**kwargs)
            else:
                text = await frame.locator(":root").aria_snapshot(**kwargs)
        refs_meta = self.extract_refs(text)
        tab.refs = set(refs_meta.keys())
        tab.refs_meta = refs_meta
        tab.dom_refs.clear()
        tab.inventory_caches.clear()
        return refs_meta, {"snapshotTaken": True, "refCount": len(refs_meta)}

    def _build_link_inventory(
        self, refs_meta: Dict[str, Dict[str, Any]], base_url: str
    ) -> List[Dict[str, Any]]:
        links: List[Dict[str, Any]] = []
        for ref, meta in refs_meta.items():
            if meta.get("role") != "link" or not meta.get("url"):
                continue
            raw_url = str(meta["url"])
            absolute_url = _absolute_url(base_url, raw_url)
            text, text_truncated = _clip_string(meta.get("name"), PAGE_LINK_FIELD_LIMIT)
            raw_url_value, raw_url_truncated = _clip_string(raw_url, PAGE_LINK_FIELD_LIMIT)
            url, url_truncated = _clip_string(absolute_url, PAGE_LINK_FIELD_LIMIT)
            record: Dict[str, Any] = {
                "ref": f"@{ref}",
                "text": text,
                "textTruncated": text_truncated,
                "rawUrl": raw_url_value,
                "rawUrlTruncated": raw_url_truncated,
                "url": url,
                "urlTruncated": url_truncated,
            }
            section = meta.get("section")
            if section is not None:
                section = dict(section)
                section["ref"] = f"@{section['ref']}"
                section["text"], section["textTruncated"] = _clip_string(
                    section.get("text"), PAGE_LINK_FIELD_LIMIT
                )
                record["section"] = section
            links.append(record)
        return links

    async def _collect_outline(self, frame: Any, selector: Optional[str]) -> Dict[str, Any]:
        root_handle = await self._page_root_handle(frame, selector)
        try:
            return await frame.evaluate(_OUTLINE_SCRIPT, root_handle)
        finally:
            try:
                await root_handle.dispose()
            except Exception:
                pass

    async def _evaluate_dom_chunk(
        self,
        root_handle: Any,
        start: int,
        limit: int,
    ) -> Dict[str, Any]:
        try:
            return await root_handle.evaluate(_DOM_CHUNK_SCRIPT, {"start": start, "limit": limit})
        except Exception as exc:
            raise BackendError(
                CODE_STALE_REF,
                "the paginated root element no longer exists; take a fresh first page",
            ) from exc

    @staticmethod
    def _bounded_dom_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        bounded: List[Dict[str, Any]] = []
        used = 2
        for record in records:
            estimate = dict(record)
            estimate.pop("cssPath", None)
            estimate["ref"] = f"@{record['ref']}"
            if record.get("parentRef") is not None:
                estimate["parentRef"] = f"@{record['parentRef']}"
            encoded = json.dumps(estimate, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            size = len(encoded) + 1
            if bounded and used + size > DOM_RESPONSE_TARGET_BYTES:
                break
            bounded.append(record)
            used += size
        return bounded

    async def _dom_root_handle_for_path(self, css_path: str) -> Any:
        frame = self.active_scope()
        locator = frame.locator(css_path)
        count = await locator.count()
        if count != 1:
            raise BackendError(
                CODE_STALE_REF,
                "the paginated root element no longer exists; take a fresh first page",
            )
        handle = await locator.element_handle()
        if handle is None:
            raise BackendError(
                CODE_STALE_REF,
                "the paginated root element no longer exists; take a fresh first page",
            )
        return handle

    async def _page_root_handle(self, frame: Any, selector: Optional[str]) -> Any:
        if selector is not None:
            spec = ic.parse_selector(selector, "selector")
            scope, resolved = self.locator_scope(spec)
            locator = scope.locator(resolved)
            count = await locator.count()
            if count != 1:
                raise BackendError(
                    CODE_INVALID,
                    f"selector must match exactly one element (matched {count})",
                )
            handle = await locator.element_handle()
            if handle is None:
                raise BackendError(CODE_INVALID, "selected page root no longer exists")
            owner_frame = await handle.owner_frame()
            if owner_frame is not frame:
                await handle.dispose()
                raise BackendError(
                    CODE_INVALID,
                    "page inspection roots must belong to the selected frame; select that frame first",
                )
            return handle
        main_locator = frame.locator(MAIN_FALLBACK_SELECTOR)
        if await main_locator.count() == 1:
            return await main_locator.element_handle()
        return await frame.evaluate_handle("() => document.documentElement")

    def locator_scope(self, spec: TargetSpec) -> Tuple[Any, str]:
        page, tab = self.require_active()
        if spec.ref is not None:
            if spec.dom_ref:
                if spec.ref not in tab.dom_refs:
                    raise BackendError(
                        CODE_STALE_REF,
                        f"ref '@{spec.ref}' is not an active DOM ref for tab {tab.tab_id}; "
                        "it is stale or was not returned by the latest dom_chunk call",
                    )
                return self.active_scope(), tab.dom_refs[spec.ref]
            if spec.ref not in tab.refs:
                raise BackendError(
                    CODE_STALE_REF,
                    f"ref '@{spec.ref}' was not exposed by the latest snapshot of tab {tab.tab_id}; "
                    "take a fresh snapshot",
                )
            return page, f"aria-ref={spec.ref}"
        if spec.selector is None:
            raise BackendError(CODE_INVALID, "selector is required")
        return self.active_scope(), spec.selector

    async def resolve_action_locator(self, spec: TargetSpec) -> Tuple[Any, Any]:
        page, tab = self.require_active()
        if spec.ref is None:
            if spec.selector is None:
                raise BackendError(CODE_INVALID, "selector is required")
            return self.active_scope(), self.active_scope().locator(spec.selector)
        if spec.dom_ref:
            if spec.ref not in tab.dom_refs:
                raise BackendError(
                    CODE_STALE_REF,
                    f"ref '@{spec.ref}' is not an active DOM ref for tab {tab.tab_id}; "
                    "it is stale or was not returned by the latest dom_chunk call",
                )
            return self.active_scope(), self.active_scope().locator(tab.dom_refs[spec.ref])
        if spec.ref not in tab.refs:
            raise BackendError(
                CODE_STALE_REF,
                f"ref '@{spec.ref}' was not exposed by the latest snapshot of tab {tab.tab_id}; "
                "take a fresh snapshot",
            )
        if spec.frame_prefix is not None:
            return page, page.locator(f"aria-ref={spec.ref}")
        aria = page.locator(f"aria-ref={spec.ref}")
        try:
            count = await asyncio.wait_for(aria.count(), timeout=2)
        except asyncio.TimeoutError:
            raise BackendError(
                ic.CODE_TIMEOUT, f"resolving ref '@{spec.ref}' exceeded its 2000ms watchdog",
                deadline_exceeded=True,
            ) from None
        if count == 1:
            return page, aria
        meta = tab.refs_meta.get(spec.ref)
        role = meta.get("role") if meta is not None else None
        if not role:
            raise BackendError(
                CODE_STALE_REF,
                f"ref '@{spec.ref}' no longer resolves to a page element (it was exposed by the latest snapshot "
                "but the element was re-rendered; role/name fallback unavailable); take a fresh snapshot",
            )
        name = meta.get("name") if meta is not None else None
        try:
            candidate = page.get_by_role(role, name=name) if name else page.get_by_role(role)
        except Exception:
            raise BackendError(
                CODE_STALE_REF,
                f"ref '@{spec.ref}' no longer resolves to a page element (it was exposed by the latest snapshot "
                "but the element was re-rendered; role/name fallback unavailable); take a fresh snapshot",
            ) from None
        try:
            count = await asyncio.wait_for(candidate.count(), timeout=2)
        except asyncio.TimeoutError:
            raise BackendError(
                ic.CODE_TIMEOUT, f"resolving ref '@{spec.ref}' exceeded its 2000ms watchdog",
                deadline_exceeded=True,
            ) from None
        if count != 1:
            return await self._remap_stale_ref(page, tab, spec, meta, role, name, count)
        return page, candidate

    async def _remap_stale_ref(
        self, page: Any, tab: Tab, spec: Any, meta: Dict[str, Any], role: str, name: Optional[str], fallback_count: int
    ) -> Tuple[Any, Any]:
        original_meta = {
            key: meta[key] for key in ("role", "name", "url", "section") if key in meta
        }
        try:
            await self.snapshot(selector=None, max_depth=None)
        except asyncio.TimeoutError:
            raise BackendError(
                ic.CODE_TIMEOUT, "remapping ref '@{}' exceeded its snapshot deadline".format(spec.ref),
                deadline_exceeded=True,
            ) from None
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(
                CODE_STALE_REF,
                f"ref '@{spec.ref}' could not be remapped (fresh snapshot failed: {type(exc).__name__}); "
                "take a fresh snapshot",
            ) from exc
        stale_ref_error = BackendError(
            CODE_STALE_REF,
            f"ref '@{spec.ref}' no longer resolves to a page element (it was exposed by the latest snapshot "
            f"but the element was re-rendered; role/name fallback found {fallback_count} candidates); "
            "take a fresh snapshot",
        )
        matches = [
            new_ref for new_ref, new_meta in tab.refs_meta.items()
            if new_ref != spec.ref and new_meta.get("role") == role and new_meta.get("name") == name
        ]
        if len(matches) != 1:
            raise stale_ref_error
        new_ref = matches[0]
        new_locator = page.locator(f"aria-ref={new_ref}")
        try:
            new_count = await asyncio.wait_for(new_locator.count(), timeout=2)
        except asyncio.TimeoutError:
            raise BackendError(
                ic.CODE_TIMEOUT, f"resolving ref '@{spec.ref}' exceeded its 2000ms watchdog",
                deadline_exceeded=True,
            ) from None
        if new_count != 1:
            raise stale_ref_error
        tab.refs.discard(spec.ref)
        tab.refs.add(new_ref)
        tab.refs_meta.pop(spec.ref, None)
        fresh_meta = tab.refs_meta[new_ref]
        for key in ("url", "section"):
            if key in original_meta and key not in fresh_meta:
                fresh_meta[key] = original_meta[key]
        tab.last_ref_remap = {"from": spec.ref, "to": new_ref}
        return page, new_locator

    def require_exposed_ref(self, ref: str) -> None:
        _page, tab = self.require_active()
        if ref.startswith("d"):
            if ref not in tab.dom_refs:
                raise BackendError(
                    CODE_STALE_REF,
                    f"ref '@{ref}' is not an active DOM ref for tab {tab.tab_id}; "
                    "take a fresh dom_chunk",
                )
            return
        if ref not in tab.refs:
            raise BackendError(
                CODE_STALE_REF,
                f"ref '@{ref}' was not exposed by the latest snapshot of tab {tab.tab_id}; "
                "take a fresh snapshot",
            )

    def _screenshots_dir(self) -> Path:
        return self.runtime_dir / "screenshots"

    async def capture_identity(self) -> Dict[str, Any]:
        page, tab = self.require_active()
        metrics = await page.evaluate(
            "() => ({x: window.scrollX, y: window.scrollY, w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio})"
        )
        return {
            "url": page.url,
            "tabId": tab.tab_id,
            "scrollX": float(metrics.get("x") or 0),
            "scrollY": float(metrics.get("y") or 0),
            "viewportWidth": float(metrics.get("w") or 0),
            "viewportHeight": float(metrics.get("h") or 0),
            "devicePixelRatio": float(metrics.get("dpr") or 1),
        }

    async def viewport_size(self) -> Dict[str, float]:
        # Camoufox may disable Playwright's fixed viewport to preserve its fingerprint dimensions.
        identity = await self.capture_identity()
        width, height = identity["viewportWidth"], identity["viewportHeight"]
        if width <= 0 or height <= 0:
            raise BackendError(CODE_INVALID, "the current viewport could not be measured")
        return {"width": width, "height": height}

    async def require_capture(self, capture_id: str) -> Dict[str, Any]:
        capture = self.captures.get(capture_id)
        if capture is None:
            raise BackendError(
                CODE_STALE_CAPTURE,
                "screenshot capture is missing, expired, or invalidated; take a new screenshot",
            )
        identity = await self.capture_identity()
        changed = [
            field
            for field in ("url", "tabId", "scrollX", "scrollY", "viewportWidth", "viewportHeight", "devicePixelRatio")
            if capture.get(field) != identity.get(field)
        ]
        if changed:
            self.captures.invalidate(capture_id)
            raise BackendError(
                CODE_STALE_CAPTURE,
                "screenshot capture is stale because " + ", ".join(changed) + " changed; take a new screenshot",
            )
        return capture

    async def resolve_capture_point(self, spec: TargetSpec, capture_id_used: Optional[str]) -> Tuple[float, float, str]:
        if spec.capture_id is None:
            raise BackendError(CODE_INVALID, "coordinate target is missing captureId")
        if capture_id_used is not None and spec.capture_id != capture_id_used:
            raise BackendError(CODE_INVALID, "both endpoints must use the same captureId")
        capture = await self.require_capture(spec.capture_id)
        x, y = css_point(capture, spec)
        return x, y, spec.capture_id

    async def screenshot(
        self,
        *,
        path: Optional[str],
        screenshot_dir: Optional[str],
        full_page: bool = False,
        inline: bool = False,
    ) -> Dict[str, Any]:
        page, tab = self.require_active()
        before = await self.capture_identity()
        try:
            if full_page:
                scroll_height = await page.evaluate("() => document.documentElement.scrollHeight")
                if not isinstance(scroll_height, (int, float)) or isinstance(scroll_height, bool):
                    raise BackendError(
                        CODE_INVALID,
                        f"full-page screenshot refused: document scrollHeight is not measurable, limit is {SCREENSHOT_MAX_FULL_PAGE_HEIGHT}px",
                    )
                if scroll_height > SCREENSHOT_MAX_FULL_PAGE_HEIGHT:
                    raise BackendError(
                        CODE_INVALID,
                        f"full-page screenshot refused: document scrollHeight is {int(scroll_height)}px, limit is {SCREENSHOT_MAX_FULL_PAGE_HEIGHT}px",
                    )
                data = await page.screenshot(scale="css", full_page=True)
            else:
                data = await page.screenshot(scale="css")
        except TypeError as exc:
            raise BackendError(
                CODE_ERROR,
                "installed Playwright does not support CSS-scale screenshots; "
                "re-run bootstrap.py with the pinned playwright version",
            ) from exc
        if not isinstance(data, (bytes, bytearray)):
            raise BackendError(CODE_ERROR, "screenshot did not return PNG bytes")
        dimensions = png_dimensions(bytes(data))
        if dimensions is None:
            raise BackendError(CODE_ERROR, "screenshot bytes are not a valid PNG")
        image_width, image_height = dimensions
        identity = await self.capture_identity()
        if identity != before:
            raise BackendError(CODE_STALE_CAPTURE, "page moved during screenshot; take a fresh screenshot")
        self.captures.invalidate_all()
        if full_page:
            if inline and len(data) > SCREENSHOT_INLINE_LIMIT:
                raise BackendError(
                    CODE_INVALID,
                    f"full-page screenshot PNG is {len(data)} bytes and exceeds the inline limit of {SCREENSHOT_INLINE_LIMIT} bytes; reduce the viewport width or take viewport screenshots",
                )
            capture_id = None
            visual_capture: Optional[Dict[str, Any]] = None
            if inline and len(data) <= SCREENSHOT_INLINE_LIMIT:
                image = base64.b64encode(bytes(data)).decode("ascii")
            else:
                image = None
        else:
            viewport_width = identity["viewportWidth"]
            viewport_height = identity["viewportHeight"]
            if abs(image_width - viewport_width) > 1 or abs(image_height - viewport_height) > 1:
                raise BackendError(CODE_STALE_CAPTURE, "screenshot dimensions do not match the CSS viewport")
            device_pixel_ratio = identity["devicePixelRatio"]
            capture_id = self.captures.register(
                {
                    "imageWidth": image_width,
                    "imageHeight": image_height,
                    "viewportWidth": viewport_width,
                    "viewportHeight": viewport_height,
                    "devicePixelRatio": device_pixel_ratio,
                    "scrollX": identity["scrollX"],
                    "scrollY": identity["scrollY"],
                    "url": identity["url"],
                    "tabId": tab.tab_id,
                    "capturedAt": iso_now(),
                }
            )
            if inline and len(data) <= SCREENSHOT_INLINE_LIMIT:
                image = base64.b64encode(bytes(data)).decode("ascii")
            else:
                image = None
            visual_capture = None
        if path is not None:
            destination = Path(path).expanduser()
        elif screenshot_dir is not None:
            destination = Path(screenshot_dir).expanduser() / f"{uuid.uuid4()}.png"
        else:
            destination = self._screenshots_dir() / f"{uuid.uuid4()}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bytes(data))
        if capture_id is not None:
            capture = self.captures.get(capture_id) or {}
            visual_capture = {
                key: capture.get(key)
                for key in (
                    "captureId",
                    "imageWidth",
                    "imageHeight",
                    "viewportWidth",
                    "viewportHeight",
                    "devicePixelRatio",
                    "scrollX",
                    "scrollY",
                    "url",
                    "capturedAt",
                )
            }
        result: Dict[str, Any] = {
            "path": str(destination),
            "format": "png",
            "scale": "css",
            "fullPage": full_page,
            "visualCapture": visual_capture,
        }
        if image is not None:
            result["image"] = image
        return result

    def session_info(self, registry_names: List[str]) -> Dict[str, Any]:
        self.reconcile_session()
        pins = None
        runtime_json = self.runtime_dir / "runtime.json"
        try:
            if runtime_json.is_file():
                raw = json.loads(runtime_json.read_text("utf-8"))
                pins = {"pins": raw.get("pins"), "browser": raw.get("browser")}
        except Exception:
            pins = None
        return {
            "engine": self.engine,
            "launched": self._is_live(),
            "browserConnected": self.browser_connected(),
            "recoveryRequired": self.recovery_required(),
            "closeReason": self._close_reason,
            "headless": self.headless,
            "adblock": self.adblock,
            "motion": self.motion,
            "humanize": self.humanize,
            "inputBackend": self.input_backend,
            "activeTab": self.active_id,
            "tabs": self.list_tabs(),
            "persistentProfile": self.profile_path is not None,
            "profilePath": self.profile_path,
            "capabilities": {
                "snapshotFormat": "aria-ai",
                "refs": "aria-ref (element-backed, per-tab, cleared on navigation and replaced by new snapshots; main-frame refs whose element was re-rendered are re-resolved by role/name, and an ambiguous or failed re-resolution returns camoufox_stale_ref so a fresh snapshot is needed)",
                "screenshots": {"format": "png", "scale": "css", "captureTtlSeconds": ic.CAPTURE_TTL_SECONDS},
                "gestures": sorted(registry_names),
                "holdMaxMs": ic.HOLD_MAX_MS,
                "actionDeadlineMs": ic.DEFAULT_ACTION_DEADLINE_MS,
                "maxActionDeadlineMs": ic.MAX_ACTION_DEADLINE_MS,
                "coordinates": True,
                "iframes": "tab-local frame IDs and explicit frame scope for DOM observations/CSS; page-wide native aria-ref routing (fNeN)",
                "inspection": {
                    "requests": True, "requestDetails": True, "console": True, "pageErrors": True,
                    "cookies": True, "webStorage": True, "webSocketEvents": True,
                    "harExport": "bounded diagnostic HAR 1.2",
                    "workerVisibility": "active page/current-origin registrations only",
                    "serviceWorkerNetwork": False, "workerDebugging": False, "browserProcessLogs": False,
                },
                "networkControl": {"routes": True, "headers": True, "offline": True, "domainContainment": False},
                "interactionEvents": {
                    "dialogs": "auto-dismiss; arm a single-use decision on the active tab before triggering input",
                    "dialogArmTtlSeconds": 30,
                    "downloads": "retained handles, explicit wait/save, atomic no-overwrite publication",
                    "downloadRetentionLimit": 32,
                    "downloadEventWaitMs": 10_000,
                    "downloadScope": "active-tab",
                },
            },
            "networkControl": {"routeCount": len(self.network_control.rules), "lastError": self.network_control.last_error},
            "harCapture": {"active": self.har.active, "pendingExport": self.har.pending},
            "runtimeDir": str(self.runtime_dir),
            "buildMetadata": pins,
        }
