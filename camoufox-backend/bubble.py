from __future__ import annotations

import atexit
import os
import socket
import subprocess
import threading
import time
from typing import Callable, List, Optional, Tuple

from input_context import CODE_NOT_LAUNCHED, BackendError

BUBBLE_ENV = "AGENT_BROWSER_BUBBLE"
DISPLAY_NAME = ":99"
X_SOCKET_PATH = "/tmp/.X11-unix/X99"
SCREEN_GEOMETRY = "1920x1200x24"
VNC_CLIP = "1920x1080+0+0"
LOOPBACK = "127.0.0.1"
RFB_PORT = 5900
HTTP_PORT = 6080
NOVNC_WEB_DIR = "/usr/share/novnc"
READY_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.1
TERMINATE_TIMEOUT_SECONDS = 3.0

_lock = threading.Lock()
_processes: List[Tuple[str, subprocess.Popen]] = []
_started = False
_exit_hook_registered = False


def bubble_enabled() -> bool:
    return os.environ.get(BUBBLE_ENV) == "1"


def _spawn(name: str, command: List[str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise BackendError(
            CODE_NOT_LAUNCHED,
            f"bubble mode could not start {name}: {exc}",
        ) from exc


def _x_socket_ready() -> bool:
    return os.path.exists(X_SOCKET_PATH)


def _rfb_ready() -> bool:
    try:
        probe = socket.create_connection((LOOPBACK, RFB_PORT), timeout=0.5)
    except OSError:
        return False
    try:
        banner = probe.recv(12)
    except OSError:
        return False
    finally:
        probe.close()
    return banner.startswith(b"RFB")


def _http_ready() -> bool:
    try:
        socket.create_connection((LOOPBACK, HTTP_PORT), timeout=0.5).close()
        return True
    except OSError:
        return False


def _dead_component() -> Optional[str]:
    for name, process in _processes:
        if process.poll() is not None:
            return name
    return None


def _teardown() -> None:
    for _name, process in reversed(_processes):
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    for _name, process in reversed(_processes):
        try:
            process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            except Exception:
                pass
        except Exception:
            pass
    _processes.clear()


def _fail(reason: str) -> None:
    _teardown()
    raise BackendError(
        CODE_NOT_LAUNCHED,
        f"bubble mode is unavailable: {reason}",
    )


def _wait_ready(check: Callable[[], bool], deadline: float, component: str, state: str) -> None:
    while True:
        dead = _dead_component()
        if dead is not None:
            _fail(f"{dead} exited while {state}")
        if check():
            return
        if time.monotonic() >= deadline:
            _fail(f"{component} was not ready within {READY_TIMEOUT_SECONDS:.0f}s while {state}")
        time.sleep(POLL_SECONDS)


def _start_locked(deadline: float) -> None:
    xvfb = _spawn("Xvfb", ["Xvfb", DISPLAY_NAME, "-screen", "0", SCREEN_GEOMETRY, "-nolisten", "tcp"])
    _processes.append(("Xvfb", xvfb))
    _wait_ready(_x_socket_ready, deadline, "Xvfb", "waiting for the X socket")
    vnc = _spawn(
        "x11vnc",
        [
            "x11vnc", "-display", DISPLAY_NAME, "-forever", "-shared", "-nopw", "-quiet",
            "-rfbport", str(RFB_PORT), "-clip", VNC_CLIP, "-noxdamage", "-defer", "10",
        ],
    )
    _processes.append(("x11vnc", vnc))
    proxy = _spawn(
        "websockify",
        ["websockify", "--web", NOVNC_WEB_DIR, str(HTTP_PORT), f"{LOOPBACK}:{RFB_PORT}"],
    )
    _processes.append(("websockify", proxy))
    _wait_ready(_rfb_ready, deadline, "x11vnc", "waiting for the RFB handshake on port 5900")
    _wait_ready(_http_ready, deadline, "websockify", "waiting for the HTTP listener on port 6080")
    os.environ["DISPLAY"] = DISPLAY_NAME


def ensure_bubble_stack() -> None:
    global _started, _exit_hook_registered
    with _lock:
        if _started:
            if _dead_component() is None:
                return
            _teardown()
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        try:
            _start_locked(deadline)
            _started = True
            if not _exit_hook_registered:
                atexit.register(stop_bubble_stack)
                _exit_hook_registered = True
        except BackendError:
            raise
        except Exception as exc:
            _teardown()
            raise BackendError(
                CODE_NOT_LAUNCHED,
                f"bubble mode could not start the private X stack: {type(exc).__name__}",
            ) from exc


def stop_bubble_stack() -> None:
    global _started
    with _lock:
        _teardown()
        _started = False