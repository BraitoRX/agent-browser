use std::sync::OnceLock;

use serde_json::{json, Value};

pub(crate) const DEFAULT_AI_GATEWAY_URL: &str = "https://ai-gateway.vercel.sh";

static HTTP_CLIENT: OnceLock<reqwest::Client> = OnceLock::new();

pub(crate) fn http_client() -> &'static reqwest::Client {
    HTTP_CLIENT.get_or_init(reqwest::Client::new)
}

pub(crate) fn is_chat_enabled() -> bool {
    std::env::var("AI_GATEWAY_API_KEY").is_ok()
}

const SKILL_NAMES: &[&str] = &["agent-browser", "slack", "electron", "dogfood", "agentcore"];

/// Locate the `skills/` directory by walking up from the executable.
/// Works for npm installs (binary in `bin/`, skills at `../skills/`) and
/// dev builds (binary deep in `cli/target/`, skills at repo root).
fn find_skills_dir() -> Option<std::path::PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let real = exe.canonicalize().unwrap_or(exe);
    let mut dir = real.parent();
    while let Some(d) = dir {
        let candidate = d.join("skills");
        if candidate.join("agent-browser").join("SKILL.md").exists() {
            return Some(candidate);
        }
        dir = d.parent();
    }
    None
}

fn load_skills() -> Vec<(String, String)> {
    let Some(skills_dir) = find_skills_dir() else {
        return Vec::new();
    };
    SKILL_NAMES
        .iter()
        .filter_map(|name| {
            let path = skills_dir.join(name).join("SKILL.md");
            let content = std::fs::read_to_string(&path).ok()?;
            Some((name.to_string(), content))
        })
        .collect()
}

fn strip_frontmatter(s: &str) -> &str {
    if !s.starts_with("---") {
        return s;
    }
    if let Some(end) = s[3..].find("---") {
        let after = &s[3 + end + 3..];
        after.trim_start_matches(['\n', '\r'])
    } else {
        s
    }
}

pub(crate) fn get_system_prompt() -> &'static str {
    static PROMPT: OnceLock<String> = OnceLock::new();
    PROMPT.get_or_init(|| {
        let skills = load_skills();

        let mut sections = String::new();
        for (name, content) in &skills {
            let body = strip_frontmatter(content);
            sections.push_str(&format!("\n\n<skill name=\"{}\">\n{}\n</skill>", name, body.trim()));
        }

        format!(
            r#"You are an AI assistant that controls a browser through agent-browser. You have an active browser session, but you can also create new sessions.

RULES:
- You MUST use the agent_browser tool for every browser action. NEVER claim you performed an action without calling the tool.
- If the user asks you to do something, call the tool first, then describe the result.
- If a request is outside your capabilities (e.g. system operations), say so honestly. Do not improvise or pretend.
- One tool call per command. Do not chain with `&&` or `;`.
- Do not add `--json`.
- Do not run non-agent-browser programs.
- Keep responses concise.
- For screenshots, omit the path argument so they save to the default location (which will be displayed inline). Screenshots from tool calls are ALREADY shown to the user. Do NOT re-display them with markdown image syntax in your text response. Never use `![...]()` to reference screenshots.
- To create a new session: add `--session <name>` to any command (e.g. `agent-browser --session my-session open https://example.com`). If the session does not exist, it will be created automatically.
- To use a different browser engine: add `--engine <engine>` 

The following skill references describe agent-browser capabilities in detail. Use them when deciding which commands to run and how to approach tasks.
{sections}"#,
        )
    })
}

pub(crate) const CHAT_TOOLS: &str = r#"[{"type":"function","function":{"name":"agent_browser","description":"Execute an agent-browser command. Runs against the active session by default. Add --session <name> to target or create a different session, and --engine <engine> to choose a browser engine.","parameters":{"type":"object","properties":{"command":{"type":"string","description":"The command to execute, e.g. 'agent-browser open https://google.com' or 'agent-browser --session new-session open https://example.com' or 'agent-browser snapshot -i' or 'agent-browser click @e3'"}},"required":["command"]}}}]"#;

pub(crate) const COMPACT_THRESHOLD_CHARS: usize = 200_000;
pub(crate) const KEEP_RECENT_MESSAGES: usize = 6;

pub(crate) fn estimate_chars(messages: &[Value]) -> usize {
    messages
        .iter()
        .map(|m| {
            let content_len = m
                .get("content")
                .map(|c| {
                    if let Some(s) = c.as_str() {
                        s.len()
                    } else {
                        c.to_string().len()
                    }
                })
                .unwrap_or(0);
            let tc_len = m
                .get("tool_calls")
                .map(|t| t.to_string().len())
                .unwrap_or(0);
            content_len + tc_len
        })
        .sum()
}

pub(crate) fn find_safe_split(messages: &[Value], keep_recent: usize) -> usize {
    if messages.len() <= keep_recent + 1 {
        return 1;
    }
    let desired = messages.len() - keep_recent;
    for i in (1..=desired).rev() {
        if messages[i].get("role").and_then(|r| r.as_str()) == Some("user") {
            return i;
        }
    }
    desired.max(1)
}

fn build_summary_text(messages: &[Value]) -> String {
    let mut text = String::new();
    for msg in messages {
        let role = msg
            .get("role")
            .and_then(|r| r.as_str())
            .unwrap_or("unknown");
        if let Some(content) = msg.get("content").and_then(|c| c.as_str()) {
            if !content.is_empty() {
                let truncated = if content.len() > 2000 {
                    format!("{}...[truncated]", &content[..2000])
                } else {
                    content.to_string()
                };
                text.push_str(&format!("[{}] {}\n\n", role, truncated));
            }
        }
        if let Some(tcs) = msg.get("tool_calls").and_then(|t| t.as_array()) {
            for tc in tcs {
                let name = tc
                    .get("function")
                    .and_then(|f| f.get("name"))
                    .and_then(|n| n.as_str())
                    .unwrap_or("");
                let args = tc
                    .get("function")
                    .and_then(|f| f.get("arguments"))
                    .and_then(|a| a.as_str())
                    .unwrap_or("");
                text.push_str(&format!("[assistant tool:{}] {}\n", name, args));
            }
        }
    }
    text
}

pub(crate) async fn summarize_for_compaction(
    client: &reqwest::Client,
    url: &str,
    api_key: &str,
    model: &str,
    messages: &[Value],
) -> Option<String> {
    let conversation = build_summary_text(messages);
    if conversation.is_empty() {
        return None;
    }

    let body = json!({
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Summarize this browser automation conversation concisely. Preserve: URLs visited, actions performed, current page state, errors encountered, and user goals. Output only the summary."
            },
            {
                "role": "user",
                "content": conversation
            }
        ],
        "max_tokens": 1024,
        "stream": false,
    });

    let resp = client
        .post(url)
        .header("Authorization", format!("Bearer {}", api_key))
        .header("Content-Type", "application/json")
        .body(body.to_string())
        .send()
        .await
        .ok()?;

    if !resp.status().is_success() {
        return None;
    }

    let result: Value = resp.json().await.ok()?;
    result
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("message"))
        .and_then(|m| m.get("content"))
        .and_then(|c| c.as_str())
        .map(|s| s.to_string())
}




const ALLOWED_COMMANDS: &[&str] = &[
    "open",
    "goto",
    "navigate",
    "back",
    "forward",
    "reload",
    "click",
    "dblclick",
    "fill",
    "type",
    "hover",
    "focus",
    "check",
    "uncheck",
    "select",
    "drag",
    "upload",
    "download",
    "press",
    "key",
    "keydown",
    "keyup",
    "keyboard",
    "scroll",
    "scrollintoview",
    "scrollinto",
    "wait",
    "screenshot",
    "pdf",
    "snapshot",
    "eval",
    "close",
    "quit",
    "exit",
    "inspect",
    "auth",
    "confirm",
    "deny",
    "connect",
    "cookies",
    "storage",
    "window",
    "frame",
    "dialog",
    "trace",
    "profiler",
    "record",
    "har",
    "network",
    "title",
    "url",
    "console",
    "errors",
    "highlight",
    "state",
    "emulate",
    "video",
    "tap",
    "swipe",
    "device",
    "batch",
    "diff",
    "find",
    "role",
    "text",
    "label",
    "placeholder",
    "alt",
    "testid",
    "first",
    "last",
    "nth",
    "mouse",
    "touchscreen",
    "attribute",
    "property",
    "set",
    "get",
    "is",
    "stream",
    "tab",
    "clipboard",
    "session",
];

const ALLOWED_GLOBAL_FLAGS: &[&str] = &["--session", "--engine"];

pub(crate) async fn execute_chat_tool(session: &str, command: &str) -> String {
    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => return format!("Failed to resolve executable: {}", e),
    };

    let single = command.split("&&").next().unwrap_or(command);
    let single = single.split(';').next().unwrap_or(single).trim();
    let stripped = single.strip_prefix("agent-browser ").unwrap_or(single);
    let words = crate::commands::shell_words_split(stripped);

    let mut global_flags: Vec<String> = Vec::new();
    let mut cmd_words: Vec<String> = Vec::new();
    let mut has_session_flag = false;
    let mut i = 0;
    while i < words.len() {
        if ALLOWED_GLOBAL_FLAGS.contains(&words[i].as_str()) {
            if words[i] == "--session" {
                has_session_flag = true;
            }
            global_flags.push(words[i].clone());
            if i + 1 < words.len() {
                global_flags.push(words[i + 1].clone());
                i += 2;
            } else {
                i += 1;
            }
        } else {
            cmd_words.push(words[i].clone());
            i += 1;
        }
    }

    let first_cmd = cmd_words.first().map(|s| s.as_str()).unwrap_or("");
    if !ALLOWED_COMMANDS.contains(&first_cmd) {
        return format!(
            "Blocked: '{}' is not a valid agent-browser command.",
            first_cmd
        );
    }

    let mut args: Vec<String> = Vec::new();
    if !has_session_flag {
        args.push("--session".into());
        args.push(session.into());
    }
    args.extend(global_flags);
    args.extend(cmd_words);

    let mut cmd = tokio::process::Command::new(&exe);
    cmd.args(&args);

    match cmd.output().await {
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            if stdout.is_empty() && !stderr.is_empty() {
                stderr
            } else if stdout.is_empty() {
                "Command completed with no output.".to_string()
            } else {
                stdout
            }
        }
        Err(e) => format!("Failed to execute command: {}", e),
    }
}



