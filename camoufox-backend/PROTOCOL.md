# camoufox backend protocol (V1)

Persistent Python worker around official `AsyncCamoufox` (Camoufox) + Playwright. No REST or websocket server. The Rust executor owns lifecycle, policy, and the 28s hard deadline; this worker owns browser execution and gesture dispatch. The current Rust integration targets macOS/Linux, not Windows. A local macOS release build and bounded real-browser acceptance have completed; see the evidence and limitations below. This is not a published distribution or full release certification.

## Files

- `worker.py` — JSON-lines worker entry point (`python worker.py --runtime-dir ABS [--motion human-fast|fast|precision]`).
- `bootstrap.py` — one-shot isolated installer (`python3 bootstrap.py --runtime-dir ABS`).
- `runtime.py` — browser/context/tab/capture runtime (lazy Camoufox/Playwright import).
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
| `camoufox_stale_ref` | `@eN`/`@fNeN` not exposed by the latest snapshot |
| `stale_visual_capture` | capture missing, expired (120s), or URL/scroll/viewport changed |
| `camoufox_timeout` | action deadline exceeded; worker is poisoned |
| `camoufox_poisoned` | browser action refused after a timed-out action |
| `camoufox_unknown_gesture` | gesture name not registered |
| `camoufox_registry_error` | gesture directory/module/schema failed to load |
| `camoufox_output_too_large` | response would exceed 16 MiB |
| `camoufox_error` | browser action failure (Playwright error, close failure, launch failure) |
| `camoufox_internal_error` | unexpected worker bug; import failures include the missing module name |

## Deadlines, poisoning, cleanup

- Per-action deadline: default 22s, env override `AGENT_BROWSER_ACTION_DEADLINE_MS`, hard cap 25s. The Rust executor kills the worker at 28s if native cleanup hangs.
- On deadline or an exception after attempted input: attempt release in `finally` and poison the worker. Deadline errors use `camoufox_timeout`; other failures retain their error code plus `poisoned:true`. Further browser actions are refused (`camoufox_poisoned`); only `close`, `session_info`, `tab_list`, `gestures` remain. No replay or restart. Pending unreleased input cannot produce a success response. Rust marks transport failures after sending as ambiguous and suppresses CLI retries/respawn replay.
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
| `evaluate` | `script` | `{result}` (JSON-safe); Camoufox's default isolated world, not page-owned globals or a main-world opt-in |
| `read` | none | `{content, url, title, source:"rendered"}` rendered body innerText; explicit `url` and read options are unsupported |
| `snapshot` | `selector?`, `maxDepth?`; true `interactive`, `compact`, `urls`, or `cursor` are rejected | `{snapshot, refs, refCount, url, tabId}` native AI format without Chrome filters |
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
| `wait` | `selector?`, `text?`, `timeout?`; no selector/text means sleep | `{waited, timeout}` |
| `waitforurl` | `url`, `timeout?` | `{waited:"url", url}` |
| `waitforloadstate` | `state`, `timeout?` | `{waited, state}` |
| `waitforfunction` | `expression`, `timeout?` | `{waited:"function"}` |
| `tab_new` | `url?`, `label?` (unique) | `{tabId, label, url, active, tabs}` |
| `tab_list` | none | `{tabs:[{tabId,label,url,active,closed}], activeId}` |
| `tab_switch` | `tabId` or label | `{tabId, label, url, active}` |
| `tab_close` | `tabId?` (defaults to active) | `{tabId, closed, activeId, adoptedTab:null}` |
| `session_info` | none | engine/launch/motion/capabilities/limits; never launches |
| `gettext` | `selector` | `{text}` |
| `getattribute` | `selector`, `attribute` | `{attribute, value}` |
| `inputvalue` | `selector` | `{value}` |
| `count` | `selector` | `{count}` |
| `boundingbox` | `selector` | `{boundingBox}` |
| `isvisible` / `isenabled` / `ischecked` | `selector` | `{visible}` / `{enabled}` / `{checked}` |
| `gestures` | `name?` | list of summaries, or full schema/description/examples for one |
| `gesture` | `name`, `params`, `observe?` (`none`/`snapshot`/`screenshot`) | gesture result; screenshot observation sets top-level `path` |
| `close` | none | `{closed:true}` then exit |

Tabs use stable never-reused `tN` ids. The active tab is never silently replaced: closed active leaves no active tab until `tab_switch`/`tab_new`. Popups are registered but never steal the active tab.

Launch/session diagnostics include `browserConnected`, `recoveryRequired`, and `closeReason` (`null`, `browser_disconnected`, or `context_closed`). `launched` reflects a connected browser with a usable owned context, not just stored object references. A connected browser without an active tab is still launched. Page close callbacks receive the emitted Page separately from the captured tab ID; tab/target queries also reconcile `page.is_closed()`. Browser disconnect and context close invalidate tabs, active binding, refs, and captures while retaining resources for explicit cleanup. A closed-session reason is latched until explicit `close`; `launch` cannot silently replace the session. Lifecycle events do not discard pending-input evidence or clear poisoning.

Browser shutdown can emit context-close before disconnect; a later observed disconnect upgrades `closeReason` to `browser_disconnected` without clearing the recovery requirement. A Playwright `TargetClosedError` triggers reconciliation, not an automatic restart: confirmed browser/context loss returns `camoufox_session_closed`, a live browser with no active tab returns `camoufox_no_active_tab`, and unconfirmed scope returns `camoufox_target_closed`. References and captures are invalidated. Failures after attempted input retain `poisoned:true` regardless of closure scope.

## Screenshot captures

`{captureId, imageWidth, imageHeight, viewportWidth, viewportHeight, devicePixelRatio, scrollX, scrollY, url, capturedAt}`. Captures use `scale="css"`; `devicePixelRatio` reports the actual window value, not the PNG/CSS ratio. Identity must remain stable during capture. Validation before dispatch compares URL, tab, scroll, viewport size, and pixel ratio; captures expire after 120s and are invalidated by mutating commands, every gesture attempt, `evaluate`, or a new screenshot. The worker never scrolls for coordinate targets. A screenshot observation is taken after invalidation and its new capture remains usable.

## Snapshots and refs

`page.aria_snapshot(mode="ai", depth=...)` output is returned verbatim; exposed refs are recorded per tab as `{role, name, framePrefix}`. New snapshots replace the set; navigation clears it; iframe prefixes route natively via `aria-ref=fNeN`. Unobserved or stale refs are rejected before any input. Screenshots do not change refs. Actions on iframe refs are allowed only for standard locator actions (`click`, `fill`, `check`, `select`, query actions); advanced gestures (`hold`, `drag`, `path`) reject non-main-frame refs.

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

- Playwright 1.61.0 `aria_snapshot` is public; Camoufox `AsyncCamoufox(...)` is entered via `__aenter__` and exited via `__aexit__` (no private APIs used).
- `scale="css"` screenshot support was exercised on macOS at 1920×1004 and DPR 1.0; high-DPI mapping remains unverified. A missing parameter fails fast with a repair hint rather than silently capturing at device scale.
- Query action names follow the canonical parser (`inputvalue`, `count`, `boundingbox`, `gettext`, `getattribute`, `isvisible`, `isenabled`, `ischecked`).
- One isolated in-memory Python smoke (`python3 -I -B -` with a heredoc harness) completed with exit status 0. Using fake browser input, it covered gesture discovery/schema rejection, ref extraction, screenshot-observation capture preservation, release after a simulated failed mouse-down, and refusal of further input after poisoning. Its temporary directory was removed; no permanent tests were added.
- Linux, high-DPI captures, popup/closed-tab/iframe routing, non-default motion profiles, timeout during a pressed input, and forced-termination cleanup remain unverified. External-site and anti-bot behavior were not tested. No full suite, lint/typecheck, or permanent tests were added or run during this acceptance.

## Acceptance evidence

The local macOS run used Python 3.12.8, Camoufox 0.5.6, Playwright 1.61.0, and the installed record `official/stable/152.0.4-beta.30 (3b43e766)`. The managed venv and browser cache were reused after launch corrections; the existing user's browser installation and MCP configuration were not replaced. All browser checks used fresh named sessions, private socket directories, explicit empty config, and controlled local pages with `human-fast`.

- `cargo build --locked --release --manifest-path cli/Cargo.toml` exited 0 for the corrected release binary without emitted warnings or errors.
- Temporary `camoufox-startup-input.py` exited 0: MCP initialization, a 35-tool core/gestures catalog, skill retrieval with `names`, default native AI snapshot refs, one trusted ref click, eight keydown events, and the expected typed value. Seven tool calls including close.
- Temporary `camoufox-gesture-acceptance.py` exited 0: built-in and copied example schemas, hold with one trusted down/up pair and 190ms observed duration for a requested 180ms, one trusted HTML5 drop with matching payload, example Shift-click followed by an unmodified trusted coordinate click, PNG IHDR dimensions matching CSS-scale capture metadata, and `stale_visual_capture` refusal without an extra click. Thirteen tool calls including close.
- Temporary `camoufox-error-acceptance.py` exited 0: MCP and one direct CLI `snapshot -i` invocation both returned exit 1 with the same `invalid_value` error; a short missing-selector wait returned `camoufox_timeout` with poisoning, and subsequent click was refused with `camoufox_poisoned`. Five tool calls including close, plus the expected-failure CLI invocation.
- All three successful harness runs ended with MCP exit 0, no tracked owned processes, and no owned PID/socket files remaining. Harnesses and evidence were retained in the session's approved temporary directory, not added as a permanent test suite.

Acceptance exposed and corrected a macOS bundle asset-path mismatch, gesture-directory shadowing of the installed Click package, and a zero-size assumption when Camoufox disables Playwright's fixed viewport. The observer fixture was also corrected to read shared DOM attributes from Camoufox's isolated evaluation world, and its longer HTML fixture was served from an owned local file to respect the URL-length bound. These results establish the listed local behaviors, not general application success or anti-bot evasion.
