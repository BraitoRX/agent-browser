# camoufox backend protocol (V1)

Persistent Python worker around official `AsyncCamoufox` (Camoufox) + Playwright. No REST or websocket server. The Rust executor owns lifecycle, policy, and the 28s hard deadline; this worker owns browser execution and gesture dispatch. The current Rust integration targets macOS/Linux, not Windows. A local macOS release build and bounded real-browser acceptance have completed; see the evidence and limitations below. This is not a published distribution or full release certification.

## Files

- `worker.py` — JSON-lines worker entry point (`python worker.py --runtime-dir ABS [--motion human-fast|fast|precision]`).
- `bootstrap.py` — one-shot isolated installer (`python3 bootstrap.py --runtime-dir ABS`).
- `runtime.py` — browser/context/tab/capture runtime (lazy Camoufox/Playwright import).
- `network_observer.py`, `browser_inspector.py`, `worker_visibility.py` — bounded network, page-event and context-state inspection.
- `network_control.py`, `har_capture.py` — explicit future-request controls and opt-in diagnostic exports.
- `input_context.py` — stdlib helpers, error codes, capture registry, input journal, target parsing.
- `registry.py` — gesture discovery + bounded JSON Schema subset validator.
- `gestures/` — built-in gestures (`click`, `hover`, `hold`, `drag`, `scroll`, `type`, `path`); `gestures/_common.py` is a helper excluded from discovery.
- `examples/custom_gesture_example.py` — external gesture example, not auto-loaded.
- `requirements.txt` — `camoufox==0.5.6`, `playwright==1.61.0`.

## Transport

- One JSON request per line on stdin (max 1 MiB); one JSON response per line on stdout (max 16 MiB). Library and plugin output is redirected to stderr after redirecting `sys.stdout = sys.stderr`.
- Requests: `{id:string, action:string, ...canonical fields}`. `id` must be non-empty.
- Success: `{id, success:true, data:{...}}`.
- Failure: `{id, success:false, error:string, code:string, data?:{...}, poisoned?:true}`. Lifecycle failures include launch/connectivity diagnostics and the reconciled active tab and tab list in `data`.
- Commands are executed serially. `close` replies, then the worker closes context/browser/Playwright and exits. EOF/SIGTERM also close owned resources and exit.
- Unknown actions and unimplemented fields fail with `camoufox_unsupported` before any browser launch. Unknown launch options are rejected, never silently ignored.

## Error codes

| code | meaning |
| --- | --- |
| `camoufox_invalid_request` | malformed JSON, bad id/action, line over 1 MiB |
| `camoufox_invalid_params` | field/type/range/schema validation failure |
| `camoufox_unsupported` | action/field/feature intentionally out of scope for V1 |
| `camoufox_not_launched` | browser command without a live launch |
| `camoufox_session_closed` | browser disconnected or context closed; explicit session close/open required, no implicit relaunch |
| `camoufox_target_closed` | target closed during an action but its scope is unconfirmed; inspect session/tab diagnostics, do not assume browser death or replay input |
| `camoufox_no_active_tab` | active tab missing/closed; explicit `tab_switch`/`tab_new` required |
| `camoufox_stale_ref` | accessibility/DOM ref or pagination cursor is stale for the current tab/document |
| `stale_visual_capture` | capture missing, expired (120s), or URL/scroll/viewport changed |
| `camoufox_timeout` | operation timeout or worker deadline expiry; inspect `data.timeoutKind` and `poisoned` |
| `camoufox_poisoned` | browser action refused after a worker deadline, unreleased input, or other ambiguous failure |
| `camoufox_unknown_gesture` | gesture name not registered |
| `camoufox_registry_error` | gesture directory/module/schema failed to load |
| `camoufox_output_too_large` | response would exceed 16 MiB |
| `camoufox_error` | browser action failure (Playwright error, close failure, launch failure) |
| `camoufox_internal_error` | unexpected worker bug; import failures include the missing module name |

### Playwright error text

MCP recovery hints use plain language. An unpoisoned operation timeout adds: "The browser is still available. Inspect the current page before continuing; do not automatically replay input." Agents should treat this as a failed step, observe the same session, and choose the next step from fresh evidence rather than restarting or abandoning the task merely because a wait expired. The structured safety fields and explicit-close requirements are unchanged.

General Playwright failures use `browser action '<action>' failed: <ExceptionClass>: <diagnostic>`. Completed Playwright timeout failures use `timed out` instead of `failed` and retain `camoufox_timeout` with `data.timeoutKind: "operation"`. They do not poison the session when input cleanup succeeds, including when an input API was attempted. The diagnostic is the first nonempty line of `Error.message`, falling back to `str(error)`; an empty result becomes `no diagnostic detail available`. It is capped at 2000 characters including ` [truncated]` when needed. Terminal controls are removed. Subsequent call-log, DOM match-list, and stack-trace lines are omitted rather than exposing the full exception.

Before summarizing or truncating, the worker redacts nonempty string values under request fields `text`, `value`, `values`, `key`, `script`, and `expression`, including nested gesture parameters and list values. Literal and common JSON/Python-escaped forms are replaced with `[redacted]`. Single-character literals require non-word boundaries, and whitespace-only literals require surrounding quotes, so a typed letter or space does not destroy the diagnostic's words or layout. This is not a general secret classifier: selectors and browser-supplied page text can remain in the reason, so keep secrets out of selectors and custom diagnostic messages.

Rust forwards `error`, `code`, and the timeout classification and preserves poisoning in JSON `data`. MCP text includes the `camoufox_` code, distinguishes unpoisoned operation timeouts, and exposes explicit-close/no-replay guidance whenever poisoning is reported; structured content retains the CLI response. A more specific error or successful release never establishes that input was not attempted, and does not authorize an automatic retry or first-match fallback.

## Deadlines, poisoning, cleanup

- Per-action deadline: default 22s, env override `AGENT_BROWSER_ACTION_DEADLINE_MS`, hard cap 25s. The Rust executor kills the worker at 28s if native cleanup hangs.
- Completed Playwright operation timeouts return `camoufox_timeout` with `data.timeoutKind: "operation"`. Release journaled inputs in `finally`; if no input remains pending, do not poison or close the session. Inspect the current page before deciding what to do next. No automatic replay is performed, and the operation may already have affected the application.
- On actual worker deadline expiry, unreleased input, or a non-timeout exception after attempted input: attempt release in `finally` and poison the worker. Worker deadline errors use `camoufox_timeout` with `data.timeoutKind: "deadline"`; other failures retain their error code plus `poisoned:true`. An operation timeout with failed release also retains `poisoned:true`. Further browser actions are refused (`camoufox_poisoned`); only `close`, `session_info`, `tab_list`, `gestures` remain. No replay or restart. Pending unreleased input cannot produce a success response. Rust marks transport failures after sending as ambiguous and suppresses CLI retries/respawn replay.
- A generic gesture, cleanup, and optional observation share one action budget. Rust caps the whole command, including an implicit browser launch, at 28s. The CLI rejects wait timeouts that cannot fit with 500ms margin inside the configured worker deadline.
- `hold` plugin duration max 20s; a hold that cannot fit its release inside the deadline fails before pressing.
- Input diagnostics: every gesture returns `diagnostics.inputDispatched` (attempt counter only), `diagnostics.semanticSuccess` is always `not_asserted`. Text typed is never echoed.

## Actions

Shared conventions: `selector` accepts CSS or native aria refs `@eN` / `@fNeN`. Coordinate targets require a screenshot `captureId`.

| action | fields | data |
| --- | --- | --- |
| `launch` | `headless?` (default true), `engine?` must be `camoufox` | `launched, browserConnected, recoveryRequired, closeReason, engine, headless, motion, humanize, runtimeDir, startedAt` |
| `navigate` | `url`, `waitUntil?` (`load`/`domcontentloaded`/`networkidle`/`commit`) | `{url, title}` |
| `back` / `forward` / `reload` | none | `{url, title}` |
| `url` / `title` / `content` | none | `{url}` / `{title}` / `{content}` |
| `evaluate` | `script` | `{result,frameId,frameUrl}` (JSON-safe), selected frame's isolated world, not page-owned globals or a main-world opt-in |
| `read` | none | `{content,url,title,source:"rendered",frameId,frameUrl}` selected-frame body text; top-level `url`; explicit URL/read options unsupported |
| `snapshot` | `selector?`, `maxDepth?` (0–100, zero unlimited); true `interactive`/`compact` rejected; boolean `urls`/`cursor` accepted for always-present annotations | `{snapshot,refs,refCount,url,tabId,frameId,frameUrl,frames,framesOmitted,selectedFrameDetached}` native AI; quoted keys preserve refs; link refs include raw `url` |
| `page_outline` | `selector?` | Bounded DOM-derived root/headings/landmark-like regions plus link/form/interactive counts for selected frame; unique `main` or document root by default |
| `page_links` | first page: `selector?`, `limit?` (1–200, default 50); continuation: `cursor`, optional unchanged `limit` | Cursor-paginated links from one native AI snapshot with actionable refs, text, raw/resolved URLs, nearest heading section, totals and page version |
| `dom_chunk` | first page: `selector?`, `limit?` (1–500, default 100); continuation: `cursor`, optional unchanged `limit` | Cursor-paginated preorder element records with document-scoped actionable `@dN` refs, fingerprint/revision validation, totals and explicit 50000-node inventory cap |
| `screenshot` | `path?`, `screenshotDir?`; `selector`, `fullPage`, `annotate`, non-png `format`, `quality` rejected before capture | `{path, format:"png", scale:"css", visualCapture}` |
| `click` | `selector` or `target`, `button?`, `count?` (1..2), `newTab?` must be false | gesture data + `{action:"click"}` |
| `dblclick` | `selector`, `button?` | double click |
| `fill` | `selector`, `value` | conventional nonhuman fill |
| `type` | `selector`, `text`, `clear?`, `delay?` | real key events |
| `press` | `key` | `{pressed}` |
| `hover` | `selector`, `settleMs?` | gesture data |
| `focus` / `check` / `uncheck` | `selector` | action echo |
| `select` | `selector`, `values` (string or array of option values; no implicit label fallback) | `{selected, count}` |
| `drag` | `source`, `target` (selector-string or target object), `button?`, `steps?`, `holdBeforeDropMs?`, `reveal?` | press/drop points, `releaseDispatched` (not semantic drop success) |
| `scroll` | `direction`, `amount?`, `selector?`, `chunkSize?`, `settleMs?`, `selector` | gesture data |
| `scrollintoview` | `selector` | Reveals exactly one visible match, then scrolls it into view without actionability waiting; absent, hidden, zero-sized, or ambiguous targets are rejected with `camoufox_invalid_params` without poisoning, so `{scrolled:true,selector}` is only returned after completion |
| `wait` | `selector?`, `text?`, `timeout?`; no selector/text means sleep | `{waited, timeout}` |
| `waitforurl` | `url`, `timeout?` | `{waited:"url", url}` |
| `waitforloadstate` | `state`, `timeout?` | `{waited, state}` |
| `waitforfunction` | `expression`, `timeout?` | `{waited:"function"}` |
| `tab_new` | `url?`, `label?` (unique) | `{tabId, label, url, active, tabs}` |
| `tab_list` | none | `{tabs:[{tabId,label,url,active,closed,frames?,framesOmitted?,selectedFrameDetached?}],activeId}` |
| `frame` / `mainframe` | `selector` (observed frame-N ID, unique iframe CSS or iframe ref) / none | Explicit DOM/CSS scope switch; `{tabId,frameId,frameUrl,frames,framesOmitted,selectedFrameDetached}`; clears refs/captures |
| `tab_switch` | `tabId` or label | `{tabId, label, url, active}` |
| `tab_close` | `tabId?` (defaults to active) | `{tabId, closed, activeId, adoptedTab:null}` |
| `session_info` | none | engine/launch/motion/capabilities/limits; never launches |
| `gettext` | `selector` | `{text}` |
| `innerhtml` | `selector` | `{html}` from selected DOM element; does not serialize shadow roots |
| `getattribute` | `selector`, `attribute` | `{attribute, value}` |
| `inputvalue` | `selector` | `{value}` |
| `count` | `selector` | `{count}` |
| `boundingbox` | `selector` | `{boundingBox}` |
| `isvisible` / `isenabled` / `ischecked` | `selector` | `{visible}` / `{enabled}` / `{checked}` |
| `gestures` | `name?` | list of summaries, or full schema/description/examples for one |
| `gesture` | `name`, `params`, `observe?` (`none`/`snapshot`/`screenshot`) | gesture result; screenshot observation sets top-level `path` |
| `close` | none | `{closed:true}` then exit |

### Inspection and explicit controls

Backend additions require a rebuilt fork binary and a fresh daemon/MCP server. Source capability is not live acceptance. Enable `network,state,debug,tabs` alongside `core,gestures` as needed. A Camoufox-started MCP server filters unsupported tools/arguments even in `all`, fixes the engine to Camoufox and rejects stale unadvertised keys before dispatch. V1 names this private protocol/runtime boundary, unrelated to OpenCode V2.

Frame IDs are stable per tab while the frame remains live and are never reused. Frame metadata returns at most 256 entries with `framesOmitted`; names/URLs there are clipped to 4096 characters. DOM/CSS, content/read/eval/snapshots and text/function waits follow selected scope. Native refs still route page-wide. Navigation/history/title/URL/load waits, network/state and screenshots remain top-level/context operations. Detached scope fails explicitly until recovery; `mainframe` works even then. Advanced drag/hold/path require main scope. Each scoped or full snapshot replaces exposed refs, matching native snapshot-cache lifetimes.

Snapshots are not a complete DOM view. Use `page_outline`, `page_links`, and `dom_chunk` for bounded structured inspection; raw DOM strings and eval results still have the 16 MiB transport ceiling without pagination. CSS reaches open shadow roots, while eval must traverse them explicitly and DOM chunks do not expand descendant shadow roots. Closed roots, semantic find and arbitrary selector-engine chaining are not exposed. Raw href metadata may be relative and may contain secrets; page-link records preserve it and also include a resolved URL.

| action | fields | data / scope |
| --- | --- | --- |
| `requests` | `clear?`, `filter?`, `type?`, `method?`, `status?` | Context-wide bounded metadata with `requestId`, `tabId`, redirects, status/failure, `dropped`, `omitted` |
| `request_detail` | `requestId` | Sensitive header arrays preserving duplicates, POST data, timings, available response body and encoding/omission markers; never replays the request |
| `console` / `errors` | `clear?` | Page console messages / uncaught page errors across registered tabs; not Firefox process logs |
| `websockets` | `clear?`, `filter?` | Bounded connection and sent/received frame events; binary payloads use base64 |
| `workers` | none | Active-page dedicated workers and current-origin service-worker registrations; 1 MiB combined view with omission counts; not worker debugging or service-worker traffic |
| `cookies_get` / `cookies_set` / `cookies_clear` | `urls?` / `cookies` / none | Owned-context cookies, including HttpOnly values; writes use public Playwright cookie fields and reject unknown fields |
| `storage_get` / `storage_set` / `storage_clear` | `type` (`local`/`session`), `key?` / `type,key,value` / `type` | Active-origin web storage; empty keys/values are valid; writes are not truncated |
| `route` / `unroute` | `url,abort?,response?,resourceType?` / `url?` | Explicit future-request rules; `*` matches any characters including `/`, newest matching rule wins; no domain containment |
| `headers` / `offline` / `credentials` | `headers` / `offline` / `username,password` | Context-wide extra headers, offline mode, or Basic Authorization header; not origin-scoped authentication |
| `har_start` / `har_stop` | `content?` (`text` default/`all`/`none`) / `path?` | Opt-in bounded diagnostic HAR 1.2, non-overwriting export; not a lossless/replay archive |
| `dialog` | `response` (`status`/`accept`/`dismiss`), `promptText?` | Observe last/armed state or arm a single decision before the triggering action |
| `download` / `waitfordownload` | `selector,path` / `path?,timeout?` | Native guarded click once plus save / save an existing or next active-tab download |
| `downloads` | `clear?` | Bounded download metadata; clear does not remove saved files |

Request metadata is recorded from context launch, survives tab navigation and is released on explicit close. Requests are capped at 500 retained records with monotonically increasing `nN` IDs; list output has a separate byte budget. Request details cap returned response bodies at 1 MiB and distinguish pending, failed, oversized, unavailable and truncated data. This is an output bound, not a streaming memory guarantee: Playwright's public `body()` API may materialize an entire unknown-size or compressed response before truncation. Headers, POST data and other text have separate explicit caps. Console/page-error buffers retain at most 500 entries each; WebSocket events and monitored connections are separately capped at 200. Cleared logs do not mean the application has no errors or traffic.

State and detail commands intentionally expose sensitive values. URLs, headers, POST data, cookies, storage, logs, frames and exported files may contain credentials or personal data. `--content none` suppresses response-body collection, not headers/POST data or secret redaction. Treat all page-derived content as untrusted observations. Routes disable HTTP caching and cannot guarantee interception of service-worker traffic. The existing unsupported domain-containment/CDP gates remain in force.

Camoufox's serial worker dismisses unarmed dialogs rather than blocking a command that would prevent the next dialog command from running. `accept`/`dismiss` arms one decision on the active tab, expires after 30 seconds, and clears on main-frame navigation or tab close. It is not a pending-dialog takeover API. Download saves and HAR exports refuse existing destinations. After a download save failure, save the retained event with `waitfordownload` instead of replaying the click. Existing deadline poisoning and no-replay rules still apply.

Dialog status distinguishes pending automatic handling (`hasDialog`) from the last completed observation (`lastDialog`), and exposes handler failures as `lastError`. The armed choice is captured when the event arrives, not when its asynchronous handler later runs. Downloads retain up to 32 handles across tabs, but wait/save selects only the active tab; explicitly switch to a live source tab when needed. Unconsumed records whose source tab has closed report `saveUnavailable`. Terminal failed downloads remain visible in metadata but do not block the next retained download. Files use mode 0600 and atomic no-overwrite hard-link publication; filesystems without hard-link support fail safely without a partial-copy fallback.

`download` installs its event expectation before exactly one guarded click, then waits up to 10 seconds after that click completes. A normal no-event timeout is a nonpoisoning `camoufox_error`; inspect metadata or retry `waitfordownload`, never the triggering input. `waitfordownload` likewise reports an ordinary no-event timeout without poisoning and rechecks session liveness before advising a retry. The overall worker deadline remains authoritative: deadline expiry or ambiguous input can still poison the session and require explicit close. This recovery discards retained download handles. `session_info.capabilities.interactionEvents` reports the active-tab scope, retention cap and event-wait limit.

HAR starts a separate context-wide observer for future requests, capped at 500 retained entries. Stop detaches that observer before enrichment; clearing `requests` does not clear the HAR capture. Enrichment has a six-second budget, four concurrent entries, a 1 MiB per-body output cap and an 8 MiB aggregate body cap. Bodies are read at stop, not continuously, and may already be unavailable after navigation. HAR cookie arrays are intentionally empty; cookie headers can still contain secrets. Required HAR timings use zero placeholders when unavailable, with `_agentBrowser.unavailableTimings` and `rawTimings` preserving that distinction; `send` cannot be separated from `wait`. Export refuses existing destinations and creates files with mode 0600. A failed write retains the serialized capture for `har_stop` with a new path without recapturing traffic. `session_info.harCapture` reports `active` and `pendingExport`; explicit close discards both states. The parent command's deadline and poisoning rules take precedence over retrying an export.

Tabs use stable never-reused `tN` ids. The active tab is never silently replaced: closed active leaves no active tab until `tab_switch`/`tab_new`. Popups are registered but never steal the active tab.

Launch/session diagnostics include `browserConnected`, `recoveryRequired`, and `closeReason` (`null`, `browser_disconnected`, or `context_closed`). `launched` reflects a connected browser with a usable owned context, not just stored object references. A connected browser without an active tab is still launched. Page close callbacks receive the emitted Page separately from the captured tab ID; tab/target queries also reconcile `page.is_closed()`. Browser disconnect and context close invalidate tabs, active binding, refs, and captures while retaining resources for explicit cleanup. A closed-session reason is latched until explicit `close`; `launch` cannot silently replace the session. Lifecycle events do not discard pending-input evidence or clear poisoning.

Browser shutdown can emit context-close before disconnect; a later observed disconnect upgrades `closeReason` to `browser_disconnected` without clearing the recovery requirement. A Playwright `TargetClosedError` triggers reconciliation, not an automatic restart: confirmed browser/context loss returns `camoufox_session_closed`, a live browser with no active tab returns `camoufox_no_active_tab`, and unconfirmed scope returns `camoufox_target_closed`. References and captures are invalidated. Failures after attempted input retain `poisoned:true` regardless of closure scope.

## Screenshot captures

`{captureId, imageWidth, imageHeight, viewportWidth, viewportHeight, devicePixelRatio, scrollX, scrollY, url, capturedAt}`. Captures use `scale="css"`; `devicePixelRatio` reports the actual window value, not the PNG/CSS ratio. Identity must remain stable during capture. Validation before dispatch compares URL, tab, scroll, viewport size, and pixel ratio; captures expire after 120s and are invalidated by mutating commands, every gesture attempt, `evaluate`, or a new screenshot. The worker never scrolls for coordinate targets. A screenshot observation is taken after invalidation and its new capture remains usable.

## Snapshots and refs

Native AI snapshot output is returned verbatim; exposed refs are recorded per tab as `{role,name,framePrefix,url?,section?}`. Every snapshot replaces the set; navigation, frame detachment and explicit scope changes clear it. Iframe prefixes route natively via `aria-ref=fNeN`. Refs absent from the recorded set are rejected before input. DOM chunks separately map returned `@dN` refs to absolute CSS paths in the selected frame. Navigation/frame changes/snapshots and typed mutating actions clear DOM refs and inventory cursors; DOM continuation also checks a deterministic structure/attribute/direct-text fingerprint. DOM-only re-rendering can invalidate a previously exposed accessibility or DOM node without a lifecycle event before the next continuation check; native locator resolution may then fail or time out. Reobserve and preserve the reported timeout/no-replay safety boundary. Screenshots do not change accessibility refs. Standard locator actions can use iframe and DOM refs; advanced `hold`/`drag`/`path` require main-frame targets and scope.

Page-link text and URL fields are bounded to 2048 characters and include explicit truncation booleans. DOM record text, identifiers, attribute names, and attribute values are bounded to 500 characters; at most 30 sorted attributes are returned per node, with explicit truncation and omission fields. DOM responses additionally stop below an internal 8 MiB target and continue from the first unreturned node through `nextCursor`.

Evaluation uses Camoufox's patched default execution context, not upstream Playwright's unmodified main-world behavior. Camoufox documents isolation in its [evaluation regression fixture](https://github.com/daijro/camoufox/blob/main/tests/patches/isolated-evaluate.py); this backend does not opt into `main_world_eval`. Shared DOM mutations remain visible to the page, and this is not a general security sandbox or an independent certification of every installed browser asset.

## Gesture registry

- `gestures/*.py` modules export `NAME`, `DESCRIPTION`, `SCHEMA`, `EXAMPLES`, `async run(ctx, params)`. Files starting with `_` are skipped. Discovery is sorted; duplicate names fail; external modules may not shadow built-ins.
- Optional trusted directory: `AGENT_BROWSER_GESTURES_DIR` (colon/OS-separator list supported by `discover`). Never scans the CWD. Extensions are trusted executable code with no sandbox; close the session to reload.
- Gesture directories are appended after interpreter and installed-package paths, allowing sibling helpers without replacing dependencies such as Click with `gestures/click.py`.
- `await ctx.viewport_size()` measures the rendered viewport using the same metrics as screenshot identity. Bounds checks remain valid when Camoufox disables Playwright's fixed viewport; measurement does not resize the window or change fingerprint settings.
- Schema validator supports only: `type` (object/array/string/boolean/number/integer), `properties`, `required`, `additionalProperties`, `items`, `oneOf`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `minItems`, `maxItems`, `description`, `default`. Unsupported keywords reject at registration. Objects are strict (`additionalProperties:false`). Booleans are not numbers; non-finite numbers are rejected; unknown params are rejected.
- Built-ins: `click` (left/right/middle, count 1..2), `hover` (settleMs 0..1000), `hold` (100..20000ms), `drag` (optional 1..4 reveal hover steps with bounded settle, `steps`, `holdBeforeDropMs`), `scroll` (bounded native wheel chunks, optional hover target), `type` (real keystrokes, bounded delay), `path` (max 64 waypoints; fast/precision motion only, rejected under human-fast).
- Drag uses the documented HTML5 sequence: down, move to target, second move to target, up. Endpoints resolve before pressing (reveal and approach may already move the pointer). Coordinate drags require two coordinates from one capture and no reveal steps; mixed coordinate/selector drags are rejected. Selector endpoints are hit-tested, re-measured without further scrolling, and checked again after approach before pressing. Button-down attempts are journaled before awaiting and release is attempted in `finally`, including failed-down cases. Live acceptance is required to establish actual event and cleanup behavior.

## Motion profiles

`--motion human-fast|fast|precision` fixes launch-time humanization per process: `human-fast` = Camoufox `humanize=0.25`, `fast`/`precision` = `humanize=False`. The 0.25 level is tuning, not a wall-time SLA. No extra random mouse paths are layered over native humanization, and the worker never claims precise paths under human-fast.

## Launch option gating (defense in depth)

Rejected before launch: `allowedDomains`, `provider`, `cdp`, `auth`, `storageState`, `restoreKey`, `extensions`, `initScripts`, `args`, `profile`, `autoConnect`, and other unknown launch keys. `noXvfb` and `webmcp` must be absent/false. The optional worker-only `metadata.policyActive` boolean blocks generic gestures without blocking typed-action launch; other metadata keys fail. Rust is the final policy authority and blocks generic gestures whenever action-policy or confirm-actions state is active. Rust strips only inert CLI metadata before forwarding. Unknown action fields fail in both adapters.

## Bootstrap

`python3 bootstrap.py --runtime-dir ABS` (Python >= 3.10):

1. Creates `ABS/venv` and installs `requirements.txt` with `ABS/venv/bin/python -m pip` (no shell, no global pip).
2. Sets private `HOME=ABS/home`, `XDG_CACHE_HOME=ABS/home/.cache` (plus `USERPROFILE`/`LOCALAPPDATA`/`APPDATA` on Windows) before fetch, and strips `GITHUB_TOKEN`/`GH_TOKEN`/`*_GITHUB_TOKEN` from the fetch env.
3. Runs `venv/bin/python -m camoufox set official/stable`, then `venv/bin/python -m camoufox fetch`. In the pinned package, `fetch` accepts a concrete version, not a channel selector.
4. Resolves the installed executable with downloads disabled, then writes `ABS/runtime.json` with version pins, best-effort active-browser metadata, and the resolved executable path under the private HOME; prints one JSON response `{installed:true, runtimeDir, venv, pins, browser, record}`. Package/fetch logs go to stderr and cannot be interpreted as the executable path.
5. Uses a stdlib `O_CREAT|O_EXCL` install lock (`ABS/.bootstrap.lock`) with a clear collision error and finally-removal; never deletes non-owned paths.

Runtime startup does not install or update packages, browsers, or default addons: `worker.py` re-points HOME/XDG_CACHE_HOME at the private directory before importing Camoufox. Launch requires the recorded executable and matching package pins and supplies `executable_path` and `exclude_addons` explicitly. Rust uses a private temporary directory for browser profiles and unpacks bundled code into a content-addressed runtime subdirectory. This does not constitute a full transitive dependency or browser asset lock.

For macOS app bundles, launch options use an asset anchor under `Contents/Resources` because Camoufox 0.5.6 reads `properties.json` beside the supplied path. The worker replaces that anchor with the validated recorded executable before passing `from_options` to `AsyncCamoufox`; the asset anchor is never executed. Linux keeps the executable-adjacent asset layout.

## Environment

| variable | effect |
| --- | --- |
| `AGENT_BROWSER_ACTION_DEADLINE_MS` | per-action deadline (1000..25000, default 22000) |
| `AGENT_BROWSER_GESTURES_DIR` | trusted external gesture directory |
| `AGENT_BROWSER_CAMOUFOX_RUNTIME` | Rust absolute runtime-root override |
| `AGENT_BROWSER_PYTHON` | Rust installer interpreter, default python3 |
| `AGENT_BROWSER_MOTION` | Rust worker launch profile; worker CLI uses `--motion` |
| `HOME`, `XDG_CACHE_HOME` | forced to the private runtime home by worker/bootstrap |
| `NO_COLOR` | respected by the parent CLI only, not needed here |

## Known risks / open items

- Anything that can break the running browser is recorded in `../CRITICAL_CHANGELOG.md`; if clicks hang to the 22s deadline and report `camoufox_poisoned`, read that file first.
- Playwright 1.61.0 `aria_snapshot` is public; Camoufox `AsyncCamoufox(...)` is entered via `__aenter__` and exited via `__aexit__` (no private APIs used).
- `scale="css"` screenshot support was exercised on macOS at 1920×1004 and DPR 1.0; high-DPI mapping remains unverified. A missing parameter fails fast with a repair hint rather than silently capturing at device scale.
- Query action names follow the canonical parser (`inputvalue`, `count`, `boundingbox`, `gettext`, `getattribute`, `innerhtml`, `isvisible`, `isenabled`, `ischecked`).
- The guarded input dispatch relies on JS swapped into the installed browser's `omni.ja`; a runtime reinstall drops it and reintroduces the click-hang/poisoning failure. Recovery is `scripts/camoufox-guard-patch.py`; the durable fix is pinning upstream `v152.0.4-beta.31` once its release assets exist.
- The DOM/MCP update does not establish live nested/cross-origin frame routing, non-default motion, high-DPI or cross-platform correctness. Closed-shadow access is not supported.

## Verification scope

Regression coverage lives in `tests/` (request retention/detail bounds, inspection buffers, storage/cookies, route precedence, HAR export, dialog arming, download publication, worker dispatch, and the timeout/lifecycle policy) using stdlib fakes, plus the Rust `camoufox_inspection`, `camoufox_lifecycle`, `camoufox_timeout`, and `camoufox_dom_contract` filters. These do not establish real Firefox event ordering, service-worker coverage, browser-crash recovery, cross-platform filesystem behavior, nested/cross-origin frames, or universal DOM access. Exact commands and per-run evidence were removed as stale; behavior-breaking changes are tracked in `../CRITICAL_CHANGELOG.md`.

Building a new release binary and enabling MCP profiles does not upgrade an already running Camoufox daemon. Reconnect the MCP server to refresh its tool catalog, then use a fresh named browser session for the new backend. Keep an existing session untouched until its owner explicitly chooses to close it; do not replay any ambiguous input during activation.
