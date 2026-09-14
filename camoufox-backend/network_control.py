import base64
import re
from typing import Any, Dict

from input_context import BackendError, CODE_INVALID, CODE_NOT_LAUNCHED


class NetworkControl:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.rules = []
        self.attached = False
        self.last_error = None
        self.headers = {}

    def context(self):
        self.runtime.require_open_session()
        if self.runtime.context is None:
            raise BackendError(CODE_NOT_LAUNCHED, "browser context is not launched")
        return self.runtime.context

    async def dispatch(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        context = self.context()
        if action == "offline":
            value = payload.get("offline", True)
            if not isinstance(value, bool):
                raise BackendError(CODE_INVALID, "offline must be a boolean")
            await context.set_offline(value)
            return {"offline": value}
        if action in ("headers", "credentials"):
            headers = payload.get("headers")
            if action == "credentials":
                username, password = payload.get("username"), payload.get("password")
                if not isinstance(username, str) or not isinstance(password, str) or ":" in username:
                    raise BackendError(CODE_INVALID, "credentials require string username/password and no colon in username")
                token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
                headers = {"Authorization": f"Basic {token}"}
            if not isinstance(headers, dict) or any(
                not isinstance(key, str) or not key or not isinstance(value, str)
                or "\r" in key or "\n" in key or "\r" in value or "\n" in value
                for key, value in headers.items()
            ):
                raise BackendError(CODE_INVALID, "headers must be a string map without CR/LF")
            await context.set_extra_http_headers(headers)
            self.headers = dict(headers)
            return {"set": True}
        if action == "route":
            url = payload.get("url")
            if not isinstance(url, str) or not url or len(url) > 8192:
                raise BackendError(CODE_INVALID, "route url must contain 1..8192 characters")
            abort = payload.get("abort", False)
            if not isinstance(abort, bool):
                raise BackendError(CODE_INVALID, "abort must be a boolean")
            response = payload.get("response")
            if response is not None:
                if abort or not isinstance(response, dict) or set(response) - {"body", "status", "headers", "contentType"}:
                    raise BackendError(CODE_INVALID, "route needs abort or a response with body/status/headers/contentType")
                if not isinstance(response.get("body", ""), str):
                    raise BackendError(CODE_INVALID, "route body must be a string")
                status = response.get("status", 200)
                if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
                    raise BackendError(CODE_INVALID, "route response status must be 200..599")
                headers = response.get("headers", {})
                if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
                    raise BackendError(CODE_INVALID, "route response headers must be a string map")
                if not isinstance(response.get("contentType", "application/json"), str):
                    raise BackendError(CODE_INVALID, "route contentType must be a string")
            types = payload.get("resourceType", "")
            if not isinstance(types, str):
                raise BackendError(CODE_INVALID, "resourceType must be a comma-separated string")
            resource_types = {item.strip().lower() for item in types.split(",") if item.strip()}
            valid_types = {"document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch", "eventsource", "websocket", "manifest", "other"}
            if resource_types - valid_types:
                raise BackendError(CODE_INVALID, "unsupported resourceType")
            if len(self.rules) >= 100:
                raise BackendError(CODE_INVALID, "route limit is 100; remove a rule first")
            matcher = re.compile("^" + re.escape(url).replace(r"\*", ".*") + "$")
            if not self.attached:
                await context.route("**/*", self._handle)
                self.attached = True
            self.rules.append((url, matcher, resource_types, abort, response))
            return {"routed": url, "serviceWorkerInterception": False, "httpCacheDisabled": True}
        if action == "unroute":
            url = payload.get("url")
            if url is not None and (not isinstance(url, str) or not url):
                raise BackendError(CODE_INVALID, "unroute url must be a nonempty string")
            remaining = [rule for rule in self.rules if url is not None and rule[0] != url]
            if not remaining and self.attached:
                await context.unroute("**/*", self._handle)
                self.attached = False
            self.rules = remaining
            return {"unrouted": url or "all"}
        raise BackendError(CODE_INVALID, "unknown network control action")

    async def _handle(self, route: Any) -> None:
        try:
            request = route.request
            for _url, matcher, types, abort, response in reversed(self.rules):
                if not matcher.fullmatch(request.url) or (types and request.resource_type.lower() not in types):
                    continue
                if abort:
                    await route.abort()
                elif response is not None:
                    await route.fulfill(status=response.get("status", 200), body=response.get("body", ""),
                                        headers=response.get("headers", {}), content_type=response.get("contentType", "application/json"))
                else:
                    await route.continue_()
                return
            await route.continue_()
        except Exception as exc:
            self.last_error = {"error": type(exc).__name__, "outcome": "unconfirmed"}
            try:
                await route.abort()
            except Exception:
                pass

    def reset(self) -> None:
        self.rules.clear()
        self.headers.clear()
        self.attached = False
        self.last_error = None
