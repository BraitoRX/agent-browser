from __future__ import annotations

from typing import Any, Dict, List, Optional

from input_context import (
    BackendError,
    CODE_ERROR,
    CODE_INVALID,
    CODE_STALE_REF,
    MAX_SELECTOR_LENGTH,
)

_REGISTRY_INSTALL_SCRIPT = r"""() => {
  const VERSION = 1;
  const existing = globalThis.__abRegistry;
  if (existing && existing.version === VERSION && typeof existing.op === "function") {
    return { installed: false, nonce: existing.nonce, version: VERSION };
  }

  const TEXT_LIMIT = 200;
  const FIELD_LIMIT = 200;
  const PATH_LIMIT = 512;
  const KEY_ATTRIBUTES = ["data-testid", "data-test", "data-qa", "data-cy"];

  let nonce = "";
  const rollNonce = () => {
    let out = "";
    for (let index = 0; index < 8; index += 1) {
      out += Math.floor(Math.random() * 16).toString(16);
    }
    return out;
  };
  nonce = rollNonce();

  const nodeToId = new WeakMap();
  const idToNode = new Map();
  let sequence = 0;

  const fail = (code, message) => ({ error: { code: code, message: message } });

  const tagOf = (node) => (node.localName || node.tagName || "").toLowerCase();

  const truncate = (value, limit) => {
    if (value === null || value === undefined) return null;
    const text = String(value);
    if (!text) return null;
    return text.length > limit ? text.slice(0, limit) : text;
  };

  const idFor = (node) => {
    if (!node || node.nodeType !== 1) return null;
    const current = nodeToId.get(node);
    if (current) return current;
    sequence += 1;
    const id = "el_" + nonce + "_" + sequence;
    nodeToId.set(node, id);
    try {
      idToNode.set(id, new WeakRef(node));
    } catch (error) {
      idToNode.set(id, { deref: () => node });
    }
    return id;
  };

  const nodeFor = (id) => {
    if (typeof id !== "string" || !id) return null;
    const record = idToNode.get(id);
    if (!record) return null;
    let node = null;
    try {
      node = record.deref();
    } catch (error) {
      node = null;
    }
    if (!node || node.nodeType !== 1 || !node.isConnected) {
      idToNode.delete(id);
      return null;
    }
    return node;
  };

  const isComponentBoundary = (node) => !!(node && node.getAttribute && node.getAttribute("data-component"));

  const labelOf = (node) => truncate(
    node.getAttribute("aria-label") || node.getAttribute("alt")
      || node.getAttribute("placeholder") || node.getAttribute("title") || "",
    FIELD_LIMIT,
  );

  const directTextOf = (node) => {
    let text = "";
    for (const child of node.childNodes) {
      if (child.nodeType === 3) text += child.nodeValue || "";
    }
    return truncate(text.replace(/\s+/g, " ").trim(), TEXT_LIMIT);
  };

  const fullTextOf = (node) => truncate((node.textContent || "").replace(/\s+/g, " ").trim(), TEXT_LIMIT);

  const escapeAttr = (value) => {
    let out = "";
    for (let index = 0; index < value.length; index += 1) {
      const ch = value[index];
      const code = value.charCodeAt(index);
      if (ch === "\\") out += "\\\\";
      else if (ch === "\"") out += "\\\"";
      else if (code < 32) out += "\\" + code.toString(16) + " ";
      else out += ch;
    }
    return out;
  };

  const escapeId = (value) => {
    try {
      return CSS.escape(value);
    } catch (error) {
      return null;
    }
  };

  const nthOfType = (node) => {
    let segment = tagOf(node);
    if (!segment) return "";
    const parent = node.parentElement;
    if (parent) {
      const sameTag = [];
      for (const child of parent.children) {
        if (child.tagName === node.tagName) sameTag.push(child);
      }
      if (sameTag.length > 1) segment += ":nth-of-type(" + (sameTag.indexOf(node) + 1) + ")";
    }
    return segment;
  };

  const makeBudget = (request) => {
    const raw = request && typeof request === "object" && request.budget ? request.budget : {};
    const queries = Number.isFinite(raw.maxQueries) ? Math.floor(raw.maxQueries) : 24;
    const visited = Number.isFinite(raw.maxVisitedNodes) ? Math.floor(raw.maxVisitedNodes) : 256;
    return {
      maxQueries: Math.max(1, Math.min(256, queries)),
      maxVisitedNodes: Math.max(1, Math.min(4096, visited)),
      queries: 0,
      visited: 0,
      exhausted: false,
    };
  };

  const budgetFor = (element) => {
    const parent = element.parentElement;
    if (!parent) return null;
    return {
      elementId: idFor(parent),
      tag: tagOf(parent),
      role: parent.getAttribute("role") || null,
      name: labelOf(parent),
      isComponentBoundary: isComponentBoundary(parent),
    };
  };

  const componentOf = (element) => {
    let owner = null;
    try {
      owner = element.closest("[data-component]");
    } catch (error) {
      owner = null;
    }
    if (!owner) return null;
    return { elementId: idFor(owner), key: owner.getAttribute("data-component"), source: "data-component" };
  };

  const liteNode = (node, extra) => Object.assign({
    elementId: idFor(node),
    tag: tagOf(node),
    role: node.getAttribute("role") || null,
    name: labelOf(node),
    directText: directTextOf(node),
    isComponentBoundary: isComponentBoundary(node),
    geometry: null,
  }, extra || {});

  const inspect = (request, budget) => {
    let target = null;
    if (typeof request.selector === "string" && request.selector) {
      if (typeof request.elementId === "string" && request.elementId) {
        return fail("invalid_request", "provide exactly one of 'selector' or 'elementId'");
      }
      if (request.selector.length > 4096) {
        return fail("invalid_selector", "'selector' exceeds the supported length");
      }
      let matches = null;
      try {
        matches = document.querySelectorAll(request.selector);
      } catch (error) {
        return fail("invalid_selector", "'selector' is not a valid CSS selector");
      }
      if (!matches || matches.length === 0) return fail("not_found", "selector matched no elements");
      if (matches.length > 1) {
        return fail("ambiguous", "selector matched " + matches.length + " elements; refine it");
      }
      target = matches[0];
    } else if (typeof request.elementId === "string" && request.elementId) {
      target = nodeFor(request.elementId);
      if (!target) return fail("expired", "elementId is unknown or the node is no longer connected");
    } else {
      return fail("invalid_request", "provide exactly one of 'selector' or 'elementId'");
    }
    return buildCard(target, request, budget);
  };

  const buildCard = (target, request, budget) => {
    const rootNode = target.getRootNode ? target.getRootNode() : null;
    const inShadowRoot = !!(rootNode && rootNode.host);
    const shadowFlags = inShadowRoot ? ["shadow-boundary"] : [];

    const queryCount = (path) => {
      if (budget.queries >= budget.maxQueries) {
        budget.exhausted = true;
        return null;
      }
      budget.queries += 1;
      try {
        return document.querySelectorAll(path);
      } catch (error) {
        return null;
      }
    };

    const seen = new Set();
    const candidates = [];
    const claim = (strategy, path, stability, flags) => {
      if (!path || seen.has(path)) return;
      seen.add(path);
      const matches = queryCount(path);
      let documentMatches = null;
      let sameNode = false;
      let verified = false;
      if (matches !== null) {
        documentMatches = matches.length;
        sameNode = matches.length === 1 && matches[0] === target;
        verified = true;
      }
      const combined = (flags || []).concat(shadowFlags);
      if (verified && path.length > PATH_LIMIT) combined.push("path-too-long");
      candidates.push({
        strategy: strategy,
        path: path,
        resolver: "native-root-css",
        documentMatches: documentMatches,
        rootMatches: documentMatches,
        sameNode: sameNode,
        verified: verified,
        actionEligible: verified && sameNode && path.length <= PATH_LIMIT,
        stability: stability,
        flags: combined,
        length: path.length,
      });
    };

    for (const attr of KEY_ATTRIBUTES) {
      const value = target.getAttribute(attr);
      if (!value) continue;
      const flags = /[0-9a-f]{8,}/i.test(value) ? ["generated-key-suspected"] : [];
      claim("testid", "[" + attr + "=\"" + escapeAttr(value) + "\"]", "observed-key", flags);
    }

    if (target.id) {
      const escaped = escapeId(target.id);
      if (escaped) {
        const flags = /[0-9a-f]{8,}/i.test(target.id) || (target.id.match(/\d/g) || []).length >= 6
          ? ["generated-id-suspected"] : [];
        claim("id", "#" + escaped, "observed-key", flags);
      }
    }

    const tag = tagOf(target);
    for (const attr of ["aria-label", "placeholder", "alt", "title", "name"]) {
      const value = target.getAttribute(attr);
      if (!value) continue;
      claim("semantic", tag + "[" + attr + "=\"" + escapeAttr(value) + "\"]", "semantic", []);
    }
    if (tag === "a" && target.getAttribute("href")) {
      claim("semantic", "a[href=\"" + escapeAttr(target.getAttribute("href")) + "\"]", "semantic", []);
    }
    if ((tag === "input" || tag === "button") && target.getAttribute("type") && target.getAttribute("name")) {
      claim(
        "semantic",
        tag + "[name=\"" + escapeAttr(target.getAttribute("name"))
          + "\"][type=\"" + escapeAttr(target.getAttribute("type")) + "\"]",
        "semantic",
        [],
      );
    }

    let anchorPath = null;
    let anchorNode = null;
    let ancestor = target.parentElement;
    let levels = 0;
    while (ancestor && ancestor.nodeType === 1 && levels < 8 && !anchorPath) {
      levels += 1;
      if (ancestor.id) {
        const escaped = escapeId(ancestor.id);
        if (escaped) {
          const matches = queryCount("#" + escaped);
          if (matches !== null && matches.length === 1 && matches[0] === ancestor) {
            anchorPath = "#" + escaped;
            anchorNode = ancestor;
          }
        }
      }
      if (!anchorPath) {
        for (const attr of KEY_ATTRIBUTES) {
          const value = ancestor.getAttribute(attr);
          if (!value) continue;
          const path = "[" + attr + "=\"" + escapeAttr(value) + "\"]";
          const matches = queryCount(path);
          if (matches !== null && matches.length === 1 && matches[0] === ancestor) {
            anchorPath = path;
            anchorNode = ancestor;
            break;
          }
        }
      }
      ancestor = ancestor.parentElement;
    }

    if (anchorPath && anchorNode) {
      const segments = [];
      let node = target;
      while (node && node !== anchorNode && segments.length < 32) {
        const segment = nthOfType(node);
        if (!segment) break;
        segments.unshift(segment);
        node = node.parentElement;
      }
      if (node === anchorNode && segments.length) {
        claim("anchored", anchorPath + " > " + segments.join(" > "), "structural", ["positional"]);
      }
    }

    const full = [];
    let node = target;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 32) {
      const segment = nthOfType(node);
      if (!segment) break;
      full.unshift(segment);
      node = node.parentElement;
      depth += 1;
    }
    if (full.length) {
      claim("structural", full.join(" > "), "structural", ["positional"]);
    }

    const wantGeometry = Array.isArray(request.fields) && request.fields.indexOf("geometry") !== -1;
    let geometry = null;
    if (wantGeometry) {
      const rect = target.getBoundingClientRect();
      geometry = {
        x: Math.round(rect.x * 100) / 100,
        y: Math.round(rect.y * 100) / 100,
        width: Math.round(rect.width * 100) / 100,
        height: Math.round(rect.height * 100) / 100,
      };
    }

    return {
      kind: "element_card",
      elementId: idFor(target),
      node: {
        tag: tag,
        id: target.id ? truncate(target.id, FIELD_LIMIT) : null,
        classes: truncate(target.getAttribute("class") || "", FIELD_LIMIT),
        role: target.getAttribute("role") || null,
        name: labelOf(target),
        type: target.getAttribute("type") || null,
        href: truncate(target.getAttribute("href") || "", FIELD_LIMIT),
      },
      text: { direct: directTextOf(target), full: fullTextOf(target) },
      candidates: candidates,
      relations: { parent: budgetFor(target), component: componentOf(target) },
      geometry: geometry,
      inShadowRoot: inShadowRoot,
      observation: {
        documentEpoch: "el_" + nonce,
        sameNode: true,
        verified: true,
        queriesUsed: budget.queries,
        budgetExhausted: budget.exhausted,
      },
    };
  };

  const expand = (request, budget) => {
    const target = nodeFor(request.elementId);
    if (!target) return fail("expired", "elementId is unknown or the node is no longer connected");
    const relation = typeof request.relation === "string" && request.relation ? request.relation : "parent";
    const rawLimit = Number.isFinite(request.limit) ? Math.floor(request.limit) : 40;
    const limit = Math.max(1, Math.min(200, rawLimit));
    const nodes = [];
    let total = 0;

    if (relation === "parent") {
      const parent = target.parentElement;
      total = parent ? 1 : 0;
      if (parent && limit >= 1) nodes.push(liteNode(parent, { depth: 1 }));
    } else if (relation === "ancestors") {
      let node = target.parentElement;
      let depth = 1;
      while (node) {
        total += 1;
        if (nodes.length < limit && budget.visited < budget.maxVisitedNodes) {
          budget.visited += 1;
          nodes.push(liteNode(node, { depth: depth }));
        }
        node = node.parentElement;
        depth += 1;
      }
    } else if (relation === "siblings") {
      const parent = target.parentElement;
      if (parent) {
        const children = Array.from(parent.children);
        total = children.length;
        children.forEach((child, index) => {
          if (child === target) return;
          if (nodes.length >= limit || budget.visited >= budget.maxVisitedNodes) return;
          budget.visited += 1;
          nodes.push(liteNode(child, { index: index, count: children.length }));
        });
      }
    } else if (relation === "subtree") {
      const stack = [];
      for (let index = target.children.length - 1; index >= 0; index -= 1) {
        stack.push({ node: target.children[index], depth: 1 });
      }
      while (stack.length) {
        const entry = stack.pop();
        total += 1;
        if (nodes.length < limit && budget.visited < budget.maxVisitedNodes) {
          budget.visited += 1;
          nodes.push(liteNode(entry.node, { depth: entry.depth }));
        } else if (nodes.length >= limit && budget.visited < budget.maxVisitedNodes) {
          budget.visited += 1;
        }
        for (let index = entry.node.children.length - 1; index >= 0; index -= 1) {
          stack.push({ node: entry.node.children[index], depth: entry.depth + 1 });
        }
      }
    } else {
      return fail("invalid_request", "unknown relation: " + String(relation));
    }

    return {
      kind: "element_expansion",
      elementId: idFor(target),
      relation: relation,
      nodes: nodes,
      omitted: Math.max(0, total - nodes.length),
      truncated: total > nodes.length,
      budgetExhausted: budget.exhausted,
    };
  };

  const reset = () => {
    delete globalThis.__abRegistry;
    return { kind: "element_registry_reset", nonce: nonce, version: VERSION };
  };

  const hitTest = (request) => {
    if (!Number.isFinite(request.x) || !Number.isFinite(request.y)) {
      return fail("invalid_request", "'x' and 'y' must be finite numbers");
    }
    let stack = [];
    try {
      stack = document.elementsFromPoint(request.x, request.y) || [];
    } catch (error) {
      return fail("internal", "hit test failed: " + (error && error.message ? error.message : String(error)));
    }
    const layers = [];
    for (const node of stack) {
      if (!node || node.nodeType !== 1) continue;
      if (layers.length >= 12) break;
      const rect = node.getBoundingClientRect();
      const style = node.ownerDocument && node.ownerDocument.defaultView
        ? node.ownerDocument.defaultView.getComputedStyle(node) : null;
      layers.push({
        elementId: idFor(node),
        tag: tagOf(node),
        role: node.getAttribute("role") || null,
        name: labelOf(node),
        directText: directTextOf(node),
        isComponentBoundary: isComponentBoundary(node),
        pointerEvents: style ? style.pointerEvents : null,
        disabled: node.disabled === true || node.getAttribute("aria-disabled") === "true",
        box: {
          x: Math.round(rect.x * 100) / 100,
          y: Math.round(rect.y * 100) / 100,
          width: Math.round(rect.width * 100) / 100,
          height: Math.round(rect.height * 100) / 100,
        },
      });
    }
    return {
      kind: "hit_test",
      point: { x: request.x, y: request.y },
      layers: layers,
      omittedLayers: Math.max(0, stack.length - layers.length),
      hit: layers.length > 0,
      topElementId: layers.length ? layers[0].elementId : null,
    };
  };

  const describe = (element) => {
    if (!element || element.nodeType !== 1) return null;
    return buildCard(element, {}, makeBudget({}));
  };

  const op = (request) => {
    if (!request || typeof request !== "object") {
      return fail("invalid_request", "request must be an object");
    }
    const budget = makeBudget(request);
    try {
      if (request.op === "inspect") return inspect(request, budget);
      if (request.op === "hit_test") return hitTest(request);
      if (request.op === "expand") return expand(request, budget);
      if (request.op === "reset") return reset();
    } catch (error) {
      return fail("internal", error && error.message ? error.message : String(error));
    }
    return fail("invalid_request", "unknown op: " + String(request.op));
  };

  globalThis.__abRegistry = { version: VERSION, nonce: nonce, op: op, describe: describe };
  return { installed: true, nonce: nonce, version: VERSION };
}"""

_REGISTRY_OP_SCRIPT = """(request) => {
  const registry = globalThis.__abRegistry;
  if (!registry || registry.version !== 1 || typeof registry.op !== "function") {
    return { __abMissing: true };
  }
  return registry.op(request);
}"""

_ERROR_CODES = {
    "invalid_selector": CODE_INVALID,
    "invalid_request": CODE_INVALID,
    "not_found": CODE_INVALID,
    "ambiguous": CODE_INVALID,
    "expired": CODE_STALE_REF,
    "internal": CODE_ERROR,
}

_RELATIONS = ("parent", "ancestors", "siblings", "subtree")
_FIELDS = ("geometry",)


class ElementCatalog:
    def __init__(self, runtime: Any):
        self._runtime = runtime
        self._controller_keys: set = set()

    @staticmethod
    def _frame_key(frame: Any) -> tuple:
        try:
            url = frame.url
        except Exception:
            url = None
        return (id(frame), url)

    async def ensure_controller(self, frame: Any) -> Dict[str, Any]:
        state = await frame.evaluate(_REGISTRY_INSTALL_SCRIPT)
        if not isinstance(state, dict) or not state.get("nonce"):
            raise BackendError(CODE_ERROR, "element registry controller failed to install in the selected frame")
        return state

    async def _run(self, frame: Any, request: Dict[str, Any]) -> Any:
        key = self._frame_key(frame)
        if key not in self._controller_keys:
            await self.ensure_controller(frame)
            self._controller_keys.add(key)
        result = await frame.evaluate(_REGISTRY_OP_SCRIPT, request)
        if isinstance(result, dict) and result.get("__abMissing") is True:
            self._controller_keys.discard(key)
            await self.ensure_controller(frame)
            self._controller_keys.add(key)
            result = await frame.evaluate(_REGISTRY_OP_SCRIPT, request)
            if isinstance(result, dict) and result.get("__abMissing") is True:
                raise BackendError(CODE_ERROR, "element registry controller is unavailable in the selected frame")
        return result

    @staticmethod
    def _translate(result: Any, expected_kind: str) -> Dict[str, Any]:
        if not isinstance(result, dict):
            raise BackendError(CODE_ERROR, "element registry returned a non-object response")
        error = result.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or "element registry request failed"
            raise BackendError(_ERROR_CODES.get(code, CODE_ERROR), str(message))
        if result.get("kind") != expected_kind:
            raise BackendError(CODE_ERROR, "element registry returned an unexpected response kind")
        return result

    async def inspect(
        self,
        frame: Any,
        *,
        selector: Optional[str] = None,
        element_id: Optional[str] = None,
        fields: Optional[List[str]] = None,
        budget: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if (selector is None) == (element_id is None):
            raise BackendError(CODE_INVALID, "provide exactly one of 'selector' or 'elementId'")
        if selector is not None and len(selector) > MAX_SELECTOR_LENGTH:
            raise BackendError(CODE_INVALID, f"'selector' exceeds {MAX_SELECTOR_LENGTH} characters")
        request: Dict[str, Any] = {"op": "inspect"}
        if selector is not None:
            request["selector"] = selector
        if element_id is not None:
            request["elementId"] = element_id
        if fields is not None:
            unknown = [field for field in fields if field not in _FIELDS]
            if unknown:
                raise BackendError(CODE_INVALID, "'fields' contains unsupported values: " + ", ".join(unknown))
            request["fields"] = list(fields)
        if budget is not None:
            request["budget"] = dict(budget)
        return self._translate(await self._run(frame, request), "element_card")

    async def hit_test(self, frame: Any, *, x: float, y: float) -> Dict[str, Any]:
        request: Dict[str, Any] = {"op": "hit_test", "x": x, "y": y}
        return self._translate(await self._run(frame, request), "hit_test")

    async def expand(
        self,
        frame: Any,
        *,
        element_id: str,
        relation: str = "parent",
        limit: Optional[int] = None,
        budget: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if relation not in _RELATIONS:
            raise BackendError(CODE_INVALID, "'relation' must be one of: " + ", ".join(_RELATIONS))
        request: Dict[str, Any] = {"op": "expand", "elementId": element_id, "relation": relation}
        if limit is not None:
            request["limit"] = limit
        if budget is not None:
            request["budget"] = dict(budget)
        return self._translate(await self._run(frame, request), "element_expansion")
