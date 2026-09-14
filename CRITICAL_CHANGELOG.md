# Critical Changelog

Changes recorded here can break the locally configured Camoufox browser at runtime and are not captured by any published release. If the browser starts hanging clicks, hits the 22s worker deadline, and reports `camoufox_session_reset_required`, read this file first.

## 2026-09-14 - Diagnostic rename: reset-required state and `inputAmbiguous` flag

- What: the failure code `camoufox_poisoned` is renamed to `camoufox_session_reset_required`, and the JSON flag carrying the old term is renamed to `inputAmbiguous` in CLI/worker responses and `session_info`. Tool descriptions, help text, docs, and test fixtures now say "the session needs a reset" or "input outcome may be ambiguous; do not replay". The safety mechanism is unchanged: the same ambiguous input states are detected, the same refusal gate applies, and explicit close remains the only recovery.
- Risk: scripts or MCP clients that matched the literal code `camoufox_poisoned` or the old JSON key stop matching; update them to the new names. The `camoufox_timeout` code and `data.timeoutKind` values are unchanged.
- Activation: source-only until the configured Rust binary is rebuilt and a fresh named daemon loads its embedded Python. Older daemons keep emitting the previous names until restarted.

## 2026-09-14 - Opt-in persistent Camoufox profiles

- What: `--profile`, `AGENT_BROWSER_PROFILE`, config `profile`, and MCP `profile` select a dedicated private profile. Browser close retains persistent cookies/site storage and the generated device identity. Unconfigured sessions remain ephemeral. A profile lock prevents concurrent workers, and switching profiles in a live session is refused.
- Risk: logged-in profiles grant the assistant access to those accounts. Profile files are private but not application-encrypted. Corrupt identity data or a changed recorded browser executable fails closed instead of replacing identity or removing login data. Session-only state, indefinite login validity, crash durability, and CAPTCHA elimination are not guaranteed.
- Activation: rebuild the configured Rust binary, close only the authorized named browser, and reconnect its MCP entry following `docs/fork-maintenance.md`. This section describes the source change, not proof of activation. No runtime reinstall or browser-engine patch is needed. Let the user sign in manually after activation; do not export existing credentials or copy their everyday browser profile.

## 2026-09-14 - Locator failure-path deadlines in the embedded backend

- What: selector clicks, bounding-box reads, and typed scroll preflight reject missing, ambiguous, or hidden targets before waiting for actionability. Gesture pre-scroll is skipped for targets already inside the viewport. Locator measurement, scrolling, hit testing, and click dispatch use explicit 2s operation limits, reduced further when the action budget is shorter.
- Risk: slow or continuously animated targets can now fail sooner. Shared gesture helpers also affect typing and other selector-based gestures. Completed Playwright timeouts retain their operation classification after conversion to backend diagnostics; cancelled input or failed input release still requires a session reset. No input is replayed automatically.
- Activation: source-only until the configured Rust binary is rebuilt and a fresh named daemon loads its embedded Python. Coordinate closure of the shared browser first and follow `docs/fork-maintenance.md`; reconnecting MCP alone cannot activate this change. No managed runtime assets or browser profiles were edited.

## 2026-09-14 - Local guard patch to the installed Camoufox browser (managed runtime)

- What: inside the installed browser's `omni.ja`, replaced three Juggler JS files and added a fourth, adding the input-dispatch ack-deadline guards.
- Why: the official asset (beta.30) lacks these guards. A humanized synthesized mouse event that never receives a renderer ack wedges the process-global input chain, so `click` never returns, the 22s worker deadline fires, and the session needs a reset. Root cause and reproduction are in `docs/fork-maintenance.md` and `camoufox-backend/PROTOCOL.md`.
- Artifact: `<runtime>/home/.cache/camoufox/browsers/official/152.0.4-beta.30-3b43e766/Camoufox.app/Contents/Resources/omni.ja`
  - before sha256: `bed61930f353ef21011487c4c0fc84e64103b00617b5f8dd0538fb261d0732a5`
  - after sha256: `7f8b549588fbc846133e86909780a31d1ae82d656c5f6e5f7c09327b60e4ed39`
  - backup: `omni.ja.guards-backup` in the same directory (sha256 `bed61930...a5`)
  - zip layout preserved (all entries stored); entry count 2436 to 2437.
- Files swapped in: `chrome/juggler/content/Helper.js`, `chrome/juggler/content/protocol/PageHandler.js`, `chrome/juggler/content/TargetRegistry.js`; added `chrome/juggler/content/input/MouseDispatch.js`. Source: the Camoufox fork `additions/juggler/`.
- Deliberately not changed: `content/FrameTree.js` (differs only by fingerprint sealing, unrelated to input dispatch).
- Risk: any runtime reinstall or `camoufox fetch`/upgrade replaces `omni.ja` and silently drops the guards, reintroducing the click-hang failure that leaves the session needing a reset. The durable fix is pinning upstream `v152.0.4-beta.31` once its release assets are published; that tag exists but has no published release yet.
- Rollback: copy `omni.ja.guards-backup` over `omni.ja`.
- Verification (2026-09-14): pre-patch, a cold-page `page.mouse.move(31, 0)` with `humanize=0.25` hung forever; post-patch it returns via the guard and the browser stays responsive. 12/12 CLI clicks on a top-edge button succeeded in 0.19 to 0.41s with no `camoufox_timeout` or `camoufox_session_reset_required`.
