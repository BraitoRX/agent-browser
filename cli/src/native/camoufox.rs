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

fn materialize_assets(root: &Path) -> Result<PathBuf, String> {
    let mut names: Vec<String> = BackendAssets::iter().map(|name| name.into_owned()).collect();
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
                return Err(format!("Packaged backend asset was modified: {}", destination.display()));
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
    let data: Value = serde_json::from_slice(&result.stdout)
        .map_err(|_| format!("Camoufox installer returned invalid output (exit {})", result.status))?;
    if !result.status.success() || data.get("installed") != Some(&Value::Bool(true)) {
        return Err(data.get("error").and_then(Value::as_str).unwrap_or("Camoufox installation failed").to_string());
    }
    Ok(data)
}

pub fn failure(id: &str, code: &str, error: &str, poisoned: bool) -> Value {
    json!({"id": id, "success": false, "code": code, "error": error, "data": {"poisoned": poisoned}})
}

pub fn validate_flags(flags: &crate::flags::Flags) -> Result<(), String> {
    if flags.cli_hide_scrollbars || !flags.hide_scrollbars {
        return Err("Scrollbar customization is not implemented by Camoufox V1".to_string());
    }
    if flags.allowed_domains.is_some() || flags.cdp.is_some() || flags.provider.is_some()
        || flags.auto_connect || flags.pin_tab || flags.cli_pin_tab || flags.profile.is_some() || flags.state.is_some()
        || flags.restore.is_some() || flags.session_name.is_some() || flags.restore_save.is_some()
        || flags.restore_check_url.is_some() || flags.restore_check_text.is_some() || flags.restore_check_fn.is_some()
        || flags.executable_path.is_some() || flags.proxy.is_some() || flags.args.is_some()
        || flags.user_agent.is_some() || flags.headers.is_some() || !flags.extensions.is_empty()
        || !flags.init_scripts.is_empty() || !flags.enable.is_empty() || !flags.plugins.is_empty()
        || flags.ignore_https_errors || flags.ca_cert.is_some() || flags.clear_ca_cert
        || flags.allow_file_access || flags.webgpu || flags.no_xvfb || flags.device.is_some()
        || flags.color_scheme.is_some() || flags.download_path.is_some() || flags.no_auto_dialog {
        return Err("Camoufox V1 does not support CDP/providers, restore/profiles, domain containment, launch plugins, proxy/custom browser settings, or pin-tab. Remove those settings or use a separate Chrome session.".to_string());
    }
    motion()?;
    Ok(())
}

pub fn validate_environment() -> Result<(), String> {
    for key in [
        "AGENT_BROWSER_ALLOWED_DOMAINS", "AGENT_BROWSER_CDP", "AGENT_BROWSER_PROVIDER",
        "AGENT_BROWSER_SESSION_NAME", "AGENT_BROWSER_RESTORE", "AGENT_BROWSER_PROFILE",
        "AGENT_BROWSER_STATE", "AGENT_BROWSER_EXECUTABLE_PATH", "AGENT_BROWSER_PROXY",
        "AGENT_BROWSER_ARGS", "AGENT_BROWSER_USER_AGENT", "AGENT_BROWSER_EXTENSIONS",
        "AGENT_BROWSER_INIT_SCRIPTS", "AGENT_BROWSER_ENABLE", "AGENT_BROWSER_CA_CERT",
        "AGENT_BROWSER_COLOR_SCHEME", "AGENT_BROWSER_DOWNLOAD_PATH", "AGENT_BROWSER_IOS_DEVICE",
    ] {
        if env::var(key).is_ok_and(|value| !value.trim().is_empty()) {
            return Err(format!("{key} is unsupported by Camoufox V1"));
        }
    }
    for key in ["AGENT_BROWSER_AUTO_CONNECT", "AGENT_BROWSER_PIN_TAB", "AGENT_BROWSER_WEBGPU",
        "AGENT_BROWSER_IGNORE_HTTPS_ERRORS", "AGENT_BROWSER_ALLOW_FILE_ACCESS", "AGENT_BROWSER_NO_XVFB",
        "AGENT_BROWSER_NO_AUTO_DIALOG"] {
        if env::var(key).is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "yes")) {
            return Err(format!("{key} is unsupported by Camoufox V1"));
        }
    }
    Ok(())
}

/// Reject unsupported surfaces before launching; only inert CLI metadata is stripped.
pub fn normalize_command(command: &Value) -> Result<Value, String> {
    if serde_json::to_vec(command).map_err(|error| error.to_string())?.len() > MAX_REQUEST {
        return Err("Camoufox request exceeds 1 MiB; nothing was sent".to_string());
    }
    let action = command.get("action").and_then(Value::as_str).unwrap_or("");
    let fields: &[&str] = match action {
        "launch" => &["headless", "engine", "webmcp", "noXvfb"],
        "navigate" => &["url", "waitUntil"],
        "back" | "forward" | "reload" | "url" | "title" | "content" | "read"
        | "tab_list" | "session_info" | "close" => &[],
        "evaluate" => &["script"],
        "snapshot" => &["selector", "maxDepth", "interactive", "compact", "urls", "cursor"],
        "screenshot" => &["path", "screenshotDir", "selector", "fullPage", "annotate", "format", "quality"],
        "click" => &["selector", "target", "button", "count", "newTab"],
        "dblclick" => &["selector", "button"],
        "fill" => &["selector", "value"],
        "type" => &["selector", "text", "clear", "delay"],
        "press" => &["key"],
        "hover" => &["selector", "settleMs"],
        "focus" | "check" | "uncheck" | "gettext" | "inputvalue" | "count" | "boundingbox"
        | "isvisible" | "isenabled" | "ischecked" => &["selector"],
        "getattribute" => &["selector", "attribute"],
        "select" => &["selector", "values"],
        "drag" => &["source", "target", "button", "steps", "holdBeforeDropMs", "reveal"],
        "scroll" => &["direction", "amount", "selector", "chunkSize", "settleMs"],
        "wait" => &["selector", "text", "timeout"],
        "waitforurl" => &["url", "timeout"],
        "waitforloadstate" => &["state", "timeout"],
        "waitforfunction" => &["expression", "timeout"],
        "tab_new" => &["url", "label"],
        "tab_switch" | "tab_close" => &["tabId"],
        "gestures" => &["name"],
        "gesture" => &["name", "params", "observe"],
        _ => return Err(format!("Action '{action}' is not supported by Camoufox V1")),
    };
    let mut result = command.clone();
    let object = result.as_object_mut().ok_or("Command must be an object")?;
    if let Some(plugins) = object.remove("plugins") {
        if plugins.as_array().is_none_or(|items| !items.is_empty()) {
            return Err("Launch/provider plugins are not supported by Camoufox V1".to_string());
        }
    }
    if action != "launch" {
        object.remove("engine");
        object.remove("headless");
    }
    for field in object.keys() {
        if field != "id" && field != "action" && !fields.contains(&field.as_str()) {
            return Err(format!("Field '{field}' on '{action}' is not supported by Camoufox V1"));
        }
    }
    for field in match action {
        "screenshot" => &["fullPage", "annotate"][..],
        "snapshot" => &["interactive", "compact", "urls", "cursor"][..],
        "click" => &["newTab"][..],
        "launch" => &["noXvfb", "webmcp"][..],
        _ => &[],
    } {
        if object.get(*field).is_some_and(|value| !value.is_null() && value != &Value::Bool(false)) {
            return Err(format!("{field} is not implemented by Camoufox V1"));
        }
        object.remove(*field);
    }
    if action == "screenshot" {
        if object.get("selector").is_some_and(|value| !value.is_null())
            || object.get("quality").is_some_and(|value| !value.is_null())
            || object.get("format").is_some_and(|value| value.as_str() != Some("png")) {
            return Err("Camoufox screenshots support viewport PNG only".to_string());
        }
    }
    if action.starts_with("wait") {
        if let Some(timeout) = object.get("timeout") {
            let ceiling = env::var("AGENT_BROWSER_ACTION_DEADLINE_MS").ok()
                .and_then(|value| value.parse::<i64>().ok()).unwrap_or(22_000).clamp(1000, 25_000) as u64;
            if timeout.as_u64().is_none_or(|value| value > ceiling.saturating_sub(500)) {
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
}

impl CamoufoxBackend {
    pub fn spawn() -> Result<Self, String> {
        if !cfg!(unix) {
            return Err("Camoufox V1 supports macOS and Linux only".to_string());
        }
        let root = runtime_dir()?;
        let python = root.join("venv").join(if cfg!(windows) { "Scripts/python.exe" } else { "bin/python" });
        if !python.is_file() || !root.join("runtime.json").is_file() {
            return Err("Camoufox is not installed; run agent-browser --engine camoufox install".to_string());
        }
        let motion = motion()?;
        let assets = materialize_assets(&root)?;
        let private_tmp = root.join("tmp");
        fs::create_dir_all(&private_tmp).map_err(|error| error.to_string())?;
        let mut command = Command::new(python);
        command.args(["-I", "-B", "-u"])
            .arg(assets.join("worker.py"))
            .arg("--runtime-dir").arg(&root)
            .arg("--motion").arg(motion)
            .env("HOME", root.join("home"))
            .env("XDG_CACHE_HOME", root.join("home/.cache"))
            .env("TMPDIR", private_tmp)
            .env("PLAYWRIGHT_BROWSERS_PATH", root.join("playwright"))
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit())
            .kill_on_drop(true);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.as_std_mut().process_group(0);
        }
        let mut child = command.spawn().map_err(|error| format!("Cannot start Camoufox worker: {error}"))?;
        let process_group = child.id();
        let stdin = child.stdin.take().ok_or("Camoufox stdin was not piped")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("Camoufox stdout was not piped")?);
        Ok(Self { child, stdin, stdout, process_group, failure: None, launched: false, headed: false })
    }

    async fn exchange(&mut self, command: &Value) -> Result<Value, String> {
        let mut bytes = serde_json::to_vec(command).map_err(|error| error.to_string())?;
        if bytes.len() > MAX_REQUEST {
            return Err("Camoufox request exceeds 1 MiB".to_string());
        }
        bytes.push(b'\n');
        self.stdin.write_all(&bytes).await.map_err(|error| error.to_string())?;
        self.stdin.flush().await.map_err(|error| error.to_string())?;
        let mut bytes = Vec::new();
        (&mut self.stdout).take(MAX_RESPONSE + 1).read_until(b'\n', &mut bytes)
            .await.map_err(|error| error.to_string())?;
        if bytes.is_empty() || bytes.len() as u64 > MAX_RESPONSE || bytes.last() != Some(&b'\n') {
            return Err("Camoufox worker closed or returned an oversized/incomplete response".to_string());
        }
        let response: Value = serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
        if response.get("id") != command.get("id") || response.get("success").and_then(Value::as_bool).is_none() {
            return Err("Camoufox worker returned a mismatched or invalid response".to_string());
        }
        Ok(response)
    }

    pub async fn execute(&mut self, command: &Value) -> Value {
        let id = command.get("id").and_then(Value::as_str).unwrap_or("");
        if id.is_empty() || serde_json::to_vec(command).map_or(true, |bytes| bytes.len() > MAX_REQUEST) {
            return failure(id, "camoufox_invalid_request", "Request needs a non-empty id and at most 1 MiB; nothing was sent", false);
        }
        if let Some(reason) = &self.failure {
            return failure(id, "camoufox_poisoned", reason, true);
        }
        let result = tokio::time::timeout(HARD_DEADLINE, self.exchange(command)).await;
        match result {
            Ok(Ok(mut response)) => {
                if response.get("poisoned").and_then(Value::as_bool) == Some(true) {
                    if !response.get("data").is_some_and(Value::is_object) {
                        response["data"] = json!({});
                    }
                    response["data"]["poisoned"] = json!(true);
                }
                if command.get("action").and_then(Value::as_str) == Some("launch")
                    && response.get("success").and_then(Value::as_bool) == Some(true) {
                    self.launched = true;
                    self.headed = response["data"]["headless"].as_bool() == Some(false);
                }
                response
            }
            other => {
                let reason = match other {
                    Ok(Err(error)) => format!("Camoufox transport failed: {error}. Outcome is ambiguous; no replay. Close the session to recover."),
                    _ => "Camoufox exceeded the 28s hard deadline. Outcome is ambiguous; no replay. Close the session to recover.".to_string(),
                };
                self.poison(&reason);
                failure(id, "camoufox_poisoned", &reason, true)
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
            unsafe { libc::kill(-(group as i32), libc::SIGKILL); }
        }
        let _ = self.child.start_kill();
    }

    pub async fn close(&mut self) {
        if self.failure.is_none() {
            let _ = self.execute(&json!({"id": uuid::Uuid::new_v4().to_string(), "action": "close"})).await;
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
