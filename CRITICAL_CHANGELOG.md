# Critical Changelog

Changes recorded here can break the locally configured Camoufox browser at runtime and are not captured by any published release. If the browser starts hanging clicks, hits the 22s worker deadline, and reports `camoufox_poisoned`, read this file first.

## 2026-09-14 - Local guard patch to the installed Camoufox browser (managed runtime)

- What: inside the installed browser's `omni.ja`, replaced three Juggler JS files and added a fourth, adding the input-dispatch ack-deadline guards.
- Why: the official asset (beta.30) lacks these guards. A humanized synthesized mouse event that never receives a renderer ack wedges the process-global input chain, so `click` never returns, the 22s worker deadline fires, and the session is poisoned. Root cause and reproduction are in `docs/fork-maintenance.md` and `camoufox-backend/PROTOCOL.md`.
- Artifact: `<runtime>/home/.cache/camoufox/browsers/official/152.0.4-beta.30-3b43e766/Camoufox.app/Contents/Resources/omni.ja`
  - before sha256: `bed61930f353ef21011487c4c0fc84e64103b00617b5f8dd0538fb261d0732a5`
  - after sha256: `7f8b549588fbc846133e86909780a31d1ae82d656c5f6e5f7c09327b60e4ed39`
  - backup: `omni.ja.guards-backup` in the same directory (sha256 `bed61930...a5`)
  - zip layout preserved (all entries stored); entry count 2436 to 2437.
- Files swapped in: `chrome/juggler/content/Helper.js`, `chrome/juggler/content/protocol/PageHandler.js`, `chrome/juggler/content/TargetRegistry.js`; added `chrome/juggler/content/input/MouseDispatch.js`. Source: the Camoufox fork `additions/juggler/`.
- Deliberately not changed: `content/FrameTree.js` (differs only by fingerprint sealing, unrelated to input dispatch).
- Risk: any runtime reinstall or `camoufox fetch`/upgrade replaces `omni.ja` and silently drops the guards, reintroducing the poisoning. The durable fix is pinning upstream `v152.0.4-beta.31` once its release assets are published; that tag exists but has no published release yet.
- Rollback: copy `omni.ja.guards-backup` over `omni.ja`.
- Verification (2026-09-14): pre-patch, a cold-page `page.mouse.move(31, 0)` with `humanize=0.25` hung forever; post-patch it returns via the guard and the browser stays responsive. 12/12 CLI clicks on a top-edge button succeeded in 0.19 to 0.41s with no `camoufox_timeout` or `camoufox_poisoned`.
