# Maintaining the Camoufox fork

## Ownership and baseline

- Agent-browser fork: `https://github.com/BraitoRX/agent-browser`, upstream `https://github.com/vercel-labs/agent-browser`.
- Camoufox fork: `https://github.com/BraitoRX/camoufox`, upstream `https://github.com/daijro/camoufox`.
- Initial agent-browser branch: `feat/camoufox-gestures`, based on `8c15ff9f71ae60c7e99e66afe1e2d4b9bf414fe2` (0.37.1).
- Initial Camoufox source baseline: `041ceb0af13c36313a987695e2e46d7a7b366148`, with no source patches in this integration.

Keep `origin` for the personal fork and `upstream` for the original repository. Use the fork's `main` branch for accepted Camoufox work and short-lived feature or integration branches for new changes. Do not push directly to upstream, rewrite shared branches, automatically rebase releases, or reset the fork's `main` to `upstream/main`.

Retiring an old workspace or configuration requires explicit approval, a verified backup of uncommitted work, and preservation of browser profile data. Do not recreate the retired `camofox-browser` setup as an automatic fallback.

No fork release is created merely by changing or pushing source. A local macOS release build and bounded real-browser acceptance have completed; the scope and remaining gaps are recorded in `camoufox-backend/PROTOCOL.md`. This is not a published release or acceptance on other platforms/applications.

## Deliberate integration boundary

The current agent-browser architecture is native Rust, not the older Playwright daemon. Its provider plugins still require CDP. Camoufox speaks Playwright/Juggler, so it is a distinct backend rather than a provider plugin or a fabricated CDP endpoint. `cli/src/native/camoufox.rs` owns packaged worker extraction, installation, IPC, and deadlines; `actions.rs` retains policy authority and lifecycle routing. The worker and trusted gesture registry are under `camoufox-backend/`.

The initial runtime pins the public Python packages `camoufox==0.5.6` and `playwright==1.61.0` and installs an official stable browser asset. It does **not** build Firefox from the Camoufox source fork. A future browser patch needs its own reviewed build/distribution decision, executable provenance, and explicit runtime update. Source fork freshness and installed browser freshness are separate facts.

The bootstrap records available browser metadata and the resolved executable. It is not a complete dependency or asset lock. Re-running installation may resolve a newer official stable browser. There is no automatic update on normal worker launch.

## Updating agent-browser

Use a short-lived integration branch from a clean, committed fork checkpoint. The following is a manual future workflow, not an instruction to run it as part of a source-only change:

```bash
git fetch upstream --tags
git switch -c integrate-upstream-YYYY-MM-DD
git merge upstream/main
```

Inspect upstream changes to command parsing, daemon launch/close, policy/confirmation, session lifecycle, transport retries, MCP argument forwarding/image responses, packaging, and skill discovery. Resolve conflicts by preserving the explicit Camoufox boundary, not by bypassing policy or converting it back into a CDP provider. The generic gesture registry should remain independent of transport plumbing.

Before accepting a release, authorize and run appropriate native compilation and isolated browser acceptance. Critical acceptance cases include: engine separation; unsupported configuration failure before launch; snapshot/ref routing; stale screenshot rejection; successful and failed input release; total deadline poisoning without replay; popup/closed-tab behavior; external gesture registration; MCP CLI/schema parity; and install/cache isolation. A fake-input Python smoke alone does not establish any of those native/browser integration guarantees. Do not multiply a user's limited verification budget implicitly.

## Updating Camoufox or Playwright

Fetch upstream into the separate Camoufox fork and review the relevant Python API, fingerprint/input patches, release channel, and browser/Playwright compatibility. Use official documentation and pinned-version source before changing assumptions. Upgrade the runtime pins and bootstrap metadata deliberately; do not treat the source tree's browser version string as proof of a released binary.

Keep the bundled skill, protocol reference, README, CLI help, MCP descriptions, and docs site aligned. The skill is served from `skill-data/camoufox/SKILL.md`, with a core reference under `skill-data/core/references/camoufox.md`. Do not put feature content into the thin `skills/agent-browser/SKILL.md` discovery stub.

## Distribution and activation

This fork's postinstall looks only at `BraitoRX/agent-browser` releases. No upstream binary fallback is used. Existing package version 0.37.1 is the integration base, not a new fork release. Until a fork release is intentionally prepared, use `cli/target/release/agent-browser` from a build of this repository and retain the surrounding skill-data layout. Upstream npm/Cargo/Homebrew installation commands still install upstream software. With the Camoufox engine selected, `upgrade` is refused instead of replacing the backend with upstream packages.

Publishing packages, creating release tags, pushing implementation commits, installing runtimes, and changing a client's MCP configuration are separate authorized operations. For initial activation, point a new MCP entry at the absolute built fork binary with `--engine camoufox --session camoufox-task mcp --tools core,gestures`; leave the previous browser entry untouched until the new path has been accepted.

The Release workflow in `.github/workflows/release.yml` is manual-only through `workflow_dispatch`. Pushes and pull requests to `main` still run normal CI, but do not publish a release. Before explicitly dispatching Release from `main`, review the inherited npm package names, package ownership, publishing credentials, target repository, and version. A source integration or branch transition is not authorization to publish.

## Local lifecycle regression acceptance

The stale-target lifecycle update completed the following bounded acceptance on macOS arm64. This supplements the initial gesture acceptance recorded in `camoufox-backend/PROTOCOL.md`; it is not a full-suite result or certification for other platforms or websites.

- `python3 -B -m unittest discover -s test/camoufox -p 'test_*lifecycle.py' -v` exited 0: 26 permanent fake-based tests cover callbacks, missed events, tab/session diagnostics, reference invalidation, explicit recovery, target-closed error classification, and preservation of ambiguous-input poisoning.
- `cargo test --manifest-path cli/Cargo.toml camoufox_lifecycle -- --nocapture` exited 0: three focused tests cover CLI/MCP lifecycle data, error codes, and text-only no-replay warnings.
- `cargo build --release --manifest-path cli/Cargo.toml` exited 0. The configured local release binary was rebuilt, only `camofox_browser` MCP was reconnected, and the updated Camoufox skill was retrieved through that connection. No runtime reinstall or global configuration edit was needed.
- A temporary headed acceptance driver used one isolated named session and `about:blank` through a private MCP process. It confirmed that page closure leaves a live browser without an active tab, context/browser closure refuses implicit recovery, CLI/MCP diagnostics agree, and explicit close/open restores snapshots. It dispatched no input. The initial fixture setup was rejected for an invalid gesture name before browser launch; after correcting the name and waiting for asynchronous daemon shutdown during cleanup, the lifecycle run exited 0. Both test daemons were confirmed inactive.
- With separate approval, the stale, poisoned shared `opencode-camoufox` session was closed and confirmed inactive. No website was reopened and no unrelated session was closed. The next open starts a fresh daemon using the updated backend; existing tabs and login state are not restored.
