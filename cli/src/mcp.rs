//! Stdio MCP server for exposing agent-browser to MCP clients.
//!
//! The server keeps stdout exclusively for newline-delimited JSON-RPC
//! messages. Tool calls are delegated to the current binary in `--json` mode
//! so MCP behavior stays aligned with the normal CLI command surface. Daemon
//! lifecycle settings, including the default idle timeout, use the same CLI
//! parser and daemon as direct commands.
//! Owned Windows Chrome uses the same private headless desktop and Job Object
//! lifetime through MCP; headed and external-connection semantics are unchanged.

use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::env;
use std::fs;
use std::io::{self, BufRead, Read, Write};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const PROTOCOL_VERSION: &str = "2025-11-25";
const SUPPORTED_PROTOCOL_VERSIONS: &[&str] =
    &["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];
const TOOL_LIST_PAGE_SIZE: usize = 64;
const TOOL_OPEN: &str = "agent_browser_open";
const TOOL_READ: &str = "agent_browser_read";
const TOOL_PAGE_OUTLINE: &str = "agent_browser_page_outline";
const TOOL_PAGE_LINKS: &str = "agent_browser_page_links";
const TOOL_DOM_CHUNK: &str = "agent_browser_dom_chunk";
const TOOL_BACK: &str = "agent_browser_back";
const TOOL_FORWARD: &str = "agent_browser_forward";
const TOOL_RELOAD: &str = "agent_browser_reload";
const TOOL_SNAPSHOT: &str = "agent_browser_snapshot";
const TOOL_CLICK: &str = "agent_browser_click";
const TOOL_DBLCLICK: &str = "agent_browser_dblclick";
const TOOL_FILL: &str = "agent_browser_fill";
const TOOL_TYPE: &str = "agent_browser_type";
const TOOL_PRESS: &str = "agent_browser_press";
const TOOL_HOVER: &str = "agent_browser_hover";
const TOOL_HOVER_HOLD: &str = "agent_browser_hover_hold";
const TOOL_FOCUS: &str = "agent_browser_focus";
const TOOL_CHECK: &str = "agent_browser_check";
const TOOL_UNCHECK: &str = "agent_browser_uncheck";
const TOOL_SELECT: &str = "agent_browser_select";
const TOOL_DRAG: &str = "agent_browser_drag";
const TOOL_UPLOAD: &str = "agent_browser_upload";
const TOOL_DOWNLOAD: &str = "agent_browser_download";
const TOOL_DOWNLOADS: &str = "agent_browser_downloads";
const TOOL_SCROLL: &str = "agent_browser_scroll";
const TOOL_SCROLL_INTO_VIEW: &str = "agent_browser_scroll_into_view";
const TOOL_WAIT_MS: &str = "agent_browser_wait_ms";
const TOOL_WAIT_FOR_SELECTOR: &str = "agent_browser_wait_for_selector";
const TOOL_WAIT_FOR_TEXT: &str = "agent_browser_wait_for_text";
const TOOL_WAIT_FOR_URL: &str = "agent_browser_wait_for_url";
const TOOL_WAIT_FOR_LOAD: &str = "agent_browser_wait_for_load";
const TOOL_WAIT_FOR_FUNCTION: &str = "agent_browser_wait_for_function";
const TOOL_WAIT_FOR_DOWNLOAD: &str = "agent_browser_wait_for_download";
const TOOL_SCREENSHOT: &str = "agent_browser_screenshot";
const TOOL_GET_TEXT: &str = "agent_browser_get_text";
const TOOL_GET_HTML: &str = "agent_browser_get_html";
const TOOL_HTML_SEARCH: &str = "agent_browser_html_search";
const TOOL_GET_VALUE: &str = "agent_browser_get_value";
const TOOL_GET_ATTR: &str = "agent_browser_get_attr";
const TOOL_GET_COUNT: &str = "agent_browser_get_count";
const TOOL_GET_BOX: &str = "agent_browser_get_box";
const TOOL_GET_URL: &str = "agent_browser_get_url";
const TOOL_GET_TITLE: &str = "agent_browser_get_title";
const TOOL_IS_VISIBLE: &str = "agent_browser_is_visible";
const TOOL_IS_ENABLED: &str = "agent_browser_is_enabled";
const TOOL_IS_CHECKED: &str = "agent_browser_is_checked";
const TOOL_FIND: &str = "agent_browser_find";
const TOOL_SET_OFFLINE: &str = "agent_browser_set_offline";
const TOOL_SET_HEADERS: &str = "agent_browser_set_headers";
const TOOL_SET_CREDENTIALS: &str = "agent_browser_set_credentials";
const TOOL_NETWORK_ROUTE: &str = "agent_browser_network_route";
const TOOL_NETWORK_UNROUTE: &str = "agent_browser_network_unroute";
const TOOL_NETWORK_REQUESTS: &str = "agent_browser_network_requests";
const TOOL_NETWORK_REQUEST: &str = "agent_browser_network_request";
const TOOL_NETWORK_WEBSOCKETS: &str = "agent_browser_network_websockets";
const TOOL_NETWORK_HAR_START: &str = "agent_browser_network_har_start";
const TOOL_NETWORK_HAR_STOP: &str = "agent_browser_network_har_stop";
const TOOL_STORAGE_GET: &str = "agent_browser_storage_get";
const TOOL_STORAGE_SET: &str = "agent_browser_storage_set";
const TOOL_STORAGE_CLEAR: &str = "agent_browser_storage_clear";
const TOOL_COOKIES_GET: &str = "agent_browser_cookies_get";
const TOOL_COOKIES_SET: &str = "agent_browser_cookies_set";
const TOOL_COOKIES_CLEAR: &str = "agent_browser_cookies_clear";
const TOOL_TAB_NEW: &str = "agent_browser_tab_new";
const TOOL_TAB_LIST: &str = "agent_browser_tab_list";
const TOOL_TAB_SWITCH: &str = "agent_browser_tab_switch";
const TOOL_TAB_CLOSE: &str = "agent_browser_tab_close";
const TOOL_FRAME_SWITCH: &str = "agent_browser_frame_switch";
const TOOL_FRAME_MAIN: &str = "agent_browser_frame_main";
const TOOL_DIALOG_STATUS: &str = "agent_browser_dialog_status";
const TOOL_DIALOG_ACCEPT: &str = "agent_browser_dialog_accept";
const TOOL_DIALOG_DISMISS: &str = "agent_browser_dialog_dismiss";
const TOOL_CONSOLE: &str = "agent_browser_console";
const TOOL_ERRORS: &str = "agent_browser_errors";
const TOOL_STATE_LIST: &str = "agent_browser_state_list";
const TOOL_STATE_CLEAR: &str = "agent_browser_state_clear";
const TOOL_STATE_SHOW: &str = "agent_browser_state_show";
const TOOL_STATE_CLEAN: &str = "agent_browser_state_clean";
const TOOL_STATE_RENAME: &str = "agent_browser_state_rename";
const TOOL_BATCH: &str = "agent_browser_batch";
const TOOL_CONFIRM: &str = "agent_browser_confirm";
const TOOL_DENY: &str = "agent_browser_deny";
const TOOL_SESSION_LIST: &str = "agent_browser_session_list";
const TOOL_SESSION_INFO: &str = "agent_browser_session_info";
const TOOL_SKILLS_LIST: &str = "agent_browser_skills_list";
const TOOL_SKILLS_GET: &str = "agent_browser_skills_get";
const TOOL_SKILLS_PATH: &str = "agent_browser_skills_path";
const TOOL_DOCTOR: &str = "agent_browser_doctor";
const TOOL_INSTALL: &str = "agent_browser_install";
const TOOL_GESTURES: &str = "agent_browser_gestures";
const TOOL_GESTURE: &str = "agent_browser_gesture";
const TOOL_CHAT: &str = "agent_browser_chat";
const TOOL_EVAL: &str = "agent_browser_eval";
const TOOL_CLOSE: &str = "agent_browser_close";
const TOOL_TOOLS_PROFILES: &str = "agent_browser_tools_profiles";
const DEFAULT_TIMEOUT_MS: u64 = 120_000;
const MAX_IMAGE_BYTES: u64 = 10 * 1024 * 1024;

#[derive(Debug)]
struct ProtocolError {
    code: i64,
    message: String,
}

impl ProtocolError {
    fn invalid_params(message: impl Into<String>) -> Self {
        Self {
            code: -32602,
            message: message.into(),
        }
    }

    fn method_not_found(method: &str) -> Self {
        Self {
            code: -32601,
            message: format!("Method not found: {}", method),
        }
    }
}

#[derive(Debug)]
struct CliRun {
    exit_code: Option<i32>,
    stdout: String,
    stderr: String,
}

#[derive(Debug, Clone)]
struct McpConfig {
    profiles: Vec<ToolProfile>,
    enabled_tools: Option<BTreeSet<&'static str>>,
    camoufox: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ToolProfile {
    Core,
    Network,
    State,
    Debug,
    Tabs,
    Mobile,
    Webmcp,
    Gestures,
    All,
}

impl ToolProfile {
    fn parse(name: &str) -> Option<Self> {
        match name {
            "core" | "default" => Some(Self::Core),
            "network" => Some(Self::Network),
            "state" | "storage" | "auth" => Some(Self::State),
            "debug" | "diagnostics" => Some(Self::Debug),
            "tabs" | "frames" => Some(Self::Tabs),
            "mobile" | "ios" => Some(Self::Mobile),
            "webmcp" => Some(Self::Webmcp),
            "gestures" => Some(Self::Gestures),
            "all" | "full" => Some(Self::All),
            _ => None,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Core => "core",
            Self::Network => "network",
            Self::State => "state",
            Self::Debug => "debug",
            Self::Tabs => "tabs",
            Self::Mobile => "mobile",
            Self::Webmcp => "webmcp",
            Self::Gestures => "gestures",
            Self::All => "all",
        }
    }

    fn description(self) -> &'static str {
        match self {
            Self::Core => "Everyday browser automation with navigation, snapshots, common interaction, waits, screenshots, basic reads, tab basics, JavaScript eval, close, and profile discovery.",
            Self::Network => "Network interception, request inspection, HAR capture, headers, credentials, and offline mode.",
            Self::State => "Cookies, storage, auth profiles, saved browser state, sessions, and bundled skills.",
            Self::Debug => "Console/errors, highlighting, tracing, profiling, accessibility audits, PDF, downloads/uploads, clipboard, doctor, install, upgrade, and chat.",
            Self::Tabs => "Tab, window, frame, and JavaScript dialog management.",
            Self::Mobile => "Viewport/device/geolocation/media emulation plus touch, swipe, and lower-level mouse tools.",
            Self::Webmcp => "No tools from this profile are currently exposed.",
            Self::Gestures => "Camoufox gesture discovery and execution, runtime installation, session information, and bundled skills. Combine with core for normal browsing.",
            Self::All => "Every MCP tool, including the full typed CLI parity surface.",
        }
    }

    fn tools(self) -> &'static [&'static str] {
        match self {
            Self::Core => CORE_PROFILE_TOOLS,
            Self::Network => NETWORK_PROFILE_TOOLS,
            Self::State => STATE_PROFILE_TOOLS,
            Self::Debug => DEBUG_PROFILE_TOOLS,
            Self::Tabs => TABS_PROFILE_TOOLS,
            Self::Mobile => MOBILE_PROFILE_TOOLS,
            Self::Webmcp => WEBMCP_PROFILE_TOOLS,
            Self::Gestures => GESTURES_PROFILE_TOOLS,
            Self::All => &[],
        }
    }
}

impl McpConfig {
    fn from_profiles(profiles: Vec<ToolProfile>) -> Self {
        Self::from_profiles_for_engine(profiles, false)
    }

    fn from_profiles_for_engine(profiles: Vec<ToolProfile>, camoufox: bool) -> Self {
        if profiles.contains(&ToolProfile::All) {
            return Self {
                profiles: vec![ToolProfile::All],
                enabled_tools: None,
                camoufox,
            };
        }

        let mut enabled_tools = BTreeSet::new();
        for profile in &profiles {
            enabled_tools.extend(profile.tools().iter().copied());
        }
        if camoufox && profiles.contains(&ToolProfile::Core) {
            enabled_tools.extend(CAMOUFOX_CORE_TOOLS.iter().copied());
        }

        Self {
            profiles,
            enabled_tools: Some(enabled_tools),
            camoufox,
        }
    }

    fn core() -> Self {
        Self::from_profiles(vec![ToolProfile::Core])
    }

    #[cfg(test)]
    fn all() -> Self {
        Self::from_profiles(vec![ToolProfile::All])
    }

    fn allows(&self, name: &str) -> bool {
        if self.camoufox && !is_camoufox_tool(name) {
            return false;
        }
        if !self.camoufox && is_camoufox_page_tool(name) {
            return false;
        }
        match &self.enabled_tools {
            Some(enabled_tools) => enabled_tools.contains(name),
            None => true,
        }
    }

    fn profile_names(&self) -> Vec<&'static str> {
        self.profiles.iter().map(|profile| profile.name()).collect()
    }

    fn profile_description(&self, profile: ToolProfile) -> &'static str {
        if !self.camoufox {
            return profile.description();
        }
        match profile {
            ToolProfile::Core => "Everyday Camoufox automation with navigation, native AI snapshots, semantic locators (find), DOM reads and interaction, full-page HTML reads, waits, viewport and full-page PNG screenshots, tab basics, isolated-world JavaScript eval, close, and profile discovery.",
            ToolProfile::Network => "Camoufox network interception, request inspection, HAR capture, headers, credentials, and offline mode.",
            ToolProfile::State => "Camoufox cookies, storage, session diagnostics, and bundled skills.",
            ToolProfile::Debug => "Camoufox download handling, console and error reads, batched commands, runtime installation, and pending-action confirm/deny.",
            ToolProfile::Tabs => "Camoufox tab and frame-scope management plus JavaScript dialog status and dialog arming.",
            ToolProfile::Mobile => "No tools from this profile are available on Camoufox.",
            ToolProfile::Webmcp => "No tools from this profile are available on Camoufox.",
            ToolProfile::Gestures => "Camoufox gesture discovery and execution, runtime installation, session information, and bundled skills. Combine with core for normal browsing.",
            ToolProfile::All => "Every tool that Camoufox V1 supports through MCP, including network inspection and state management.",
        }
    }

    fn profile_tool_count(&self, profile: ToolProfile) -> usize {
        if !self.camoufox {
            return if profile == ToolProfile::All {
                tools()
                    .iter()
                    .filter(|tool| {
                        tool.get("name")
                            .and_then(|name| name.as_str())
                            .is_some_and(|name| !is_camoufox_page_tool(name))
                    })
                    .count()
            } else {
                profile.tools().len()
            };
        }
        if profile == ToolProfile::All {
            return tools()
                .iter()
                .filter(|tool| {
                    tool.get("name")
                        .and_then(|name| name.as_str())
                        .is_some_and(is_camoufox_tool)
                })
                .count();
        }
        let mut count = profile
            .tools()
            .iter()
            .filter(|name| is_camoufox_tool(name))
            .count();
        if profile == ToolProfile::Core {
            count += CAMOUFOX_CORE_TOOLS.len();
        }
        count
    }
}

impl Default for McpConfig {
    fn default() -> Self {
        Self::core()
    }
}

const CORE_PROFILE_TOOLS: &[&str] = &[
    TOOL_TOOLS_PROFILES,
    TOOL_OPEN,
    TOOL_READ,
    TOOL_SNAPSHOT,
    TOOL_BACK,
    TOOL_FORWARD,
    TOOL_RELOAD,
    TOOL_CLICK,
    TOOL_FILL,
    TOOL_TYPE,
    TOOL_PRESS,
    TOOL_CHECK,
    TOOL_UNCHECK,
    TOOL_SELECT,
    TOOL_SCROLL,
    TOOL_WAIT_MS,
    TOOL_WAIT_FOR_SELECTOR,
    TOOL_WAIT_FOR_TEXT,
    TOOL_WAIT_FOR_LOAD,
    TOOL_SCREENSHOT,
    TOOL_GET_TEXT,
    TOOL_HTML_SEARCH,
    TOOL_GET_URL,
    TOOL_GET_TITLE,
    TOOL_TAB_NEW,
    TOOL_TAB_LIST,
    TOOL_TAB_SWITCH,
    TOOL_TAB_CLOSE,
    TOOL_EVAL,
    TOOL_CLOSE,
];

const GESTURES_PROFILE_TOOLS: &[&str] = &[
    TOOL_GESTURES,
    TOOL_GESTURE,
    TOOL_INSTALL,
    TOOL_SESSION_INFO,
    TOOL_SKILLS_LIST,
    TOOL_SKILLS_GET,
];

const WEBMCP_PROFILE_TOOLS: &[&str] = &[];

const NETWORK_PROFILE_TOOLS: &[&str] = &[
    TOOL_SET_HEADERS,
    TOOL_SET_CREDENTIALS,
    TOOL_SET_OFFLINE,
    TOOL_NETWORK_ROUTE,
    TOOL_NETWORK_UNROUTE,
    TOOL_NETWORK_REQUESTS,
    TOOL_NETWORK_REQUEST,
    TOOL_NETWORK_WEBSOCKETS,
    TOOL_NETWORK_HAR_START,
    TOOL_NETWORK_HAR_STOP,
];

const STATE_PROFILE_TOOLS: &[&str] = &[
    TOOL_STORAGE_GET,
    TOOL_STORAGE_SET,
    TOOL_STORAGE_CLEAR,
    TOOL_COOKIES_GET,
    TOOL_COOKIES_SET,
    TOOL_COOKIES_CLEAR,
    TOOL_STATE_LIST,
    TOOL_STATE_CLEAR,
    TOOL_STATE_SHOW,
    TOOL_STATE_CLEAN,
    TOOL_STATE_RENAME,
    TOOL_SESSION_LIST,
    TOOL_SESSION_INFO,
    TOOL_SKILLS_LIST,
    TOOL_SKILLS_GET,
    TOOL_SKILLS_PATH,
];

const DEBUG_PROFILE_TOOLS: &[&str] = &[
    TOOL_WAIT_FOR_DOWNLOAD,
    TOOL_UPLOAD,
    TOOL_DOWNLOAD,
    TOOL_DOWNLOADS,
    TOOL_CONSOLE,
    TOOL_ERRORS,
    TOOL_BATCH,
    TOOL_CONFIRM,
    TOOL_DENY,
    TOOL_DOCTOR,
    TOOL_INSTALL,
    TOOL_CHAT,
];

const TABS_PROFILE_TOOLS: &[&str] = &[
    TOOL_BACK,
    TOOL_FORWARD,
    TOOL_RELOAD,
    TOOL_TAB_NEW,
    TOOL_TAB_LIST,
    TOOL_TAB_SWITCH,
    TOOL_TAB_CLOSE,
    TOOL_FRAME_SWITCH,
    TOOL_FRAME_MAIN,
    TOOL_DIALOG_STATUS,
    TOOL_DIALOG_ACCEPT,
    TOOL_DIALOG_DISMISS,
];

const MOBILE_PROFILE_TOOLS: &[&str] = &[];

const CAMOUFOX_CORE_TOOLS: &[&str] = &[
    TOOL_FIND,
    TOOL_GET_HTML,
    TOOL_HTML_SEARCH,
    TOOL_SCROLL_INTO_VIEW,
    TOOL_GET_ATTR,
    TOOL_GET_VALUE,
    TOOL_GET_COUNT,
    TOOL_GET_BOX,
    TOOL_IS_VISIBLE,
    TOOL_IS_ENABLED,
    TOOL_IS_CHECKED,
    TOOL_HOVER,
    TOOL_HOVER_HOLD,
    TOOL_FOCUS,
    TOOL_DBLCLICK,
    TOOL_WAIT_FOR_URL,
    TOOL_WAIT_FOR_FUNCTION,
];

const CAMOUFOX_BROWSER_TOOLS: &[&str] = &[
    TOOL_FRAME_SWITCH,
    TOOL_FRAME_MAIN,
    TOOL_OPEN,
    TOOL_READ,
    TOOL_SNAPSHOT,
    TOOL_BACK,
    TOOL_FORWARD,
    TOOL_RELOAD,
    TOOL_CLICK,
    TOOL_FILL,
    TOOL_TYPE,
    TOOL_PRESS,
    TOOL_CHECK,
    TOOL_UNCHECK,
    TOOL_SELECT,
    TOOL_DRAG,
    TOOL_SCROLL,
    TOOL_DOWNLOAD,
    TOOL_DOWNLOADS,
    TOOL_WAIT_MS,
    TOOL_WAIT_FOR_SELECTOR,
    TOOL_WAIT_FOR_TEXT,
    TOOL_WAIT_FOR_LOAD,
    TOOL_WAIT_FOR_DOWNLOAD,
    TOOL_SCREENSHOT,
    TOOL_GET_TEXT,
    TOOL_GET_URL,
    TOOL_GET_TITLE,
    TOOL_TAB_NEW,
    TOOL_TAB_LIST,
    TOOL_TAB_SWITCH,
    TOOL_TAB_CLOSE,
    TOOL_EVAL,
    TOOL_CONSOLE,
    TOOL_ERRORS,
    TOOL_DIALOG_STATUS,
    TOOL_DIALOG_ACCEPT,
    TOOL_DIALOG_DISMISS,
];

const CAMOUFOX_STATE_TOOLS: &[&str] = &[
    TOOL_STORAGE_GET,
    TOOL_STORAGE_SET,
    TOOL_STORAGE_CLEAR,
    TOOL_COOKIES_GET,
    TOOL_COOKIES_SET,
    TOOL_COOKIES_CLEAR,
];

const CAMOUFOX_LOCAL_TOOLS: &[&str] = &[
    TOOL_TOOLS_PROFILES,
    TOOL_CLOSE,
    TOOL_CONFIRM,
    TOOL_DENY,
    TOOL_BATCH,
    TOOL_INSTALL,
    TOOL_SKILLS_LIST,
    TOOL_SKILLS_GET,
    TOOL_SKILLS_PATH,
    TOOL_SESSION_LIST,
    TOOL_SESSION_INFO,
    TOOL_GESTURES,
    TOOL_GESTURE,
];

fn is_camoufox_tool(name: &str) -> bool {
    CAMOUFOX_CORE_TOOLS.contains(&name)
        || CAMOUFOX_BROWSER_TOOLS.contains(&name)
        || CAMOUFOX_STATE_TOOLS.contains(&name)
        || CAMOUFOX_LOCAL_TOOLS.contains(&name)
        || NETWORK_PROFILE_TOOLS.contains(&name)
}

fn is_camoufox_page_tool(name: &str) -> bool {
    matches!(
        name,
        TOOL_PAGE_OUTLINE | TOOL_PAGE_LINKS | TOOL_DOM_CHUNK | TOOL_HTML_SEARCH
    )
}

/// Run the MCP stdio server until stdin closes or a `shutdown` request is
/// received.
pub fn run_mcp(args: &[String]) -> Result<(), String> {
    let camoufox = env::var("AGENT_BROWSER_ENGINE").is_ok_and(|engine| engine == "camoufox");
    let config = parse_mcp_config_for_engine(args, camoufox)?;
    let stdin = io::stdin();
    let mut stdout = io::stdout();

    for line in stdin.lock().lines() {
        let mut exit_after_response = false;
        let response = match line {
            Ok(line) => handle_line(&line, &config, &mut exit_after_response),
            Err(e) => Some(error_response(
                Value::Null,
                -32603,
                format!("Failed to read stdin: {}", e),
            )),
        };

        if let Some(response) = response {
            if write_json_line(&mut stdout, &response).is_err() {
                break;
            }
        }

        if exit_after_response {
            break;
        }
    }

    Ok(())
}

#[cfg(test)]
fn parse_mcp_config(args: &[String]) -> Result<McpConfig, String> {
    parse_mcp_config_for_engine(args, false)
}

fn parse_mcp_config_for_engine(args: &[String], camoufox: bool) -> Result<McpConfig, String> {
    let mut tools_arg: Option<String> = None;
    let mut i = 0;

    while i < args.len() {
        let arg = &args[i];
        if arg == "--tools" {
            let Some(value) = args.get(i + 1) else {
                return Err("Missing value for --tools".to_string());
            };
            tools_arg = Some(value.to_string());
            i += 2;
        } else if let Some(value) = arg.strip_prefix("--tools=") {
            tools_arg = Some(value.to_string());
            i += 1;
        } else {
            return Err(format!(
                "Unknown mcp option: {}\nUsage: agent-browser mcp [--tools <profiles>]",
                arg
            ));
        }
    }

    let Some(tools_arg) = tools_arg else {
        return Ok(McpConfig::from_profiles_for_engine(
            vec![ToolProfile::Core],
            camoufox,
        ));
    };

    let mut profiles = Vec::new();
    for raw_name in tools_arg.split(',') {
        let name = raw_name.trim();
        if name.is_empty() {
            continue;
        }
        let Some(profile) = ToolProfile::parse(name) else {
            return Err(format!(
                "Unknown MCP tools profile: {}\nValid profiles: {}",
                name,
                tool_profile_names().join(", ")
            ));
        };
        profiles.push(profile);
    }

    if profiles.is_empty() {
        return Err("Missing value for --tools".to_string());
    }

    Ok(McpConfig::from_profiles_for_engine(profiles, camoufox))
}

fn handle_line(line: &str, config: &McpConfig, exit_after_response: &mut bool) -> Option<Value> {
    let message: Value = match serde_json::from_str(line) {
        Ok(value) => value,
        Err(e) => {
            return Some(error_response(
                Value::Null,
                -32700,
                format!("Parse error: {}", e),
            ));
        }
    };

    let id = message.get("id").cloned();
    let method = match message.get("method").and_then(|v| v.as_str()) {
        Some(method) => method,
        None => {
            return id.map(|id| error_response(id, -32600, "Invalid request: missing method"));
        }
    };

    // Notifications do not receive responses.
    let id = id?;

    match handle_request(method, message.get("params"), config, exit_after_response) {
        Ok(result) => Some(json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": result,
        })),
        Err(err) => Some(error_response(id, err.code, err.message)),
    }
}

fn handle_request(
    method: &str,
    params: Option<&Value>,
    config: &McpConfig,
    exit_after_response: &mut bool,
) -> Result<Value, ProtocolError> {
    match method {
        "initialize" => Ok(initialize_result(params, config)),
        "ping" => Ok(json!({})),
        "tools/list" => list_tools(params, config),
        "tools/call" => call_tool(params, config),
        "shutdown" => {
            *exit_after_response = true;
            Ok(json!({}))
        }
        _ => Err(ProtocolError::method_not_found(method)),
    }
}

fn list_tools(params: Option<&Value>, config: &McpConfig) -> Result<Value, ProtocolError> {
    let tools = tools_for_config(config);
    let start = tool_list_cursor(params, tools.len())?;
    let end = (start + TOOL_LIST_PAGE_SIZE).min(tools.len());
    let mut result = json!({
        "tools": tools[start..end].to_vec(),
    });

    if end < tools.len() {
        result["nextCursor"] = json!(end.to_string());
    }

    Ok(result)
}

fn tool_list_cursor(params: Option<&Value>, total: usize) -> Result<usize, ProtocolError> {
    let Some(cursor) = params.and_then(|p| p.get("cursor")) else {
        return Ok(0);
    };

    let cursor = cursor
        .as_str()
        .ok_or_else(|| ProtocolError::invalid_params("tools/list cursor must be a string"))?;
    let index = cursor
        .parse::<usize>()
        .map_err(|_| ProtocolError::invalid_params("Invalid tools/list cursor"))?;

    if index > total {
        return Err(ProtocolError::invalid_params("Invalid tools/list cursor"));
    }

    Ok(index)
}

fn initialize_result(params: Option<&Value>, config: &McpConfig) -> Value {
    let requested = params
        .and_then(|p| p.get("protocolVersion"))
        .and_then(|v| v.as_str())
        .unwrap_or(PROTOCOL_VERSION);
    let protocol_version = if SUPPORTED_PROTOCOL_VERSIONS.contains(&requested) {
        requested
    } else {
        PROTOCOL_VERSION
    };

    json!({
        "protocolVersion": protocol_version,
        "capabilities": {
            "tools": {}
        },
        "serverInfo": {
            "name": "agent-browser",
            "title": "agent-browser",
            "version": env!("CARGO_PKG_VERSION")
        },
        "instructions": format!(
            "Use the typed agent_browser_* tools to control a browser. Active MCP tools profile(s): {}. Discovery order: take a screenshot first and work from what you see; for long pages scroll with PageDown/PageUp and take a fresh screenshot per viewport before coordinate gestures. Use agent_browser_html_search to locate content in the live page HTML and get CSS paths without dumping the page into context. Use find or scoped eval for exact selectors. Use agent_browser_snapshot only as a last resort for genuinely complex structure; full snapshots and tab_list on ad-heavy pages return very large output. Use agent_browser_tools_profiles to see available startup profiles.",
            config.profile_names().join(", ")
        )
    })
}

fn tools_for_config(config: &McpConfig) -> Vec<Value> {
    tools()
        .into_iter()
        .filter(|tool| {
            tool.get("name")
                .and_then(|name| name.as_str())
                .is_some_and(|name| config.allows(name))
        })
        .map(|tool| {
            if config.camoufox {
                camoufox_tool(tool)
            } else {
                tool
            }
        })
        .collect()
}

/// Project the shared catalog onto the native backend without changing Chrome's contract.
fn camoufox_tool(mut tool: Value) -> Value {
    let name = tool["name"].as_str().unwrap_or("").to_string();
    let props = tool["inputSchema"]["properties"].as_object_mut().unwrap();
    for key in [
        "restore",
        "restoreSave",
        "restoreCheckUrl",
        "restoreCheckText",
        "restoreCheckFn",
        "allowedDomains",
        "caCert",
        "clearCaCert",
    ] {
        props.remove(key);
    }
    props.insert("engine".into(), json!({
        "type": "string", "enum": ["camoufox"],
        "description": "This MCP server is bound to Camoufox. Use a separate server for another engine."
    }));
    props.get_mut("timeoutMs").unwrap()["minimum"] = json!(30000);
    props.get_mut("namespace").unwrap()["description"] =
        json!("Optional namespace isolating daemon sockets.");
    props.get_mut("profile").unwrap()["description"] = json!("Absolute path to a private persistent Camoufox profile. Reuse it to retain login storage across restarts. Close before changing profiles; only one browser may own a profile. Omit to use configured defaults.");
    props.get_mut("extraArgs").unwrap()["description"] = json!("Advanced CLI arguments, still subject to Camoufox capability and safety validation. Engine overrides are rejected.");
    let removed: &[&str] = match name.as_str() {
        TOOL_READ => &[
            "url",
            "raw",
            "requireMd",
            "llms",
            "outline",
            "filter",
            "readTimeoutMs",
        ],
        TOOL_SNAPSHOT => &["interactive", "compact", "includeUrls"],
        TOOL_CLICK => &["newTab"],
        TOOL_INSTALL => &["withDeps"],
        _ => &[],
    };
    for key in removed {
        props.remove(*key);
    }
    match name.as_str() {
        TOOL_SNAPSHOT => {
            props.get_mut("depth").unwrap()["maximum"] = json!(100);
            props.get_mut("depth").unwrap()["description"] =
                json!("Native tree depth limit, 0–100. Zero or omission means unlimited.");
            props.insert("selector".into(), selector_schema());
        }
        TOOL_SELECT => {
            props.get_mut("values").unwrap()["description"] =
                json!("Exact option values to select, not labels.")
        }
        _ => {}
    }
    let description = match name.as_str() {
        TOOL_CLOSE => Some("Close the named browser session without deleting its persistent profile. Tabs and transient state end; persistent cookies and site storage remain. Leave a user's persistent browser open unless closure or recovery is requested."),
        TOOL_READ => Some("Read rendered body text from the selected frame, not a URL fetch or markdown extractor. Use scoped DOM queries or eval when accessibility snapshots omit content. Returns frameId/frameUrl."),
        TOOL_SNAPSHOT => Some("Capture the native AI accessibility tree with refs, link URLs and pointer-cursor markers already included. No Chrome filtering options. Every snapshot, including a scoped one, replaces exposed refs; navigation, frame detachment and scope changes clear them. DOM-only changes can still cause native locator failures. Use DOM queries for content absent from this accessibility view. Pass quiet:true on volatile pages to wait up to 3s for a 250ms mutation-silent window before capturing."),
        TOOL_SCREENSHOT => Some("Capture the viewport as PNG, or the full scrollable page with fullPage. Returns its path and, for viewport captures, a captureId for coordinate gestures. An inline visual image is attached when small enough. Element crops, annotation and JPEG are not supported."),
        TOOL_EVAL => Some("Evaluate JavaScript in the selected frame's isolated world using stdin. DOM access is available; page-script globals are not guaranteed. Scripts may mutate the page and invalidate coordinate captures. Traverse open shadowRoot explicitly in JavaScript; closed shadow roots are not exposed. Returns frameId/frameUrl."),
        TOOL_FRAME_SWITCH => Some("Select an observation/CSS frame using a tab-local frame-N ID from tab_list/snapshot, a unique iframe CSS selector relative to the selected frame, or an exposed iframe @ref. Supports nested/cross-origin frames. Clears refs/captures. A detached selected frame fails rather than falling back. Navigation, URL/title/load waits and screenshots remain top-level."),
        TOOL_FRAME_MAIN => Some("Return observation and CSS scope to the main frame, including after selected-frame detachment. Clears refs and coordinate captures."),
        TOOL_TAB_LIST => Some("List tabs and their bounded frame trees, including tab-local frame IDs, parent IDs, selected state and framesOmitted. Closed tabs are never silently replaced."),
        TOOL_WAIT_FOR_FUNCTION => Some("Wait for a JavaScript expression in the selected frame's isolated world to become truthy."),
        TOOL_WAIT_FOR_TEXT => Some("Wait for visible matching text in the selected frame."),
        TOOL_DRAG => Some("Drag one main-frame element to another with native input. Frame-prefixed refs are not supported for drag. Never replay an ambiguous input."),
        TOOL_HOVER_HOLD => Some("Keep native mouse micro-movement at the selector so hover-revealed UI stays visible. Auto-stops before input actions, navigation, tab or frame changes, session close, or maxMs."),
        TOOL_INSTALL => Some("Explicitly provision the private Camoufox Python environment and browser cache. Python 3.10+ is required. This may take several minutes; installation is never automatic at startup."),
        _ => None,
    };
    if let Some(description) = description {
        tool["description"] = json!(description);
    }
    if name == TOOL_READ {
        tool["title"] = json!("Read rendered page");
    }
    tool
}

fn validate_camoufox_engine_args(args: &[String]) -> Result<(), ProtocolError> {
    for (index, arg) in args.iter().enumerate() {
        let engine = if arg == "--engine" {
            Some(args.get(index + 1).map(String::as_str).unwrap_or(""))
        } else {
            arg.strip_prefix("--engine=")
        };
        if engine.is_some_and(|engine| engine != "camoufox") {
            return Err(ProtocolError::invalid_params("This MCP server is bound to Camoufox; engine overrides require a separate MCP server"));
        }
    }
    Ok(())
}

/// Validate the advertised engine-specific boundary before any subprocess or browser input.
fn camoufox_arguments(name: &str, arguments: &Value) -> Result<Value, ProtocolError> {
    validate_arguments_object(arguments)?;
    let definition = camoufox_tool(
        tools()
            .into_iter()
            .find(|tool| tool["name"] == name)
            .unwrap(),
    );
    let props = definition["inputSchema"]["properties"].as_object().unwrap();
    if let Some(arguments) = arguments.as_object() {
        for (key, value) in arguments {
            let Some(property) = props.get(key) else {
                return Err(ProtocolError::invalid_params(format!(
                    "Argument '{key}' is not available for {name} on Camoufox"
                )));
            };
            if property
                .get("enum")
                .and_then(Value::as_array)
                .is_some_and(|values| !values.contains(value))
            {
                return Err(ProtocolError::invalid_params(format!(
                    "Unsupported value for '{key}' on Camoufox"
                )));
            }
        }
    }
    if optional_timeout(arguments)? < 30_000 {
        return Err(ProtocolError::invalid_params(
            "Camoufox timeoutMs must be at least 30000",
        ));
    }
    validate_camoufox_engine_args(
        &optional_string_array(arguments, "extraArgs")?.unwrap_or_default(),
    )?;
    if name == TOOL_BATCH {
        if let Some(commands) = arguments.get("commands").and_then(Value::as_array) {
            for command in commands {
                if let Some(items) = command.as_array() {
                    let args: Vec<String> = items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect();
                    validate_camoufox_engine_args(&args)?;
                }
            }
        }
    }
    let mut result = arguments.as_object().cloned().unwrap_or_default();
    result.insert("engine".into(), json!("camoufox"));
    Ok(Value::Object(result))
}

fn tool_profile_names() -> Vec<&'static str> {
    [
        ToolProfile::Core,
        ToolProfile::Network,
        ToolProfile::State,
        ToolProfile::Debug,
        ToolProfile::Tabs,
        ToolProfile::Mobile,
        ToolProfile::Webmcp,
        ToolProfile::Gestures,
        ToolProfile::All,
    ]
    .iter()
    .map(|profile| profile.name())
    .collect()
}

fn tool_profile_summaries(config: &McpConfig) -> Vec<Value> {
    [
        ToolProfile::Core,
        ToolProfile::Network,
        ToolProfile::State,
        ToolProfile::Debug,
        ToolProfile::Tabs,
        ToolProfile::Mobile,
        ToolProfile::Webmcp,
        ToolProfile::Gestures,
        ToolProfile::All,
    ]
    .iter()
    .map(|profile| {
        json!({
            "name": profile.name(),
            "description": config.profile_description(*profile),
            "toolCount": config.profile_tool_count(*profile),
            "usage": format!("agent-browser mcp --tools {}", profile.name()),
        })
    })
    .collect()
}

fn tools() -> Vec<Value> {
    let mut tools = vec![
        tool(
            TOOL_GESTURES,
            "Discover gestures",
            "List trusted Camoufox gesture modules, or get one module's schema and examples. Requires the Camoufox engine. Does not launch a browser.",
            json!({"name": {"type": "string", "description": "Optional gesture name; omit to list available gestures."}}),
            &[],
        ),
        tool(
            TOOL_GESTURE,
            "Execute gesture",
            "Execute a schema-validated native Camoufox gesture. Discover its schema first. Coordinates require a fresh screenshot captureId. Optional observation is returned in the same call. Input diagnostics do not assert application success. Input whose outcome may be ambiguous must not be retried; close the session. Disabled when action policies or confirm-actions are active.",
            json!({
                "name": {"type": "string"},
                "params": {"type": "object", "description": "Parameters matching the discovered gesture schema."},
                "observe": {"type": "string", "enum": ["none", "snapshot", "screenshot"], "default": "none"}
            }),
            &["name", "params"],
        ),
        tool(
            TOOL_TOOLS_PROFILES,
            "MCP tool profiles",
            "List MCP startup tool profiles and how to enable them.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_OPEN,
            "Open page",
            "Launch the browser and optionally navigate to a URL. Never replay ambiguous input.",
            json!({
                "url": { "type": "string", "description": "URL to open. Omit to launch about:blank." },
                "headed": { "type": "boolean", "description": "Show the browser window. Explicit true/false overrides AGENT_BROWSER_HEADED and config; omit to use those defaults." },
                "adblock": { "type": "boolean", "description": "Camoufox only: load the managed runtime's bundled uBlock Origin addon for this session. Explicit true/false overrides AGENT_BROWSER_ADBLOCK and config; omit to use those defaults. Close the session before changing it." }
                ,"webmcp": { "type": "boolean", "description": "Set false to disable WebMCP by passing --no-webmcp." }
            }),
            &[],
        ),
        tool(
            TOOL_READ,
            "Read URL",
            "Fetch a URL as agent-readable text, preferring text/markdown. Omit url to read the active tab.",
            json!({
                "url": { "type": "string", "description": "URL to read. Bare hosts are normalized to https. Omit to read the active tab." },
                "raw": { "type": "boolean", "description": "Return the response body without HTML extraction." },
                "requireMd": { "type": "boolean", "description": "Fail unless the response is Content-Type: text/markdown." },
                "llms": { "type": "string", "enum": ["index", "full"], "description": "Return nearest-ancestor llms data: index for compact llms.txt links, full for llms-full.txt." },
                "outline": { "type": "boolean", "description": "Return a heading outline for the selected page instead of the full page text." },
                "filter": { "type": "string", "description": "Filter page sections, --llms links/sections, or --outline headings." },
                "readTimeoutMs": { "type": "integer", "description": "Request timeout in milliseconds." }
            }),
            &[],
        ),
        tool(
            TOOL_SNAPSHOT,
            "Snapshot page",
            "Capture the native AI accessibility tree with refs, link URLs and pointer-cursor markers already included. No Chrome filtering options.",
            json!({
                "depth": { "type": "integer", "minimum": 0, "maximum": 100, "description": "Native tree depth limit, 0-100." },
                "selector": { "type": "string", "description": "Scope the snapshot with an observed @ref, CSS selector, or xpath= selector." },
                "quiet": { "type": "boolean", "description": "Wait up to 3s for a 250ms mutation-silent window before capturing." }
            }),
            &[],
        ),
        tool(
            TOOL_PAGE_OUTLINE,
            "Outline page",
            "Return a bounded DOM-derived heading and landmark outline for the selected frame. Camoufox only.",
            json!({
                "selector": { "type": "string", "description": "Optional unique root as an observation @ref, CSS selector, or xpath= selector." }
            }),
            &[],
        ),
        tool(
            TOOL_PAGE_LINKS,
            "List page links",
            "Return a bounded cursor-paginated link inventory with refs, text, and URLs. Operates only in the selected frame. Camoufox only.",
            json!({
                "selector": { "type": "string", "description": "Optional unique first-page root as an observation @ref, CSS selector, or xpath= selector." },
                "cursor": { "type": "string", "description": "Opaque nextCursor from the preceding page. Do not combine with selector." },
                "limit": { "type": "integer", "minimum": 1, "maximum": 200, "default": 50, "description": "Maximum links to return. Set on the first page; omit or keep the same value with a cursor." }
            }),
            &[],
        ),
        tool(
            TOOL_DOM_CHUNK,
            "Read DOM chunk",
            "Return cursor-paginated structured DOM element records with document-scoped actionable @dN refs. Camoufox only.",
            json!({
                "selector": { "type": "string", "description": "Optional unique first-page root as an observation @ref, CSS selector, or xpath= selector." },
                "cursor": { "type": "string", "description": "Opaque nextCursor from the preceding chunk. Do not combine with selector." },
                "limit": { "type": "integer", "minimum": 1, "maximum": 500, "default": 100, "description": "Maximum DOM element records to return. Set on the first chunk; omit or keep the same value with a cursor." }
            }),
            &[],
        ),
        tool(
            TOOL_CLICK,
            "Click element",
            "Click an element by observed @ref, CSS selector, or xpath= selector.",
            json!({
                "selector": selector_schema(),
                "newTab": { "type": "boolean", "default": false, "description": "Open link targets in a new tab after applying session setup." }
            }),
            &["selector"],
        ),
        tool(
            TOOL_FILL,
            "Fill input",
            "Clear and fill an input by observed @ref, CSS selector, or xpath= selector.",
            json!({
                "selector": selector_schema(),
                "text": { "type": "string", "description": "Text to fill." }
            }),
            &["selector", "text"],
        ),
        tool(
            TOOL_TYPE,
            "Type text",
            "Type text into an element by observed @ref, CSS selector, or xpath= selector.",
            json!({
                "selector": selector_schema(),
                "text": { "type": "string", "description": "Text to type." },
                "clear": { "type": "boolean", "default": false, "description": "Clear the field before typing." },
                "delayMs": { "type": "integer", "minimum": 0, "description": "Delay between keystrokes." }
            }),
            &["selector", "text"],
        ),
        tool(
            TOOL_PRESS,
            "Press key",
            "Press a key at the current focus.",
            json!({
                "key": { "type": "string", "description": "Key name such as Enter, Tab, or Control+a." }
            }),
            &["key"],
        ),
        tool(TOOL_HOVER, "Hover element", "Hover an element.", json!({ "selector": selector_schema() }), &["selector"]),
        {
            let mut definition = tool(
                TOOL_HOVER_HOLD,
                "Hover hold",
                "Keep native mouse micro-movement at the selector so hover-revealed UI stays visible. Auto-stops before input actions, navigation, tab or frame changes, session close, or maxMs.",
                json!({
                    "selector": selector_schema(),
                    "maxMs": { "type": "integer", "minimum": 1000, "maximum": 120000, "default": 30000, "description": "Maximum hold duration in milliseconds. Only valid when starting a hold." },
                    "stop": { "type": "boolean", "description": "Pass true to stop; omit selector and maxMs." }
                }),
                &[],
            );
            definition["inputSchema"]["properties"]["selector"]["minLength"] = json!(1);
            definition["inputSchema"]["oneOf"] = json!([
                { "required": ["selector"], "not": { "required": ["stop"] } },
                {
                    "required": ["stop"],
                    "properties": { "stop": { "const": true } },
                    "not": { "anyOf": [{ "required": ["selector"] }, { "required": ["maxMs"] }] }
                }
            ]);
            definition
        },
        tool(TOOL_FOCUS, "Focus element", "Focus an element.", json!({ "selector": selector_schema() }), &["selector"]),
        tool(TOOL_CHECK, "Check element", "Check a checkbox or switch.", json!({ "selector": selector_schema() }), &["selector"]),
        tool(TOOL_UNCHECK, "Uncheck element", "Uncheck a checkbox or switch.", json!({ "selector": selector_schema() }), &["selector"]),
        tool(
            TOOL_SELECT,
            "Select options",
            "Select one or more options in a select element.",
            json!({
                "selector": selector_schema(),
                "values": {
                    "type": "array",
                    "items": { "type": "string" },
                    "minItems": 1,
                    "description": "Option values or labels to select."
                }
            }),
            &["selector", "values"],
        ),
        tool(
            TOOL_SCROLL,
            "Scroll page",
            "Scroll the page or an element.",
            json!({
                "direction": { "type": "string", "enum": ["up", "down", "left", "right"], "default": "down" },
                "amount": { "type": "integer", "default": 300, "description": "Pixels to scroll." },
                "selector": { "type": "string", "description": "Optional element selector to scroll." }
            }),
            &[],
        ),
        tool(TOOL_SCROLL_INTO_VIEW, "Scroll into view", "Scroll an element into view.", json!({ "selector": selector_schema() }), &["selector"]),
        tool(TOOL_WAIT_MS, "Wait milliseconds", "Wait for a fixed time.", json!({ "ms": { "type": "integer", "minimum": 0 } }), &["ms"]),
        wait_tool(TOOL_WAIT_FOR_SELECTOR, "Wait for selector", "Wait for an element to appear.", json!({ "selector": selector_schema() }), &["selector"]),
        wait_tool(TOOL_WAIT_FOR_TEXT, "Wait for text", "Wait for text to appear.", json!({ "text": { "type": "string" } }), &["text"]),
        wait_tool(TOOL_WAIT_FOR_URL, "Wait for URL", "Wait for the current URL to match a pattern.", json!({ "url": { "type": "string", "description": "URL glob or pattern." } }), &["url"]),
        wait_tool(TOOL_WAIT_FOR_LOAD, "Wait for load state", "Wait for a page load state.", json!({ "state": { "type": "string", "enum": ["load", "domcontentloaded", "networkidle"] } }), &["state"]),
        wait_tool(TOOL_WAIT_FOR_FUNCTION, "Wait for function", "Wait for a JavaScript expression to become truthy.", json!({ "expression": { "type": "string" } }), &["expression"]),
        tool(
            TOOL_SCREENSHOT,
            "Take screenshot",
            "Capture the viewport as PNG, or the full scrollable page with fullPage. Returns its path and, for viewport captures, a captureId for coordinate gestures. An inline visual image is attached when small enough. Element crops, annotation and JPEG are not supported.",
            json!({
                "path": { "type": "string", "description": "Optional output path." },
                "screenshotDir": { "type": "string", "description": "Default output directory when path is omitted." },
                "fullPage": { "type": "boolean", "description": "Capture the entire scrollable page instead of the viewport. Full-page captures return no captureId and cannot be used for coordinate gestures." }
            }),
            &[],
        ),
        tool(TOOL_GET_TEXT, "Get text", "Get visible text from an element.", json!({ "selector": selector_schema() }), &["selector"]),
        tool(TOOL_GET_HTML, "Get HTML", "Get innerHTML from an element. Use selector `html` to read the whole document element's contents.", json!({ "selector": selector_schema() }), &["selector"]),
        tool(
            TOOL_HTML_SEARCH,
            "Search page HTML",
            "Search the live page HTML server-side and return only matching excerpts with CSS paths. The full HTML never enters the conversation. Prefer this over full snapshots or get_html for locating content; use snapshot only as last resort.",
            json!({
                "query": { "type": "string", "description": "Literal text to search for, case-insensitive. Required unless regex is set." },
                "regex": { "type": "string", "description": "Python-style regular expression; overrides query when both are provided. Invalid patterns fail with a clear error." },
                "selector": { "type": "string", "description": "Optional CSS selector restricting the search to the first matching element's subtree." },
                "maxResults": { "type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": "Maximum excerpts to return." },
                "contextChars": { "type": "integer", "minimum": 1, "maximum": 400, "default": 120, "description": "Characters of surrounding context per excerpt." }
            }),
            &["query"],
        ),
        tool(TOOL_GET_VALUE, "Get value", "Get an input value.", json!({ "selector": selector_schema() }), &["selector"]),
        tool(TOOL_GET_URL, "Get URL", "Get the current page URL.", json!({}), &[]),
        tool(TOOL_GET_TITLE, "Get title", "Get the current page title.", json!({}), &[]),
        tool(
            TOOL_EVAL,
            "Evaluate JavaScript",
            "Run JavaScript in the page using stdin to avoid shell escaping.",
            json!({
                "script": { "type": "string", "description": "JavaScript expression or script to evaluate." }
            }),
            &["script"],
        ),
        tool(
            TOOL_CLOSE,
            "Close browser",
            "Close the current browser session.",
            json!({
                "all": { "type": "boolean", "default": false, "description": "Close all active sessions." }
            }),
            &[],
        ),
    ];
    tools.extend(parity_tools());
    tools
}

fn parity_tools() -> Vec<Value> {
    vec![
        tool(TOOL_BACK, "Back", "Navigate back.", json!({}), &[]),
        tool(TOOL_FORWARD, "Forward", "Navigate forward.", json!({}), &[]),
        tool(TOOL_RELOAD, "Reload", "Reload the page.", json!({}), &[]),
        tool(
            TOOL_DBLCLICK,
            "Double-click element",
            "Double-click an element.",
            json!({ "selector": selector_schema() }),
            &["selector"],
        ),
        tool(
            TOOL_DRAG,
            "Drag and drop",
            "Drag one element to another.",
            json!({ "source": selector_schema(), "target": selector_schema() }),
            &["source", "target"],
        ),
        tool(
            TOOL_UPLOAD,
            "Upload files",
            "Upload files through a file input.",
            json!({ "selector": selector_schema(), "files": string_array_schema("File paths to upload.") }),
            &["selector", "files"],
        ),
        tool(
            TOOL_DOWNLOAD,
            "Download file",
            "Click an element once and save the download. Camoufox refuses existing destinations; after a save-only failure, use wait_for_download without replaying the click.",
            json!({ "selector": selector_schema(), "path": { "type": "string" } }),
            &["selector", "path"],
        ),
        tool(
            TOOL_DOWNLOADS,
            "Download metadata",
            "Camoufox only: list bounded download metadata across tabs, including saveUnavailable markers for closed source tabs. Clear forgets metadata, not saved files. URLs may contain secrets.",
            json!({ "clear": { "type": "boolean" } }),
            &[],
        ),
        tool(
            TOOL_WAIT_FOR_DOWNLOAD,
            "Wait for download",
            "Wait for a browser download and save it to disk. Camoufox saves the oldest retained unconsumed active-tab event, or waits for the next; existing destinations are refused.",
            json!({ "path": { "type": "string", "description": "Optional output path." }, "waitTimeoutMs": wait_timeout_schema() }),
            &[],
        ),
        tool(
            TOOL_GET_ATTR,
            "Get attribute",
            "Get an element attribute.",
            json!({ "selector": selector_schema(), "name": { "type": "string" } }),
            &["selector", "name"],
        ),
        tool(
            TOOL_GET_COUNT,
            "Get count",
            "Count matching elements.",
            json!({ "selector": selector_schema() }),
            &["selector"],
        ),
        tool(
            TOOL_GET_BOX,
            "Get box",
            "Get an element bounding box.",
            json!({ "selector": selector_schema() }),
            &["selector"],
        ),
        tool(
            TOOL_IS_VISIBLE,
            "Is visible",
            "Check whether an element is visible.",
            json!({ "selector": selector_schema() }),
            &["selector"],
        ),
        tool(
            TOOL_IS_ENABLED,
            "Is enabled",
            "Check whether an element is enabled.",
            json!({ "selector": selector_schema() }),
            &["selector"],
        ),
        tool(
            TOOL_IS_CHECKED,
            "Is checked",
            "Check whether an element is checked.",
            json!({ "selector": selector_schema() }),
            &["selector"],
        ),
        tool(
            TOOL_FIND,
            "Find element",
            "Find an element with semantic locators and optionally act on it. On match, the \"text\" subaction is read-only and returns the located element's text and a unique CSS selector.",
            json!({
                "locator": { "type": "string", "enum": ["role", "text", "label", "placeholder", "alt", "title", "testid", "first", "last", "nth"] },
                "value": { "type": "string", "description": "Role, text, label, selector, or test id." },
                "action": { "type": "string", "description": "Optional action: click, fill, check, hover, text." },
                "text": { "type": "string", "description": "Optional value for the fill action." },
                "index": { "type": "integer", "description": "Index for nth locator." },
                "name": { "type": "string", "description": "Accessible name filter for role locator." },
                "exact": { "type": "boolean", "description": "Exact, case-sensitive match. For the role locator it applies to the accessible name, whose default is a case-insensitive substring. The role value itself always matches case-insensitively, with or without exact.", "default": false }
            }),
            &["locator", "value"],
        ),
        tool(
            TOOL_SET_OFFLINE,
            "Set offline",
            "Toggle offline mode.",
            json!({ "enabled": { "type": "boolean" } }),
            &["enabled"],
        ),
        tool(
            TOOL_SET_HEADERS,
            "Set headers",
            "Set extra HTTP headers from a JSON object.",
            json!({ "headers": { "type": "object", "additionalProperties": { "type": "string" } } }),
            &["headers"],
        ),
        tool(
            TOOL_SET_CREDENTIALS,
            "Set credentials",
            "Set HTTP credentials for the current tab and tabs opened later.",
            json!({ "username": { "type": "string" }, "password": { "type": "string" } }),
            &["username", "password"],
        ),
        tool(
            TOOL_NETWORK_ROUTE,
            "Network route",
            "Route matching requests. Camoufox routes affect the owned context, disable HTTP cache, and cannot guarantee service-worker interception; this is not domain containment.",
            json!({ "url": { "type": "string" }, "abort": { "type": "boolean" }, "body": { "type": "string" }, "resourceType": { "type": "string", "description": "Comma-separated resource types." } }),
            &["url"],
        ),
        tool(
            TOOL_NETWORK_UNROUTE,
            "Network unroute",
            "Remove network routes.",
            json!({ "url": { "type": "string" } }),
            &[],
        ),
        tool(
            TOOL_NETWORK_REQUESTS,
            "Network requests",
            "List captured network requests. Camoufox records bounded context-wide metadata from launch, with tab IDs and dropped counts. URLs can contain secrets; bodies and headers are returned only by request detail.",
            json!({ "clear": { "type": "boolean" }, "filter": { "type": "string" }, "type": { "type": "string" }, "method": { "type": "string" }, "status": { "type": "string" } }),
            &[],
        ),
        tool(
            TOOL_NETWORK_REQUEST,
            "Network request detail",
            "Show one request by id, including sensitive headers, POST data, and available response body. Binary bodies are base64; pending/unavailable/capped data is explicitly marked.",
            json!({ "requestId": { "type": "string" } }),
            &["requestId"],
        ),
        tool(
            TOOL_NETWORK_WEBSOCKETS,
            "WebSocket events",
            "Read bounded WebSocket lifecycle and sent/received frame events across tabs. Payloads may contain secrets. Binary frames are base64; dropped and truncated data are explicit.",
            json!({ "clear": { "type": "boolean" }, "filter": { "type": "string" } }),
            &[],
        ),
        tool(
            TOOL_NETWORK_HAR_START,
            "HAR start",
            "Start HAR capture. Embeds text response bodies by default; content controls which bodies are embedded.",
            json!({ "content": { "type": "string", "enum": ["all", "text", "none"] } }),
            &[],
        ),
        tool(
            TOOL_NETWORK_HAR_STOP,
            "HAR stop",
            "Stop HAR capture.",
            json!({ "path": { "type": "string" } }),
            &[],
        ),
        tool(
            TOOL_STORAGE_GET,
            "Storage get",
            "Get localStorage or sessionStorage. Camoufox reads the active origin, with explicit size limits. Values may contain credentials or personal data.",
            json!({ "storageType": storage_type_schema(), "key": { "type": "string" } }),
            &["storageType"],
        ),
        tool(
            TOOL_STORAGE_SET,
            "Storage set",
            "Set localStorage or sessionStorage.",
            json!({ "storageType": storage_type_schema(), "key": { "type": "string" }, "value": { "type": "string" } }),
            &["storageType", "key", "value"],
        ),
        tool(
            TOOL_STORAGE_CLEAR,
            "Storage clear",
            "Clear localStorage or sessionStorage.",
            json!({ "storageType": storage_type_schema() }),
            &["storageType"],
        ),
        tool(
            TOOL_COOKIES_GET,
            "Cookies get",
            "Get cookies, including sensitive HttpOnly values. Camoufox reads the owned context and reports omitted entries when output is capped.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_COOKIES_SET,
            "Cookies set",
            "Set one cookie.",
            json!({ "name": { "type": "string" }, "value": { "type": "string" }, "url": { "type": "string" }, "domain": { "type": "string" }, "path": { "type": "string" }, "httpOnly": { "type": "boolean" }, "secure": { "type": "boolean" }, "sameSite": { "type": "string", "enum": ["Strict", "Lax", "None"] }, "expires": { "type": "integer" } }),
            &["name", "value"],
        ),
        tool(
            TOOL_COOKIES_CLEAR,
            "Cookies clear",
            "Clear cookies.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_TAB_NEW,
            "Tab new",
            "Open a new tab after applying session setup before its first navigation.",
            json!({ "url": { "type": "string" }, "label": { "type": "string" } }),
            &[],
        ),
        tool(TOOL_TAB_LIST, "Tab list", "List tabs. Camoufox reconciles closed pages and never silently adopts a replacement active tab.", json!({}), &[]),
        tool(
            TOOL_TAB_SWITCH,
            "Tab switch",
            "Switch to a tab by id (t1), label, or CDP target id. Switching also binds the session to that tab.",
            json!({ "tab": { "type": "string", "description": "Tab id (t1), label, or CDP target id." } }),
            &["tab"],
        ),
        tool(
            TOOL_TAB_CLOSE,
            "Tab close",
            "Close a tab by id (t1), label, or CDP target id. Omit to close the current tab.",
            json!({ "tab": { "type": "string", "description": "Tab id (t1), label, or CDP target id." } }),
            &[],
        ),
        tool(
            TOOL_FRAME_SWITCH,
            "Frame switch",
            "Switch frame by selector, ref, or id.",
            json!({ "frame": { "type": "string" } }),
            &["frame"],
        ),
        tool(
            TOOL_FRAME_MAIN,
            "Frame main",
            "Switch to the main frame.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_DIALOG_STATUS,
            "Dialog status",
            "Show JavaScript dialog state. Camoufox reports the last observation, pending automatic handling, and any pre-armed decision; it cannot take over a pending dialog.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_DIALOG_ACCEPT,
            "Dialog accept",
            "Accept a JavaScript dialog. Camoufox arms one active-tab decision for 30 seconds BEFORE the triggering action; navigation or tab close clears it.",
            json!({ "text": { "type": "string" } }),
            &[],
        ),
        tool(
            TOOL_DIALOG_DISMISS,
            "Dialog dismiss",
            "Dismiss a JavaScript dialog. Camoufox instead arms the next active-tab dialog's dismissal for 30 seconds before the triggering action; unarmed dialogs already auto-dismiss.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_CONSOLE,
            "Console logs",
            "Read console logs.",
            json!({ "clear": { "type": "boolean" } }),
            &[],
        ),
        tool(
            TOOL_ERRORS,
            "Page errors",
            "Read page errors.",
            json!({ "clear": { "type": "boolean" } }),
            &[],
        ),
        tool(
            TOOL_STATE_LIST,
            "State list",
            "List saved states.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_STATE_CLEAR,
            "State clear",
            "Clear saved state.",
            json!({ "name": { "type": "string" }, "all": { "type": "boolean" } }),
            &[],
        ),
        tool(
            TOOL_STATE_SHOW,
            "State show",
            "Show a saved state file.",
            json!({ "path": { "type": "string" } }),
            &["path"],
        ),
        tool(
            TOOL_STATE_CLEAN,
            "State clean",
            "Delete old saved states.",
            json!({ "olderThanDays": { "type": "integer", "minimum": 0 } }),
            &["olderThanDays"],
        ),
        tool(
            TOOL_STATE_RENAME,
            "State rename",
            "Rename saved state.",
            json!({ "oldName": { "type": "string" }, "newName": { "type": "string" } }),
            &["oldName", "newName"],
        ),
        tool(
            TOOL_BATCH,
            "Batch",
            "Run multiple commands sequentially.",
            json!({ "commands": { "type": "array", "items": { "type": "array", "items": { "type": "string" }, "minItems": 1 }, "minItems": 1 }, "bail": { "type": "boolean" } }),
            &["commands"],
        ),
        tool(
            TOOL_CONFIRM,
            "Confirm action",
            "Approve a pending action.",
            json!({ "id": { "type": "string" } }),
            &["id"],
        ),
        tool(
            TOOL_DENY,
            "Deny action",
            "Deny a pending action.",
            json!({ "id": { "type": "string" } }),
            &["id"],
        ),
        tool(
            TOOL_SESSION_LIST,
            "Session list",
            "List active sessions.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_SESSION_INFO,
            "Session info",
            "Show session, daemon, launch, and restore diagnostics. For Camoufox, runtime.launched, browserConnected, recoveryRequired, and closeReason describe browser liveness; runtime.inputBackend reports the dispatch backend (juggler or os-native) and runtime.vncUrl is the live noVNC view when os-native is active.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_SKILLS_LIST,
            "Skills list",
            "List bundled skills.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_SKILLS_GET,
            "Skills get",
            "Get bundled skill content.",
            json!({ "names": string_array_schema("Skill names."), "all": { "type": "boolean" }, "full": { "type": "boolean" } }),
            &[],
        ),
        tool(
            TOOL_SKILLS_PATH,
            "Skills path",
            "Print skill directory path.",
            json!({ "name": { "type": "string" } }),
            &[],
        ),
        tool(
            TOOL_DOCTOR,
            "Doctor",
            "Diagnose the installation.",
            json!({ "offline": { "type": "boolean" }, "quick": { "type": "boolean" }, "fix": { "type": "boolean" }, "headed": { "type": "boolean", "description": "Run the doctor's live launch probe headed. Explicit true/false overrides AGENT_BROWSER_HEADED/config." }, "debug": { "type": "boolean", "description": "Verbose diagnostics from the probes' scratch daemons." } }),
            &[],
        ),
        tool(
            TOOL_INSTALL,
            "Install",
            "Explicitly provision the private Camoufox Python environment and browser cache. Python 3.10+ is required. This may take several minutes; installation is never automatic at browser startup.",
            json!({}),
            &[],
        ),
        tool(
            TOOL_CHAT,
            "Chat",
            "Run a single-shot natural-language browser instruction.",
            json!({ "message": { "type": "string" }, "model": { "type": "string" }, "verbose": { "type": "boolean" }, "quiet": { "type": "boolean" } }),
            &["message"],
        ),
    ]
}

fn selector_schema() -> Value {
    json!({
        "type": "string",
        "description": "Element @ref from a snapshot, a document-scoped DOM @dN ref when available, a CSS selector, or an XPath selector prefixed with xpath=."
    })
}

fn string_array_schema(description: &str) -> Value {
    json!({
        "type": "array",
        "items": { "type": "string" },
        "minItems": 1,
        "description": description,
    })
}

fn storage_type_schema() -> Value {
    json!({
        "type": "string",
        "enum": ["local", "session"],
    })
}

fn wait_timeout_schema() -> Value {
    json!({
        "type": "integer",
        "minimum": 1,
        "description": "Maximum time for the browser wait condition."
    })
}

fn tool(name: &str, title: &str, description: &str, properties: Value, required: &[&str]) -> Value {
    let mut props = match properties {
        Value::Object(map) => map,
        _ => serde_json::Map::new(),
    };
    props.insert("engine".to_string(), json!({
        "type": "string", "enum": ["camoufox"],
        "description": "Browser engine. Omit to inherit AGENT_BROWSER_ENGINE/config. Camoufox V1 has an explicit supported subset; unsupported commands fail without falling back."
    }));
    props.insert(
        "session".to_string(),
        json!({
            "type": "string",
            "description": "Optional isolated browser session name."
        }),
    );
    props.insert(
        "namespace".to_string(),
        json!({
            "type": "string",
            "description": "Optional namespace that isolates daemon sockets and restore-state directories."
        }),
    );
    props.insert(
        "restore".to_string(),
        json!({
            "oneOf": [
                { "type": "boolean" },
                { "type": "string" }
            ],
            "description": "Restore and auto-save browser state. true uses the current session as the key; a string uses that explicit key."
        }),
    );
    props.insert(
        "restoreSave".to_string(),
        json!({
            "type": "string",
            "enum": ["auto", "always", "never"],
            "description": "Auto-save policy for restored state."
        }),
    );
    props.insert(
        "restoreCheckUrl".to_string(),
        json!({
            "type": "string",
            "description": "Optional URL pattern that restored state must match."
        }),
    );
    props.insert(
        "restoreCheckText".to_string(),
        json!({
            "type": "string",
            "description": "Optional page text that restored state must expose."
        }),
    );
    props.insert(
        "restoreCheckFn".to_string(),
        json!({
            "type": "string",
            "description": "Optional JavaScript expression that must evaluate truthy after restore."
        }),
    );
    props.insert(
        "allowedDomains".to_string(),
        json!({
            "type": "array",
            "items": { "type": "string" },
            "description": "Restrict browser and read traffic to these domain patterns. Chromium sessions also disable RTCPeerConnection while this is active."
        }),
    );
    props.insert(
        "caCert".to_string(),
        json!({
            "type": "string",
            "description": "Path to a CA certificate or PEM bundle trusted by a locally launched Chromium browser on Linux."
        }),
    );
    props.insert(
        "clearCaCert".to_string(),
        json!({
            "type": "boolean",
            "description": "Explicitly clear CA trust retained by the running browser session."
        }),
    );
    props.insert(
        "idleTimeout".to_string(),
        json!({
            "type": "string",
            "description": "Daemon idle timeout such as 30s, 5m, 1h, or raw milliseconds. Defaults to 1h; 0 disables idle shutdown."
        }),
    );
    props.insert(
        "profile".to_string(),
        json!({
            "type": "string",
            "description": "Persistent browser profile directory, forwarded through --profile. Omit to use configured defaults."
        }),
    );
    props.insert(
        "extraArgs".to_string(),
        json!({
            "type": "array",
            "items": { "type": "string" },
            "description": "Advanced: extra CLI arguments for this command, preserving full CLI parity."
        }),
    );
    props.insert(
        "timeoutMs".to_string(),
        json!({
            "type": "integer",
            "minimum": 1,
            "default": DEFAULT_TIMEOUT_MS,
            "description": "Maximum time to wait for this tool call."
        }),
    );

    let mut schema = serde_json::Map::new();
    schema.insert("type".to_string(), json!("object"));
    schema.insert("properties".to_string(), Value::Object(props));
    schema.insert("additionalProperties".to_string(), json!(false));
    if !required.is_empty() {
        schema.insert("required".to_string(), json!(required));
    }

    json!({
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": Value::Object(schema),
        "annotations": tool_annotations(name),
    })
}

fn tool_annotations(name: &str) -> Value {
    json!({
        "readOnlyHint": is_read_only_tool(name),
        "openWorldHint": is_open_world_tool(name),
    })
}

fn is_read_only_tool(name: &str) -> bool {
    matches!(
        name,
        TOOL_SNAPSHOT
            | TOOL_PAGE_OUTLINE
            | TOOL_PAGE_LINKS
            | TOOL_DOM_CHUNK
            | TOOL_GESTURES
            | TOOL_READ
            | TOOL_WAIT_MS
            | TOOL_WAIT_FOR_SELECTOR
            | TOOL_WAIT_FOR_TEXT
            | TOOL_WAIT_FOR_URL
            | TOOL_WAIT_FOR_LOAD
            | TOOL_WAIT_FOR_FUNCTION
            | TOOL_GET_TEXT
            | TOOL_GET_HTML
            | TOOL_HTML_SEARCH
            | TOOL_GET_VALUE
            | TOOL_GET_ATTR
            | TOOL_GET_COUNT
            | TOOL_GET_BOX
            | TOOL_GET_URL
            | TOOL_GET_TITLE
            | TOOL_IS_VISIBLE
            | TOOL_IS_ENABLED
            | TOOL_IS_CHECKED
            | TOOL_NETWORK_REQUEST
            | TOOL_STORAGE_GET
            | TOOL_COOKIES_GET
            | TOOL_TAB_LIST
            | TOOL_DIALOG_STATUS
            | TOOL_STATE_LIST
            | TOOL_STATE_SHOW
            | TOOL_SESSION_LIST
            | TOOL_SESSION_INFO
            | TOOL_SKILLS_LIST
            | TOOL_SKILLS_GET
            | TOOL_SKILLS_PATH
    )
}

fn is_open_world_tool(name: &str) -> bool {
    !matches!(
        name,
        TOOL_GESTURES
            | TOOL_SESSION_LIST
            | TOOL_SESSION_INFO
            | TOOL_SKILLS_LIST
            | TOOL_SKILLS_GET
            | TOOL_SKILLS_PATH
            | TOOL_DOCTOR
            | TOOL_INSTALL
    )
}

fn is_known_tool(name: &str) -> bool {
    tools()
        .iter()
        .any(|tool| tool.get("name").and_then(|v| v.as_str()) == Some(name))
}

fn wait_tool(
    name: &str,
    title: &str,
    description: &str,
    mut properties: Value,
    required: &[&str],
) -> Value {
    if let Value::Object(ref mut props) = properties {
        props.insert(
            "waitTimeoutMs".to_string(),
            json!({
                "type": "integer",
                "minimum": 1,
                "description": "Maximum time for the browser wait condition."
            }),
        );
    }
    tool(name, title, description, properties, required)
}

fn call_tool(params: Option<&Value>, config: &McpConfig) -> Result<Value, ProtocolError> {
    let params =
        params.ok_or_else(|| ProtocolError::invalid_params("tools/call requires params"))?;
    let name = params
        .get("name")
        .and_then(|v| v.as_str())
        .ok_or_else(|| ProtocolError::invalid_params("tools/call params.name must be a string"))?;
    let arguments = params.get("arguments").unwrap_or(&Value::Null);

    if !is_known_tool(name) {
        return Err(ProtocolError::invalid_params(format!(
            "Unknown tool: {}",
            name
        )));
    }

    if !config.allows(name) {
        if config.camoufox && !is_camoufox_tool(name) {
            return Err(ProtocolError::invalid_params(format!("Tool {name} is unavailable on Camoufox, including in the all profile. Use a supported tool or a separate engine session.")));
        }
        return Err(ProtocolError::invalid_params(format!(
            "Tool {} is not enabled by the active MCP tools profile(s): {}. Restart with `agent-browser mcp --tools all` or add a profile that includes it.",
            name,
            config.profile_names().join(", ")
        )));
    }

    let resolved_arguments;
    let arguments = if config.camoufox {
        resolved_arguments = camoufox_arguments(name, arguments)?;
        &resolved_arguments
    } else {
        arguments
    };

    match name {
        TOOL_TOOLS_PROFILES => call_tools_profiles(config),
        TOOL_OPEN => call_open(arguments),
        TOOL_READ => call_read(arguments),
        TOOL_SNAPSHOT => call_snapshot(arguments),
        TOOL_PAGE_OUTLINE => call_optional_one(arguments, &["page-outline"], "selector"),
        TOOL_PAGE_LINKS => call_paginated_page(arguments, "page-links"),
        TOOL_DOM_CHUNK => call_paginated_page(arguments, "dom-chunk"),
        TOOL_CLICK => call_click(arguments),
        TOOL_BACK => call_literal(arguments, &["back"]),
        TOOL_FORWARD => call_literal(arguments, &["forward"]),
        TOOL_RELOAD => call_literal(arguments, &["reload"]),
        TOOL_DBLCLICK => call_simple_selector(arguments, "dblclick"),
        TOOL_FILL => call_fill(arguments),
        TOOL_TYPE => call_type(arguments),
        TOOL_PRESS => call_press(arguments),
        TOOL_HOVER => call_simple_selector(arguments, "hover"),
        TOOL_HOVER_HOLD => call_cli_tool(arguments, hover_hold_args(arguments)?, None),
        TOOL_FOCUS => call_simple_selector(arguments, "focus"),
        TOOL_CHECK => call_simple_selector(arguments, "check"),
        TOOL_UNCHECK => call_simple_selector(arguments, "uncheck"),
        TOOL_SELECT => call_select(arguments),
        TOOL_DRAG => call_drag(arguments),
        TOOL_GESTURES => call_gestures(arguments),
        TOOL_GESTURE => call_gesture(arguments),
        TOOL_UPLOAD => call_upload(arguments),
        TOOL_DOWNLOAD => call_download(arguments),
        TOOL_DOWNLOADS => {
            let mut args = vec!["network".to_string(), "downloads".to_string()];
            if optional_bool(arguments, "clear")?.unwrap_or(false) {
                args.push("--clear".to_string());
            }
            call_cli_tool(arguments, args, None)
        }
        TOOL_SCROLL => call_scroll(arguments),
        TOOL_SCROLL_INTO_VIEW => call_simple_selector(arguments, "scrollintoview"),
        TOOL_WAIT_MS => call_wait_ms(arguments),
        TOOL_WAIT_FOR_SELECTOR => call_wait_flag(arguments, None, "selector"),
        TOOL_WAIT_FOR_TEXT => call_wait_flag(arguments, Some("--text"), "text"),
        TOOL_WAIT_FOR_URL => call_wait_flag(arguments, Some("--url"), "url"),
        TOOL_WAIT_FOR_LOAD => call_wait_flag(arguments, Some("--load"), "state"),
        TOOL_WAIT_FOR_FUNCTION => call_wait_flag(arguments, Some("--fn"), "expression"),
        TOOL_WAIT_FOR_DOWNLOAD => call_wait_download(arguments),
        TOOL_SCREENSHOT => call_screenshot(arguments),
        TOOL_GET_TEXT => call_get_selector(arguments, "text"),
        TOOL_GET_HTML => call_get_selector(arguments, "html"),
        TOOL_HTML_SEARCH => call_html_search(arguments),
        TOOL_GET_VALUE => call_get_selector(arguments, "value"),
        TOOL_GET_ATTR => call_get_attr(arguments),
        TOOL_GET_COUNT => call_get_selector(arguments, "count"),
        TOOL_GET_BOX => call_get_selector(arguments, "box"),
        TOOL_GET_URL => call_cli_tool(arguments, vec!["get".to_string(), "url".to_string()], None),
        TOOL_GET_TITLE => call_cli_tool(
            arguments,
            vec!["get".to_string(), "title".to_string()],
            None,
        ),
        TOOL_IS_VISIBLE => call_is(arguments, "visible"),
        TOOL_IS_ENABLED => call_is(arguments, "enabled"),
        TOOL_IS_CHECKED => call_is(arguments, "checked"),
        TOOL_FIND => call_find(arguments),
        TOOL_SET_OFFLINE => call_set_bool(arguments, "offline", "enabled"),
        TOOL_SET_HEADERS => call_set_headers(arguments),
        TOOL_SET_CREDENTIALS => call_set_credentials(arguments),
        TOOL_NETWORK_ROUTE => call_network_route(arguments),
        TOOL_NETWORK_UNROUTE => call_optional_one(arguments, &["network", "unroute"], "url"),
        TOOL_NETWORK_REQUESTS => call_network_requests(arguments),
        TOOL_NETWORK_WEBSOCKETS => call_network_websockets(arguments),
        TOOL_NETWORK_REQUEST => call_one_string(arguments, "network request", "requestId"),
        TOOL_NETWORK_HAR_START => {
            let mut args: Vec<String> = ["network", "har", "start"]
                .iter()
                .map(|s| s.to_string())
                .collect();
            if let Some(content) = optional_string(arguments, "content")? {
                if !content.is_empty() {
                    args.push("--content".to_string());
                    args.push(content);
                }
            }
            call_cli_tool(arguments, args, None)
        }
        TOOL_NETWORK_HAR_STOP => call_optional_one(arguments, &["network", "har", "stop"], "path"),
        TOOL_STORAGE_GET => call_storage_get(arguments),
        TOOL_STORAGE_SET => call_storage_set(arguments),
        TOOL_STORAGE_CLEAR => call_storage_clear(arguments),
        TOOL_COOKIES_GET => call_literal(arguments, &["cookies", "get"]),
        TOOL_COOKIES_SET => call_cookies_set(arguments),
        TOOL_COOKIES_CLEAR => call_literal(arguments, &["cookies", "clear"]),
        TOOL_TAB_NEW => call_tab_new(arguments),
        TOOL_TAB_LIST => call_literal(arguments, &["tab", "list"]),
        TOOL_TAB_SWITCH => call_one_string(arguments, "tab", "tab"),
        TOOL_TAB_CLOSE => call_optional_one(arguments, &["tab", "close"], "tab"),
        TOOL_FRAME_SWITCH => call_one_string(arguments, "frame", "frame"),
        TOOL_FRAME_MAIN => call_literal(arguments, &["frame", "main"]),
        TOOL_DIALOG_STATUS => call_literal(arguments, &["dialog", "status"]),
        TOOL_DIALOG_ACCEPT => call_optional_one(arguments, &["dialog", "accept"], "text"),
        TOOL_DIALOG_DISMISS => call_literal(arguments, &["dialog", "dismiss"]),
        TOOL_CONSOLE => call_clearable(arguments, "console"),
        TOOL_ERRORS => call_clearable(arguments, "errors"),
        TOOL_STATE_LIST => call_literal(arguments, &["state", "list"]),
        TOOL_STATE_CLEAR => call_state_clear(arguments),
        TOOL_STATE_SHOW => call_one_string(arguments, "state show", "path"),
        TOOL_STATE_CLEAN => call_state_clean(arguments),
        TOOL_STATE_RENAME => call_state_rename(arguments),
        TOOL_BATCH => call_batch(arguments),
        TOOL_CONFIRM => call_one_string(arguments, "confirm", "id"),
        TOOL_DENY => call_one_string(arguments, "deny", "id"),
        TOOL_SESSION_LIST => call_literal(arguments, &["session", "list"]),
        TOOL_SESSION_INFO => call_literal(arguments, &["session", "info"]),
        TOOL_SKILLS_LIST => call_literal(arguments, &["skills", "list"]),
        TOOL_SKILLS_GET => call_skills_get(arguments),
        TOOL_SKILLS_PATH => call_optional_one(arguments, &["skills", "path"], "name"),
        TOOL_DOCTOR => call_doctor(arguments),
        TOOL_INSTALL => call_install(arguments),
        TOOL_CHAT => call_chat(arguments),
        TOOL_EVAL => call_eval(arguments),
        TOOL_CLOSE => call_close(arguments),
        _ => unreachable!("known MCP tool missing call handler: {}", name),
    }
}

fn call_tools_profiles(config: &McpConfig) -> Result<Value, ProtocolError> {
    let profiles = tool_profile_summaries(config);
    let compose = if config.camoufox {
        "core,network,gestures"
    } else {
        "core,network"
    };
    let text = format!(
        "Active MCP tools profile(s): {}\n\nAvailable profiles:\n{}\n\nRestart the MCP server with `agent-browser mcp --tools <profile>` or combine profiles with commas, for example `agent-browser mcp --tools {compose}`. The all profile exposes only tools available for this server's engine.",
        config.profile_names().join(", "),
        profiles
            .iter()
            .filter_map(|profile| {
                Some(format!(
                    "- {}: {} tools. {}",
                    profile.get("name")?.as_str()?,
                    profile.get("toolCount")?.as_u64()?,
                    profile.get("description")?.as_str()?
                ))
            })
            .collect::<Vec<_>>()
            .join("\n")
    );

    Ok(json!({
        "content": [{
            "type": "text",
            "text": text,
        }],
        "structuredContent": {
            "activeProfiles": config.profile_names(),
            "profiles": profiles,
            "usage": {
                "default": "agent-browser mcp",
                "compose": format!("agent-browser mcp --tools {compose}"),
                "all": "agent-browser mcp --tools all",
            }
        },
        "isError": false,
    }))
}

fn call_cli_tool(
    arguments: &Value,
    command_args: Vec<String>,
    stdin_body: Option<String>,
) -> Result<Value, ProtocolError> {
    validate_arguments_object(arguments)?;
    let session = optional_string(arguments, "session")?;
    let timeout_ms = optional_timeout(arguments)?;
    let cli_args = cli_tool_args(arguments, command_args, session.as_deref())?;

    let run = run_cli(&cli_args, stdin_body, timeout_ms).map_err(|e| {
        ProtocolError::invalid_params(format!("Failed to run agent-browser: {}", e))
    })?;
    Ok(tool_result_from_run(run))
}

fn cli_tool_args(
    arguments: &Value,
    command_args: Vec<String>,
    session: Option<&str>,
) -> Result<Vec<String>, ProtocolError> {
    let extra_args = optional_string_array(arguments, "extraArgs")?.unwrap_or_default();
    let mut args = vec!["--json".to_string()];
    append_common_global_args(&mut args, arguments, session)?;
    args.extend(command_args);
    args.extend(extra_args);
    Ok(args)
}

fn command_parts(command: &str) -> Vec<String> {
    command
        .split_whitespace()
        .map(ToString::to_string)
        .collect()
}

fn call_literal(arguments: &Value, parts: &[&str]) -> Result<Value, ProtocolError> {
    call_cli_tool(
        arguments,
        parts.iter().map(|s| s.to_string()).collect(),
        None,
    )
}

fn call_one_string(arguments: &Value, command: &str, key: &str) -> Result<Value, ProtocolError> {
    let mut args = command_parts(command);
    args.push(required_string(arguments, key)?);
    call_cli_tool(arguments, args, None)
}

fn call_optional_one(arguments: &Value, parts: &[&str], key: &str) -> Result<Value, ProtocolError> {
    let mut args: Vec<String> = parts.iter().map(|s| s.to_string()).collect();
    if let Some(value) = optional_string(arguments, key)? {
        if !value.is_empty() {
            args.push(value);
        }
    }
    call_cli_tool(arguments, args, None)
}

/// Route structured Camoufox pagination through the canonical CLI parser.
fn call_paginated_page(arguments: &Value, command: &str) -> Result<Value, ProtocolError> {
    call_cli_tool(arguments, paginated_page_args(arguments, command)?, None)
}

fn paginated_page_args(arguments: &Value, command: &str) -> Result<Vec<String>, ProtocolError> {
    let mut args = vec![command.to_string()];
    if let Some(selector) = optional_string(arguments, "selector")? {
        args.push(selector);
    }
    if let Some(cursor) = optional_string(arguments, "cursor")? {
        args.push("--cursor".to_string());
        args.push(cursor);
    }
    if let Some(limit) = optional_u64(arguments, "limit")? {
        args.push("--limit".to_string());
        args.push(limit.to_string());
    }
    Ok(args)
}

/// Build the CLI args for the open tool. Explicit booleans are forwarded as
/// `--flag true|false` so an MCP caller can override env/config defaults
/// (e.g. headed: false with AGENT_BROWSER_HEADED=1 set); an absent field
/// sends nothing and leaves the env/config resolution to the CLI.
fn open_args(arguments: &Value) -> Result<Vec<String>, ProtocolError> {
    let mut args = Vec::new();
    if let Some(headed) = optional_bool(arguments, "headed")? {
        args.push("--headed".to_string());
        args.push(headed.to_string());
    }
    if let Some(adblock) = optional_bool(arguments, "adblock")? {
        args.push("--adblock".to_string());
        args.push(adblock.to_string());
    }
    if let Some(webmcp) = optional_bool(arguments, "webmcp")? {
        args.push("--no-webmcp".to_string());
        args.push((!webmcp).to_string());
    }
    args.push("open".to_string());
    if let Some(url) = optional_string(arguments, "url")? {
        if !url.is_empty() {
            args.push(url);
        }
    }
    Ok(args)
}

fn call_open(arguments: &Value) -> Result<Value, ProtocolError> {
    let args = open_args(arguments)?;
    call_cli_tool(arguments, args, None)
}

fn call_read(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["read".to_string()];
    if optional_bool(arguments, "raw")?.unwrap_or(false) {
        args.push("--raw".to_string());
    }
    if optional_bool(arguments, "requireMd")?.unwrap_or(false) {
        args.push("--require-md".to_string());
    }
    if let Some(llms) = optional_string(arguments, "llms")? {
        args.push("--llms".to_string());
        args.push(llms);
    }
    if optional_bool(arguments, "outline")?.unwrap_or(false) {
        args.push("--outline".to_string());
    }
    if let Some(filter) = optional_string(arguments, "filter")? {
        args.push("--filter".to_string());
        args.push(filter);
    }
    if let Some(timeout) = optional_u64(arguments, "readTimeoutMs")? {
        args.push("--timeout".to_string());
        args.push(timeout.to_string());
    }
    if let Some(url) = optional_string(arguments, "url")? {
        args.push(url);
    }
    call_cli_tool(arguments, args, None)
}

/// Preserve the shared snapshot contract while the CLI resolves defaults.
fn call_snapshot(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["snapshot".to_string()];
    if let Some(depth) = optional_u64(arguments, "depth")? {
        args.push("-d".to_string());
        args.push(depth.to_string());
    }
    if let Some(selector) = optional_string(arguments, "selector")? {
        args.push("-s".to_string());
        args.push(selector);
    }
    if optional_bool(arguments, "quiet")?.unwrap_or(false) {
        args.push("--snapshot-quiet".to_string());
    }

    call_cli_tool(arguments, args, None)
}

fn call_simple_selector(arguments: &Value, command: &str) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    call_cli_tool(arguments, vec![command.to_string(), selector], None)
}

fn click_command_args(arguments: &Value) -> Result<Vec<String>, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    let mut args = vec!["click".to_string(), selector];
    if optional_bool(arguments, "newTab")?.unwrap_or(false) {
        args.push("--new-tab".to_string());
    }
    Ok(args)
}

fn call_click(arguments: &Value) -> Result<Value, ProtocolError> {
    call_cli_tool(arguments, click_command_args(arguments)?, None)
}

fn call_fill(arguments: &Value) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    let text = required_string(arguments, "text")?;
    call_cli_tool(arguments, vec!["fill".to_string(), selector, text], None)
}

fn call_type(arguments: &Value) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    let text = required_string(arguments, "text")?;
    let mut args = vec!["type".to_string(), selector, text];
    if optional_bool(arguments, "clear")?.unwrap_or(false) {
        args.push("--clear".to_string());
    }
    if let Some(delay) = optional_u64(arguments, "delayMs")? {
        args.push("--delay".to_string());
        args.push(delay.to_string());
    }
    call_cli_tool(arguments, args, None)
}

fn call_press(arguments: &Value) -> Result<Value, ProtocolError> {
    let key = required_string(arguments, "key")?;
    call_cli_tool(arguments, vec!["press".to_string(), key], None)
}

fn call_drag(arguments: &Value) -> Result<Value, ProtocolError> {
    let source = required_string(arguments, "source")?;
    let target = required_string(arguments, "target")?;
    call_cli_tool(arguments, vec!["drag".to_string(), source, target], None)
}

fn call_gestures(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["gestures".to_string()];
    if let Some(name) = optional_string(arguments, "name")? {
        args.push(name);
    }
    call_cli_tool(arguments, args, None)
}

fn call_gesture(arguments: &Value) -> Result<Value, ProtocolError> {
    let name = required_string(arguments, "name")?;
    let params = arguments
        .get("params")
        .filter(|value| value.is_object())
        .ok_or_else(|| ProtocolError::invalid_params("params must be a JSON object"))?;
    let mut args = vec![
        "gesture".to_string(),
        name,
        "--params".to_string(),
        params.to_string(),
    ];
    if let Some(observe) = optional_string(arguments, "observe")? {
        args.extend(["--observe".to_string(), observe]);
    }
    call_cli_tool(arguments, args, None)
}

fn call_upload(arguments: &Value) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    let files = required_string_array(arguments, "files")?;
    let mut args = vec!["upload".to_string(), selector];
    args.extend(files);
    call_cli_tool(arguments, args, None)
}

fn call_download(arguments: &Value) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    let path = required_string(arguments, "path")?;
    call_cli_tool(
        arguments,
        vec!["download".to_string(), selector, path],
        None,
    )
}

fn call_select(arguments: &Value) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    let values = required_string_array(arguments, "values")?;
    let mut args = vec!["select".to_string(), selector];
    args.extend(values);
    call_cli_tool(arguments, args, None)
}

fn call_scroll(arguments: &Value) -> Result<Value, ProtocolError> {
    let direction = optional_string(arguments, "direction")?.unwrap_or_else(|| "down".to_string());
    let amount = optional_i64(arguments, "amount")?.unwrap_or(300);
    let mut args = vec!["scroll".to_string(), direction, amount.to_string()];
    if let Some(selector) = optional_string(arguments, "selector")? {
        args.push("--selector".to_string());
        args.push(selector);
    }
    call_cli_tool(arguments, args, None)
}

fn call_wait_ms(arguments: &Value) -> Result<Value, ProtocolError> {
    let ms = required_u64(arguments, "ms")?;
    call_cli_tool(arguments, vec!["wait".to_string(), ms.to_string()], None)
}

fn call_wait_flag(
    arguments: &Value,
    flag: Option<&str>,
    value_key: &str,
) -> Result<Value, ProtocolError> {
    let value = required_string(arguments, value_key)?;
    let mut args = vec!["wait".to_string()];
    if let Some(flag) = flag {
        args.push(flag.to_string());
        args.push(value);
    } else {
        args.push(value);
    }
    if let Some(timeout) = optional_u64(arguments, "waitTimeoutMs")? {
        args.push("--timeout".to_string());
        args.push(timeout.to_string());
    }
    call_cli_tool(arguments, args, None)
}

fn call_wait_download(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["wait".to_string(), "--download".to_string()];
    if let Some(path) = optional_string(arguments, "path")? {
        args.push(path);
    }
    if let Some(timeout) = optional_u64(arguments, "waitTimeoutMs")? {
        args.push("--timeout".to_string());
        args.push(timeout.to_string());
    }
    call_cli_tool(arguments, args, None)
}

fn call_screenshot(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = Vec::new();
    if let Some(dir) = optional_string(arguments, "screenshotDir")? {
        args.push("--screenshot-dir".to_string());
        args.push(dir);
    }

    args.push("screenshot".to_string());
    if let Some(path) = optional_string(arguments, "path")? {
        args.push(path);
    }
    if optional_bool(arguments, "fullPage")?.unwrap_or(false) {
        args.push("--full".to_string());
    }
    args.push("--inline-image".to_string());

    validate_arguments_object(arguments)?;
    let session = optional_string(arguments, "session")?;
    let timeout_ms = optional_timeout(arguments)?;
    let cli_args = cli_tool_args(arguments, args, session.as_deref())?;
    let run = run_cli(&cli_args, None, timeout_ms).map_err(|e| {
        ProtocolError::invalid_params(format!("Failed to run agent-browser: {}", e))
    })?;
    let parsed = serde_json::from_str::<Value>(run.stdout.trim()).ok();
    let Some(parsed) = parsed else {
        return Ok(tool_result_from_run(run));
    };
    let image = parsed
        .get("data")
        .and_then(|data| data.get("image"))
        .and_then(|value| value.as_str())
        .map(str::to_string);
    let mut sanitized = parsed.clone();
    if let Some(data) = sanitized
        .get_mut("data")
        .and_then(|value| value.as_object_mut())
    {
        data.remove("image");
    }
    let sanitized_stdout = serde_json::to_string(&sanitized).unwrap_or_default();
    let sanitized_run = CliRun {
        exit_code: run.exit_code,
        stdout: sanitized_stdout,
        stderr: run.stderr,
    };
    let mut result = tool_result_from_run(sanitized_run);
    if image.is_some() {
        let parsed_success = parsed
            .get("success")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if run.exit_code == Some(0) && parsed_success {
            if let Some(image) = image {
                if image.len() <= (MAX_IMAGE_BYTES as usize) * 4 / 3 {
                    result["content"].as_array_mut().map(|content| {
                        content.push(json!({
                            "type": "image",
                            "data": image,
                            "mimeType": "image/png",
                        }))
                    });
                }
            }
        }
    }
    Ok(result)
}

fn call_get_selector(arguments: &Value, what: &str) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    call_cli_tool(
        arguments,
        vec!["get".to_string(), what.to_string(), selector],
        None,
    )
}

fn call_html_search(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec![
        "html-search".to_string(),
        required_string(arguments, "query")?,
    ];
    if let Some(regex) = optional_string(arguments, "regex")? {
        if !regex.is_empty() {
            args.push("--regex".to_string());
            args.push(regex);
        }
    }
    if let Some(selector) = optional_string(arguments, "selector")? {
        if !selector.is_empty() {
            args.push("--selector".to_string());
            args.push(selector);
        }
    }
    if let Some(max_results) = optional_u64(arguments, "maxResults")? {
        if !(1..=20).contains(&max_results) {
            return Err(ProtocolError::invalid_params(
                "maxResults must be between 1 and 20",
            ));
        }
        args.push("--max-results".to_string());
        args.push(max_results.to_string());
    }
    if let Some(context_chars) = optional_u64(arguments, "contextChars")? {
        if !(1..=400).contains(&context_chars) {
            return Err(ProtocolError::invalid_params(
                "contextChars must be between 1 and 400",
            ));
        }
        args.push("--context-chars".to_string());
        args.push(context_chars.to_string());
    }
    call_cli_tool(arguments, args, None)
}

fn call_get_attr(arguments: &Value) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    let name = required_string(arguments, "name")?;
    call_cli_tool(
        arguments,
        vec!["get".to_string(), "attr".to_string(), selector, name],
        None,
    )
}

fn call_is(arguments: &Value, what: &str) -> Result<Value, ProtocolError> {
    let selector = required_string(arguments, "selector")?;
    call_cli_tool(
        arguments,
        vec!["is".to_string(), what.to_string(), selector],
        None,
    )
}

fn hover_hold_args(arguments: &Value) -> Result<Vec<String>, ProtocolError> {
    let selector = optional_string(arguments, "selector")?;
    let stop = optional_bool(arguments, "stop")?;
    let max_ms = optional_u64(arguments, "maxMs")?;
    if max_ms.is_some_and(|value| !(1000..=120000).contains(&value)) {
        return Err(ProtocolError::invalid_params(
            "maxMs must be between 1000 and 120000",
        ));
    }
    let mut args = vec!["hover-hold".to_string()];
    match (selector, stop) {
        (Some(selector), None) if !selector.trim().is_empty() && selector != "stop" => {
            args.push(selector);
            if let Some(max_ms) = max_ms {
                args.extend(["--max-ms".to_string(), max_ms.to_string()]);
            }
        }
        (None, Some(true)) if max_ms.is_none() => args.push("stop".to_string()),
        _ => return Err(ProtocolError::invalid_params(
            "Provide exactly one non-empty selector or stop=true. The selector 'stop' is reserved, and stopping does not accept maxMs.",
        )),
    }
    Ok(args)
}

fn call_find(arguments: &Value) -> Result<Value, ProtocolError> {
    let locator = required_string(arguments, "locator")?;
    let value = required_string(arguments, "value")?;
    let mut args = vec!["find".to_string(), locator.clone()];
    if locator == "nth" {
        let index = optional_i64(arguments, "index")?.unwrap_or(0);
        args.push(index.to_string());
    }
    args.push(value);
    let action = optional_string(arguments, "action")?;
    let text = optional_string(arguments, "text")?;
    let name = optional_string(arguments, "name")?;
    let exact = optional_bool(arguments, "exact")?.unwrap_or(false);
    if let Some(action) = action {
        args.push(action);
    } else if name.is_some() || exact || text.is_some() {
        args.push("click".to_string());
    }
    if let Some(text) = text {
        args.push(text);
    }
    if let Some(name) = name {
        args.push("--name".to_string());
        args.push(name);
    }
    if exact {
        args.push("--exact".to_string());
    }
    call_cli_tool(arguments, args, None)
}

fn call_set_bool(arguments: &Value, setting: &str, key: &str) -> Result<Value, ProtocolError> {
    let enabled = optional_bool(arguments, key)?.unwrap_or(true);
    call_cli_tool(
        arguments,
        vec![
            "set".to_string(),
            setting.to_string(),
            if enabled { "on" } else { "off" }.to_string(),
        ],
        None,
    )
}

fn call_set_headers(arguments: &Value) -> Result<Value, ProtocolError> {
    let headers = optional_value(arguments, "headers")?
        .ok_or_else(|| ProtocolError::invalid_params("headers must be an object"))?;
    let headers_json = serde_json::to_string(headers)
        .map_err(|e| ProtocolError::invalid_params(format!("headers encode error: {}", e)))?;
    call_cli_tool(
        arguments,
        vec!["set".to_string(), "headers".to_string(), headers_json],
        None,
    )
}

fn call_set_credentials(arguments: &Value) -> Result<Value, ProtocolError> {
    let username = required_string(arguments, "username")?;
    let password = required_string(arguments, "password")?;
    call_cli_tool(
        arguments,
        vec![
            "set".to_string(),
            "credentials".to_string(),
            username,
            password,
        ],
        None,
    )
}

fn call_network_route(arguments: &Value) -> Result<Value, ProtocolError> {
    let url = required_string(arguments, "url")?;
    let mut args = vec!["network".to_string(), "route".to_string(), url];
    if optional_bool(arguments, "abort")?.unwrap_or(false) {
        args.push("--abort".to_string());
    }
    if let Some(body) = optional_string(arguments, "body")? {
        args.push("--body".to_string());
        args.push(body);
    }
    if let Some(resource_type) = optional_string(arguments, "resourceType")? {
        args.push("--resource-type".to_string());
        args.push(resource_type);
    }
    call_cli_tool(arguments, args, None)
}

fn call_network_requests(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["network".to_string(), "requests".to_string()];
    if optional_bool(arguments, "clear")?.unwrap_or(false) {
        args.push("--clear".to_string());
    }
    for (key, flag) in [
        ("filter", "--filter"),
        ("type", "--type"),
        ("method", "--method"),
        ("status", "--status"),
    ] {
        if let Some(value) = optional_string(arguments, key)? {
            args.push(flag.to_string());
            args.push(value);
        }
    }
    call_cli_tool(arguments, args, None)
}

fn call_network_websockets(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["network".to_string(), "websockets".to_string()];
    if optional_bool(arguments, "clear")?.unwrap_or(false) {
        args.push("--clear".to_string());
    }
    if let Some(filter) = optional_string(arguments, "filter")? {
        args.push("--filter".to_string());
        args.push(filter);
    }
    call_cli_tool(arguments, args, None)
}

fn call_storage_get(arguments: &Value) -> Result<Value, ProtocolError> {
    let storage_type = required_string(arguments, "storageType")?;
    let mut args = vec!["storage".to_string(), storage_type, "get".to_string()];
    if let Some(key) = optional_string(arguments, "key")? {
        args.push(key);
    }
    call_cli_tool(arguments, args, None)
}

fn call_storage_set(arguments: &Value) -> Result<Value, ProtocolError> {
    let storage_type = required_string(arguments, "storageType")?;
    let key = required_string(arguments, "key")?;
    let value = required_string(arguments, "value")?;
    call_cli_tool(
        arguments,
        vec![
            "storage".to_string(),
            storage_type,
            "set".to_string(),
            key,
            value,
        ],
        None,
    )
}

fn call_storage_clear(arguments: &Value) -> Result<Value, ProtocolError> {
    let storage_type = required_string(arguments, "storageType")?;
    call_cli_tool(
        arguments,
        vec!["storage".to_string(), storage_type, "clear".to_string()],
        None,
    )
}

fn call_cookies_set(arguments: &Value) -> Result<Value, ProtocolError> {
    let name = required_string(arguments, "name")?;
    let value = required_string(arguments, "value")?;
    let mut args = vec!["cookies".to_string(), "set".to_string(), name, value];
    for (key, flag) in [
        ("url", "--url"),
        ("domain", "--domain"),
        ("path", "--path"),
        ("sameSite", "--sameSite"),
    ] {
        if let Some(value) = optional_string(arguments, key)? {
            args.push(flag.to_string());
            args.push(value);
        }
    }
    if let Some(value) = optional_i64(arguments, "expires")? {
        args.push("--expires".to_string());
        args.push(value.to_string());
    }
    if optional_bool(arguments, "httpOnly")?.unwrap_or(false) {
        args.push("--httpOnly".to_string());
    }
    if optional_bool(arguments, "secure")?.unwrap_or(false) {
        args.push("--secure".to_string());
    }
    call_cli_tool(arguments, args, None)
}

fn call_tab_new(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["tab".to_string(), "new".to_string()];
    if let Some(url) = optional_string(arguments, "url")? {
        args.push(url);
    }
    if let Some(label) = optional_string(arguments, "label")? {
        args.push("--label".to_string());
        args.push(label);
    }
    call_cli_tool(arguments, args, None)
}

fn call_clearable(arguments: &Value, command: &str) -> Result<Value, ProtocolError> {
    let mut args = vec![command.to_string()];
    if optional_bool(arguments, "clear")?.unwrap_or(false) {
        args.push("--clear".to_string());
    }
    call_cli_tool(arguments, args, None)
}

fn call_state_clear(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["state".to_string(), "clear".to_string()];
    if optional_bool(arguments, "all")?.unwrap_or(false) {
        args.push("--all".to_string());
    }
    if let Some(name) = optional_string(arguments, "name")? {
        args.push(name);
    }
    call_cli_tool(arguments, args, None)
}

fn call_state_clean(arguments: &Value) -> Result<Value, ProtocolError> {
    let days = required_u64(arguments, "olderThanDays")?;
    call_cli_tool(
        arguments,
        vec![
            "state".to_string(),
            "clean".to_string(),
            "--older-than".to_string(),
            days.to_string(),
        ],
        None,
    )
}

fn call_state_rename(arguments: &Value) -> Result<Value, ProtocolError> {
    let old_name = required_string(arguments, "oldName")?;
    let new_name = required_string(arguments, "newName")?;
    call_cli_tool(
        arguments,
        vec![
            "state".to_string(),
            "rename".to_string(),
            old_name,
            new_name,
        ],
        None,
    )
}

fn call_batch(arguments: &Value) -> Result<Value, ProtocolError> {
    let commands_value = optional_value(arguments, "commands")?
        .ok_or_else(|| ProtocolError::invalid_params("commands must be an array"))?;
    let commands = commands_value
        .as_array()
        .ok_or_else(|| ProtocolError::invalid_params("commands must be an array"))?;
    let mut parsed_commands = Vec::with_capacity(commands.len());
    for (i, command) in commands.iter().enumerate() {
        let items = command.as_array().ok_or_else(|| {
            ProtocolError::invalid_params(format!("commands[{}] must be an array", i))
        })?;
        let mut args = Vec::with_capacity(items.len());
        for (j, item) in items.iter().enumerate() {
            args.push(
                item.as_str()
                    .ok_or_else(|| {
                        ProtocolError::invalid_params(format!(
                            "commands[{}][{}] must be a string",
                            i, j
                        ))
                    })?
                    .to_string(),
            );
        }
        parsed_commands.push(args);
    }
    let mut args = vec!["batch".to_string()];
    if optional_bool(arguments, "bail")?.unwrap_or(false) {
        args.push("--bail".to_string());
    }
    let stdin = serde_json::to_string(&parsed_commands)
        .map_err(|e| ProtocolError::invalid_params(format!("commands encode error: {}", e)))?;
    call_cli_tool(arguments, args, Some(stdin))
}

fn call_skills_get(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["skills".to_string(), "get".to_string()];
    if optional_bool(arguments, "all")?.unwrap_or(false) {
        args.push("--all".to_string());
    }
    if let Some(names) = optional_string_array(arguments, "names")? {
        args.extend(names);
    }
    if optional_bool(arguments, "full")?.unwrap_or(false) {
        args.push("--full".to_string());
    }
    call_cli_tool(arguments, args, None)
}

/// Build the CLI args for the doctor tool. offline/quick/fix are parsed by
/// doctor as bare presence flags, so they are only sent when true; the
/// value-taking booleans are forwarded explicitly so callers can override
/// env/config defaults (e.g. headed: false with AGENT_BROWSER_HEADED=1).
fn doctor_args(arguments: &Value) -> Result<Vec<String>, ProtocolError> {
    let mut args = vec!["doctor".to_string()];
    for (key, flag) in [
        ("offline", "--offline"),
        ("quick", "--quick"),
        ("fix", "--fix"),
    ] {
        if optional_bool(arguments, key)?.unwrap_or(false) {
            args.push(flag.to_string());
        }
    }
    for (key, flag) in [("headed", "--headed"), ("debug", "--debug")] {
        if let Some(value) = optional_bool(arguments, key)? {
            args.push(flag.to_string());
            args.push(value.to_string());
        }
    }
    Ok(args)
}

fn call_doctor(arguments: &Value) -> Result<Value, ProtocolError> {
    let args = doctor_args(arguments)?;
    call_cli_tool(arguments, args, None)
}

fn call_install(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["install".to_string()];
    if optional_bool(arguments, "withDeps")?.unwrap_or(false) {
        args.push("--with-deps".to_string());
    }
    let mut arguments = arguments.clone();
    let camoufox = arguments.get("engine").and_then(Value::as_str) == Some("camoufox")
        || (arguments.get("engine").is_none()
            && env::var("AGENT_BROWSER_ENGINE").as_deref() == Ok("camoufox"));
    if camoufox && arguments.get("timeoutMs").is_none() {
        arguments["timeoutMs"] = json!(2_100_000);
    }
    call_cli_tool(&arguments, args, None)
}

fn call_chat(arguments: &Value) -> Result<Value, ProtocolError> {
    let message = required_string(arguments, "message")?;
    let mut args = Vec::new();
    if let Some(model) = optional_string(arguments, "model")? {
        args.push("--model".to_string());
        args.push(model);
    }
    if optional_bool(arguments, "verbose")?.unwrap_or(false) {
        args.push("--verbose".to_string());
    }
    if optional_bool(arguments, "quiet")?.unwrap_or(false) {
        args.push("--quiet".to_string());
    }
    args.push("chat".to_string());
    args.push(message);
    call_cli_tool(arguments, args, None)
}

fn call_eval(arguments: &Value) -> Result<Value, ProtocolError> {
    let script = required_string(arguments, "script")?;
    call_cli_tool(
        arguments,
        vec!["eval".to_string(), "--stdin".to_string()],
        Some(script),
    )
}

fn call_close(arguments: &Value) -> Result<Value, ProtocolError> {
    let mut args = vec!["close".to_string()];
    if optional_bool(arguments, "all")?.unwrap_or(false) {
        args.push("--all".to_string());
    }
    call_cli_tool(arguments, args, None)
}

fn validate_arguments_object(arguments: &Value) -> Result<(), ProtocolError> {
    if arguments.is_null() || arguments.is_object() {
        Ok(())
    } else {
        Err(ProtocolError::invalid_params(
            "tool arguments must be an object",
        ))
    }
}

fn optional_value<'a>(arguments: &'a Value, key: &str) -> Result<Option<&'a Value>, ProtocolError> {
    validate_arguments_object(arguments)?;
    Ok(arguments.get(key))
}

fn required_string(arguments: &Value, key: &str) -> Result<String, ProtocolError> {
    optional_value(arguments, key)?
        .and_then(|v| v.as_str())
        .map(ToString::to_string)
        .ok_or_else(|| ProtocolError::invalid_params(format!("{} must be a string", key)))
}

fn optional_string(arguments: &Value, key: &str) -> Result<Option<String>, ProtocolError> {
    match optional_value(arguments, key)? {
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(ProtocolError::invalid_params(format!(
            "{} must be a string",
            key
        ))),
        None => Ok(None),
    }
}

fn optional_bool(arguments: &Value, key: &str) -> Result<Option<bool>, ProtocolError> {
    match optional_value(arguments, key)? {
        Some(Value::Bool(value)) => Ok(Some(*value)),
        Some(_) => Err(ProtocolError::invalid_params(format!(
            "{} must be a boolean",
            key
        ))),
        None => Ok(None),
    }
}

fn required_u64(arguments: &Value, key: &str) -> Result<u64, ProtocolError> {
    optional_value(arguments, key)?
        .and_then(|v| v.as_u64())
        .ok_or_else(|| {
            ProtocolError::invalid_params(format!("{} must be a non-negative integer", key))
        })
}

fn optional_u64(arguments: &Value, key: &str) -> Result<Option<u64>, ProtocolError> {
    match optional_value(arguments, key)? {
        Some(v) => v.as_u64().map(Some).ok_or_else(|| {
            ProtocolError::invalid_params(format!("{} must be a non-negative integer", key))
        }),
        None => Ok(None),
    }
}

fn optional_i64(arguments: &Value, key: &str) -> Result<Option<i64>, ProtocolError> {
    match optional_value(arguments, key)? {
        Some(v) => v
            .as_i64()
            .map(Some)
            .ok_or_else(|| ProtocolError::invalid_params(format!("{} must be an integer", key))),
        None => Ok(None),
    }
}

fn optional_string_array(
    arguments: &Value,
    key: &str,
) -> Result<Option<Vec<String>>, ProtocolError> {
    match optional_value(arguments, key)? {
        Some(value) => parse_string_array(value, key).map(Some),
        None => Ok(None),
    }
}

fn required_string_array(arguments: &Value, key: &str) -> Result<Vec<String>, ProtocolError> {
    let value = optional_value(arguments, key)?
        .ok_or_else(|| ProtocolError::invalid_params(format!("{} must be an array", key)))?;
    parse_string_array(value, key)
}

fn parse_string_array(value: &Value, key: &str) -> Result<Vec<String>, ProtocolError> {
    let arr = value
        .as_array()
        .ok_or_else(|| ProtocolError::invalid_params(format!("{} must be an array", key)))?;
    if arr.is_empty() {
        return Err(ProtocolError::invalid_params(format!(
            "{} must not be empty",
            key
        )));
    }

    arr.iter()
        .enumerate()
        .map(|(i, item)| {
            item.as_str().map(ToString::to_string).ok_or_else(|| {
                ProtocolError::invalid_params(format!("{}[{}] must be a string", key, i))
            })
        })
        .collect()
}

fn optional_timeout(arguments: &Value) -> Result<u64, ProtocolError> {
    match arguments.get("timeoutMs") {
        Some(v) => v
            .as_u64()
            .filter(|ms| *ms > 0)
            .ok_or_else(|| ProtocolError::invalid_params("timeoutMs must be a positive integer")),
        None => Ok(DEFAULT_TIMEOUT_MS),
    }
}

fn append_session_args(args: &mut Vec<String>, session: Option<&str>) {
    if let Some(session) = session {
        args.push("--session".to_string());
        args.push(session.to_string());
    }
}

fn append_common_global_args(
    args: &mut Vec<String>,
    arguments: &Value,
    session: Option<&str>,
) -> Result<(), ProtocolError> {
    if let Some(engine) = optional_string(arguments, "engine")? {
        if !matches!(engine.as_str(), "camoufox") {
            return Err(ProtocolError::invalid_params("engine must be camoufox"));
        }
        args.extend(["--engine".to_string(), engine]);
    }
    if let Some(namespace) = optional_string(arguments, "namespace")? {
        args.push("--namespace".to_string());
        args.push(namespace);
    }
    append_session_args(args, session);

    if let Some(profile) = optional_string(arguments, "profile")? {
        args.extend(["--profile".to_string(), profile]);
    }

    if let Some(idle_timeout) = optional_string(arguments, "idleTimeout")? {
        args.push("--idle-timeout".to_string());
        args.push(idle_timeout);
    }

    if let Some(restore) = arguments.get("restore") {
        if let Some(enabled) = restore.as_bool() {
            if enabled {
                args.push("--restore".to_string());
            }
        } else if let Some(key) = restore.as_str() {
            args.push(format!("--restore={}", key));
        } else {
            return Err(ProtocolError::invalid_params(
                "restore must be a boolean or string",
            ));
        }
    }

    if let Some(policy) = optional_string(arguments, "restoreSave")? {
        args.push("--restore-save".to_string());
        args.push(policy);
    }
    if let Some(check) = optional_string(arguments, "restoreCheckUrl")? {
        args.push("--restore-check-url".to_string());
        args.push(check);
    }
    if let Some(check) = optional_string(arguments, "restoreCheckText")? {
        args.push("--restore-check-text".to_string());
        args.push(check);
    }
    if let Some(check) = optional_string(arguments, "restoreCheckFn")? {
        args.push("--restore-check-fn".to_string());
        args.push(check);
    }
    if let Some(domains) = optional_string_array(arguments, "allowedDomains")? {
        if !domains.is_empty() {
            args.push("--allowed-domains".to_string());
            args.push(domains.join(","));
        }
    }
    let ca_cert = optional_string(arguments, "caCert")?;
    let clear_ca_cert = optional_bool(arguments, "clearCaCert")?.unwrap_or(false);
    if ca_cert.is_some() && clear_ca_cert {
        return Err(ProtocolError::invalid_params(
            "Cannot use caCert with clearCaCert",
        ));
    }
    if let Some(ca_cert) = ca_cert {
        args.push("--ca-cert".to_string());
        args.push(ca_cert);
    } else if clear_ca_cert {
        args.push("--no-ca-cert".to_string());
    }

    Ok(())
}

fn run_cli(args: &[String], stdin_body: Option<String>, timeout_ms: u64) -> Result<CliRun, String> {
    let exe = env::current_exe().map_err(|e| e.to_string())?;
    let mut command = Command::new(exe);
    command
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(if stdin_body.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        });
    let selected_engine = args
        .windows(2)
        .rev()
        .find(|pair| pair[0] == "--engine")
        .map(|pair| pair[1].clone())
        .or_else(|| env::var("AGENT_BROWSER_ENGINE").ok());
    let camoufox = selected_engine.as_deref() == Some("camoufox");
    if camoufox && timeout_ms < 30_000 {
        return Err("Camoufox MCP timeoutMs must be at least 30000 so the daemon can report its bounded input outcome".to_string());
    }
    #[cfg(unix)]
    if camoufox {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }

    let mut child = command.spawn().map_err(|e| e.to_string())?;

    if let Some(body) = stdin_body {
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| "failed to open child stdin".to_string())?;
        stdin
            .write_all(body.as_bytes())
            .map_err(|e| format!("failed to write child stdin: {}", e))?;
    }

    let mut child_stdout = child
        .stdout
        .take()
        .ok_or_else(|| "failed to open child stdout".to_string())?;
    let mut child_stderr = child
        .stderr
        .take()
        .ok_or_else(|| "failed to open child stderr".to_string())?;

    let stdout_thread = thread::spawn(move || {
        let mut buf = Vec::new();
        child_stdout.read_to_end(&mut buf).map(|_| buf)
    });
    let stderr_thread = thread::spawn(move || {
        let mut buf = Vec::new();
        child_stderr.read_to_end(&mut buf).map(|_| buf)
    });

    let started = Instant::now();
    let timeout = Duration::from_millis(timeout_ms);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {
                if started.elapsed() >= timeout {
                    #[cfg(unix)]
                    if camoufox {
                        // The CLI group includes only its installer children, not the detached daemon.
                        unsafe {
                            libc::kill(-(child.id() as i32), libc::SIGKILL);
                        }
                    }
                    let _ = child.kill();
                    let _ = child.wait();
                    let stdout = join_output(stdout_thread)?;
                    let stderr = join_output(stderr_thread)?;
                    return Ok(CliRun {
                        exit_code: None,
                        stdout,
                        stderr: append_timeout_message(stderr, timeout_ms),
                    });
                }
                thread::sleep(Duration::from_millis(20));
            }
            Err(e) => return Err(e.to_string()),
        }
    };

    let stdout = join_output(stdout_thread)?;
    let stderr = join_output(stderr_thread)?;

    Ok(CliRun {
        exit_code: status.code(),
        stdout,
        stderr,
    })
}

fn join_output(handle: thread::JoinHandle<io::Result<Vec<u8>>>) -> Result<String, String> {
    let bytes = handle
        .join()
        .map_err(|_| "failed to join output reader".to_string())?
        .map_err(|e| e.to_string())?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn append_timeout_message(stderr: String, timeout_ms: u64) -> String {
    let msg = format!("agent-browser command timed out after {}ms", timeout_ms);
    if stderr.trim().is_empty() {
        msg
    } else {
        format!("{}\n{}", stderr.trim_end(), msg)
    }
}

fn tool_result_from_run(run: CliRun) -> Value {
    let parsed = serde_json::from_str::<Value>(run.stdout.trim()).ok();
    let cli_success = parsed
        .as_ref()
        .and_then(|v| v.get("success"))
        .and_then(|v| v.as_bool())
        .unwrap_or_else(|| run.exit_code == Some(0));
    let success = run.exit_code == Some(0) && cli_success;
    let mut content = vec![json!({
        "type": "text",
        "text": tool_text(parsed.as_ref(), &run.stdout, &run.stderr),
    })];

    if success {
        if let Some(image) = parsed.as_ref().and_then(image_content_from_response) {
            content.push(image);
        }
    }

    json!({
        "content": content,
        "structuredContent": {
            "exitCode": run.exit_code,
            "stdout": run.stdout,
            "stderr": run.stderr,
            "response": parsed,
        },
        "isError": !success,
    })
}

fn tool_text(parsed: Option<&Value>, stdout: &str, stderr: &str) -> String {
    let mut text = match parsed {
        Some(value) => response_text(value).unwrap_or_else(|| {
            serde_json::to_string_pretty(value).unwrap_or_else(|_| stdout.trim().to_string())
        }),
        None => stdout.trim().to_string(),
    };

    let stderr = stderr.trim();
    if !stderr.is_empty() {
        if !text.is_empty() {
            text.push_str("\n\nstderr:\n");
        }
        text.push_str(stderr);
    }

    if text.is_empty() {
        "(no output)".to_string()
    } else {
        text
    }
}

fn response_text(value: &Value) -> Option<String> {
    if let Some(obj) = value.as_object() {
        if obj.get("success").and_then(|v| v.as_bool()) == Some(false) {
            if let Some(code) = obj
                .get("code")
                .and_then(Value::as_str)
                .filter(|code| code.starts_with("camoufox_"))
            {
                let error = obj.get("error").and_then(Value::as_str)?;
                let mut text = format!("{code}: {error}");
                if obj.get("inputAmbiguous").and_then(Value::as_bool) == Some(true)
                    || value["data"]["inputAmbiguous"].as_bool() == Some(true)
                {
                    text.push_str("\nInput outcome may be ambiguous; do not replay. Close the session and inspect application state before continuing.");
                } else if code == "camoufox_timeout" {
                    text.push_str("\nThe browser is still available. Inspect the current page before continuing; do not automatically replay input.");
                }
                return Some(text);
            }
            return obj
                .get("error")
                .and_then(|v| v.as_str())
                .map(ToString::to_string);
        }

        if let Some(data) = obj.get("data") {
            if ["headings", "links", "nodes"]
                .iter()
                .any(|key| data.get(*key).and_then(Value::as_array).is_some())
            {
                return Some(
                    serde_json::to_string_pretty(data).unwrap_or_else(|_| data.to_string()),
                );
            }
            // Accessibility reports carry a URL alongside their findings. Use
            // the same report formatter as the CLI before the generic string
            // field fallback turns the MCP text content into only that URL.
            if data.get("axeVersion").is_some()
                && data
                    .get("violations")
                    .and_then(|value| value.as_array())
                    .is_some()
            {
                return Some(crate::output::format_a11y_text(data));
            }
            if data.get("found").and_then(|v| v.as_bool()) == Some(true)
                && data.get("selector").and_then(|v| v.as_str()).is_some()
            {
                return Some(
                    serde_json::to_string_pretty(data).unwrap_or_else(|_| data.to_string()),
                );
            }
            if data.get("matches").and_then(|v| v.as_array()).is_some() {
                return Some(
                    serde_json::to_string_pretty(data).unwrap_or_else(|_| data.to_string()),
                );
            }
            if let Some(capture) = data.get("visualCapture").and_then(|v| v.as_object()) {
                if let Some(capture_id) = capture.get("captureId").and_then(|v| v.as_str()) {
                    let path = data.get("path").and_then(|v| v.as_str()).unwrap_or("");
                    return Some(format!("{}\ncaptureId: {}", path, capture_id));
                }
            }
            for key in [
                "snapshot", "text", "html", "report", "value", "content", "title", "url", "path",
            ] {
                if let Some(s) = data.get(key).and_then(|v| v.as_str()) {
                    return Some(s.to_string());
                }
            }
            if let Some(result) = data.get("result") {
                return Some(
                    serde_json::to_string_pretty(result).unwrap_or_else(|_| result.to_string()),
                );
            }
        }
    }

    None
}

fn image_content_from_response(value: &Value) -> Option<Value> {
    let path = value.get("data")?.get("path")?.as_str()?;
    let mime_type = image_mime_type(path)?;
    let metadata = fs::metadata(path).ok()?;
    if metadata.len() > MAX_IMAGE_BYTES {
        return None;
    }
    let bytes = fs::read(path).ok()?;
    Some(json!({
        "type": "image",
        "data": STANDARD.encode(bytes),
        "mimeType": mime_type,
    }))
}

fn image_mime_type(path: &str) -> Option<&'static str> {
    let lower = path.to_lowercase();
    if lower.ends_with(".png") {
        Some("image/png")
    } else if lower.ends_with(".jpg") || lower.ends_with(".jpeg") {
        Some("image/jpeg")
    } else {
        None
    }
}

fn error_response(id: Value, code: i64, message: impl Into<String>) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {
            "code": code,
            "message": message.into(),
        },
    })
}

fn write_json_line(stdout: &mut io::Stdout, value: &Value) -> io::Result<()> {
    let line = serde_json::to_string(value)?;
    stdout.write_all(line.as_bytes())?;
    stdout.write_all(b"\n")?;
    stdout.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_tool_exposes_launch_options() {
        let tools = tools();
        let open = tools
            .iter()
            .find(|t| t["name"].as_str() == Some(TOOL_OPEN))
            .unwrap();
        assert!(!open["description"]
            .as_str()
            .unwrap()
            .contains("WebMCP availability metadata"));
        let props = &open["inputSchema"]["properties"];
        assert!(props.get("headed").is_some());
        assert!(props.get("webgpu").is_none());
        assert!(props.get("webmcp").is_some());
    }

    #[test]
    fn open_args_forwards_explicit_booleans() {
        // Absent fields send nothing (env/config resolution stays with the CLI).
        assert_eq!(open_args(&json!({})).unwrap(), vec!["open"]);

        // Explicit true and false are both forwarded, so MCP callers can
        // override env/config just like `--headed false`.
        assert_eq!(
            open_args(&json!({ "headed": true })).unwrap(),
            vec!["--headed", "true", "open"]
        );
        assert_eq!(
            open_args(&json!({ "headed": false })).unwrap(),
            vec!["--headed", "false", "open"]
        );
        assert_eq!(
            open_args(&json!({ "webmcp": false })).unwrap(),
            vec!["--no-webmcp", "true", "open"]
        );
        assert_eq!(
            open_args(&json!({ "webmcp": true })).unwrap(),
            vec!["--no-webmcp", "false", "open"]
        );
        // webgpu no longer exists in the schema or in open_args forwarding.
        assert_eq!(
            open_args(&json!({ "webgpu": false, "url": "https://example.com" })).unwrap(),
            vec!["open", "https://example.com"]
        );
    }

    #[test]
    fn doctor_tool_exposes_options() {
        let tools = tools();
        let doctor = tools
            .iter()
            .find(|t| t["name"].as_str() == Some(TOOL_DOCTOR))
            .unwrap();
        let props = &doctor["inputSchema"]["properties"];
        assert!(props.get("offline").is_some());
        assert!(props.get("quick").is_some());
        assert!(props.get("fix").is_some());
        assert!(props.get("webgpu").is_none());
        assert!(props.get("headed").is_some());
        assert!(props.get("debug").is_some());
    }

    #[test]
    fn doctor_args_forwards_explicit_booleans() {
        assert_eq!(doctor_args(&json!({})).unwrap(), vec!["doctor"]);
        // Presence flags only sent when true.
        assert_eq!(
            doctor_args(&json!({ "offline": true, "quick": false })).unwrap(),
            vec!["doctor", "--offline"]
        );
        // Value-taking booleans forwarded both ways so env/config can be
        // overridden.
        assert_eq!(
            doctor_args(&json!({ "headed": false })).unwrap(),
            vec!["doctor", "--headed", "false"]
        );
        assert_eq!(
            doctor_args(&json!({ "debug": true })).unwrap(),
            vec!["doctor", "--debug", "true"]
        );
        // webgpu no longer exists in the doctor schema or arg forwarding.
        assert_eq!(
            doctor_args(&json!({ "webgpu": true, "headed": false })).unwrap(),
            vec!["doctor", "--headed", "false"]
        );
    }

    #[test]
    fn tools_list_uses_unique_names() {
        let tools = tools();
        let mut names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), tools.len());
    }

    #[test]
    fn tools_list_is_paginated() {
        let config = McpConfig::all();
        let first_page = list_tools(None, &config).unwrap();
        let first_tools = first_page["tools"].as_array().unwrap();
        assert_eq!(first_tools.len(), TOOL_LIST_PAGE_SIZE);
        let next_cursor = first_page["nextCursor"].as_str().unwrap();

        let second_page = list_tools(Some(&json!({ "cursor": next_cursor })), &config).unwrap();
        let second_tools = second_page["tools"].as_array().unwrap();
        assert!(!second_tools.is_empty());
        assert_ne!(first_tools[0]["name"], second_tools[0]["name"]);
    }

    #[test]
    fn tools_list_rejects_invalid_cursor() {
        let err = list_tools(
            Some(&json!({ "cursor": "not-a-cursor" })),
            &McpConfig::all(),
        )
        .unwrap_err();
        assert_eq!(err.code, -32602);
    }

    #[test]
    fn tools_list_defaults_to_core_profile() {
        let config = McpConfig::default();
        let result = list_tools(None, &config).unwrap();
        let names: Vec<&str> = result["tools"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|tool| tool["name"].as_str())
            .collect();

        assert!(names.contains(&TOOL_TOOLS_PROFILES));
        assert!(names.contains(&TOOL_OPEN));
        assert!(names.contains(&TOOL_READ));
        assert!(names.contains(&TOOL_SNAPSHOT));
        assert!(names.contains(&TOOL_CLICK));
        assert!(names.contains(&TOOL_SCREENSHOT));
        assert!(!names.contains(&TOOL_NETWORK_HAR_START));
        assert!(result.get("nextCursor").is_none());
    }

    #[test]
    fn parse_mcp_config_accepts_tools_profiles() {
        let config = parse_mcp_config(&["--tools".into(), "core,network".into()]).unwrap();
        assert!(config.allows(TOOL_OPEN));
        assert!(config.allows(TOOL_READ));
        assert!(config.allows(TOOL_NETWORK_REQUESTS));
    }

    #[test]
    fn parse_mcp_config_accepts_all_profile() {
        let config = parse_mcp_config(&["--tools=all".into()]).unwrap();
        assert!(config.allows(TOOL_OPEN));
        assert!(config.allows(TOOL_READ));
        assert!(config.allows(TOOL_NETWORK_HAR_START));
    }

    #[test]
    fn parse_mcp_config_rejects_unknown_profile() {
        let err = parse_mcp_config(&["--tools".into(), "bogus".into()]).unwrap_err();
        assert!(err.contains("Unknown MCP tools profile"));
    }

    #[test]
    fn call_tool_rejects_disabled_profile_tool() {
        let err = call_tool(
            Some(&json!({
                "name": TOOL_NETWORK_HAR_START,
                "arguments": {}
            })),
            &McpConfig::default(),
        )
        .unwrap_err();
        assert_eq!(err.code, -32602);
        assert!(err.message.contains("not enabled"));
    }

    #[test]
    fn response_text_uses_read_content_before_url_metadata() {
        let text = response_text(&json!({
            "success": true,
            "data": {
                "url": "https://example.com/docs",
                "content": "# Docs\n\nReadable content."
            }
        }))
        .unwrap();

        assert_eq!(text, "# Docs\n\nReadable content.");
    }

    #[test]
    fn response_text_formats_a11y_findings_before_url_metadata() {
        let text = response_text(&json!({
            "success": true,
            "data": {
                "url": "https://example.com",
                "axeVersion": "4.12.1",
                "counts": {
                    "violations": 1,
                    "incomplete": 0,
                    "passes": 12,
                    "inapplicable": 20
                },
                "violations": [{
                    "id": "image-alt",
                    "impact": "critical",
                    "help": "Images must have alternative text",
                    "nodeCount": 1,
                    "nodes": [{ "target": ["#hero"] }]
                }],
                "incomplete": []
            }
        }))
        .unwrap();

        assert!(text.contains("violations: 1"));
        assert!(text.contains("[critical] image-alt"));
        assert!(text.contains("  - #hero"));
        assert_ne!(text, "https://example.com");
    }

    #[test]
    fn click_command_args_include_new_tab() {
        let args = click_command_args(&json!({
            "selector": "@e1",
            "newTab": true,
        }))
        .unwrap();

        assert_eq!(args, vec!["click", "@e1", "--new-tab"]);
    }

    #[test]
    fn common_global_args_use_equals_form_for_string_restore_key() {
        let mut args = Vec::new();

        append_common_global_args(
            &mut args,
            &json!({
                "session": "work",
                "restore": "open"
            }),
            Some("work"),
        )
        .unwrap();

        assert_eq!(args, vec!["--session", "work", "--restore=open"]);
    }

    #[test]
    fn common_global_args_include_allowed_domains() {
        let mut args = Vec::new();

        append_common_global_args(
            &mut args,
            &json!({
                "allowedDomains": ["example.com", "*.example.org"]
            }),
            None,
        )
        .unwrap();

        assert_eq!(args, vec!["--allowed-domains", "example.com,*.example.org"]);
    }

    #[test]
    fn common_global_args_include_idle_timeout() {
        let mut args = Vec::new();

        append_common_global_args(
            &mut args,
            &json!({
                "idleTimeout": "0"
            }),
            None,
        )
        .unwrap();

        assert_eq!(args, vec!["--idle-timeout", "0"]);
    }

    #[test]
    fn common_global_args_include_ca_cert() {
        let mut args = Vec::new();

        append_common_global_args(
            &mut args,
            &json!({
                "caCert": "/tmp/proxy-ca.pem"
            }),
            None,
        )
        .unwrap();

        assert_eq!(args, vec!["--ca-cert", "/tmp/proxy-ca.pem"]);
    }

    #[test]
    fn common_global_args_include_clear_ca_cert() {
        let mut args = Vec::new();

        append_common_global_args(&mut args, &json!({ "clearCaCert": true }), None).unwrap();

        assert_eq!(args, vec!["--no-ca-cert"]);
    }

    #[test]
    fn common_global_args_reject_ca_cert_with_clear() {
        let mut args = Vec::new();
        let error = append_common_global_args(
            &mut args,
            &json!({
                "caCert": "/tmp/proxy-ca.pem",
                "clearCaCert": true
            }),
            None,
        )
        .unwrap_err();

        assert!(error.message.contains("Cannot use caCert with clearCaCert"));
        assert!(args.is_empty());
    }

    #[test]
    fn tool_schema_includes_extra_args_for_cli_parity() {
        let tools = tools();
        let open = tools
            .iter()
            .find(|t| t["name"].as_str() == Some(TOOL_OPEN))
            .unwrap();
        assert_eq!(
            open["inputSchema"]["properties"]["extraArgs"]["type"],
            "array"
        );
        assert_eq!(
            open["inputSchema"]["properties"]["restoreSave"]["enum"][0],
            "auto"
        );
        assert_eq!(
            open["inputSchema"]["properties"]["namespace"]["type"],
            "string"
        );
        assert_eq!(
            open["inputSchema"]["properties"]["allowedDomains"]["type"],
            "array"
        );
        assert_eq!(
            open["inputSchema"]["properties"]["idleTimeout"]["type"],
            "string"
        );
    }

    #[test]
    fn tool_schema_har_start_content_matches_cli_modes() {
        let tools = tools();
        let har_start = tools
            .iter()
            .find(|t| t["name"].as_str() == Some(TOOL_NETWORK_HAR_START))
            .unwrap();
        let modes = har_start["inputSchema"]["properties"]["content"]["enum"]
            .as_array()
            .unwrap();
        // Must stay in sync with the CLI parser's accepted --content values.
        assert_eq!(modes, &vec![json!("all"), json!("text"), json!("none")]);
    }

    #[test]
    fn required_string_reads_present_field() {
        let value = required_string(&json!({ "selector": "@e1" }), "selector").unwrap();
        assert_eq!(value, "@e1");
    }

    #[test]
    fn tool_result_preserves_tab_gone_recovery_data() {
        let run = CliRun {
            exit_code: Some(1),
            stdout: json!({
                "success": false,
                "data": {
                    "targetId": "DEAD_TARGET",
                    "lastUrl": "https://example.com/path"
                },
                "error": "tab_gone: bound tab is gone",
                "code": "tab_gone"
            })
            .to_string(),
            stderr: String::new(),
        };

        let result = tool_result_from_run(run);
        assert_eq!(result["isError"], true);
        assert_eq!(result["structuredContent"]["exitCode"], 1);
        assert_eq!(
            result["structuredContent"]["response"]["data"]["targetId"],
            "DEAD_TARGET"
        );
        assert_eq!(
            result["structuredContent"]["response"]["data"]["lastUrl"],
            "https://example.com/path"
        );
    }

    #[test]
    fn camoufox_lifecycle_errors_preserve_cli_data_and_expose_codes() {
        for code in [
            "camoufox_session_closed",
            "camoufox_target_closed",
            "camoufox_no_active_tab",
        ] {
            let response = json!({
                "success": false,
                "code": code,
                "error": "Inspect session info and tab list before continuing.",
                "data": {
                    "browserConnected": false,
                    "recoveryRequired": true,
                    "closeReason": "browser_disconnected"
                }
            });
            let result = tool_result_from_run(CliRun {
                exit_code: Some(1),
                stdout: response.to_string(),
                stderr: String::new(),
            });
            assert_eq!(result["isError"], true);
            assert_eq!(result["structuredContent"]["response"], response);
            let text = result["content"][0]["text"].as_str().unwrap();
            assert!(text.starts_with(&format!("{code}:")));
            assert!(!text.contains("Input outcome may be ambiguous"));
        }
    }

    #[test]
    fn camoufox_lifecycle_reset_state_is_visible_in_text_only_clients() {
        for top_level in [true, false] {
            let mut response = json!({
                "success": false,
                "code": "camoufox_session_closed",
                "error": "Browser disconnected; close the session before reopening.",
                "data": {}
            });
            if top_level {
                response["inputAmbiguous"] = json!(true);
            } else {
                response["data"]["inputAmbiguous"] = json!(true);
            }
            let result = tool_result_from_run(CliRun {
                exit_code: Some(1),
                stdout: response.to_string(),
                stderr: String::new(),
            });
            assert_eq!(result["structuredContent"]["response"], response);
            assert!(result["content"][0]["text"]
                .as_str()
                .unwrap()
                .contains("do not replay"));
        }
    }

    #[test]
    fn camoufox_timeout_without_reset_requirement_preserves_cli_response_and_recovery_hint() {
        for timeout_kind in ["operation", "deadline"] {
            let response = json!({
                "success": false,
                "code": "camoufox_timeout",
                "error": "browser action 'hover' timed out: TimeoutError: target was not actionable",
                "data": {"timeoutKind": timeout_kind}
            });
            let result = tool_result_from_run(CliRun {
                exit_code: Some(1),
                stdout: response.to_string(),
                stderr: String::new(),
            });
            assert_eq!(result["isError"], true);
            assert_eq!(result["structuredContent"]["response"], response);
            let text = result["content"][0]["text"].as_str().unwrap();
            assert!(text.starts_with("camoufox_timeout:"));
            assert!(text.contains("The browser is still available"));
            assert!(!text.contains("poison"));
            assert!(text.contains("Inspect the current page"));
            assert!(text.contains("do not automatically replay input"));
            assert!(!text.contains("Close the session"));
        }
    }

    #[test]
    fn camoufox_timeout_reset_requirement_overrides_the_recovery_hint() {
        for timeout_kind in ["operation", "deadline"] {
            for top_level in [true, false] {
                let mut response = json!({
                    "success": false,
                    "code": "camoufox_timeout",
                    "error": "input cleanup or action deadline failed",
                    "data": {"timeoutKind": timeout_kind}
                });
                if top_level {
                    response["inputAmbiguous"] = json!(true);
                } else {
                    response["data"]["inputAmbiguous"] = json!(true);
                }
                let result = tool_result_from_run(CliRun {
                    exit_code: Some(1),
                    stdout: response.to_string(),
                    stderr: String::new(),
                });
                assert_eq!(result["isError"], true);
                assert_eq!(result["structuredContent"]["response"], response);
                let text = result["content"][0]["text"].as_str().unwrap();
                assert!(text.contains("Close the session"));
                assert!(text.contains("do not replay"));
                assert!(!text.contains("The browser is still available"));
            }
        }
    }

    #[test]
    fn camoufox_lifecycle_diagnostics_preserve_daemon_browser_distinction() {
        let response = json!({
            "success": true,
            "data": {
                "active": true,
                "runtime": {
                    "engine": "camoufox",
                    "launched": false,
                    "browserConnected": false,
                    "recoveryRequired": true,
                    "closeReason": "browser_disconnected"
                }
            }
        });
        let result = tool_result_from_run(CliRun {
            exit_code: Some(0),
            stdout: response.to_string(),
            stderr: String::new(),
        });
        assert_eq!(result["isError"], false);
        assert_eq!(result["structuredContent"]["response"], response);
        assert!(result["content"][0]["text"]
            .as_str()
            .unwrap()
            .contains("browserConnected"));
    }

    #[test]
    fn camoufox_inspection_profiles_expose_inspection_tools() {
        let cases: [(ToolProfile, &[&str]); 3] = [
            (
                ToolProfile::Network,
                &[TOOL_NETWORK_REQUEST, TOOL_NETWORK_WEBSOCKETS],
            ),
            (ToolProfile::Debug, &[TOOL_DOWNLOADS]),
            (ToolProfile::Tabs, &[TOOL_DIALOG_STATUS]),
        ];

        for (profile, expected) in cases {
            let config = McpConfig::from_profiles(vec![profile]);
            let tools = tools_for_config(&config);
            for &name in expected {
                assert!(
                    tools.iter().any(|tool| tool["name"].as_str() == Some(name)),
                    "{} profile should expose {}",
                    profile.name(),
                    name
                );
                assert!(config.allows(name));
            }
        }
    }

    #[test]
    fn camoufox_page_tool_text_keeps_structured_records() {
        let response = json!({
            "success": true,
            "data": {
                "nodes": [{"ref": "@d1", "tag": "main"}],
                "url": "https://example.com"
            }
        });
        let text = response_text(&response).unwrap();
        assert!(text.contains("@d1"));
        assert!(text.contains("https://example.com"));
    }

    #[test]
    fn camoufox_inspection_request_detail_forwards_request_id() {
        let tool = tools()
            .into_iter()
            .find(|tool| tool["name"].as_str() == Some(TOOL_NETWORK_REQUEST))
            .unwrap();
        assert_eq!(tool["inputSchema"]["required"], json!(["requestId"]));

        let arguments = json!({ "requestId": "n1" });
        let mut command_args = command_parts("network request");
        command_args.push(required_string(&arguments, "requestId").unwrap());
        let args = cli_tool_args(&arguments, command_args, None).unwrap();
        assert_eq!(args, vec!["--json", "network", "request", "n1"]);

        let flags = crate::flags::parse_flags(&args);
        let command =
            crate::commands::parse_command(&crate::flags::clean_args(&args), &flags).unwrap();
        assert_eq!(command["action"], "request_detail");
        assert_eq!(command["requestId"], "n1");
    }

    #[test]
    fn camoufox_inspection_wait_for_download_is_not_read_only() {
        let tool = tools()
            .into_iter()
            .find(|tool| tool["name"].as_str() == Some(TOOL_WAIT_FOR_DOWNLOAD))
            .unwrap();
        assert_eq!(tool["annotations"]["readOnlyHint"], false);
        assert!(!is_read_only_tool(TOOL_WAIT_FOR_DOWNLOAD));
    }

    #[test]
    fn required_string_rejects_missing_field() {
        let err = required_string(&json!({}), "selector").unwrap_err();
        assert_eq!(err.code, -32602);
    }

    #[test]
    fn required_string_array_reads_values() {
        let values = required_string_array(&json!({ "values": ["a", "b"] }), "values").unwrap();
        assert_eq!(values, vec!["a", "b"]);
    }

    #[test]
    fn initialize_echoes_supported_protocol_version() {
        let result = initialize_result(
            Some(&json!({
                "protocolVersion": "2024-11-05"
            })),
            &McpConfig::default(),
        );
        assert_eq!(result["protocolVersion"], "2024-11-05");
    }

    #[test]
    fn initialize_defaults_to_latest_protocol_version() {
        let result = initialize_result(None, &McpConfig::default());
        assert_eq!(result["protocolVersion"], PROTOCOL_VERSION);
    }
}
