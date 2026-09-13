#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REQUIREMENTS = SCRIPT_DIR / "requirements.txt"
PINS = {"camoufox": "0.5.6", "playwright": "1.61.0"}
MIN_PYTHON = (3, 10)
LOG_STREAM = sys.stderr
FETCH_TIMEOUT_SECONDS = 900
INSTALL_TIMEOUT_SECONDS = 900


class BootstrapError(Exception):
    pass


class InstallLock:
    def __init__(self, runtime_dir: Path):
        self.path = runtime_dir / ".bootstrap.lock"
        self.token = f"{os.getpid()} {time.time():.0f}"

    def acquire(self) -> None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                owner = self.path.read_text("utf-8").strip()
            except OSError:
                owner = "unknown"
            raise BootstrapError(
                f"another bootstrap appears to be running or left a stale lock at {self.path} "
                f"(owner: {owner}). Remove the lock file manually only if no bootstrap process "
                "is alive, then retry."
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.token)

    def release(self) -> None:
        try:
            if self.path.is_file() and self.path.read_text("utf-8").strip() == self.token:
                self.path.unlink()
        except OSError:
            pass


def private_env(runtime_dir: Path) -> dict:
    env = os.environ.copy()
    home = runtime_dir / "home"
    cache = home / ".cache"
    env["HOME"] = str(home)
    env["XDG_CACHE_HOME"] = str(cache)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
        env["APPDATA"] = str(home / "AppData" / "Roaming")
    for key in list(env):
        upper = key.upper()
        if upper in {"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "GH_PAT"} or upper.endswith("_GITHUB_TOKEN"):
            env.pop(key, None)
    return env


def run_streaming(cmd, env, cwd, timeout, label) -> int:
    try:
        process = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=LOG_STREAM, stderr=LOG_STREAM)
    except OSError as exc:
        raise BootstrapError(f"{label} could not start: {exc}") from exc
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise BootstrapError(f"{label} timed out after {timeout}s")


def probe_metadata(venv_python: Path, env: dict) -> dict:
    metadata = {"browserVersion": None, "browserPath": None, "probe": "unavailable"}
    for args, key in ((["active"], "browserVersion"), (["path"], "browserPath")):
        try:
            completed = subprocess.run(
                [str(venv_python), "-m", "camoufox", *args],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        text = completed.stdout.decode("utf-8", errors="replace").strip()
        if not text or len(text) > 512:
            continue
        first_line = text.splitlines()[0].strip()
        if first_line:
            metadata[key] = first_line
            metadata["probe"] = "official CLI"
    return metadata


def installed_executable(venv_python: Path, env: dict, runtime_dir: Path) -> str:
    script = (
        "import contextlib, sys\n"
        "with contextlib.redirect_stdout(sys.stderr):\n"
        "    from camoufox.pkgman import camoufox_path, launch_path\n"
        "    executable = launch_path(camoufox_path(download_if_missing=False))\n"
        "print(executable)\n"
    )
    result = subprocess.run(
        [str(venv_python), "-c", script],
        env=env, stdout=subprocess.PIPE, stderr=LOG_STREAM, timeout=60, check=False,
    )
    if result.returncode != 0:
        raise BootstrapError("installed browser executable could not be resolved")
    executable = Path(result.stdout.decode("utf-8").strip()).resolve()
    try:
        executable.relative_to((runtime_dir / "home").resolve())
    except ValueError:
        raise BootstrapError("browser executable resolved outside the private runtime home") from None
    if not executable.is_file():
        raise BootstrapError("installed browser executable is missing")
    return str(executable)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the isolated camoufox backend runtime.")
    parser.add_argument("--runtime-dir", required=True, help="absolute runtime directory")
    args = parser.parse_args(argv)

    if sys.version_info < MIN_PYTHON:
        print(json.dumps({
            "installed": False,
            "error": f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required",
            "python": sys.version.split()[0],
        }))
        return 2

    runtime_dir = Path(args.runtime_dir).expanduser()
    if not runtime_dir.is_absolute():
        runtime_dir = (Path.cwd() / runtime_dir).resolve()
    else:
        runtime_dir = runtime_dir.resolve()

    if not REQUIREMENTS.is_file():
        print(json.dumps({"installed": False, "error": f"requirements file is missing: {REQUIREMENTS}"}))
        return 2

    lock = InstallLock(runtime_dir)
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        home = runtime_dir / "home"
        cache = home / ".cache"
        (cache / "pip").mkdir(parents=True, exist_ok=True)
        (runtime_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            (home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
            (home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
        lock.acquire()
        env = private_env(runtime_dir)

        venv_dir = runtime_dir / "venv"
        venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not venv_python.is_file():
            code = run_streaming(
                [sys.executable, "-m", "venv", str(venv_dir)],
                env,
                runtime_dir,
                INSTALL_TIMEOUT_SECONDS,
                "venv creation",
            )
            if code != 0:
                raise BootstrapError(f"venv creation failed with exit code {code}")

        pip_check = subprocess.run(
            [str(venv_python), "-m", "pip", "--version"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if pip_check.returncode != 0:
            code = run_streaming(
                [str(venv_python), "-m", "ensurepip", "--upgrade"],
                env,
                runtime_dir,
                INSTALL_TIMEOUT_SECONDS,
                "pip bootstrap",
            )
            if code != 0:
                raise BootstrapError(f"pip bootstrap failed with exit code {code}")

        code = run_streaming(
            [
                str(venv_python), "-m", "pip", "install",
                "--disable-pip-version-check", "--no-input", "--no-warn-script-location",
                "-r", str(REQUIREMENTS),
            ],
            env,
            SCRIPT_DIR,
            INSTALL_TIMEOUT_SECONDS,
            "package install",
        )
        if code != 0:
            raise BootstrapError(f"package install failed with exit code {code}")

        code = run_streaming(
            [str(venv_python), "-m", "camoufox", "set", "official/stable"],
            env,
            SCRIPT_DIR,
            FETCH_TIMEOUT_SECONDS,
            "browser channel selection",
        )
        if code != 0:
            raise BootstrapError(f"browser channel selection failed with exit code {code}")

        code = run_streaming(
            [str(venv_python), "-m", "camoufox", "fetch"],
            env,
            SCRIPT_DIR,
            FETCH_TIMEOUT_SECONDS,
            "browser fetch",
        )
        if code != 0:
            raise BootstrapError(f"browser fetch failed with exit code {code}")

        metadata = probe_metadata(venv_python, env)
        executable = installed_executable(venv_python, env, runtime_dir)
        record = {
            "schema": 1,
            "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "runtimeDir": str(runtime_dir),
            "python": sys.version.split()[0],
            "venv": str(venv_dir),
            "pins": PINS,
            "browser": {
                "fetched": True,
                "version": metadata["browserVersion"],
                "path": metadata["browserPath"],
                "executablePath": executable,
                "source": "official/stable via `python -m camoufox set official/stable` and `python -m camoufox fetch`",
                "metadataProbe": metadata["probe"],
            },
            "note": "Browser version metadata is recorded when the official CLI exposes it; "
            "this is not a full dependency lock.",
        }
        record_path = runtime_dir / "runtime.json"
        tmp_path = runtime_dir / ".runtime.json.tmp"
        tmp_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(record_path)

        print(json.dumps({
            "installed": True,
            "runtimeDir": str(runtime_dir),
            "python": sys.version.split()[0],
            "venv": str(venv_dir),
            "pins": PINS,
            "browser": record["browser"],
            "record": str(record_path),
        }))
        return 0
    except (BootstrapError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"installed": False, "runtimeDir": str(runtime_dir), "error": str(exc)}))
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
