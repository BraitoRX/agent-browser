use serde_json::{json, Value};
use std::env;
use std::fs;
use std::path::PathBuf;
use std::sync::Arc;

use crate::connection::{get_socket_dir, INTERNAL_DAEMON_SHUTDOWN_ACTION};

use super::idle::IdleActivity;
use super::policy::{ActionPolicy, ConfirmActions, PolicyResult};

pub struct PendingConfirmation {
    pub action: String,
    pub cmd: Value,
    pub approved_actions: Vec<String>,
}

pub struct DaemonState {
    /// Camoufox Python worker; the only browser backend.
    pub camoufox: Option<super::camoufox::CamoufoxBackend>,
    pub session_name: Option<String>,
    /// When the most recent browser-touching command finished. Periodic
    /// autosaves wait for a quiet period after this so a multi-second save
    /// never lands in the middle of an active command burst.
    pub last_command_finished: Option<std::time::Instant>,
    pub session_id: String,
    pub policy: Option<ActionPolicy>,
    pub pending_confirmation: Option<PendingConfirmation>,
    pub confirm_actions: Option<ConfirmActions>,
    /// Daemon-owned activity clock shared by commands and stream servers.
    pub idle_activity: Arc<IdleActivity>,
    /// Browser engine name for observability.
    pub engine: String,
    /// Default timeout for wait operations, from AGENT_BROWSER_DEFAULT_TIMEOUT env var.
    /// Actions already approved while replaying a confirmed command.
    pub confirmed_policy_actions: Vec<String>,
}

impl DaemonState {
    pub fn new() -> Self {
        let session_id =
            env::var("AGENT_BROWSER_SESSION").unwrap_or_else(|_| "default".to_string());
        Self {
            camoufox: None,
            session_name: env::var("AGENT_BROWSER_SESSION_NAME").ok(),
            last_command_finished: None,
            session_id,
            policy: ActionPolicy::load_if_exists(),
            pending_confirmation: None,
            confirm_actions: ConfirmActions::from_env(),
            idle_activity: Arc::new(IdleActivity::new()),
            engine: env::var("AGENT_BROWSER_ENGINE").unwrap_or_else(|_| "camoufox".to_string()),
            confirmed_policy_actions: Vec::new(),
        }
    }

    pub fn new_with_idle(idle_activity: Arc<IdleActivity>) -> Self {
        let mut s = Self::new();
        s.idle_activity = idle_activity;
        s
    }

    /// True when the default idle timeout must not shut down this session:
    /// never pull a headed browser out from under a human.
    pub(crate) fn blocks_default_idle_shutdown(&self) -> bool {
        self.camoufox
            .as_ref()
            .is_some_and(|backend| backend.headed)
    }

    fn engine_file_path(session_id: &str) -> PathBuf {
        get_socket_dir().join(format!("{}.engine", session_id))
    }

    fn write_engine_file(session_id: &str, engine: &str) {
        let _ = fs::write(Self::engine_file_path(session_id), engine);
    }
}

impl Drop for DaemonState {
    fn drop(&mut self) {}
}

pub(crate) async fn close_current_browser(state: &mut DaemonState) -> Result<(), String> {
    if let Some(mut backend) = state.camoufox.take() {
        backend.close().await;
    }
    Ok(())
}

/// Close every browser backend owned by the daemon.
pub(crate) async fn close_all_browser_backends(state: &mut DaemonState) -> Result<(), String> {
    close_current_browser(state).await
}

async fn handle_close(state: &mut DaemonState) -> Result<Value, String> {
    close_all_browser_backends(state).await?;
    Ok(json!({ "closed": true }))
}

fn skip_launch_action(action: &str) -> bool {
    if action == INTERNAL_DAEMON_SHUTDOWN_ACTION {
        return true;
    }

    matches!(
        action,
        "" | "close"
            | "hover_hold_stop"
            | "confirm"
            | "deny"
            | "session_info"
            | "gestures"
            | "gesture"
    )
}

fn policy_actions_for_command(cmd: &Value, action: &str) -> Vec<String> {
    let mut actions = vec![action.to_string()];
    // `a11y <url>` performs a real browser navigation before the audit. Keep
    // navigation deny and confirmation policies effective for the compound
    // command instead of treating it as a read-only audit.
    if action == "a11y" && cmd.get("url").and_then(|v| v.as_str()).is_some() {
        actions.push("navigate".to_string());
    }
    actions
}

async fn execute_camoufox_command(cmd: &Value, state: &mut DaemonState) -> Value {
    use super::camoufox::{
        failure, normalize_command, requested_profile, CamoufoxBackend, BUBBLE_PROFILE_DIR,
        HARD_DEADLINE,
    };
    let id = cmd.get("id").and_then(Value::as_str).unwrap_or("");
    let action = cmd.get("action").and_then(Value::as_str).unwrap_or("");
    if action == "close" {
        return match handle_close(state).await {
            Ok(data) => success_response(id, data),
            Err(error) => error_response(id, &error),
        };
    }
    let mut command = match normalize_command(cmd) {
        Ok(command) => command,
        Err(error) => return failure(id, "camoufox_unsupported", &error, false),
    };
    let profile = match requested_profile(cmd) {
        Ok(profile) => profile,
        Err(error) => return failure(id, "camoufox_invalid_params", &error, false),
    };
    state.engine = "camoufox".to_string();
    DaemonState::write_engine_file(&state.session_id, &state.engine);
    if state.camoufox.is_none() {
        match CamoufoxBackend::spawn() {
            Ok(backend) => state.camoufox = Some(backend),
            Err(error) => return failure(id, "camoufox_not_launched", &error, false),
        }
    }
    let backend = state
        .camoufox
        .as_mut()
        .expect("Camoufox worker was just created");
    let effective_profile = if backend.bubble && profile.is_some() {
        Some(BUBBLE_PROFILE_DIR.to_string())
    } else {
        profile.clone()
    };
    if backend.launched
        && effective_profile != backend.profile
        && !matches!(action, "session_info" | "gestures")
    {
        return failure(id, "camoufox_invalid_params", "Close the session before changing the Camoufox profile; the current profile has not been modified", false);
    }
    let requested_headless = cmd.get("headless").and_then(Value::as_bool);
    if backend.launched && requested_headless.is_some_and(|headless| headless == backend.headed) {
        return failure(
            id,
            "camoufox_unsupported",
            "Close the session before changing headed mode",
            false,
        );
    }
    let headless = requested_headless.unwrap_or_else(|| {
        if backend.launched {
            !backend.headed
        } else {
            !env::var("AGENT_BROWSER_HEADED")
                .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "yes"))
        }
    });
    let requested_adblock = cmd.get("adblock").and_then(Value::as_bool);
    if backend.launched && requested_adblock.is_some_and(|adblock| adblock != backend.adblock) {
        return failure(
            id,
            "camoufox_unsupported",
            "Close the session before changing the adblock setting",
            false,
        );
    }
    let adblock = requested_adblock.unwrap_or_else(|| {
        if backend.launched {
            backend.adblock
        } else {
            env::var("AGENT_BROWSER_ADBLOCK")
                .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "yes"))
        }
    });
    if action == "launch" {
        command["headless"] = json!(headless);
        command["profile"] = json!(profile);
        command["adblock"] = json!(adblock);
    }
    let outcome = tokio::time::timeout(HARD_DEADLINE, async {
        if !backend.launched
            && !matches!(
                action,
                "launch" | "session_info" | "gestures" | "tab_list" | "read" | "hover_hold_stop"
            )
        {
            let mut launched = backend.execute(&json!({"id": format!("{id}:launch"), "action": "launch", "engine": "camoufox", "headless": headless, "profile": profile, "adblock": adblock})).await;
            if launched.get("success").and_then(Value::as_bool) != Some(true) {
                launched["id"] = json!(id);
                return launched;
            }
        }
        let mut response = backend.execute(&command).await;
        if action == "session_info" && backend.bubble {
            if let Some(port) = backend.vnc_port {
                if response.get("data").is_none() {
                    response["data"] = json!({});
                }
                response["data"]["vncUrl"] = json!(format!(
                    "http://127.0.0.1:{port}/vnc.html?autoconnect=1&quality=6&compression=0&resize=scale&reconnect=1&view_only=1"
                ));
                if let Some(name) = &backend.container_name {
                    response["data"]["vncDomainUrl"] = json!(format!(
                        "https://{name}.orb.local/vnc.html?autoconnect=1&quality=6&compression=0&resize=scale&reconnect=1&view_only=1"
                    ));
                }
            }
        }
        response
    })
    .await;
    match outcome {
        Ok(response) => response,
        Err(_) => {
            let reason = "Camoufox command exceeded 28s including startup. Input outcome is ambiguous; no replay. Close the session to recover.";
            backend.poison(reason);
            failure(id, "camoufox_session_reset_required", reason, true)
        }
    }
}

pub async fn execute_command(cmd: &Value, state: &mut DaemonState) -> Value {
    let action = cmd.get("action").and_then(|v| v.as_str()).unwrap_or("");
    let id = cmd
        .get("id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    let requested_engine = cmd.get("engine").and_then(Value::as_str);
    let use_camoufox =
        state.camoufox.is_some() || requested_engine.unwrap_or(&state.engine) == "camoufox";
    if use_camoufox
        && !matches!(
            action,
            "close" | "confirm" | "deny" | INTERNAL_DAEMON_SHUTDOWN_ACTION
        )
    {
        if requested_engine.is_some_and(|engine| engine != "camoufox") {
            return super::camoufox::failure(
                &id,
                "camoufox_unsupported",
                "Close the current session before changing browser engines",
                false,
            );
        }
        let validation = super::camoufox::validate_environment()
            .and_then(|_| super::camoufox::normalize_command(cmd).map(|_| ()));
        if let Err(error) = validation {
            return super::camoufox::failure(&id, "camoufox_unsupported", &error, false);
        }
        if state.session_name.is_some() {
            return super::camoufox::failure(
                &id,
                "camoufox_unsupported",
                "Camoufox V1 cannot enforce restore settings",
                false,
            );
        }
        if action == "gesture" && (state.policy.is_some() || state.confirm_actions.is_some()) {
            return super::camoufox::failure(&id, "camoufox_unsupported", "Generic gestures are disabled when action policies or confirm-actions are active; use typed actions", false);
        }
    }

    let policy_actions = policy_actions_for_command(cmd, action);

    // Hot-reload and check action policy
    if let Some(ref mut policy) = state.policy {
        let _ = policy.reload();
        let mut confirmation_required: Option<String> = None;
        for policy_action in &policy_actions {
            match policy.check(policy_action) {
                PolicyResult::Allow => {}
                PolicyResult::Deny(reason) => {
                    return error_response(
                        &id,
                        &format!("Action '{}' denied by policy: {}", policy_action, reason),
                    );
                }
                PolicyResult::RequiresConfirmation => {
                    if !state.confirmed_policy_actions.contains(policy_action)
                        && confirmation_required.is_none()
                    {
                        confirmation_required = Some(policy_action.to_string());
                    }
                }
            }
        }
        if let Some(policy_action) = confirmation_required {
            state.pending_confirmation = Some(PendingConfirmation {
                action: policy_action.clone(),
                cmd: cmd.clone(),
                approved_actions: state.confirmed_policy_actions.clone(),
            });
            return json!({
                "id": id,
                "success": true,
                "data": {
                    "confirmation_required": true,
                    "confirmation_id": id,
                    "action": policy_action
                },
            });
        }
    }

    // Check AGENT_BROWSER_CONFIRM_ACTIONS (category-based, independent of policy file)
    if action != "confirm" && action != "deny" {
        if let Some(ref ca) = state.confirm_actions {
            for policy_action in &policy_actions {
                if state.confirmed_policy_actions.contains(policy_action) {
                    continue;
                }
                if ca.requires_confirmation(policy_action) {
                    state.pending_confirmation = Some(PendingConfirmation {
                        action: policy_action.to_string(),
                        cmd: cmd.clone(),
                        approved_actions: state.confirmed_policy_actions.clone(),
                    });
                    return json!({
                        "id": id,
                        "success": true,
                        "data": {
                            "confirmation_required": true,
                            "confirmation_id": id,
                            "action": policy_action,
                        },
                    });
                }
            }
        }
    }

    if use_camoufox && !matches!(action, "confirm" | "deny") {
        let mut response = execute_camoufox_command(cmd, state).await;
        if let Some(data) = response.get_mut("data").and_then(Value::as_object_mut) {
            data.insert("engine".to_string(), json!("camoufox"));
        }
        state.last_command_finished = Some(std::time::Instant::now());
        return response;
    }

    let result = match action {
        "close" => handle_close(state).await,
        "confirm" => handle_confirm(cmd, state).await,
        "deny" => handle_deny(cmd, state).await,
        _ => Err(format!("Not yet implemented: {}", action)),
    };

    if !skip_launch_action(action) {
        state.last_command_finished = Some(std::time::Instant::now());
    }

    match result {
        Ok(data) => success_response(&id, data),
        Err(e) => error_response(&id, &e),
    }
}

fn success_response(id: &str, data: Value) -> Value {
    json!({
        "id": id,
        "success": true,
        "data": data,
    })
}

fn error_response(id: &str, error: &str) -> Value {
    let mut resp = json!({
        "id": id,
        "success": false,
        "error": error,
    });
    if let Some((code, _)) = error.split_once(": ") {
        if code.starts_with("webmcp_") {
            resp["code"] = json!(code);
        }
    }
    resp
}

async fn handle_confirm(_cmd: &Value, state: &mut DaemonState) -> Result<Value, String> {
    let pending = state
        .pending_confirmation
        .take()
        .ok_or("No pending confirmation")?;

    let mut approved_actions = pending.approved_actions.clone();
    if !approved_actions.iter().any(|a| a == &pending.action) {
        approved_actions.push(pending.action.clone());
    }
    let previous_confirmed = std::mem::replace(
        &mut state.confirmed_policy_actions,
        approved_actions.into_iter().collect(),
    );
    let result = Box::pin(execute_command(&pending.cmd, state)).await;
    state.confirmed_policy_actions = previous_confirmed;

    Ok(json!({ "confirmed": true, "action": pending.action, "result": result }))
}

async fn handle_deny(_cmd: &Value, state: &mut DaemonState) -> Result<Value, String> {
    let pending = state
        .pending_confirmation
        .take()
        .ok_or("No pending confirmation")?;

    Ok(json!({ "denied": true, "action": pending.action }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn camoufox_hover_hold_stop_skips_implicit_launch_policy_actions() {
        assert!(skip_launch_action("hover_hold_stop"));
        let command = json!({"id": "stop", "action": "hover_hold_stop"});
        assert_eq!(
            policy_actions_for_command(&command, "hover_hold_stop"),
            vec!["hover_hold_stop".to_string()],
        );
    }
}