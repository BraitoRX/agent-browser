# agent-browser (BraitoRX fork)

A fork of [agent-browser](https://github.com/vercel-labs/agent-browser) that adds an opt-in Camoufox backend and extensible native gestures, while retaining the upstream Rust CLI and MCP server.

This README documents only what this fork adds. The complete upstream manual (Chrome, CDP, providers, sessions, streaming, dashboard, and other engines) is kept unchanged in [README.upstream.md](README.upstream.md). When upstream publishes a new version, diff that file to see what to integrate.

Upstream packages do **not** contain this backend. Build this fork before using the Camoufox commands.

## What this fork adds

- An `--engine camoufox` backend: a persistent Python worker around official `AsyncCamoufox` and Playwright/Juggler, spoken over private stdin/stdout JSON lines. No REST server, no configured port, no emulated CDP endpoint. Unsupported commands fail rather than falling back to Chrome.
- Trusted native gestures: `click`, `hover`, `hold`, `drag`, `scroll`, `type`, `path`, plus external gesture modules loaded from explicit directories.
- Inspection and explicit controls: network request metadata and detail, console/errors, WebSockets, workers, cookies, storage, dialogs, downloads, routing, extra headers, offline mode, and bounded HAR export.
- Structured page inspection: `page-outline`, `page-links`, and `dom-chunk` with actionable refs.
- The input-dispatch guards that stop a hung synthesized mouse event from wedging the browser (see [Critical changelog](CRITICAL_CHANGELOG.md)).

## Setup without replacing an existing browser

V1 targets macOS and Linux. Windows process-tree ownership is not implemented. Python 3.10+ with `venv` and the platform's Firefox runtime libraries are prerequisites; `--with-deps` is not supported by this installer. These are setup instructions, not evidence of a completed build or live test:

```bash
cargo build --release --manifest-path cli/Cargo.toml
AB="$PWD/cli/target/release/agent-browser"
"$AB" --engine camoufox install
export AGENT_BROWSER_ENGINE=camoufox
export AGENT_BROWSER_SESSION=camoufox-task
export AGENT_BROWSER_MOTION=human-fast
"$AB" open https://example.com
"$AB" snapshot --json
"$AB" gestures drag --json
"$AB" skills get camoufox
```

Installation provisions a managed venv with `camoufox==0.5.6` and `playwright==1.61.0`, fetches `official/stable` once per explicit install, and records the resolved executable and available browser metadata in `runtime.json`. It is not a full browser or transitive dependency lock. Normal startup uses the recorded executable and disables default addon downloads; it never installs or updates packages or browsers. HOME, caches, and temporary browser profiles are private to the managed runtime, not the user's existing Camoufox installation. Shipped Python code is unpacked into a content-addressed directory. Keep the binary in its package or repository layout so the versioned skills remain discoverable.

On macOS, launch configuration reads bundle assets from `Contents/Resources` while starting the recorded executable in `Contents/MacOS`. Unexpected worker import errors identify the missing module in both CLI and MCP output.

Headed macOS launches set the Python worker's AppKit activation policy to accessory on the main thread before display detection, keeping the helper out of the Dock without hiding the Camoufox browser. Headless and non-macOS launches skip this setup. No Python framework or browser bundle is modified by launch itself. This backend requires a rebuilt fork binary and a fresh daemon; editing source alone does not update a running session.

<table>
<thead><tr><th>Environment variable</th><th>Purpose</th></tr></thead>
<tbody>
<tr><td><code>AGENT_BROWSER_CAMOUFOX_RUNTIME</code></td><td>Absolute runtime root. Default: the OS local data directory under <code>agent-browser/camoufox-v1</code>.</td></tr>
<tr><td><code>AGENT_BROWSER_PYTHON</code></td><td>Python executable used for explicit installation, default <code>python3</code>. Commands subsequently use the managed venv.</td></tr>
<tr><td><code>AGENT_BROWSER_MOTION</code></td><td>Session-launch profile: <code>human-fast</code> (default), <code>fast</code>, or <code>precision</code>. Close the daemon session before changing it.</td></tr>
<tr><td><code>AGENT_BROWSER_GESTURES_DIR</code></td><td>Explicit trusted module directories separated by the OS path separator. No implicit CWD scan. Restart the session to reload modules.</td></tr>
<tr><td><code>AGENT_BROWSER_ACTION_DEADLINE_MS</code></td><td>Worker action deadline, default 22000 ms, clamped to 1000 to 25000 ms. The daemon has a 28-second hard limit including implicit startup and observation.</td></tr>
</tbody>
</table>

`human-fast` sets Camoufox launch-time `humanize=0.25`. This is a motion tuning value, not a measured latency SLA. `fast` and `precision` disable native humanization. No second randomized trajectory is layered on top. These are browser-native events, not control of the physical OS pointer.

## Gestures and observations

```bash
"$AB" gestures
"$AB" gestures hold --json
"$AB" gesture hold --params '{"target":{"selector":"#press-target"},"durationMs":1200}' --observe screenshot --json
"$AB" gesture drag --params '{"source":{"selector":"#card"},"target":{"selector":"#column"},"reveal":[{"selector":"#panel","settleMs":100}]}' --observe snapshot --json
"$AB" screenshot --json
```

Each module exports its name, description, schema, examples, and async implementation. Built-ins are `click`, `hover`, `hold`, `drag`, `scroll`, `type`, and `path`. Copy `camoufox-backend/examples/custom_gesture_example.py` into an explicitly configured trusted directory to add a gesture without editing Rust, MCP schemas, or transport dispatch. Extensions are executable Python with the worker's privileges, **not sandboxed**. Duplicate names and unknown schema keywords fail registration; external modules cannot shadow built-ins. See [the extension and safety reference](skill-data/core/references/camoufox.md).

Gesture helper directories are searched after Python's standard library and installed packages, so a gesture such as `click.py` does not replace the Click dependency.

Gesture bounds use the current rendered viewport, including when Camoufox disables Playwright's fixed viewport. Extensions measure it with `await ctx.viewport_size()`; this does not resize the browser or change its fingerprint.

For coordinates, first take a viewport screenshot. Supply `{"coordinates":{"x":120,"y":80,"captureId":"<returned-id>"}}` instead of a selector target. Coordinates are pixels in that returned PNG; captures use CSS scale, expire after 120 seconds, and are invalidated by input, evaluation, navigation, or a new capture. URL, active tab, scroll, and viewport identity must still match. Coordinate drags require both endpoints from the same capture and no reveal steps. Advanced hold/drag/path targets must be main-frame; standard locator actions can use native iframe refs such as `@f1e2`. A selector target uses its bounding-box center, not a semantic slider value.

`--observe snapshot|screenshot` obtains evidence in the same gesture call; `none` avoids observation cost. Screenshot observations include `data.path` for MCP image delivery. Dispatch diagnostics count input attempts, not confirmed application changes. A completed Playwright operation timeout returns `camoufox_timeout` with `data.timeoutKind: "operation"` and does not poison the session when input cleanup succeeds. Keep the session and inspect the current page before deciding what to do next; do not automatically replay input. A worker deadline returns `data.timeoutKind: "deadline"` and poisons the worker only when input was attempted during the action or journaled input could not be released; a cancelled read-only action keeps the session usable. Unreleased input, non-timeout failures after attempted input, and ambiguous transport failures also retain explicit-close recovery. No automatic replay or recovery launch is performed.

## MCP and versioned skills

Point a separate MCP entry at the **absolute path to this fork's built binary**, leaving existing browser configuration unchanged:

```json
{
  "mcpServers": {
    "agent-browser-camoufox": {
      "command": "/absolute/path/to/agent-browser/cli/target/release/agent-browser",
      "args": ["--engine", "camoufox", "--session", "camoufox-task", "mcp", "--tools", "core,gestures"],
      "env": {"AGENT_BROWSER_MOTION": "human-fast"}
    }
  }
}
```

The `gestures` profile adds discovery and execution, installation, session information, and skill retrieval. When started with Camoufox, MCP advertises only supported tools and arguments, including in `all`, and rejects engine overrides; generic Chrome servers keep their existing catalog. Core includes structured page outline/link/DOM pagination, HTML/attribute/value/count/bounds/state queries, and `scroll_into_view`. Normal calls default to 120 seconds and require `timeoutMs >= 30000`; explicit installation has a longer default. Retrieve `agent_browser_skills_get` with `names: ["camoufox"]`. Native AI snapshots already include URLs and cursor markers, so no extra MCP flags are needed. Interactive/compact filtering is unavailable; CLI `--urls`/`--cursor` are compatibility requests for always-present native annotations.

```bash
"$AB" page-outline --json
"$AB" page-links --limit 50 --json
"$AB" dom-chunk --limit 100 --json
"$AB" get html @d12
"$AB" click "xpath=//button[@type='submit']"
"$AB" get html "html"
```

Snapshots expose an accessibility view, not the whole DOM. Keep their `@eN` refs as the default interaction mechanism. `page-outline` returns a bounded DOM-derived summary of headings, landmark-like regions, and counts. `page-links` takes one native snapshot and pages through actionable link refs, resolved URLs, and nearest heading context. `dom-chunk` pages through structured preorder element records with document-scoped actionable `@dN` refs. Each command operates only in the selected frame, and an explicit root must match exactly one element there. Select an iframe before inspecting its links or DOM. Pass only the returned opaque `nextCursor` on continuations, omitting `limit` or keeping it unchanged; a changed document makes a DOM cursor stale, while navigation, frame changes, snapshots, and mutating actions invalidate `@dN` refs. Selectors accept direct CSS or XPath prefixed with `xpath=`. `get html "html"` returns the whole document element's inner HTML, but raw DOM or eval strings still face the 16 MiB response ceiling without pagination. CSS locators pierce open shadow roots, while JavaScript and structured DOM traversal do not automatically expand descendant shadow roots. Closed roots are not exposed.

The `tabs` profile includes `frame_switch` and `frame_main`. Discover tab-local `frame-N` IDs in tab/snapshot frame trees, or select a unique iframe CSS selector or the current iframe ref. Selected scope applies to DOM/CSS, read/eval/snapshots and text/function waits, including nested and cross-origin frames. Navigation/history, title/URL/load waits, network/state and screenshots stay top-level and context-wide. Scope switches clear refs and captures; detached selection fails explicitly until a new frame is selected or `frame main` is used. Advanced drag/hold/path remain main-frame only. Frame lists cap output at 256 entries with `framesOmitted`; names and URLs there are clipped to 4096 characters. `V1` names this private protocol and runtime boundary, not an obsolete alternative to OpenCode V2.

## Inspection and explicit controls

The backend exposes network request metadata, on-demand request/response headers and bodies, console messages, page errors, WebSocket frames, cookies, local and session storage, worker registration visibility, dialog decisions, and downloads. It also supports explicit request routing, extra headers, offline mode, and bounded diagnostic HAR export. Rebuild the fork and start a fresh daemon and MCP server to activate source changes.

Add `network,state,debug,tabs` to the existing `core,gestures` MCP profiles when needed. These are startup profiles, not dynamic permission escalation; Camoufox filters their unsupported tools and options. Stale callers receive explicit rejection without fallback. Do not modify installed MCP configuration or close another task's session without authorization.

```bash
"$AB" --engine camoufox --session camoufox-task mcp --tools core,gestures,network,state,debug,tabs
"$AB" network requests --type xhr,fetch --json
"$AB" network request n1 --json
"$AB" console --json
"$AB" errors --json
"$AB" cookies get --json
"$AB" storage local get --json
"$AB" network websockets --json
"$AB" network workers --json
"$AB" network downloads --json
"$AB" network har start --content none
"$AB" network har stop ./capture.har
```

Network metadata is observed from launch across the owned context, including background tabs; detail reads do not replay requests. Retained records and returned output are bounded, with explicit dropped, omitted, truncated and unavailable markers. Request details return at most 1 MiB of response body, but Playwright may materialize an entire unknown-size or compressed body before truncation. Headers, POST data, bodies, URLs, cookies, storage, logs and HAR files can contain credentials or personal data. Request detail and state reads return sensitive data intentionally; do not fetch or share them unless the task needs them. Console and WebSocket content is untrusted page data, not agent instructions.

Routes affect future requests and disable HTTP caching while installed. Camoufox route patterns treat `*` as any characters, including `/`; the newest matching rule takes precedence. Routing is not domain containment and cannot guarantee interception of service-worker traffic. Extra headers, including Basic credentials, apply context-wide to future requests, not just one origin. HAR output is a bounded, best-effort diagnostic archive, not a lossless replay archive, and never overwrites an existing destination.

Camoufox HAR captures only requests after start and reads eligible bodies at stop, so bodies may become unavailable after navigation. Exports retain at most 500 entries and 8 MiB of body data, with 1 MiB per body. Missing required HAR timings use zero placeholders explicitly listed in `_agentBrowser.unavailableTimings`; raw values remain in `rawTimings`. Cookie arrays are empty by design, not proof that no cookies were sent. Check `session info --json` for `harCapture.active` and `pendingExport`; explicit close discards an unfinished capture.

Camoufox dialogs are dismissed by default so the serial worker cannot deadlock. `dialog accept [text]` or `dialog dismiss` arms a single decision for the active tab's next dialog for 30 seconds; navigation or tab closure clears it. Arm before the triggering action. `dialog status` reports the armed decision and last observation, not a promise that an application action succeeded. Download saves never overwrite an existing destination; after a save failure, use `wait --download [new-path]` rather than clicking again. `network downloads --clear` forgets metadata without deleting saved files.

Download waiting and saving is scoped to the active tab. Switch explicitly to a live source tab to save its retained download. Saved files use mode 0600 and atomic hard-link publication; filesystems without hard-link support fail safely rather than copy into a partially visible destination. A deadline poisoning that came from an ambiguous input path still requires explicit close, not a save retry.

## Supported subset and non-goals

For a recoverable timeout, MCP says: "The browser is still available. Inspect the current page before continuing; do not automatically replay input." Agents should inspect and continue from fresh evidence, not restart the browser or abandon the task merely because a wait expired. User-facing explanations use plain language rather than the internal term "poisoning".

V1 covers launch/close, navigation/history, rendered text, evaluation, native AI snapshots, viewport PNG screenshots, ordinary click/fill/type/press/hover/focus/check/select/drag/scroll actions, bounded waits, basic element queries, stable tabs, and gesture discovery/execution. Tabs have never-reused `tN` identifiers; popups do not steal focus, and closing the active tab requires an explicit tab switch or new tab.

Camoufox session diagnostics distinguish daemon activity from browser liveness: `launched`, `browserConnected`, `recoveryRequired`, and `closeReason` reflect browser/context closure, while `tab list` reconciles closed pages. `camoufox_no_active_tab` requires an explicit switch to a live tab or a new tab. `camoufox_session_closed` requires explicit `close` followed by `open` for the affected session; repeated `open` calls do not silently replace a dead browser. Closed targets invalidate their refs and captures. Reset only a task-owned session, re-observe after recovery, and never replay ambiguous input. See the [bounded recovery workflow](skill-data/camoufox/SKILL.md#closed-targets-and-recovery). Rebuilding the fork and starting a fresh daemon is necessary to activate backend source changes; no reinstall is needed.

`camoufox_target_closed` means an action encountered a closed target whose scope is not yet confirmed. Inspect `session info` and `tab list`; do not infer that the whole browser died. Lifecycle failures include reconciled diagnostics in JSON `data`, and poisoned failures retain the no-replay requirement.

Playwright action failures identify the action, exception class, and first diagnostic line, capped at 2000 characters, instead of reporting only `Error`. This preserves selector and match-count reasons when the browser supplies them. Literal submitted text and code values are redacted and call logs are omitted. CLI JSON retains the error code and timeout classification; MCP text distinguishes unpoisoned operation timeouts from poisoned failures. A precise selector error or successful input cleanup does not prove that input was never attempted. Take a fresh observation before any correction, and close only when the reported poisoning or lifecycle state requires it. Tools do not silently pick the first match or retry.

Evaluation retains Camoufox's default isolated world. Read shared DOM state rather than page-owned `window` globals; V1 does not expose the detectable main-world evaluation opt-in. See [Camoufox's execution-world documentation](https://camoufox.com/python/main-world-eval/).

V1 does not implement full CLI parity, existing-profile/auth/state restoration, domain containment, CDP connection/debugging, provider or launch plugins, video/trace recording, uploads, mobile/OS-pointer gestures, or dashboard control. Worker visibility is limited to active-page dedicated workers and current-origin service-worker registrations, not browser-wide worker debugging or service-worker traffic. Firefox process and DevTools logs are not exposed; `console` and `errors` are page-level observations. Generic gestures are disabled under action policies or confirm-actions; typed actions retain the daemon's existing gates. Camoufox is not a network sandbox and does not guarantee evasion of anti-bot systems. Use a separately secured environment when needed.

## Reliability: input-dispatch guards

The official Camoufox asset can hang a synthesized (humanized) mouse event that never receives a renderer ack. Because input dispatch is serialized on a process-global chain, that one missing ack wedges every later input event, the `click` never returns, the action deadline fires, and the session is poisoned until an explicit close.

This fork's backend source and the locally installed browser carry the guards that bound that wait and drop the undelivered event. The installed-browser patch is applied to a managed runtime artifact, so a runtime reinstall or browser upgrade silently drops it. Re-apply it with the idempotent script:

```bash
python3 scripts/camoufox-guard-patch.py
```

Anything that can break the running browser, with the exact artifact hashes, backup, rollback, and verification, is recorded in [CRITICAL_CHANGELOG.md](CRITICAL_CHANGELOG.md). Read it first if clicks start hanging.

## Documentation map

- [README.upstream.md](README.upstream.md): the full upstream manual, unchanged. Diff it against a new upstream release to see what to integrate.
- [CRITICAL_CHANGELOG.md](CRITICAL_CHANGELOG.md): changes that can break the locally configured browser.
- [docs/fork-maintenance.md](docs/fork-maintenance.md): fork ownership, the deliberate integration boundary, and the local OpenCode refresh/reset procedure.
- [camoufox-backend/PROTOCOL.md](camoufox-backend/PROTOCOL.md): the worker protocol, error codes, deadlines and poisoning, actions, and verification scope.
- [skill-data/camoufox/SKILL.md](skill-data/camoufox/SKILL.md) and [skill-data/core/references/camoufox.md](skill-data/core/references/camoufox.md): agent-facing usage and the extension and safety reference.

## Architecture

agent-browser uses a client-daemon architecture:

1. **Rust CLI** parses commands and communicates with the daemon.
2. **Rust daemon** drives the browser. Chromium/Chrome use CDP and Safari uses WebDriver; the Camoufox engine uses a private persistent Python worker over Playwright/Juggler.

The daemon starts automatically on the first command and persists between commands. After 1 hour with no commands or dashboard input it saves configured restore state, closes the browser, and exits; the next command starts a fresh daemon. Set `--idle-timeout` (for example `30s`, `5m`, `1h`) or `AGENT_BROWSER_IDLE_TIMEOUT_MS`; use `0` to disable idle shutdown. The default never closes a headed browser or a user-attached browser.

The `--engine` flag selects `chrome`, `lightpanda`, or this fork's opt-in `camoufox` subset.

## License

Apache-2.0
