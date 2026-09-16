mod chat;
mod color;
mod commands;
mod connection;
mod doctor;
mod flags;
mod mcp;
mod native;
mod output;
mod skills;
#[cfg(test)]
mod test_utils;
mod validation;

use serde_json::json;
use sha2::{Digest, Sha256};
use std::env;
#[cfg(unix)]
use std::path::PathBuf;
use std::process::{exit, Command};

#[cfg(windows)]
use windows_sys::Win32::Foundation::CloseHandle;
#[cfg(windows)]
use windows_sys::Win32::System::Threading::OpenProcess;

use commands::{gen_id, parse_command, ParseError};
use connection::{
    cleanup_stale_files, daemon_unreachable, ensure_daemon, get_socket_dir,
    send_command, walk_daemons, DaemonOptions, Response,
};
use flags::{clean_args, parse_flags, Flags};
use output::{
    print_command_help, print_help, print_response_with_opts, print_version, OutputOptions,
};

fn serialize_json_value(value: &serde_json::Value) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| {
        r#"{"success":false,"error":"Failed to serialize JSON response"}"#.to_string()
    })
}

fn print_json_value(value: serde_json::Value) {
    println!("{}", serialize_json_value(&value));
}

fn print_json_error(message: impl AsRef<str>) {
    print_json_value(json!({
        "success": false,
        "error": message.as_ref(),
    }));
}

fn print_json_error_with_type(message: impl AsRef<str>, error_type: &str) {
    print_json_value(json!({
        "success": false,
        "error": message.as_ref(),
        "type": error_type,
    }));
}











/// Effective engine for this invocation: an explicit --engine wins; without
/// one, Camoufox becomes the default when its runtime is installed, and the
/// upstream chrome path remains the fallback for bare installs.
pub fn resolve_default_engine() -> String {
    "camoufox".to_string()
}

fn resolve_engine(flags: &Flags) -> String {
    flags.engine.clone().unwrap_or_else(resolve_default_engine)
}



fn mark_restarted_background(resp: &mut Response) {
    if !resp.success {
        return;
    }

    let Some(data) = resp.data.as_mut().and_then(|v| v.as_object_mut()) else {
        return;
    };

    let lifecycle = data
        .entry("lifecycle".to_string())
        .or_insert_with(|| json!({}));
    if !lifecycle.is_object() {
        *lifecycle = json!({});
    }
    if let Some(obj) = lifecycle.as_object_mut() {
        obj.insert("restartedBackground".to_string(), json!(true));
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ConfirmationPrompt {
    action: String,
    category: String,
    description: String,
    confirmation_id: String,
}

fn confirmation_prompt_from_data(data: &serde_json::Value) -> Option<ConfirmationPrompt> {
    if data
        .get("confirmation_required")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        let action = data
            .get("action")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        return Some(ConfirmationPrompt {
            action: action.clone(),
            category: data
                .get("category")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            description: data
                .get("description")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .unwrap_or(action.as_str())
                .to_string(),
            confirmation_id: data
                .get("confirmation_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
        });
    }

    data.get("result")
        .and_then(|v| v.get("data"))
        .and_then(confirmation_prompt_from_data)
}

fn confirmation_prompt_from_response(resp: &Response) -> Option<ConfirmationPrompt> {
    resp.data.as_ref().and_then(confirmation_prompt_from_data)
}

fn run_interactive_confirmations(
    mut resp: Response,
    flags: &Flags,
    output_opts: &OutputOptions,
) -> Response {
    while let Some(prompt) = confirmation_prompt_from_response(&resp) {
        eprintln!("[agent-browser] Action requires confirmation:");
        if prompt.category.is_empty() {
            eprintln!("  {}", prompt.description);
        } else {
            eprintln!("  {}: {}", prompt.category, prompt.description);
        }
        eprint!("  Allow? [y/N]: ");

        let mut input = String::new();
        let approved = if std::io::IsTerminal::is_terminal(&std::io::stdin()) {
            std::io::stdin().read_line(&mut input).is_ok()
                && matches!(input.trim().to_lowercase().as_str(), "y" | "yes")
        } else {
            false
        };

        let confirm_cmd = if approved {
            json!({
                "id": gen_id(),
                "action": "confirm",
                "confirmationId": prompt.confirmation_id
            })
        } else {
            json!({
                "id": gen_id(),
                "action": "deny",
                "confirmationId": prompt.confirmation_id
            })
        };

        match send_command(confirm_cmd, &flags.session) {
            Ok(next_resp) => {
                if !approved {
                    eprintln!("{} Action denied", color::error_indicator());
                    exit(1);
                }
                resp = next_resp;
            }
            Err(e) => {
                eprintln!("{} {}", color::error_indicator(), e);
                exit(1);
            }
        }
    }

    print_response_with_opts(&resp, None, output_opts);
    resp
}



fn canonical_path(path: PathBuf) -> PathBuf {
    path.canonicalize().unwrap_or(path)
}

fn git_toplevel() -> Option<PathBuf> {
    let output = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let raw = String::from_utf8(output.stdout).ok()?;
    let path = raw.trim();
    if path.is_empty() {
        None
    } else {
        Some(canonical_path(PathBuf::from(path)))
    }
}

fn resolve_session_id_scope(scope: &str) -> Result<(String, PathBuf), String> {
    match scope {
        "worktree" => {
            let path = git_toplevel().unwrap_or_else(|| {
                canonical_path(env::current_dir().unwrap_or_else(|_| PathBuf::from(".")))
            });
            Ok(("worktree".to_string(), path))
        }
        "cwd" => {
            let path = canonical_path(env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
            Ok(("cwd".to_string(), path))
        }
        "git-root" => git_toplevel()
            .map(|path| ("git-root".to_string(), path))
            .ok_or_else(|| "Not inside a Git working tree".to_string()),
        other => Err(format!(
            "Unknown session id scope '{}'. Use worktree, cwd, or git-root.",
            other
        )),
    }
}

fn run_session_id(args: &[String], json_mode: bool) {
    let mut scope = "worktree".to_string();
    let mut prefix: Option<String> = None;
    let mut json_output = json_mode;

    let mut i = 2;
    while i < args.len() {
        match args[i].as_str() {
            "--scope" => {
                if let Some(value) = args.get(i + 1) {
                    scope = value.clone();
                    i += 1;
                }
            }
            "--prefix" => {
                if let Some(value) = args.get(i + 1) {
                    prefix = Some(value.clone());
                    i += 1;
                }
            }
            "--json" => json_output = true,
            _ => {}
        }
        i += 1;
    }

    let (resolved_scope, path) = match resolve_session_id_scope(&scope) {
        Ok(result) => result,
        Err(e) => {
            if json_output {
                print_json_error(e);
            } else {
                eprintln!("{} {}", color::error_indicator(), e);
            }
            exit(1);
        }
    };

    let path_str = path.to_string_lossy().to_string();
    let mut hasher = Sha256::new();
    hasher.update(path_str.as_bytes());
    let hash = format!("{:x}", hasher.finalize());
    let suffix = &hash[..12];

    let prefix = prefix
        .as_deref()
        .map(validation::sanitize_session_component)
        .filter(|s| !s.is_empty());
    let session = match prefix {
        Some(prefix) => format!("{}-{}", prefix, suffix),
        None => suffix.to_string(),
    };

    if json_output {
        print_json_value(json!({
            "success": true,
            "data": {
                "session": session,
                "scope": resolved_scope,
                "path": path_str,
                "hash": suffix
            }
        }));
    } else {
        println!("{}", session);
    }
}

fn run_session_info(session: &str, json_mode: bool) {
    let inventory = walk_daemons();
    let active = inventory.sessions.iter().find(|s| s.name == session);
    let runtime = active.and_then(|_| {
        send_command(
            json!({
                "id": gen_id(),
                "action": "session_info"
            }),
            session,
        )
        .ok()
    });

    let runtime_data = runtime.as_ref().and_then(|resp| resp.data.clone());
    let runtime_error = runtime.as_ref().and_then(|resp| {
        if resp.success {
            None
        } else {
            resp.error.clone()
        }
    });

    if json_mode {
        print_json_value(json!({
            "success": true,
            "data": {
                "session": session,
                "namespace": env::var("AGENT_BROWSER_NAMESPACE").ok(),
                "socketDir": get_socket_dir().to_string_lossy(),
                "active": active.is_some(),
                "pid": active.map(|s| s.pid),
                "version": active.and_then(|s| s.version.clone()),
                "runtime": runtime_data,
                "runtimeError": runtime_error,
            }
        }));
        return;
    }

    println!("Session: {}", session);
    println!("Socket dir: {}", get_socket_dir().to_string_lossy());
    if let Ok(namespace) = env::var("AGENT_BROWSER_NAMESPACE") {
        println!("Namespace: {}", namespace);
    }
    if let Some(active) = active {
        println!("Daemon: running (pid {})", active.pid);
        if let Some(ref version) = active.version {
            println!("Version: {}", version);
        }
    } else {
        println!("Daemon: not running");
    }
    if let Some(data) = runtime_data {
        if let Some(restore_status) = data.get("restoreStatus").and_then(|v| v.as_str()) {
            println!("Restore status: {}", restore_status);
        }
        if let Some(save_status) = data.get("saveStatus").and_then(|v| v.as_str()) {
            println!("Save status: {}", save_status);
        }
        if let Some(engine) = data.get("engine").and_then(|v| v.as_str()) {
            println!("Engine: {}", engine);
        }
        let camoufox = data.get("engine").and_then(|v| v.as_str()) == Some("camoufox");
        let browser_launched = data
            .get("browserLaunched")
            .and_then(|v| v.as_bool())
            .or_else(|| {
                if camoufox {
                    data.get("launched").and_then(|v| v.as_bool())
                } else {
                    None
                }
            });
        if let Some(launched) = browser_launched {
            println!("Browser launched: {}", launched);
        }
        if camoufox {
            if let Some(connected) = data.get("browserConnected").and_then(|v| v.as_bool()) {
                println!("Browser connected: {}", connected);
            }
            if let Some(required) = data.get("recoveryRequired").and_then(|v| v.as_bool()) {
                println!("Recovery required: {}", required);
            }
            if let Some(reason) = data.get("closeReason").and_then(|v| v.as_str()) {
                println!(
                    "Close reason: {} (close the affected session before reopening)",
                    reason
                );
            }
            if let Some(url) = data.get("vncDomainUrl").and_then(|v| v.as_str()) {
                println!("VNC domain: {}", url);
            }
        }
    } else if let Some(err) = runtime_error {
        println!("Runtime info unavailable: {}", err);
    }
}

fn run_session(args: &[String], session: &str, json_mode: bool) {
    let subcommand = args.get(1).map(|s| s.as_str());

    match subcommand {
        Some("id") => run_session_id(args, json_mode),
        Some("info") => run_session_info(session, json_mode),
        Some("list") => {
            let sessions: Vec<String> = walk_daemons()
                .sessions
                .into_iter()
                .map(|s| s.name)
                .collect();

            if json_mode {
                println!(
                    r#"{{"success":true,"data":{{"sessions":{}}}}}"#,
                    serde_json::to_string(&sessions).unwrap_or_default()
                );
            } else if sessions.is_empty() {
                println!("No active sessions");
            } else {
                println!("Active sessions:");
                for s in &sessions {
                    let marker = if s == session {
                        color::cyan("→")
                    } else {
                        " ".to_string()
                    };
                    println!("{} {}", marker, s);
                }
            }
        }
        None | Some(_) => {
            // Just show current session
            if json_mode {
                print_json_value(json!({
                    "success": true,
                    "data": {
                        "session": session,
                    },
                }));
            } else {
                println!("{}", session);
            }
        }
    }
}

/// Start the dashboard with an explicit proxy-origin allowlist and a unique
/// access token. Both are kept separate from the request Host so DNS rebinding
/// cannot grant access to an attacker-controlled origin. Persist the effective
/// settings beside the PID so repeated starts cannot silently claim that a live
/// process adopted different settings.
fn run_close_all(flags: &Flags) {
    // walk_daemons auto-cleans stale .pid / .sock / .stream sidecar files and
    // separates out the standalone dashboard. We only want to send `close` to
    // real session daemons; the dashboard has its own `dashboard stop`.
    let inventory = walk_daemons();
    let sessions: Vec<(String, u32)> = inventory
        .sessions
        .iter()
        .map(|s| (s.name.clone(), s.pid))
        .collect();

    if sessions.is_empty() {
        if flags.json {
            print_json_value(json!({
                "success": true,
                "data": { "closed": 0, "sessions": [] },
            }));
        } else {
            println!("No active sessions");
        }
        return;
    }

    let mut closed: Vec<String> = Vec::new();
    let mut failed: Vec<(String, String)> = Vec::new();

    for (session, pid) in &sessions {
        let cmd = json!({ "id": gen_id(), "action": "close" });
        match send_command(cmd, session) {
            Ok(resp) if resp.success => closed.push(session.clone()),
            Ok(resp) => {
                let err = resp.error.unwrap_or_else(|| "Unknown error".to_string());
                failed.push((session.clone(), err));
            }
            Err(_) => {
                // Daemon is unreachable despite its process existing.
                // Force-kill the process and clean up stale files so future
                // sessions stay healthy.
                #[cfg(unix)]
                unsafe {
                    libc::kill(*pid as i32, libc::SIGKILL);
                }
                #[cfg(windows)]
                unsafe {
                    let handle = OpenProcess(1, 0, *pid); // PROCESS_TERMINATE = 1
                    if handle != 0 {
                        windows_sys::Win32::System::Threading::TerminateProcess(handle, 1);
                        CloseHandle(handle);
                    }
                }
                cleanup_stale_files(session);
                closed.push(session.clone());
            }
        }
    }

    if flags.json {
        print_json_value(json!({
            "success": failed.is_empty(),
            "data": {
                "closed": closed.len(),
                "sessions": closed,
                "failed": failed.iter().map(|(s, e)| json!({"session": s, "error": e})).collect::<Vec<_>>(),
            },
        }));
    } else {
        for s in &closed {
            println!("{} Closed session: {}", color::green("✓"), s);
        }
        for (s, e) in &failed {
            eprintln!("{} Failed to close {}: {}", color::error_indicator(), s, e);
        }
        if closed.is_empty() && !failed.is_empty() {
            exit(1);
        }
    }

    if !failed.is_empty() {
        exit(1);
    }
}

fn main() {
    // Rust ignores SIGPIPE by default, causing println! to panic on broken pipes.
    // Reset to SIG_DFL so the OS terminates the process cleanly instead.
    #[cfg(unix)]
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }

    // Prevent MSYS/Git Bash path translation from mangling arguments
    #[cfg(windows)]
    {
        env::set_var("MSYS_NO_PATHCONV", "1");
        env::set_var("MSYS2_ARG_CONV_EXCL", "*");
    }

    // Native daemon mode: when AGENT_BROWSER_DAEMON is set, run as the daemon process
    if env::var("AGENT_BROWSER_DAEMON").is_ok() {
        // Ignore SIGPIPE so the daemon isn't killed when the parent drops
        // the piped stderr handle after confirming the daemon is ready.
        #[cfg(unix)]
        unsafe {
            libc::signal(libc::SIGPIPE, libc::SIG_IGN);
        }
        let session = env::var("AGENT_BROWSER_SESSION").unwrap_or_else(|_| "default".to_string());
        let rt = tokio::runtime::Runtime::new().expect("Failed to create tokio runtime");
        rt.block_on(native::daemon::run_daemon(&session));
        return;
    }

    let args: Vec<String> = env::args().skip(1).collect();
    let flags = parse_flags(&args);
    if let Some(ref namespace) = flags.namespace {
        env::set_var("AGENT_BROWSER_NAMESPACE", namespace);
    }
    let clean = clean_args(&args);

    let has_help = args.iter().any(|a| a == "--help" || a == "-h");
    let has_version = args.iter().any(|a| a == "--version" || a == "-V");

    if has_help {
        if let Some(cmd) = clean.first() {
            if print_command_help(cmd) {
                return;
            }
        }
        print_help();
        return;
    }

    if has_version {
        print_version();
        return;
    }

    if clean.is_empty() {
        print_help();
        return;
    }

    let effective_engine = resolve_engine(&flags);
    let camoufox_default = effective_engine == "camoufox";

    // Handle install separately
    if clean.first().map(|s| s.as_str()) == Some("install") {
        let with_deps = args.iter().any(|a| a == "--with-deps" || a == "-d");
        if camoufox_default {
            let result = if with_deps {
                Err("Camoufox install does not support --with-deps; provision OS libraries separately".to_string())
            } else {
                native::camoufox::install()
            };
            match result {
                Ok(data) => {
                    if flags.json {
                        println!("{}", json!({"success": true, "data": data}));
                    } else {
                        println!(
                            "Camoufox runtime installed at {}",
                            data["runtimeDir"].as_str().unwrap_or("")
                        );
                    }
                }
                Err(error) => {
                    if flags.json {
                        print_json_error(error);
                    } else {
                        eprintln!("{} {}", color::error_indicator(), error);
                    }
                    exit(1);
                }
            }
            return;
        }
        return;
    }

    // Handle doctor separately (doesn't need daemon; spawns its own scratch
    // session for the live launch test).
    if clean.first().map(|s| s.as_str()) == Some("doctor") {
        let opts = doctor::DoctorOptions {
            fix: args.iter().any(|a| a == "--fix"),
            json: flags.json,
            // Explicit CLI opt-in only: a global AGENT_BROWSER_WEBGPU/config
            // "webgpu": true must not make every doctor run launch the extra
            // Chrome probe (and fail on hosts missing Vulkan deps).
            // Merged (env/config included) so the probe reflects how the
            // user's sessions actually launch.
        };
        exit(doctor::run_doctor(opts));
    }

    // Handle profiles command (doesn't need daemon)

    // Handle skills command (doesn't need daemon)
    if clean.first().map(|s| s.as_str()) == Some("skills") {
        skills::run_skills(&clean, flags.json);
        return;
    }

    // Handle MCP stdio server mode. This must never share stdout with normal
    // CLI output because stdout is reserved for JSON-RPC protocol messages.
    if clean.first().map(|s| s.as_str()) == Some("mcp") {
        if camoufox_default {
            if let Err(error) = native::camoufox::validate_flags(&flags) {
                eprintln!("{} {}", color::error_indicator(), error);
                exit(1);
            }
            env::set_var("AGENT_BROWSER_ENGINE", "camoufox");
            env::set_var("AGENT_BROWSER_SESSION", &flags.session);
            if let Some(profile) = &flags.profile {
                env::set_var("AGENT_BROWSER_PROFILE", profile);
            }
            if let Some(backend) = &flags.input_backend {
                env::set_var("AGENT_BROWSER_INPUT_BACKEND", backend);
            }
            if flags.headed || flags.cli_headed {
                env::set_var("AGENT_BROWSER_HEADED", if flags.headed { "1" } else { "0" });
            }
            if let Some(policy) = &flags.action_policy {
                env::set_var("AGENT_BROWSER_ACTION_POLICY", policy);
            }
            if let Some(actions) = &flags.confirm_actions {
                env::set_var("AGENT_BROWSER_CONFIRM_ACTIONS", actions);
            }
        }
        if let Err(err) = mcp::run_mcp(&clean[1..]) {
            eprintln!("{} {}", color::error_indicator(), err);
            exit(1);
        }
        return;
    }

    // Handle session separately (doesn't need daemon)
    if clean.first().map(|s| s.as_str()) == Some("session") {
        run_session(&clean, &flags.session, flags.json);
        return;
    }

    // Handle close --all: close all active sessions
    if matches!(
        clean.first().map(|s| s.as_str()),
        Some("close") | Some("quit") | Some("exit")
    ) && clean.iter().any(|a| a == "--all")
    {
        run_close_all(&flags);
        return;
    }

    // Handle chat command
    if clean.first().map(|s| s.as_str()) == Some("chat") {
        let message = if clean.len() > 1 {
            Some(clean[1..].join(" "))
        } else {
            None
        };
        chat::run_chat(&flags, message);
        return;
    }

    let mut cmd = match parse_command(&clean, &flags) {
        Ok(c) => c,
        Err(e) => {
            if flags.json {
                let error_type = match &e {
                    ParseError::UnknownCommand { .. } => "unknown_command",
                    ParseError::UnknownSubcommand { .. } => "unknown_subcommand",
                    ParseError::MissingArguments { .. } => "missing_arguments",
                    ParseError::InvalidValue { .. } => "invalid_value",
                    ParseError::InvalidSessionName { .. } => "invalid_session_name",
                };
                print_json_error_with_type(e.format(), error_type);
            } else {
                eprintln!("{}", color::red(&e.format()));
            }
            exit(1);
        }
    };

    // Handle --password-stdin for auth save
    if cmd.get("action").and_then(|v| v.as_str()) == Some("auth_save") {
        if cmd.get("password").is_some() {
            eprintln!(
                "{} Passwords on the command line may be visible in process listings and shell history. Use --password-stdin instead.",
                color::warning_indicator()
            );
        }
        if cmd
            .get("passwordStdin")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            let mut pass = String::new();
            if std::io::stdin().read_line(&mut pass).is_err() || pass.is_empty() {
                eprintln!(
                    "{} Failed to read password from stdin",
                    color::error_indicator()
                );
                exit(1);
            }
            let pass = pass.trim_end_matches('\n').trim_end_matches('\r');
            if pass.is_empty() {
                eprintln!("{} Password from stdin is empty", color::error_indicator());
                exit(1);
            }
            cmd["password"] = json!(pass);
            cmd.as_object_mut().unwrap().remove("passwordStdin");
        }
    }

    // Handle state management commands locally — these are pure file operations
    // that don't need a daemon, avoiding an unnecessary daemon startup that
    // would lack runtime config like session_name.
    if let Some(result) = native::state::dispatch_state_command(&cmd) {
        let action = cmd.get("action").and_then(|v| v.as_str());
        let resp = match result {
            Ok(data) => connection::Response {
                success: true,
                data: Some(data),
                error: None,
                code: None,
                warning: None,
            },
            Err(e) => connection::Response {
                success: false,
                data: None,
                error: Some(e),
                code: None,
                warning: None,
            },
        };
        let output_opts = OutputOptions::from_flags(&flags);
        output::print_response_with_opts(&resp, action, &output_opts);
        if !resp.success {
            exit(1);
        }
        return;
    }

    let daemon_opts = DaemonOptions {
        headed: flags.headed,
        debug: flags.debug,
        profile: flags.profile.as_deref(),
        input_backend: flags.input_backend.as_deref(),
        action_policy: flags.action_policy.as_deref(),
        confirm_actions: flags.confirm_actions.as_deref(),
        engine: Some(effective_engine.as_str()),
        idle_timeout: flags.idle_timeout.as_deref(),
        default_timeout: flags.default_timeout,
    };

    let daemon_result = match ensure_daemon(&flags.session, &daemon_opts) {
        Ok(result) => result,
        Err(e) => {
            if flags.json {
                print_json_error(e);
            } else {
                eprintln!("{} {}", color::error_indicator(), e);
            }
            exit(1);
        }
    };
    let _daemon_was_already_running = daemon_result.already_running;
    let daemon_restarted = daemon_result.restarted;


    // Handle batch command: from args or stdin
    if cmd.get("action").and_then(|v| v.as_str()) == Some("batch") {
        let bail = cmd.get("bail").and_then(|v| v.as_bool()).unwrap_or(false);
        let arg_commands = cmd.get("commands").and_then(|v| v.as_array()).map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str())
                .map(commands::shell_words_split)
                .collect::<Vec<Vec<String>>>()
        });
        run_batch(&flags, &daemon_opts, bail, arg_commands);
        return;
    }

    let output_opts = OutputOptions::from_flags(&flags);

    match send_command_with_respawn(cmd.clone(), &flags.session, &daemon_opts) {
        Ok(mut resp) => {
            if daemon_restarted {
                mark_restarted_background(&mut resp);
            }
            if flags.confirm_interactive && confirmation_prompt_from_response(&resp).is_some() {
                resp = run_interactive_confirmations(resp, &flags, &output_opts);
                if daemon_restarted {
                    mark_restarted_background(&mut resp);
                }
                if !resp.success {
                    exit(1);
                }
                return;
            }
            let success = resp.success;
            // Extract action for context-specific output handling
            let action = cmd.get("action").and_then(|v| v.as_str());
            print_response_with_opts(&resp, action, &output_opts);
            if !success {
                exit(1);
            }
        }
        Err(e) => {
            if flags.json {
                print_json_error(e);
            } else {
                eprintln!("{} {}", color::error_indicator(), e);
            }
            exit(1);
        }
    }
}

/// send_command plus the daemon-shutdown-race recovery: ensure_daemon no
/// longer pays a settle-sleep on every invocation, so a daemon that exited
/// right after its liveness check surfaces as an unreachable socket on the
/// request itself. Respawn once and retry before reporting failure.
fn send_command_with_respawn(
    cmd: serde_json::Value,
    session: &str,
    daemon_opts: &DaemonOptions,
) -> Result<connection::Response, String> {
    let first_attempt = send_command(cmd.clone(), session);
    match first_attempt {
        Err(ref e) if daemon_unreachable(e) => match ensure_daemon(session, daemon_opts) {
            Ok(_) => send_command(cmd, session),
            Err(_) => first_attempt,
        },
        other => other,
    }
}

fn run_batch(
    flags: &Flags,
    daemon_opts: &DaemonOptions,
    bail: bool,
    arg_commands: Option<Vec<Vec<String>>>,
) {
    let commands: Vec<Vec<String>> = if let Some(cmds) = arg_commands {
        cmds
    } else {
        use std::io::Read as _;

        let mut input = String::new();
        if let Err(e) = std::io::stdin().read_to_string(&mut input) {
            if flags.json {
                print_json_error(format!("Failed to read stdin: {}", e));
            } else {
                eprintln!("{} Failed to read stdin: {}", color::error_indicator(), e);
            }
            exit(1);
        }

        match serde_json::from_str(&input) {
            Ok(c) => c,
            Err(e) => {
                if flags.json {
                    print_json_error(format!(
                        "Invalid JSON input: {}. Expected an array of string arrays, e.g. [[\"open\", \"https://example.com\"], [\"snapshot\"]]",
                        e
                    ));
                } else {
                    eprintln!(
                        "{} Invalid JSON input: {}. Expected an array of string arrays.",
                        color::error_indicator(),
                        e
                    );
                }
                exit(1);
            }
        }
    };

    if commands.is_empty() {
        if flags.json {
            println!("[]");
        }
        return;
    }

    let output_opts = OutputOptions::from_flags(flags);

    let mut results: Vec<serde_json::Value> = Vec::new();
    let mut had_error = false;

    for (i, cmd_args) in commands.iter().enumerate() {
        if cmd_args.is_empty() {
            continue;
        }

        let parsed = match parse_command(cmd_args, flags) {
            Ok(c) => c,
            Err(e) => {
                had_error = true;
                if flags.json {
                    results.push(json!({
                        "command": cmd_args,
                        "success": false,
                        "error": e.format(),
                    }));
                    if bail {
                        break;
                    }
                } else {
                    eprintln!(
                        "{} Command {}: {}",
                        color::error_indicator(),
                        i + 1,
                        e.format()
                    );
                    if bail {
                        exit(1);
                    }
                }
                continue;
            }
        };

        let action = parsed
            .get("action")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        match send_command_with_respawn(parsed, &flags.session, daemon_opts) {
            Ok(resp) => {
                if flags.json {
                    let mut result = json!({
                        "command": cmd_args,
                        "success": resp.success,
                        "result": resp.data,
                        "error": resp.error,
                    });
                    // Match the single-command `Response` serialization,
                    // which only emits `code` when set (e.g. `tab_gone`).
                    // Without this, machine-readable error codes are
                    // silently dropped in batch mode.
                    if let Some(ref code) = resp.code {
                        result["code"] = json!(code);
                    }
                    // Mirror the single-command serialization: emit `warning`
                    // too, not just `code`.
                    if let Some(ref warning) = resp.warning {
                        result["warning"] = json!(warning);
                    }
                    results.push(result);
                } else {
                    if i > 0 {
                        println!();
                    }
                    print_response_with_opts(&resp, action.as_deref(), &output_opts);
                }
                if !resp.success {
                    had_error = true;
                    if bail {
                        if !flags.json {
                            exit(1);
                        }
                        break;
                    }
                }
            }
            Err(e) => {
                had_error = true;
                if flags.json {
                    results.push(json!({
                        "command": cmd_args,
                        "success": false,
                        "error": e.to_string(),
                    }));
                    if bail {
                        break;
                    }
                } else {
                    eprintln!("{} Command {}: {}", color::error_indicator(), i + 1, e);
                    if bail {
                        exit(1);
                    }
                }
            }
        }
    }

    if flags.json {
        println!(
            "{}",
            serde_json::to_string(&results).unwrap_or_else(|_| "[]".to_string())
        );
    }

    if had_error {
        exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_serialize_json_value_escapes_control_characters() {
        let payload = serialize_json_value(&json!({
            "success": false,
            "error": "Daemon process exited during startup:\nline \"quoted\"\u{001b}[2mansi\u{001b}[22m",
        }));

        let parsed: serde_json::Value = serde_json::from_str(&payload).unwrap();
        assert_eq!(parsed["success"], false);
        assert_eq!(
            parsed["error"],
            "Daemon process exited during startup:\nline \"quoted\"\u{001b}[2mansi\u{001b}[22m"
        );
    }





    #[test]
    fn test_resolve_session_id_scope_accepts_cwd_and_rejects_unknown() {
        let (scope, path) = resolve_session_id_scope("cwd").unwrap();

        assert_eq!(scope, "cwd");
        assert!(path.is_absolute());
        assert!(resolve_session_id_scope("branch").is_err());
    }

    #[test]
    fn test_confirmation_prompt_from_response_finds_nested_confirm_result() {
        let resp = Response {
            success: true,
            code: None,
            data: Some(json!({
                "confirmed": true,
                "action": "navigate",
                "result": {
                    "id": "original-command",
                    "success": true,
                    "data": {
                        "confirmation_required": true,
                        "confirmation_id": "original-command",
                        "action": "test:action:launch.mutate"
                    }
                }
            })),
            error: None,
            warning: None,
        };

        let prompt = confirmation_prompt_from_response(&resp).unwrap();

        assert_eq!(prompt.action, "test:action:launch.mutate");
        assert_eq!(prompt.description, "test:action:launch.mutate");
        assert_eq!(prompt.confirmation_id, "original-command");
    }
}
