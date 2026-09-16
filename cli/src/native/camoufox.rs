//! Private JSON-lines transport for the Camoufox Python worker. No CDP fallback.

use rust_embed::Embed;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};

#[derive(Embed)]
#[folder = "../camoufox-backend/"]
#[exclude = "**/__pycache__/**"]
struct BackendAssets;

const MAX_REQUEST: usize = 1024 * 1024;
const MAX_RESPONSE: u64 = 16 * 1024 * 1024;
pub const HARD_DEADLINE: Duration = Duration::from_secs(28);

pub fn runtime_dir() -> Result<PathBuf, String> {
    let path = match env::var_os("AGENT_BROWSER_CAMOUFOX_RUNTIME") {
        Some(path) if !path.is_empty() => PathBuf::from(path),
        _ => dirs::data_local_dir()
            .ok_or("Cannot locate the local data directory; set AGENT_BROWSER_CAMOUFOX_RUNTIME")?
            .join("agent-browser")
            .join("camoufox-v1"),
    };
    if !path.is_absolute() {
        return Err("AGENT_BROWSER_CAMOUFOX_RUNTIME must be an absolute path".to_string());
    }
    Ok(path)
}

pub fn motion() -> Result<String, String> {
    let value = env::var("AGENT_BROWSER_MOTION").unwrap_or_else(|_| "human-fast".to_string());
    if !matches!(value.as_str(), "human-fast" | "fast" | "precision") {
        return Err("AGENT_BROWSER_MOTION must be human-fast, fast, or precision".to_string());
    }
    Ok(value)
}

pub const INPUT_BACKEND_JUGGLER: &str = "juggler";
pub const INPUT_BACKEND_OSNATIVE: &str = "os-native";
pub const BUBBLE_IMAGE: &str = "agent-browser-camoufox:bubble";
pub const BUBBLE_RUNTIME_DIR: &str = "/opt/agent-browser-runtime";
pub const BUBBLE_WORKER_DIR: &str = "/worker";
pub const BUBBLE_PROFILE_DIR: &str = "/profile";

pub fn input_backend() -> Result<String, String> {
    input_backend_value(env::var("AGENT_BROWSER_INPUT_BACKEND").ok())
}

fn input_backend_value(raw: Option<String>) -> Result<String, String> {
    let value = raw.unwrap_or_else(|| INPUT_BACKEND_JUGGLER.to_string());
    if !matches!(value.as_str(), "juggler" | "os-native") {
        return Err("AGENT_BROWSER_INPUT_BACKEND must be juggler or os-native".to_string());
    }
    Ok(value)
}

fn bubble_probe_message(
    docker_found: bool,
    daemon_reachable: bool,
    image_present: bool,
) -> Option<String> {
    if !docker_found {
        return Some("The docker CLI is required for --input-backend os-native; install Docker (or OrbStack on macOS) and put docker on PATH".to_string());
    }
    if !daemon_reachable {
        return Some("The Docker daemon is unreachable; start OrbStack (or Docker Desktop) before using --input-backend os-native".to_string());
    }
    if !image_present {
        return Some(format!(
            "The bubble image {BUBBLE_IMAGE} is missing; run camoufox-backend/bubble/build.sh from the repo root to build it"
        ));
    }
    None
}

fn probe_docker() -> Result<(), String> {
    if which_docker().is_err() {
        if let Some(message) = bubble_probe_message(false, false, false) {
            return Err(message);
        }
    }
    if CommandProbe::run(["version", "--format", "{{.Server.Version}}"]).is_err() {
        if let Some(message) = bubble_probe_message(true, false, false) {
            return Err(message);
        }
    }
    if CommandProbe::run(["image", "inspect", BUBBLE_IMAGE, "--format", "{{.Id}}"]).is_err() {
        if let Some(message) = bubble_probe_message(true, true, false) {
            return Err(message);
        }
    }
    Ok(())
}

fn which_docker() -> Result<PathBuf, String> {
    let path_env = env::var("PATH").unwrap_or_default();
    for dir in env::split_paths(&path_env) {
        let candidate = dir.join("docker");
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    for candidate in [
        dirs::home_dir().map(|home| home.join(".orbstack/bin/docker")),
        Some(PathBuf::from("/opt/homebrew/bin/docker")),
        Some(PathBuf::from("/usr/local/bin/docker")),
    ]
    .into_iter()
    .flatten()
    {
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    Err("docker binary not found on PATH".to_string())
}

struct CommandProbe;

impl CommandProbe {
    fn run<I, S>(args: I) -> Result<String, String>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let output = std::process::Command::new("docker")
            .args(args.into_iter().map(|arg| arg.as_ref().to_string()))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output()
            .map_err(|error| error.to_string())?;
        if output.status.success() {
            Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
        } else {
            Err(format!("docker exited with {}", output.status))
        }
    }
}

fn free_vnc_port() -> Result<u16, String> {
    std::net::TcpListener::bind(("127.0.0.1", 0))
        .map_err(|error| format!("Cannot allocate a VNC port: {error}"))?
        .local_addr()
        .map(|addr| addr.port())
        .map_err(|error| format!("Cannot determine the VNC port: {error}"))
}

/// Deterministic bubble container name for a named session. OrbStack publishes
/// the container at https://{name}.orb.local, so a session-derived name gives
/// the bubble a fixed local domain and a fixed noVNC URL across relaunches.
/// Anonymous ("default") sessions keep the PID+nanosecond scheme because they
/// have no stable identity to derive a name from.
fn bubble_container_name() -> String {
    match env::var("AGENT_BROWSER_SESSION") {
        Ok(session) if !session.trim().is_empty() && session != "default" => {
            format!("agent-browser-bubble-{session}")
        }
        _ => format!(
            "agent-browser-bubble-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|elapsed| elapsed.subsec_nanos())
                .unwrap_or(0)
        ),
    }
}

/// Remove a leftover container holding the deterministic name. A previous
/// daemon with the same session may not have cleaned up (crash, forced kill);
/// without this the fixed name would collide and docker run would refuse to
/// start. Missing containers are not an error.
fn remove_bubble_container(name: &str) {
    let _ = std::process::Command::new("docker")
        .args(["rm", "-f", name])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn bubble_run_args(
    assets: &Path,
    motion: &str,
    vnc_port: u16,
    container_name: &str,
    host_profile: Option<&Path>,
    deadline_env: Option<(&str, &str)>,
) -> Vec<String> {
    let mut args = vec![
        "run".to_string(),
        "-i".to_string(),
        "--rm".to_string(),
        "--name".to_string(),
        container_name.to_string(),
        "--security-opt=no-new-privileges".to_string(),
        "--shm-size=1g".to_string(),
        "-p".to_string(),
        format!("127.0.0.1:{vnc_port}:6080"),
        "-v".to_string(),
        format!("{}:{BUBBLE_WORKER_DIR}:ro", assets.display()),
        "-e".to_string(),
        "AGENT_BROWSER_BUBBLE=1".to_string(),
        "-e".to_string(),
        format!("AGENT_BROWSER_INPUT_BACKEND={INPUT_BACKEND_OSNATIVE}"),
    ];
    if let Some((key, value)) = deadline_env {
        args.push("-e".to_string());
        args.push(format!("{key}={value}"));
    }
    if let Some(profile) = host_profile {
        args.push("-v".to_string());
        args.push(format!("{}:{BUBBLE_PROFILE_DIR}", profile.display()));
        args.push("-e".to_string());
        args.push(format!("AGENT_BROWSER_PROFILE={BUBBLE_PROFILE_DIR}"));
    }
    args.push(BUBBLE_IMAGE.to_string());
    args.push("--runtime-dir".to_string());
    args.push(BUBBLE_RUNTIME_DIR.to_string());
    args.push("--motion".to_string());
    args.push(motion.to_string());
    args
}

fn materialize_assets(root: &Path) -> Result<PathBuf, String> {
    let mut names: Vec<String> = BackendAssets::iter()
        .map(|name| name.into_owned())
        .collect();
    names.sort();
    let mut hash = Sha256::new();
    for name in &names {
        let asset = BackendAssets::get(name).ok_or("Embedded Camoufox asset is missing")?;
        hash.update(name.as_bytes());
        hash.update([0]);
        hash.update(asset.data.as_ref());
    }
    let directory = root.join("backend").join(hex::encode(hash.finalize()));
    for name in names {
        let asset = BackendAssets::get(&name).ok_or("Embedded Camoufox asset is missing")?;
        let destination = directory.join(&name);
        if let Ok(existing) = fs::read(&destination) {
            if existing != asset.data.as_ref() {
                return Err(format!(
                    "Packaged backend asset was modified: {}",
                    destination.display()
                ));
            }
            continue;
        }
        fs::create_dir_all(destination.parent().ok_or("Invalid backend asset path")?)
            .map_err(|error| error.to_string())?;
        let temporary = destination.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
        fs::write(&temporary, asset.data.as_ref()).map_err(|error| error.to_string())?;
        fs::rename(&temporary, &destination).map_err(|error| error.to_string())?;
    }
    Ok(directory)
}

/// Installation is explicit. Normal worker startup only unpacks the shipped code.
pub fn install() -> Result<Value, String> {
    if !cfg!(unix) {
        return Err("Camoufox V1 supports macOS and Linux; Windows process-tree ownership is not implemented".to_string());
    }
    let root = runtime_dir()?;
    let assets = materialize_assets(&root)?;
    let python = env::var_os("AGENT_BROWSER_PYTHON").unwrap_or_else(|| "python3".into());
    let result = std::process::Command::new(python)
        .args(["-I", "-B"])
        .arg(assets.join("bootstrap.py"))
        .arg("--runtime-dir")
        .arg(&root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .output()
        .map_err(|error| format!("Cannot start Python 3.10+ for Camoufox installation: {error}"))?;
    let data: Value = serde_json::from_slice(&result.stdout).map_err(|_| {
        format!(
            "Camoufox installer returned invalid output (exit {})",
            result.status
        )
    })?;
    if !result.status.success() || data.get("installed") != Some(&Value::Bool(true)) {
        return Err(data
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("Camoufox installation failed")
            .to_string());
    }
    Ok(data)
}

pub fn failure(id: &str, code: &str, error: &str, poisoned: bool) -> Value {
    json!({"id": id, "success": false, "code": code, "error": error, "data": {"inputAmbiguous": poisoned}})
}

pub fn validate_flags(flags: &crate::flags::Flags) -> Result<(), String> {
    if flags.cli_hide_scrollbars || !flags.hide_scrollbars {
        return Err("Scrollbar customization is not implemented by Camoufox V1".to_string());
    }
    if flags.allowed_domains.is_some()
        || flags.cdp.is_some()
        || flags.auto_connect
        || flags.pin_tab
        || flags.cli_pin_tab
        || flags.state.is_some()
        || flags.restore.is_some()
        || flags.session_name.is_some()
        || flags.restore_save.is_some()
        || flags.restore_check_url.is_some()
        || flags.restore_check_text.is_some()
        || flags.restore_check_fn.is_some()
        || flags.executable_path.is_some()
        || flags.proxy.is_some()
        || flags.args.is_some()
        || flags.user_agent.is_some()
        || flags.headers.is_some()
        || !flags.extensions.is_empty()
        || !flags.init_scripts.is_empty()
        || !flags.enable.is_empty()
        || flags.ignore_https_errors
        || flags.ca_cert.is_some()
        || flags.clear_ca_cert
        || flags.allow_file_access
        || flags.webgpu
        || flags.no_xvfb
        || flags.color_scheme.is_some()
        || flags.download_path.is_some()
        || flags.no_auto_dialog
    {
        return Err("Camoufox V1 does not support CDP/providers, state restoration, domain containment, launch plugins, proxy/custom browser settings, or pin-tab. Remove those settings or use a separate Chrome session.".to_string());
    }
    if let Some(profile) = &flags.profile {
        profile_path(profile)?;
    }
    let backend = input_backend()?;
    if backend == INPUT_BACKEND_OSNATIVE {
        probe_docker()?;
    }
    motion()?;
    Ok(())
}

pub fn validate_environment() -> Result<(), String> {
    for key in [
        "AGENT_BROWSER_ALLOWED_DOMAINS",
        "AGENT_BROWSER_CDP",
        "AGENT_BROWSER_PROVIDER",
        "AGENT_BROWSER_SESSION_NAME",
        "AGENT_BROWSER_RESTORE",
        "AGENT_BROWSER_STATE",
        "AGENT_BROWSER_EXECUTABLE_PATH",
        "AGENT_BROWSER_PROXY",
        "AGENT_BROWSER_ARGS",
        "AGENT_BROWSER_USER_AGENT",
        "AGENT_BROWSER_EXTENSIONS",
        "AGENT_BROWSER_INIT_SCRIPTS",
        "AGENT_BROWSER_ENABLE",
        "AGENT_BROWSER_CA_CERT",
        "AGENT_BROWSER_COLOR_SCHEME",
        "AGENT_BROWSER_DOWNLOAD_PATH",
        "AGENT_BROWSER_IOS_DEVICE",
    ] {
        if env::var(key).is_ok_and(|value| !value.trim().is_empty()) {
            return Err(format!("{key} is unsupported by Camoufox V1"));
        }
    }
    for key in [
        "AGENT_BROWSER_AUTO_CONNECT",
        "AGENT_BROWSER_PIN_TAB",
        "AGENT_BROWSER_WEBGPU",
        "AGENT_BROWSER_IGNORE_HTTPS_ERRORS",
        "AGENT_BROWSER_ALLOW_FILE_ACCESS",
        "AGENT_BROWSER_NO_XVFB",
        "AGENT_BROWSER_NO_AUTO_DIALOG",
    ] {
        if env::var(key).is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "yes")) {
            return Err(format!("{key} is unsupported by Camoufox V1"));
        }
    }
    Ok(())
}

/// Resolve profile paths before the worker replaces HOME; never silently accept a temporary profile.
pub fn profile_path(value: &str) -> Result<String, String> {
    let path = Path::new(value);
    if value.trim().is_empty() || !path.is_absolute() {
        return Err("Camoufox --profile requires a non-empty absolute directory path".to_string());
    }
    if fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        return Err("Camoufox profile directory must not be a symlink".to_string());
    }
    Ok(fs::canonicalize(path)
        .unwrap_or_else(|_| path.to_path_buf())
        .to_string_lossy()
        .into_owned())
}

/// Explicit null requests ephemeral storage; absence inherits the daemon's launch configuration.
pub fn requested_profile(command: &Value) -> Result<Option<String>, String> {
    match command.get("profile") {
        Some(Value::Null) => Ok(None),
        Some(Value::String(path)) => profile_path(path).map(Some),
        Some(_) => Err("Camoufox profile must be an absolute path string or null".to_string()),
        None => env::var("AGENT_BROWSER_PROFILE")
            .ok()
            .map(|path| profile_path(&path))
            .transpose(),
    }
}

/// Reject unsupported surfaces before launching; only inert CLI metadata is stripped.
/// Inspection and explicit controls retain canonical action names and daemon policy gates.
pub fn normalize_command(command: &Value) -> Result<Value, String> {
    if serde_json::to_vec(command)
        .map_err(|error| error.to_string())?
        .len()
        > MAX_REQUEST
    {
        return Err("Camoufox request exceeds 1 MiB; nothing was sent".to_string());
    }
    let action = command.get("action").and_then(Value::as_str).unwrap_or("");
    let fields: &[&str] = match action {
        "launch" => &[
            "headless", "engine", "webmcp", "noXvfb", "profile", "adblock",
        ],
        "navigate" => &["url", "waitUntil"],
        "back" | "forward" | "reload" | "url" | "title" | "content" | "read" | "tab_list"
        | "session_info" | "close" | "mainframe" => &[],
        "frame" => &["selector"],
        "evaluate" => &["script"],
        "snapshot" => &[
            "selector",
            "maxDepth",
            "interactive",
            "compact",
            "urls",
            "cursor",
            "quiet",
        ],
        "screenshot" => &[
            "path",
            "screenshotDir",
            "selector",
            "fullPage",
            "annotate",
            "format",
            "quality",
        ],
        "click" => &["selector", "target", "button", "count", "newTab"],
        "dblclick" => &["selector", "button"],
        "fill" => &["selector", "value"],
        "type" => &["selector", "text", "clear", "delay"],
        "press" => &["key"],
        "hover" => &["selector", "settleMs"],
        "hover_hold" => &["selector", "maxMs"],
        "hover_hold_stop" => &[],
        "focus" | "check" | "uncheck" | "gettext" | "inputvalue" | "count" | "boundingbox"
        | "isvisible" | "isenabled" | "ischecked" | "innerhtml" | "scrollintoview" => &["selector"],
        "getattribute" => &["selector", "attribute"],
        "select" => &["selector", "values"],
        "drag" => &[
            "source",
            "target",
            "button",
            "steps",
            "holdBeforeDropMs",
            "reveal",
        ],
        "scroll" => &["direction", "amount", "selector", "chunkSize", "settleMs"],
        "wait" => &["selector", "text", "timeout"],
        "waitforurl" => &["url", "timeout"],
        "waitforloadstate" => &["state", "timeout"],
        "waitforfunction" => &["expression", "timeout"],
        "tab_new" => &["url", "label"],
        "tab_switch" | "tab_close" => &["tabId"],
        "gestures" => &["name"],
        "gesture" => &["name", "params", "observe"],
        "requests" => &["clear", "filter", "type", "method", "status"],
        "request_detail" => &["requestId"],
        "workers" => &[],
        "console" | "errors" => &["clear"],
        "websockets" => &["clear", "filter"],
        "cookies_get" => &["urls"],
        "cookies_set" => &["cookies"],
        "cookies_clear" => &[],
        "storage_get" => &["type", "key"],
        "storage_set" => &["type", "key", "value"],
        "storage_clear" => &["type"],
        "route" => &["url", "abort", "response", "resourceType"],
        "unroute" => &["url"],
        "headers" => &["headers"],
        "offline" => &["offline"],
        "credentials" => &["username", "password"],
        "har_start" => &["content"],
        "har_stop" => &["path"],
        "dialog" => &["response", "promptText"],
        "download" => &["selector", "path"],
        "waitfordownload" => &["path", "timeout"],
        "downloads" => &["clear"],
        "page_outline" => &["selector"],
        "page_links" => &["selector", "cursor", "limit"],
        "dom_chunk" => &["selector", "cursor", "limit"],
        "getbyrole" => &["role", "subaction", "name", "exact", "value"],
        "getbytext" => &["text", "subaction", "exact", "value"],
        "getbylabel" => &["label", "subaction", "exact", "value"],
        "getbyplaceholder" => &["placeholder", "subaction", "exact", "value"],
        "getbyalttext" => &["text", "subaction", "exact", "value"],
        "getbytitle" => &["text", "subaction", "exact", "value"],
        "getbytestid" => &["testId", "subaction", "value"],
        "nth" => &["selector", "index", "subaction", "value"],
        _ => return Err(format!("Action '{action}' is not supported by Camoufox V1")),
    };
    let mut result = command.clone();
    if command.get("profile").is_some() {
        requested_profile(command)?;
    }
    let object = result.as_object_mut().ok_or("Command must be an object")?;
    if let Some(plugins) = object.remove("plugins") {
        if plugins.as_array().is_none_or(|items| !items.is_empty()) {
            return Err("Launch/provider plugins are not supported by Camoufox V1".to_string());
        }
    }
    if action != "launch" {
        object.remove("engine");
        object.remove("headless");
        object.remove("profile");
        object.remove("adblock");
    }
    for field in object.keys() {
        if field != "id" && field != "action" && !fields.contains(&field.as_str()) {
            return Err(format!(
                "Field '{field}' on '{action}' is not supported by Camoufox V1"
            ));
        }
    }
    for field in match action {
        "screenshot" => &["fullPage", "annotate"][..],
        "snapshot" => &["interactive", "compact"][..],
        "click" => &["newTab"][..],
        "launch" => &["noXvfb", "webmcp"][..],
        _ => &[],
    } {
        if object
            .get(*field)
            .is_some_and(|value| !value.is_null() && value != &Value::Bool(false))
        {
            return Err(format!("{field} is not implemented by Camoufox V1"));
        }
        object.remove(*field);
    }
    if action == "screenshot" {
        if object.get("selector").is_some_and(|value| !value.is_null())
            || object.get("quality").is_some_and(|value| !value.is_null())
            || object
                .get("format")
                .is_some_and(|value| value.as_str() != Some("png"))
        {
            return Err("Camoufox screenshots support viewport PNG only".to_string());
        }
    }
    if action.starts_with("wait") {
        if let Some(timeout) = object.get("timeout") {
            let ceiling = env::var("AGENT_BROWSER_ACTION_DEADLINE_MS")
                .ok()
                .and_then(|value| value.parse::<i64>().ok())
                .unwrap_or(22_000)
                .clamp(1000, 25_000) as u64;
            if timeout
                .as_u64()
                .is_none_or(|value| value > ceiling.saturating_sub(500))
            {
                return Err(format!("Camoufox wait timeout must be at most {}ms to leave time inside the action deadline", ceiling.saturating_sub(500)));
            }
        }
    }
    Ok(result)
}

pub struct CamoufoxBackend {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    process_group: Option<u32>,
    failure: Option<String>,
    /// Successful startup history, not liveness. Keep this set after browser loss
    /// so implicit launch cannot replace a session that requires explicit close.
    pub launched: bool,
    pub headed: bool,
    pub profile: Option<String>,
    pub adblock: bool,
    pub bubble: bool,
    bubble_profile_mounted: bool,
    pub container_name: Option<String>,
    pub vnc_port: Option<u16>,
}

impl CamoufoxBackend {
    pub fn spawn() -> Result<Self, String> {
        if !cfg!(unix) {
            return Err("Camoufox V1 supports macOS and Linux only".to_string());
        }
        let backend = input_backend()?;
        let bubble = backend == INPUT_BACKEND_OSNATIVE;
        let root = runtime_dir()?;
        let motion = motion()?;
        let assets = materialize_assets(&root)?;
        let (mut command, vnc_port, container_name, bubble_profile_mounted) = if bubble {
            let vnc_port = free_vnc_port()?;
            let container_name = bubble_container_name();
            remove_bubble_container(&container_name);
            let host_profile = env::var("AGENT_BROWSER_PROFILE")
                .ok()
                .filter(|path| !path.is_empty())
                .map(|path| {
                    let path = PathBuf::from(path);
                    let _ = fs::create_dir_all(&path);
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o700));
                    }
                    path
                });
            let bubble_profile_mounted = host_profile.is_some();
            let deadline = env::var("AGENT_BROWSER_ACTION_DEADLINE_MS").ok();
            let gestures_dir = env::var("AGENT_BROWSER_GESTURES_DIR")
                .ok()
                .filter(|value| !value.is_empty());
            let args = bubble_run_args(
                &assets,
                &motion,
                vnc_port,
                &container_name,
                host_profile.as_deref(),
                deadline
                    .as_deref()
                    .map(|value| ("AGENT_BROWSER_ACTION_DEADLINE_MS", value)),
            );
            let mut command = Command::new("docker");
            command.args(&args);
            if let Some(dir) = gestures_dir {
                let path = PathBuf::from(&dir);
                if path.is_dir() {
                    command
                        .args(["-v", &format!("{}:/worker/gestures-external:ro", dir)])
                        .args(["-e", "AGENT_BROWSER_GESTURES_DIR=/worker/gestures-external"]);
                }
            }
            (
                command,
                Some(vnc_port),
                Some(container_name),
                bubble_profile_mounted,
            )
        } else {
            let python = root.join("venv").join(if cfg!(windows) {
                "Scripts/python.exe"
            } else {
                "bin/python"
            });
            if !python.is_file() || !root.join("runtime.json").is_file() {
                return Err(
                    "Camoufox is not installed; run agent-browser --engine camoufox install"
                        .to_string(),
                );
            }
            let private_tmp = root.join("tmp");
            fs::create_dir_all(&private_tmp).map_err(|error| error.to_string())?;
            let mut command = Command::new(python);
            command
                .args(["-I", "-B", "-u"])
                .arg(assets.join("worker.py"))
                .arg("--runtime-dir")
                .arg(&root)
                .arg("--motion")
                .arg(&motion)
                .env("HOME", root.join("home"))
                .env("XDG_CACHE_HOME", root.join("home/.cache"))
                .env("TMPDIR", private_tmp)
                .env("PLAYWRIGHT_BROWSERS_PATH", root.join("playwright"));
            (command, None, None, false)
        };
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.as_std_mut().process_group(0);
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("Cannot start Camoufox worker: {error}"))?;
        let process_group = child.id();
        let stdin = child.stdin.take().ok_or("Camoufox stdin was not piped")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("Camoufox stdout was not piped")?);
        Ok(Self {
            child,
            stdin,
            stdout,
            process_group,
            failure: None,
            launched: false,
            headed: false,
            profile: None,
            adblock: false,
            container_name,
            vnc_port,
            bubble,
            bubble_profile_mounted,
        })
    }

    async fn exchange(&mut self, command: &Value) -> Result<Value, String> {
        let mut bytes = serde_json::to_vec(command).map_err(|error| error.to_string())?;
        if bytes.len() > MAX_REQUEST {
            return Err("Camoufox request exceeds 1 MiB".to_string());
        }
        bytes.push(b'\n');
        self.stdin
            .write_all(&bytes)
            .await
            .map_err(|error| error.to_string())?;
        self.stdin
            .flush()
            .await
            .map_err(|error| error.to_string())?;
        let mut bytes = Vec::new();
        (&mut self.stdout)
            .take(MAX_RESPONSE + 1)
            .read_until(b'\n', &mut bytes)
            .await
            .map_err(|error| error.to_string())?;
        if bytes.is_empty() || bytes.len() as u64 > MAX_RESPONSE || bytes.last() != Some(&b'\n') {
            return Err(
                "Camoufox worker closed or returned an oversized/incomplete response".to_string(),
            );
        }
        let response: Value = serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
        if response.get("id") != command.get("id")
            || response.get("success").and_then(Value::as_bool).is_none()
        {
            return Err("Camoufox worker returned a mismatched or invalid response".to_string());
        }
        Ok(response)
    }

    pub async fn execute(&mut self, command: &Value) -> Value {
        let id = command.get("id").and_then(Value::as_str).unwrap_or("");
        if id.is_empty()
            || serde_json::to_vec(command).map_or(true, |bytes| bytes.len() > MAX_REQUEST)
        {
            return failure(
                id,
                "camoufox_invalid_request",
                "Request needs a non-empty id and at most 1 MiB; nothing was sent",
                false,
            );
        }
        if let Some(reason) = &self.failure {
            return failure(id, "camoufox_session_reset_required", reason, true);
        }
        let forwarded = if self.bubble_profile_mounted
            && command.get("action").and_then(Value::as_str) == Some("launch")
        {
            let mut rewritten = command.clone();
            if let Some(object) = rewritten.as_object_mut() {
                object.insert("profile".to_string(), json!(BUBBLE_PROFILE_DIR));
            }
            rewritten
        } else {
            command.clone()
        };
        let result = tokio::time::timeout(HARD_DEADLINE, self.exchange(&forwarded)).await;
        match result {
            Ok(Ok(mut response)) => {
                if response.get("inputAmbiguous").and_then(Value::as_bool) == Some(true) {
                    if !response.get("data").is_some_and(Value::is_object) {
                        response["data"] = json!({});
                    }
                    response["data"]["inputAmbiguous"] = json!(true);
                }
                if command.get("action").and_then(Value::as_str) == Some("launch")
                    && response.get("success").and_then(Value::as_bool) == Some(true)
                {
                    self.launched = true;
                    self.headed = response["data"]["headless"].as_bool() == Some(false);
                    self.profile = response["data"]["profilePath"].as_str().map(str::to_string);
                    self.adblock = response["data"]["adblock"].as_bool().unwrap_or(false);
                    if let Some(port) = self.vnc_port {
                        response["data"]["vncUrl"] = json!(format!(
                            "http://127.0.0.1:{port}/vnc.html?autoconnect=1&quality=6&compression=0&resize=scale&reconnect=1&view_only=1"
                        ));
                        if let Some(name) = &self.container_name {
                            response["data"]["vncDomainUrl"] = json!(format!(
                                "https://{name}.orb.local/vnc.html?autoconnect=1&quality=6&compression=0&resize=scale&reconnect=1&view_only=1"
                            ));
                        }
                        response["data"]["nativeVnc"] = json!(format!("vnc://127.0.0.1:{port}"));
                    }
                }
                response
            }
            other => {
                let reason = match other {
                    Ok(Err(error)) => format!("Camoufox transport failed: {error}. Outcome is ambiguous; no replay. Close the session to recover."),
                    _ => "Camoufox exceeded the 28s hard deadline. Outcome is ambiguous; no replay. Close the session to recover.".to_string(),
                };
                self.poison(&reason);
                failure(id, "camoufox_session_reset_required", &reason, true)
            }
        }
    }

    pub fn poison(&mut self, reason: &str) {
        self.failure = Some(reason.to_string());
        self.terminate();
    }

    fn terminate(&mut self) {
        #[cfg(unix)]
        if let Some(group) = self.process_group.take() {
            // Only the process group created for this worker is signaled.
            unsafe {
                libc::kill(-(group as i32), libc::SIGKILL);
            }
        }
        let _ = self.child.start_kill();
        if let Some(name) = self.container_name.take() {
            let _ = std::process::Command::new("docker")
                .args(["rm", "-f", &name])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
    }

    pub async fn close(&mut self) {
        if self.failure.is_none() {
            let _ = self
                .execute(&json!({"id": uuid::Uuid::new_v4().to_string(), "action": "close"}))
                .await;
        }
        self.terminate();
        let _ = tokio::time::timeout(Duration::from_millis(500), self.child.wait()).await;
        self.launched = false;
    }
}

impl Drop for CamoufoxBackend {
    fn drop(&mut self) {
        self.terminate();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn camoufox_input_backend_env_validation() {
        assert_eq!(input_backend_value(None).unwrap(), INPUT_BACKEND_JUGGLER);
        assert_eq!(
            input_backend_value(Some("os-native".to_string())).unwrap(),
            INPUT_BACKEND_OSNATIVE
        );
        assert!(input_backend_value(Some("hid".to_string())).is_err());
    }

    #[test]
    fn camoufox_bubble_probe_messages() {
        assert!(bubble_probe_message(true, true, true).is_none());
        assert!(bubble_probe_message(false, true, true)
            .unwrap()
            .contains("docker CLI"));
        assert!(bubble_probe_message(true, false, true)
            .unwrap()
            .contains("OrbStack"));
        assert!(bubble_probe_message(true, true, false)
            .unwrap()
            .contains("build.sh"));
    }

    #[test]
    fn camoufox_bubble_run_args_shape() {
        let args = bubble_run_args(
            Path::new("/tmp/assets"),
            "fast",
            9101,
            "agent-browser-bubble-42-1",
            Some(Path::new("/Users/x/profiles/main")),
            Some(("AGENT_BROWSER_ACTION_DEADLINE_MS", "22000")),
        );
        let joined = args.join(" ");
        assert!(args.contains(&"--name".to_string()));
        assert!(joined.contains("agent-browser-bubble-42-1"));
        assert!(joined.contains("127.0.0.1:9101:6080"));
        assert!(joined.contains("/Users/x/profiles/main:/profile"));
        assert!(joined.contains("AGENT_BROWSER_PROFILE=/profile"));
        assert!(joined.contains("AGENT_BROWSER_BUBBLE=1"));
        assert!(joined.contains("AGENT_BROWSER_INPUT_BACKEND=os-native"));
        assert!(joined.contains("AGENT_BROWSER_ACTION_DEADLINE_MS=22000"));
        assert!(joined.contains("agent-browser-camoufox:bubble"));
        assert!(joined.contains("--runtime-dir /opt/agent-browser-runtime"));
        assert!(joined.contains("--motion fast"));
        assert!(args.last().map(String::as_str) == Some("fast"));
        let ephemeral =
            bubble_run_args(Path::new("/assets"), "human-fast", 8080, "c1", None, None).join(" ");
        assert!(!ephemeral.contains("/profile"));
        assert!(!ephemeral.contains("AGENT_BROWSER_PROFILE"));
        assert!(ephemeral.contains("--motion human-fast"));
    }

    #[test]
    fn camoufox_bubble_container_name_is_session_derived() {
        let guard = crate::test_utils::EnvGuard::new(&["AGENT_BROWSER_SESSION"]);
        guard.set("AGENT_BROWSER_SESSION", "tarea1");
        assert_eq!(bubble_container_name(), "agent-browser-bubble-tarea1");
        guard.set("AGENT_BROWSER_SESSION", "tarea2");
        assert_eq!(bubble_container_name(), "agent-browser-bubble-tarea2");
        guard.set("AGENT_BROWSER_SESSION", "default");
        let anonymous = bubble_container_name();
        assert!(anonymous.starts_with("agent-browser-bubble-"));
        assert!(!anonymous.starts_with("agent-browser-bubble-tarea"));
        guard.remove("AGENT_BROWSER_SESSION");
        let unset = bubble_container_name();
        assert!(unset.starts_with("agent-browser-bubble-"));
        assert!(!unset.starts_with("agent-browser-bubble-tarea"));
    }

    #[test]
    fn camoufox_persistent_profile_normalization_and_defaults() {
        let guard = crate::test_utils::EnvGuard::new(&["AGENT_BROWSER_PROFILE"]);
        let path = env::temp_dir()
            .join("agent-browser-profile-contract")
            .to_string_lossy()
            .into_owned();
        guard.set("AGENT_BROWSER_PROFILE", &path);
        assert_eq!(requested_profile(&json!({})).unwrap(), Some(path.clone()));
        assert_eq!(requested_profile(&json!({"profile": null})).unwrap(), None);
        assert!(requested_profile(&json!({"profile": 1})).is_err());
        for invalid in ["", " ", "relative-profile", "~/profile"] {
            assert!(profile_path(invalid).is_err());
        }
        let launch = json!({"id":"1", "action":"launch", "profile":path});
        assert_eq!(normalize_command(&launch).unwrap(), launch);
        let navigation =
            json!({"id":"2", "action":"navigate", "url":"about:blank", "profile":path});
        assert!(normalize_command(&navigation)
            .unwrap()
            .get("profile")
            .is_none());
        assert!(normalize_command(
            &json!({"id":"3", "action":"launch", "profile":path, "storageState":"state.json"})
        )
        .is_err());
        let adblock_launch = json!({"id":"4", "action":"launch", "adblock":true});
        assert_eq!(normalize_command(&adblock_launch).unwrap(), adblock_launch);
        assert!(normalize_command(
            &json!({"id":"5", "action":"navigate", "url":"about:blank", "adblock":true})
        )
        .unwrap()
        .get("adblock")
        .is_none());
        assert!(normalize_command(
            &json!({"id":"6", "action":"launch", "adblock":true, "storageState":"state.json"})
        )
        .is_err());
    }

    #[test]
    fn camoufox_inspection_normalize_preserves_canonical_payloads() {
        for command in [
            json!({"id":"1","action":"requests","clear":true,"filter":"api","type":"xhr","method":"POST","status":"2xx"}),
            json!({"id":"1","action":"request_detail","requestId":"n1"}),
            json!({"id":"1","action":"workers"}),
            json!({"id":"1","action":"websockets","clear":true,"filter":"api"}),
            json!({"id":"1","action":"console","clear":true}),
            json!({"id":"1","action":"errors","clear":false}),
            json!({"id":"1","action":"cookies_get","urls":["https://example.com"]}),
            json!({"id":"1","action":"cookies_set","cookies":[{"name":"a","value":"b"}]}),
            json!({"id":"1","action":"cookies_clear"}),
            json!({"id":"1","action":"storage_get","type":"local","key":""}),
            json!({"id":"1","action":"storage_set","type":"session","key":"k","value":""}),
            json!({"id":"1","action":"storage_clear","type":"local"}),
            json!({"id":"1","action":"route","url":"**/api/**","abort":false,"response":{"body":"ok"},"resourceType":"xhr"}),
            json!({"id":"1","action":"unroute","url":"**/api/**"}),
            json!({"id":"1","action":"headers","headers":{"x-example":"value"}}),
            json!({"id":"1","action":"offline","offline":true}),
            json!({"id":"1","action":"credentials","username":"user","password":"example"}),
            json!({"id":"1","action":"har_start","content":"none"}),
            json!({"id":"1","action":"har_stop","path":"out.har"}),
            json!({"id":"1","action":"dialog","response":"accept","promptText":"ok"}),
            json!({"id":"1","action":"download","selector":"#dl","path":"out.bin"}),
            json!({"id":"1","action":"waitfordownload","path":"out.bin","timeout":400}),
            json!({"id":"1","action":"downloads","clear":true}),
            json!({"id":"1","action":"page_outline","selector":"xpath=//main"}),
            json!({"id":"1","action":"page_links","cursor":"l-token-50","limit":100}),
            json!({"id":"1","action":"dom_chunk","cursor":"d-token-100","limit":250}),
        ] {
            let normalized = normalize_command(&command)
                .unwrap_or_else(|error| panic!("{command} rejected: {error}"));
            assert_eq!(normalized, command);
        }
    }

    #[test]
    fn camoufox_inspection_normalize_rejects_unsupported_surfaces() {
        for command in [
            json!({"id":"1","action":"cdp_url"}),
            json!({"id":"1","action":"pause"}),
            json!({"id":"1","action":"navigate","url":"https://example.com","allowedDomains":["example.com"]}),
            json!({"id":"1","action":"launch","allowedDomains":["example.com"]}),
            json!({"id":"1","action":"requests","domains":["example.com"]}),
            json!({"id":"1","action":"route","url":"*","handler":"continue"}),
        ] {
            let error = normalize_command(&command).unwrap_err();
            assert!(
                error.contains("not supported by Camoufox V1"),
                "{command}: {error}"
            );
        }
    }

    #[test]
    fn camoufox_find_normalize_accepts_semantic_locators_and_rejects_unknown_fields() {
        let accepted = [
            json!({"id":"1","action":"getbyrole","role":"button","subaction":"text","name":null,"exact":false}),
            json!({"id":"2","action":"getbyrole","role":"button","subaction":"click","name":"Submit","exact":true,"value":"x"}),
            json!({"id":"3","action":"nth","selector":"nav a","index":-1,"subaction":"click"}),
            json!({"id":"4","action":"nth","selector":"@e1","index":0,"subaction":"fill","value":"text"}),
            json!({"id":"5","action":"getbytext","text":"Sign in","subaction":"click","exact":true}),
            json!({"id":"6","action":"getbylabel","label":"Email","subaction":"fill","exact":false,"value":"a@b.c"}),
            json!({"id":"7","action":"getbyplaceholder","placeholder":"Search","subaction":"text","exact":false}),
            json!({"id":"8","action":"getbyalttext","text":"Logo","subaction":"click","exact":false}),
            json!({"id":"9","action":"getbytitle","text":"Details","subaction":"hover","exact":true}),
            json!({"id":"10","action":"getbytestid","testId":"submit","subaction":"click"}),
        ];
        for command in accepted {
            let normalized = normalize_command(&command)
                .unwrap_or_else(|error| panic!("{command} rejected: {error}"));
            assert_eq!(normalized, command);
        }
        let rejected = [
            json!({"id":"11","action":"getbyrole","role":"button","bogus":"x"}),
            json!({"id":"12","action":"getbytestid","testId":"submit","exact":true}),
            json!({"id":"13","action":"nth","selector":"a","index":0,"exact":true}),
            json!({"id":"14","action":"getbytext","text":"x","label":"y"}),
        ];
        for command in rejected {
            let error = normalize_command(&command).unwrap_err();
            assert!(
                error.contains("not supported by Camoufox V1"),
                "{command}: {error}"
            );
        }
    }

    #[test]
    fn camoufox_hover_hold_normalize_accepts_canonical_fields() {
        for command in [
            json!({"id":"1","action":"hover_hold","selector":"#player","maxMs":30000}),
            json!({"id":"2","action":"hover_hold","selector":"@e1"}),
            json!({"id":"3","action":"hover_hold_stop"}),
        ] {
            let normalized = normalize_command(&command)
                .unwrap_or_else(|error| panic!("{command} rejected: {error}"));
            assert_eq!(normalized, command);
        }
        for command in [
            json!({"id":"4","action":"hover_hold","selector":"#player","bogus":true}),
            json!({"id":"5","action":"hover_hold_stop","maxMs":30000}),
            json!({"id":"6","action":"hover_hold_stop","selector":"#player"}),
        ] {
            let error = normalize_command(&command).unwrap_err();
            assert!(
                error.contains("not supported by Camoufox V1"),
                "{command}: {error}"
            );
        }
    }
}
