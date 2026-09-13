# Camoufox V1 reference

Load `skills get camoufox` for the workflow, or call MCP `agent_browser_skills_get` with `names: ["camoufox"]`. This reference describes the fork's supported subset, not upstream CLI parity. The Python protocol and source live in `camoufox-backend/`; the Rust lifecycle adapter is `cli/src/native/camoufox.rs`.

## Commands

| Command | Behavior |
| --- | --- |
| `--engine camoufox install` | Explicit isolated venv/browser installation; no `--with-deps` or automatic startup updates |
| `gestures [name]` | Discover summaries, or a full schema and examples; no browser launch |
| `gesture <name> --params '<object>' [--observe none|snapshot|screenshot]` | Validate parameters, execute serially, optionally observe in the same call |
| `snapshot [-s <CSS-or-ref>] [-d <depth>]` | Native Playwright AI snapshot with element-backed aria refs; Chrome filtering flags are unsupported. MCP defaults to `interactive: false` for Camoufox and rejects explicit `true` |
| `screenshot [path] --json` | Viewport PNG plus capture metadata; no full-page, annotated, or element capture |
| `read` | Rendered current-page text; no explicit URL fetch or read options |
| `tab new [url] [--label <label>]`, `tab`, `tab <id-or-label>`, `tab close [id]` | Stable `tN` IDs, no popup focus stealing, no silent replacement of a closed active tab |

Navigation/history, evaluation, basic element reads, standard click/fill/type/press/hover/focus/check/uncheck/select/drag/scroll, and bounded waits use the canonical CLI commands. Unsupported fields fail rather than being treated as successful no-ops. Select operates on option values in the V1 worker; do not assume visible-label fallback unless supplied by a later verified implementation.

Evaluation uses Camoufox's default isolated world. Read shared DOM state, such as element values or data attributes; page-owned `window` globals are not visible. V1 does not expose the detectable main-world evaluation opt-in. See [the upstream execution-world reference](https://camoufox.com/python/main-world-eval/).

## Built-in gestures

| Name | Parameters and limits |
| --- | --- |
| `click` | `target`, optional `button` and `count` (1 or 2) |
| `hover` | `target`, optional `settleMs` (0–1000) |
| `hold` | Main-frame `target`, `durationMs` (100–20000), optional button and pre-settle |
| `drag` | Main-frame `source` and `target`, optional button, steps (1–60), drop dwell (0–5000), and 1–4 CSS-selector reveal hover steps |
| `scroll` | Direction, amount, optional selector and bounded wheel chunk/settle settings |
| `type` | Selector, text, optional clear, per-key delay, and post-settle; text is not echoed in diagnostics |
| `path` | Main-frame selector source, up to 64 element-relative waypoints inside its box, and optional duration/button; only fast/precision motion |

A target is exactly `{"selector":"@e2"}` or `{"coordinates":{"x":120,"y":80,"captureId":"..."}}`. Never mix the shapes. Coordinate drags require two endpoints sharing the same fresh capture and no reveal. Selector endpoints resolve to box centers; observe the resulting application value rather than assuming geometric movement achieved a semantic target.

## Writing an extension

Use `camoufox-backend/examples/custom_gesture_example.py` as the starting point. Configure an absolute trusted directory with `AGENT_BROWSER_GESTURES_DIR` before launching a fresh session. Files beginning with `_` are helpers and are skipped. Discovery is sorted; names are unique and cannot shadow built-ins. Changes reload only with a new worker; if the directory environment changes, use a new daemon session too.

Sibling helpers remain importable, but gesture directories follow the standard library and installed packages in Python's import search path. Do not rely on a gesture filename overriding an installed dependency.

Use `await ctx.viewport_size()` to read the current rendered viewport. Camoufox may disable Playwright's fixed viewport for fingerprint consistency, so `page.viewport_size` is not a reliable source for gesture bounds. The helper measures without resizing the browser.

Every module exports `NAME`, `DESCRIPTION`, `SCHEMA`, `EXAMPLES`, and `async run(ctx, params)`. The schema root must be a strict object or `oneOf` of strict objects. The bounded validator supports `type`, `properties`, `required`, `additionalProperties:false`, `items`, `oneOf`, `enum`, numeric bounds, string/array length bounds, `description`, and `default`. Defaults are descriptive; the implementation must apply them. Unknown keywords and unknown parameters fail. Booleans are not numbers. Do not advertise full JSON Schema support.

Use `ctx.page` for public Playwright methods, `ctx.require_exposed_ref` for native refs, `ctx.resolve_capture_point` for captures, `ctx.set_stage` for diagnostics, and `ctx.note_input_dispatched` before every input attempt. Journal button/key down attempts **before** awaiting them and release in `finally`, including failed-down cases. Use the packaged helper functions for standard click/keyboard operations. Respect `ctx.remaining_ms()` and leave release time; do not catch a timeout and continue input. Return `ctx.diagnostics()` without claiming semantic success or echoing typed secrets. Native action cleanup and process termination require live acceptance; do not infer that a source-only check proves physical event behavior.

Modules are trusted executable code, not a sandbox. Schema validation protects the command shape, not the filesystem, network, or browser from a malicious extension. Generic gestures are rejected with any active action policy or confirm-actions setting; standard typed commands retain existing policy gates. Browser domain containment, CDP/provider features, launch plugins, auth/restore/profiles, and mobile/OS-pointer interaction are outside V1.

## Runtime and update boundaries

### Session liveness

`session info` reports the actual Camoufox `launched` state together with `browserConnected`, `recoveryRequired`, and `closeReason` (`null`, `browser_disconnected`, or `context_closed`). Browser/context closure invalidates registered tabs, refs, and captures. Page closure clears that tab's refs and active binding without adopting another tab; `tab list` reconciles closed pages even when a close event was missed.

`camoufox_no_active_tab` means a live browser needs an explicit `tab <id>` or `tab new`. `camoufox_session_closed` means explicit session `close` followed by `open` is required; the backend does not silently relaunch or replay commands. Reset only the affected task-owned session, at most once for the same failure. Ask before closing an ambiguously owned shared session. Re-observe after recovery and never replay a timed-out or possibly dispatched input. Old tabs, login state, refs, and captures are not restored. The [Camoufox skill](../../camoufox/SKILL.md#closed-targets-and-recovery) includes the MCP equivalents.

`camoufox_target_closed` is a closure race with unconfirmed scope, not proof that the browser disconnected. Inspect `session info` and `tab list` before choosing recovery. Lifecycle failures carry reconciled diagnostics in JSON `data`; poisoning and the no-replay rule take precedence over tab/session recovery.

### Installed runtime

The default runtime is the OS local data directory under `agent-browser/camoufox-v1`; `AGENT_BROWSER_CAMOUFOX_RUNTIME` must be absolute. `AGENT_BROWSER_PYTHON` chooses the install interpreter. Private HOME/cache variables are established before importing Camoufox. The worker uses the recorded executable and pinned Python packages, with default addons disabled to avoid startup downloads. The Camoufox source fork remains separately upstream-tracked; the initial runtime uses an official binary, not a newly built Firefox fork.

On macOS, bundle assets are read from `Contents/Resources` independently of the recorded `Contents/MacOS` executable. Unexpected worker import failures include the missing module name in CLI and MCP errors; report that name rather than blindly reinstalling or retrying input.

`AGENT_BROWSER_MOTION` selects `human-fast` (native humanize 0.25), `fast`, or `precision` at daemon/worker launch. `AGENT_BROWSER_ACTION_DEADLINE_MS` defaults to 22000 and is clamped to 1000–25000. The Rust command hard deadline is 28 seconds including implicit launch and observation. A poisoned or ambiguous command is not retried automatically. Close and inspect before continuing; use a new daemon session to pick up changed environment variables.

Primary references: [Camoufox Python usage](https://camoufox.com/python/usage/), [native cursor humanization](https://camoufox.com/fingerprint/cursor-movement/), [experimental remote-server warning](https://camoufox.com/python/remote-server/), and [Playwright Python APIs](https://playwright.dev/python/docs/api/class-page). Installation records browser metadata but is not a complete transitive dependency lock. Bounded local macOS acceptance passed for startup/ref input, hold/drag, the example extension, screenshot coordinates, failure refusal, and cleanup. This does not establish Linux, high-DPI, popup/iframe, forced-termination, or external-site behavior; see `camoufox-backend/PROTOCOL.md` for the exact coverage.
