---
name: camoufox
description: Use this fork's explicit Camoufox backend for LLM-driven app interaction and native gestures. Covers isolated installation, MCP setup, native AI snapshots and refs, captured coordinates, gesture discovery/extensions, motion profiles, and safe handling of ambiguous input. Load after core when the engine is camoufox; do not assume Chrome CLI parity.
allowed-tools: Bash(agent-browser:*)
---

# Camoufox with agent-browser

Use the built `BraitoRX/agent-browser` fork, not the upstream npm/Homebrew/Cargo binary. A source-built binary passed bounded local macOS acceptance, but this is not a published browser distribution or proof of another application's behavior. Do not claim build, installation, or application success without execution evidence. Do not modify an existing browser or MCP setup merely to try this backend.

## Establish the session

Use a separate named session and keep the engine explicit for the whole task. Installation is a one-time authorized setup action, not an automatic recovery step:

```bash
agent-browser --engine camoufox install
export AGENT_BROWSER_ENGINE=camoufox
export AGENT_BROWSER_SESSION=camoufox-task
export AGENT_BROWSER_MOTION=human-fast
agent-browser open https://example.com
agent-browser snapshot --json
```

V1 targets macOS/Linux and requires Python 3.10+ with `venv` and Firefox OS libraries. The installer creates a private venv, HOME, and browser cache; it pins Camoufox 0.5.6 and Playwright 1.61.0 and records the downloaded `official/stable` executable. Startup does not fetch packages, browsers, or default addons. `AGENT_BROWSER_CAMOUFOX_RUNTIME` overrides the absolute runtime root; `AGENT_BROWSER_PYTHON` chooses the install-time Python. There is no additional HTTP service or port to configure.

For MCP, use a separate entry pointing to the absolute fork binary with arguments `--engine camoufox --session camoufox-task mcp --tools core,gestures`. Each tool also accepts an `engine` argument. Retrieve this skill with `agent_browser_skills_get` (`names: ["camoufox"]`). Do not turn on every MCP profile just to use gestures.

## Observe, act, verify

1. Take `snapshot` without Chrome's `-i`, `-c`, URL, or cursor filters. MCP defaults to `interactive: false` for Camoufox; explicit `true` is rejected. Read the native AI text and use only refs exposed by that tab's latest snapshot, such as `@e2` or `@f1e2`. CSS selectors are also supported. Re-snapshot after navigation or material page changes; never reconstruct a ref from a role/name guess.
2. Prefer typed `click`, `fill`, `type`, `press`, `hover`, `select`, and bounded waits for ordinary interaction. `fill` is the fast nonhuman text replacement; `type` emits key events. Use `gestures [name]` to discover an advanced gesture's current schema before calling it.
3. Execute a gesture with a JSON object and, if needed, `--observe snapshot` or `--observe screenshot`. `none` avoids observation work. Returned dispatch diagnostics are not proof of the desired application change. Check the relevant visible or application state separately, within the task's verification authorization.

Camoufox evaluation runs in its default isolated world. Read shared DOM values, text, or attributes instead of page-owned `window` globals. V1 does not expose the detectable main-world evaluation opt-in. Do not enable a less-stealthy execution mode just to make an assertion script work.

```bash
agent-browser gestures drag --json
agent-browser gesture drag --params '{"source":{"selector":"#card"},"target":{"selector":"#column"}}' --observe snapshot --json
agent-browser gesture hold --params '{"target":{"selector":"#press-target"},"durationMs":1200}' --observe screenshot --json
```

MCP equivalents are `agent_browser_gestures` with an optional `name`, and `agent_browser_gesture` with `name`, `params`, and optional `observe`. Screenshot observations keep `data.path` at the top level so MCP can attach the image in the same response.

## Coordinate and tab safety

Take a fresh viewport screenshot and use its `visualCapture.captureId` with pixel coordinates in that PNG:

```json
{"name":"click","params":{"target":{"coordinates":{"x":120,"y":80,"captureId":"<returned-id>"}}},"observe":"screenshot"}
```

Never guess coordinates or reuse an old capture after input, evaluation, navigation, scrolling, a new screenshot, or a viewport change. Captures expire after 120 seconds and are checked against URL, tab, scroll, and viewport identity. Coordinate drags require two coordinates from the same capture, without reveal steps. Selector drags may use up to four CSS-selector hover steps in `reveal` to expose hidden controls before resolving endpoints. Both endpoints must remain inside the viewport. A selector resolves to its box center, not a slider percentage or an inferred drop zone.

Tabs use stable `tN` IDs, not indexes. A popup is listed but does not become active. Closing the active tab leaves no active tab until an explicit `tab <id>` or `tab new`. Advanced hold/drag/path gestures support main-frame targets only; ordinary locator actions can route native iframe refs.

## Closed targets and recovery

When resuming a session or after a closed-target error, inspect `session info` and `tab list` before further interaction. A running daemon is not proof of a usable browser. Camoufox reports `launched`, `browserConnected`, `recoveryRequired`, and `closeReason`; closed tabs lose their refs and captures and cannot remain active.

- For `camoufox_no_active_tab` with a live browser, explicitly select an observed, task-relevant live tab or create a new one. Do not restart a healthy browser or silently adopt another tab.
- For `camoufox_target_closed`, the closure scope is unconfirmed. Inspect the returned diagnostics, `session info`, and `tab list`; do not assume the whole browser died. Take a fresh observation before deciding how to continue, without replaying possibly dispatched input.
- For `camoufox_session_closed`, the browser disconnected or its context closed. `open` and `tab new` do not repair that session. If the named session belongs to this task, explicitly `close` it, then `open` the intended URL once and take a new snapshot. Keep the same engine and session arguments. Do not use `close --all`, reinstall, change profiles, or disable safety settings as recovery steps. Ask before closing a shared session when ownership is unclear. This backend does not restore the old tabs, login state, refs, or captures.
- A timeout, `poisoned: true`, or possible input dispatch takes precedence over recovery convenience: never replay the failed click, typing, submission, or gesture. Inspect application state before deciding what to do next. For a failed read-only observation, resume only after fresh observation of the recovered session. If the same failure recurs after one recovery attempt, stop and report the diagnostics instead of looping.

MCP equivalents are `agent_browser_session_info`, `agent_browser_tab_list`, `agent_browser_tab_switch` / `agent_browser_tab_new`, and an explicit `agent_browser_close` followed by `agent_browser_open` for a dead task-owned session.

## Motion and failure handling

- `human-fast` is the default: Camoufox launch-time `humanize=0.25`. It is a tuning value, not a wall-time guarantee. Do not add a second randomized path on top.
- `fast` and `precision` disable native humanization. The bounded element-relative `path` gesture requires one of these profiles. Change `AGENT_BROWSER_MOTION` before starting a new daemon session, not during a gesture.
- The worker action deadline defaults to 22 seconds and is capped at 25 seconds; Rust enforces 28 seconds including implicit launch and observation. Hold duration is capped at 20 seconds and must leave time for release. Camoufox MCP calls require `timeoutMs >= 30000`.
- On a timeout, poisoned-session response, or ambiguous transport error, **do not replay input**. Close the session, inspect application state, and start a new session deliberately. Release attempts are cleanup, not evidence that the application operation did or did not happen.

## Extensions and exclusions

`AGENT_BROWSER_GESTURES_DIR` loads explicit trusted directories, never an implicit CWD. Modules own `NAME`, `DESCRIPTION`, strict `SCHEMA`, `EXAMPLES`, and `async run(ctx, params)`. Add one module and restart the session; no server or MCP plumbing change is needed. Extensions run Python with worker privileges and are not sandboxed. Review the original module before enabling it. Use the packaged example and [detailed reference](../core/references/camoufox.md).

Do not use this backend for domain containment, auth/profile/state restoration, CDP, provider/launch plugins, network interception, recording, uploads/downloads, mobile gestures, or physical OS-pointer control. Unsupported features must fail, not fall back to Chrome. Generic gestures are disabled whenever action policies or confirm-actions are active; use typed actions that retain the core gates. Camoufox is not a network sandbox or an anti-bot guarantee.

When finished, close the named session. A completed `close` also shuts down its daemon. To change daemon-time motion/extension settings, close first or use a fresh session name; do not assume changing the current shell updates an already-running daemon's environment.
