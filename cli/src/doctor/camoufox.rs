//! Check the Camoufox V1 stack: managed runtime install, input backend
//! configuration (including the Docker/OrbStack bubble prerequisites), and
//! motion profile.

use std::env;
use std::path::PathBuf;
use std::process::{Command, Stdio};

use super::{Check, Status};
use crate::native::camoufox::{
    input_backend, motion, runtime_dir, BUBBLE_IMAGE, INPUT_BACKEND_JUGGLER,
};

pub(super) fn check(checks: &mut Vec<Check>) {
    let category = "Camoufox";

    match runtime_dir() {
        Ok(root) => {
            let runtime_json = root.join("runtime.json");
            if runtime_json_readable(&runtime_json) {
                checks.push(Check::new(
                    "camoufox.runtime",
                    category,
                    Status::Pass,
                    format!("Managed runtime at {}", root.display()),
                ));
            } else {
                checks.push(
                    Check::new(
                        "camoufox.runtime",
                        category,
                        Status::Warn,
                        format!(
                            "Camoufox runtime is not installed at {} (missing or unreadable runtime.json)",
                            root.display()
                        ),
                    )
                    .with_fix("run agent-browser --engine camoufox install"),
                );
            }
        }
        Err(e) => checks.push(
            Check::new("camoufox.runtime", category, Status::Warn, e).with_fix(
                "set AGENT_BROWSER_CAMOUFOX_RUNTIME to an absolute runtime directory",
            ),
        ),
    }

    match input_backend() {
        Ok(backend) => {
            if backend == INPUT_BACKEND_JUGGLER {
                checks.push(Check::new(
                    "camoufox.input_backend",
                    category,
                    Status::Pass,
                    "Input backend juggler (default; no Docker needed)",
                ));
            } else {
                let (docker_found, daemon_reachable, image_present) = probe_docker();
                let (status, message) = match bubble_outcome(
                    docker_found,
                    daemon_reachable,
                    image_present,
                ) {
                    Some(reason) => (Status::Warn, reason),
                    None => (
                        Status::Pass,
                        format!(
                            "Input backend os-native; Docker daemon reachable and bubble image {} present",
                            BUBBLE_IMAGE
                        ),
                    ),
                };
                checks.push(Check::new(
                    "camoufox.input_backend",
                    category,
                    status,
                    message,
                ));
            }
        }
        Err(e) => checks.push(Check::new(
            "camoufox.input_backend",
            category,
            Status::Fail,
            e,
        )),
    }

    match motion() {
        Ok(value) => checks.push(Check::new(
            "camoufox.motion",
            category,
            Status::Pass,
            format!("Motion profile {}", value),
        )),
        Err(e) => checks.push(
            Check::new("camoufox.motion", category, Status::Fail, e)
                .with_fix("set AGENT_BROWSER_MOTION to human-fast, fast, or precision"),
        ),
    }
}

fn runtime_json_readable(path: &std::path::Path) -> bool {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|content| serde_json::from_str::<serde_json::Value>(&content).ok())
        .is_some()
}

/// Mirror of the launch-time Docker readiness probe for os-native input.
/// Returns (docker cli found, daemon reachable, bubble image present).
fn probe_docker() -> (bool, bool, bool) {
    if which_docker().is_none() {
        return (false, false, false);
    }
    let daemon_reachable = docker_run(["version", "--format", "{{.Server.Version}}"]).is_ok();
    if !daemon_reachable {
        return (true, false, false);
    }
    let image_present = docker_run(["image", "inspect", BUBBLE_IMAGE, "--format", "{{.Id}}"])
        .is_ok();
    (true, daemon_reachable, image_present)
}

fn bubble_outcome(
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

fn which_docker() -> Option<PathBuf> {
    let path_env = env::var("PATH").unwrap_or_default();
    for dir in env::split_paths(&path_env) {
        let candidate = dir.join("docker");
        if candidate.is_file() {
            return Some(candidate);
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
            return Some(candidate);
        }
    }
    None
}

fn docker_run<I, S>(args: I) -> Result<String, String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let output = Command::new("docker")
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bubble_outcome_matches_launch_time_messages() {
        assert!(bubble_outcome(true, true, true).is_none());
        assert!(bubble_outcome(false, true, true)
            .unwrap()
            .contains("docker CLI"));
        assert!(bubble_outcome(true, false, true)
            .unwrap()
            .contains("OrbStack"));
        assert!(bubble_outcome(true, true, false)
            .unwrap()
            .contains("build.sh"));
    }

    #[test]
    fn probe_docker_short_circuits_without_the_cli() {
        let found = which_docker().is_some();
        let (found_probe, _reachable, _image) = probe_docker();
        assert_eq!(found_probe, found);
    }
}