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

Before accepting a release, authorize and run appropriate native compilation and isolated browser acceptance. Critical acceptance cases include: engine separation; unsupported configuration failure before launch; snapshot/ref routing; stale screenshot rejection; successful and failed input release; worker-deadline classification without replay (a session reset is required only when input state is ambiguous); popup/closed-tab behavior; external gesture registration; MCP CLI/schema parity; and install/cache isolation. A fake-input Python smoke alone does not establish any of those native/browser integration guarantees. Do not multiply a user's limited verification budget implicitly.

## Updating Camoufox or Playwright

Fetch upstream into the separate Camoufox fork and review the relevant Python API, fingerprint/input patches, release channel, and browser/Playwright compatibility. Use official documentation and pinned-version source before changing assumptions. Upgrade the runtime pins and bootstrap metadata deliberately; do not treat the source tree's browser version string as proof of a released binary.

Keep the bundled skill, protocol reference, README, CLI help, MCP descriptions, and docs site aligned. The skill is served from `skill-data/camoufox/SKILL.md`, with a core reference under `skill-data/core/references/camoufox.md`. Do not put feature content into the thin `skills/agent-browser/SKILL.md` discovery stub.

## Distribution and activation

This fork's postinstall looks only at `BraitoRX/agent-browser` releases. No upstream binary fallback is used. Existing package version 0.37.1 is the integration base, not a new fork release. Until a fork release is intentionally prepared, use `cli/target/release/agent-browser` from a build of this repository and retain the surrounding skill-data layout. Upstream npm/Cargo/Homebrew installation commands still install upstream software. With the Camoufox engine selected, `upgrade` is refused instead of replacing the backend with upstream packages.

Publishing packages, creating release tags, pushing implementation commits, installing runtimes, and changing a client's MCP configuration are separate authorized operations. For initial activation, point a new MCP entry at the absolute built fork binary with `--engine camoufox --session camoufox-task mcp --tools core,gestures`; leave the previous browser entry untouched until the new path has been accepted.

The Release workflow in `.github/workflows/release.yml` is manual-only through `workflow_dispatch`. Pushes and pull requests to `main` still run normal CI, but do not publish a release. Before explicitly dispatching Release from `main`, review the inherited npm package names, package ownership, publishing credentials, target repository, and version. A source integration or branch transition is not authorization to publish.

## Running the backend in a container

The Rust daemon spawns the Python worker as a child with piped stdio, so a container must run the whole stack (CLI, MCP, daemon, worker, browser), not just the worker. On macOS this means a Linux container, which bypasses the host's Camoufox runtime entirely.

The Camoufox backend embeds its Python sources at compile time, so the Linux binary build must also mount `camoufox-backend/`; the Compose build services already do. Build the Linux CLI binary first (none is committed), then the runtime image from the workspace root so the image can copy both `agent-browser/` and `camoufox/additions/`:

```bash
cd agent-browser && docker compose -f docker/docker-compose.yml run --rm build-linux
cd .. && docker build --platform linux/arm64 \
  --build-arg BROWSER_BINARY=agent-browser-linux-arm64 \
  -f agent-browser/docker/Dockerfile.runtime -t agent-browser-camoufox:runtime .
```

On an arm64 host use the arm64 binary; on x86_64 use `--platform linux/amd64` with `agent-browser-linux-x64`. The browser asset follows the container's Python platform, so the binary and the image platform must agree.

The image installs the browser and bakes in the input-dispatch guards at build time, so no post-install guard step is needed. It is headless by default, needs no published port, and MCP clients attach over stdio. The install lives in the image, not a mounted volume: mounting an existing volume over the runtime would shadow the image's installed browser and guards, so rebuild the image to update them instead. `/data` is reserved for scratch and screenshots, and the socket directory is private to the container.

Point an MCP entry at it, leaving existing browser configuration unchanged:

```json
{
  "mcpServers": {
    "agent-browser-camoufox-container": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "--init", "--platform", "linux/arm64", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--shm-size=512m", "agent-browser-camoufox:runtime", "--engine", "camoufox", "--session", "camoufox-task", "mcp", "--tools", "core,gestures"]
    }
  }
}
```

`agent-browser/docker/docker-compose.runtime.yml` provides the same image with an optional scratch volume. Headed runs additionally need an `Xvfb` process and `DISPLAY` in the container; the backend never starts `Xvfb` itself and rejects `noXvfb=true`.

## Local OpenCode refresh and reset

Use this procedure for routine maintenance of the configured macOS setup. Do not rediscover the configuration, enumerate every process, or probe alternate API syntaxes on every reset. Recheck only a value contradicted by current evidence or a command that fails. Build, reset, and verification still require task authorization; this runbook is not blanket permission.

### Recorded configuration

Configuration and documentation references recorded on 2026-09-14. This section is a procedure; the persistence acceptance scope is recorded below:

- Repository: `/Users/braito/Documents/Code/Projects/agent-browser-camoufox/agent-browser`.
- MCP entry: `browser`, in `~/.config/opencode/opencode.jsonc` under `mcp.servers`. (Renamed from `camofox_browser`; verified 2026-09-17 via `GET /api/mcp`.)
- Configured executable: the repository's `cli/target/release/agent-browser`, not an upstream package or a PATH-resolved executable.
- Arguments: `--engine camoufox --session opencode-camoufox mcp --tools core,gestures,network,state,debug,tabs`.
- OpenCode location for this setup: `/Users/braito/Documents/Code/Projects`. This is the conversation location, not the nested repository and not the browser session name. For a conversation at another location, use its actual OpenCode location instead.
- Browser configuration: `/Users/braito/.config/opencode/camoufox/agent-browser.json`, selected by `AGENT_BROWSER_CONFIG`. It sets `headed: true`, `profile: "/Users/braito/.config/opencode/camoufox/profiles/personal"`, and `idleTimeout: "0"`. The MCP working directory is the repository above. Keep the existing entry and its environment unchanged. `AGENT_BROWSER_HEADED` is `true`; `AGENT_BROWSER_MOTION` is `human-fast`. Reuse the configured persistent profile and session; do not create competing sessions or remove the profile during maintenance.
- Managed browser runtime: `/Users/braito/Library/Application Support/agent-browser/camoufox-v1`. Do not edit extracted workers, the managed venv, browser assets, cookies, or profiles as an activation step.

The OpenCode MCP process, agent-browser daemon, and Python/browser worker are separate lifetimes. Reconnecting MCP refreshes its instructions and tool catalog but does not reset an existing browser daemon. Closing the named browser session shuts down that daemon but does not reload the MCP server. Backend Python is embedded in the Rust binary, so Rust or bundled Python changes require a rebuild and a fresh daemon. A reconnect alone cannot activate unbuilt source. Skill-only files are read from the surrounding `skill-data` tree; retrieve the skill again rather than reinstalling the browser.

### Authorized activation sequence

1. Preserve existing work. Do not stash, reset Git, reinstall the runtime, publish, or change global configuration. If the shared `opencode-camoufox` session is live and another task or manual login owns it, coordinate before closing it. A stale daemon PID or this conversation's own active session is not evidence of another browser task.
2. For changed Rust or embedded backend source, build the exact configured executable. Skip the build for a reset of an already-current binary or a skill-only edit. Stop on a build failure; do not reconnect and claim the new code is active.

```bash
REPO='/Users/braito/Documents/Code/Projects/agent-browser-camoufox/agent-browser'
cd "$REPO"
cargo build --release --manifest-path cli/Cargo.toml
```

3. If a browser reset is authorized, call the configured MCP `agent_browser_close` for `session: "browser"`, with no `all` flag. OpenCode exposes it as `browser_agent_browser_close`. If MCP is unavailable, the equivalent scoped CLI command is:

```bash
REPO='/Users/braito/Documents/Code/Projects/agent-browser-camoufox/agent-browser'
"$REPO/cli/target/release/agent-browser" --engine camoufox --session browser close --json
```

4. Reconnect only the MCP entry in the correct OpenCode location. Use OpenCode's authenticated `api` CLI, not an unauthenticated HTTP request or a whole-service restart. The working routes are the experimental ones (the legacy `/api/mcp/{server}/disconnect` shape returns 404; verified 2026-09-17). These endpoints take no request body:

```bash
LOCATION='/Users/braito/Documents/Code/Projects'
opencode api POST "/api/experimental/mcp/browser/disconnect?location[directory]=$LOCATION"
opencode api POST "/api/experimental/mcp/browser/connect?location[directory]=$LOCATION"
opencode api GET "/api/mcp?location[directory]=$LOCATION"
```

Run the commands sequentially and inspect each result; stop on a failed disconnect rather than blindly continuing. Confirm `camofox_browser` reports `connected` in the scoped GET response. If connect fails, report that MCP remains disconnected and the actual error; do not loop or escalate to restarting OpenCode.

5. Retrieve `agent_browser_skills_get` with `names: ["camoufox"]` when the workflow changed. Confirm the closed browser daemon is inactive with `agent_browser_session_info` when the task includes a reset. Do not open a website just to verify a maintenance operation. Unless reopening was requested, leave the browser closed; the next authorized `open` starts the new daemon. A stale client catalog may need a fresh turn, not a broader service restart.

### Location scoping and safety boundaries

The V2 API resolves the server's default location when no location is supplied. Do not assume changing the shell directory selects the conversation's MCP connection. The API contract declares `location` as a `deepObject` query parameter, so keep `location[directory]` inside the quoted request path as above rather than relying on a separate CLI `--param` option. The connect/disconnect routes have no request body and return HTTP 204 on success. These shapes come from the V2 OpenAPI contract; the reset commands were not executed as part of this documentation update.

Never use `opencode service restart`, `close --all`, broad `pkill`/`killall`, profile deletion, or runtime reinstallation for this routine reset. Unexpected orphan processes require a separate ownership diagnosis and authorization, not an expanded kill command. Closing discards current tabs, transient state, refs, captures, and unfinished HAR state; a configured persistent profile retains its on-disk login storage. Wait for `session info` to report `active: false` before a scripted reopen because a close response can precede daemon exit. Reuse the same profile and never replay ambiguous input after resetting.

Report build exit status, whether MCP was reconnected, whether the named daemon was closed, and whether anything was reopened. Distinguish source review, an authorized isolated smoke check, and real-browser acceptance. Do not add permanent tests, run suites, or browse a live site merely because a reset was requested.

Authoritative OpenCode V2 references: [API overview and authenticated CLI](https://opencode.ai/v2/docs/api), [OpenAPI contract](https://opencode.ai/v2/openapi.json), and [MCP configuration](https://opencode.ai/v2/docs/mcp-servers).

## Verification status

Behavior-changing work records its acceptance in this order: `CRITICAL_CHANGELOG.md` for anything that can break the locally configured browser, and the durable guarantees listed here. Session-by-session acceptance transcripts were removed as stale; the surviving guarantees are:

- Lifecycle: 26 fake-based lifecycle tests plus three focused Rust `camoufox_lifecycle` tests cover callbacks, missed events, tab/session diagnostics, reference invalidation, explicit recovery, target-closed classification, and preservation of the ambiguous-input reset requirement. Temporary headed acceptance additionally confirmed that page closure leaves a live browser without an active tab, context/browser closure refuses implicit recovery, and explicit close/open restores snapshots.
- Timeout policy: 41 fake-based tests plus Rust `camoufox_timeout` tests separate cleanly released operation timeouts (`data.timeoutKind: "operation"`, session preserved) from worker deadlines (`"deadline"`, a reset required only when input was attempted during the action, journaled input could not be released, or a launch/close was cancelled; a cancelled read-only action keeps the session usable). Failed releases, non-timeout ambiguous input, and transport failures keep the no-replay safety boundary.
- DOM/MCP: three Rust `camoufox_dom_contract` tests cover engine-specific profile filtering. Frame detachment clears refs/captures without silently replacing detached selection; tab/session closure clears retained frame state. Main-frame aria refs whose element was re-rendered are re-resolved by role/name during actions; ambiguous or empty re-resolution returns `camoufox_stale_ref` rather than an internal locator error. Frame-prefixed refs have no such fallback.
- Input dispatch: the installed browser carries the guards described in `CRITICAL_CHANGELOG.md`. Re-apply them after any runtime reinstall with `scripts/camoufox-guard-patch.py` (idempotent).
- Persistent profiles: focused profile/MCP tests and temporary localhost acceptance on the configured macOS binary verified private profile permissions, exclusive ownership, refusal of live profile changes, and persistence of an HttpOnly cookie, localStorage, IndexedDB, and the generated device identity across close/open. The restart check waits for daemon shutdown before reopening. The first check exposed missing session-info profile metadata, which was corrected; an immediate reopen also exposed the existing asynchronous daemon-close boundary. These checks used disposable test data, not real accounts. The local MCP was reconnected and a fresh headed `opencode-camoufox` daemon reported the configured personal profile with `persistentProfile: true`. Website login expiry, CAPTCHA behavior, browser upgrades, and forced-crash durability remain unverified.

These guarantees do not certify live nested/cross-origin frame interaction, universal DOM access, anti-bot behavior, non-default motion, high-DPI or cross-platform correctness, or a generally reliable agent strategy. No release was published or deployed beyond the configured local MCP binary.
