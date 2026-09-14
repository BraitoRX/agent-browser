from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Optional

IDENTITY_FILENAME = "camoufox-identity.json"
LOCK_FILENAME = "camoufox-identity.lock"
FORMAT_VERSION = 1
MAX_IDENTITY_BYTES = 1024 * 1024
ALLOWED_FIREFOX_PREFS = frozenset(("webgl.enable-webgl2", "webgl.force-enabled"))
CAMOU_CONFIG_CHUNK_RE = re.compile(r"^CAMOU_CONFIG_([0-9]+)$")


class PersistentProfileError(ValueError):
    pass


class PersistentProfile:
    """Own one private profile lock and its generated identity; closing never deletes account data."""

    def __init__(self, path: str) -> None:
        if not isinstance(path, str):
            raise PersistentProfileError("profile path must be a string")
        if not path:
            raise PersistentProfileError("profile path must not be empty")
        if path.startswith("~"):
            raise PersistentProfileError(
                f"profile path must be expanded, not start with '~': {path!r}"
            )
        if "\x00" in path:
            raise PersistentProfileError("profile path must not contain NUL characters")
        if not os.path.isabs(path):
            raise PersistentProfileError(f"profile path must be absolute: {path!r}")
        self.path = Path(path)
        self._lock_fd: Optional[int] = None

    def acquire(self) -> None:
        if self._lock_fd is not None:
            raise PersistentProfileError("profile lock is already held by this instance")
        self._ensure_profile_dir()
        self.path = self.path.resolve()
        fd = self._open_lock_file()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise PersistentProfileError(
                    f"profile at {self.path} is already locked by another process"
                ) from exc
            self._verify_lock_file(fd)
        except BaseException:
            self._release_fd(fd)
            raise
        self._lock_fd = fd

    def launch_kwargs(self, executable_path: str) -> Dict[str, Any]:
        if self._lock_fd is None:
            raise PersistentProfileError("profile lock is not held")
        given = self._resolve_browser_path(executable_path)
        identity = self._read_identity()
        if identity is None:
            if any(entry.name != LOCK_FILENAME for entry in self.path.iterdir()):
                raise PersistentProfileError("profile has existing data but no identity; refusing to generate a replacement")
            return {}
        stored = identity["browserPath"]
        if os.path.realpath(stored) != given:
            raise PersistentProfileError(
                f"stored identity belongs to browser {stored!r}, not {executable_path!r}"
            )
        config = identity.get("config")
        prefs = identity.get("firefox_user_prefs")
        if not isinstance(config, dict) or not config:
            raise PersistentProfileError(
                f"identity 'config' in {self.path} must be an object"
            )
        if not isinstance(prefs, dict):
            raise PersistentProfileError(
                f"identity 'firefox_user_prefs' in {self.path} must be an object"
            )
        if set(prefs) - ALLOWED_FIREFOX_PREFS or any(not isinstance(value, bool) for value in prefs.values()):
            raise PersistentProfileError("identity contains unsupported Firefox preferences")
        if "humanize" in config or "humanize:maxTime" in config:
            raise PersistentProfileError("identity must not override the session motion setting")
        return {
            "config": config,
            "firefox_user_prefs": prefs,
            "i_know_what_im_doing": True,
        }

    def save_identity(self, options: Dict[str, Any], executable_path: str) -> None:
        if self._lock_fd is None:
            raise PersistentProfileError(
                "profile lock is not held; call acquire() before save_identity()"
            )
        if not isinstance(options, dict):
            raise PersistentProfileError("options must be a dict")
        browser_path = self._resolve_browser_path(executable_path)
        identity_path = self.path / IDENTITY_FILENAME
        try:
            os.lstat(identity_path)
        except FileNotFoundError:
            pass
        else:
            raise PersistentProfileError(
                f"identity file {identity_path} already exists; refusing to overwrite"
            )
        identity = {
            "formatVersion": FORMAT_VERSION,
            "browserPath": browser_path,
            "config": self._extract_config(options),
            "firefox_user_prefs": self._extract_prefs(options),
        }
        try:
            encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
        except (TypeError, ValueError) as exc:
            raise PersistentProfileError(f"identity is not serializable: {exc}") from exc
        payload = encoded.encode("utf-8")
        if len(payload) > MAX_IDENTITY_BYTES:
            raise PersistentProfileError(
                f"serialized identity exceeds the {MAX_IDENTITY_BYTES} byte limit"
            )
        self._publish_identity(identity_path, payload)

    def release(self) -> None:
        fd = self._lock_fd
        if fd is None:
            return
        self._lock_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _ensure_profile_dir(self) -> None:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            st = self._create_profile_dir()
        self._check_existing_dir(st)

    def _create_profile_dir(self) -> os.stat_result:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.mkdir(self.path, 0o700)
        except FileNotFoundError as exc:
            raise PersistentProfileError(
                f"parent directory of {self.path} does not exist"
            ) from exc
        except FileExistsError:
            return os.lstat(self.path)
        try:
            st = os.lstat(self.path)
        except FileNotFoundError as exc:
            raise PersistentProfileError(
                f"profile directory {self.path} disappeared during creation"
            ) from exc
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PersistentProfileError(
                f"profile path {self.path} was replaced during creation"
            )
        try:
            os.chmod(self.path, 0o700)
            return os.lstat(self.path)
        except OSError as exc:
            raise PersistentProfileError(
                f"could not secure the new profile directory {self.path}: {exc}"
            ) from exc

    def _check_existing_dir(self, st: os.stat_result) -> None:
        if stat.S_ISLNK(st.st_mode):
            raise PersistentProfileError(f"profile path is a symlink: {self.path}")
        if not stat.S_ISDIR(st.st_mode):
            raise PersistentProfileError(f"profile path is not a directory: {self.path}")
        if st.st_uid != os.getuid():
            raise PersistentProfileError(
                f"profile directory {self.path} is owned by uid {st.st_uid}, "
                f"not the current user {os.getuid()}"
            )
        if st.st_mode & 0o077:
            raise PersistentProfileError(
                f"profile directory {self.path} is accessible to group or others; "
                "refusing to adjust existing data"
            )

    def _open_lock_file(self) -> int:
        lock_path = self.path / LOCK_FILENAME
        try:
            fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PersistentProfileError(
                    f"profile lock file {lock_path} is a symlink"
                ) from exc
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise PersistentProfileError(
                    f"profile lock file {lock_path} is not usable by the current user"
                ) from exc
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise PersistentProfileError(
                    f"profile lock file {lock_path} is not a regular file"
                )
            if st.st_uid != os.getuid():
                raise PersistentProfileError(
                    f"profile lock file {lock_path} is owned by another user"
                )
            if st.st_mode & 0o077:
                raise PersistentProfileError(
                    f"profile lock file {lock_path} is accessible to group or others"
                )
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _verify_lock_file(self, fd: int) -> None:
        lock_path = self.path / LOCK_FILENAME
        try:
            current = os.lstat(lock_path)
        except OSError as exc:
            raise PersistentProfileError(
                f"profile lock file {lock_path} disappeared while acquiring the lock: {exc}"
            ) from exc
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise PersistentProfileError(
                f"profile lock file {lock_path} was replaced while acquiring the lock"
            )

    def _read_identity(self) -> Optional[Dict[str, Any]]:
        identity_path = self.path / IDENTITY_FILENAME
        try:
            fd = os.open(
                identity_path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PersistentProfileError(
                    f"identity file {identity_path} is a symlink"
                ) from exc
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise PersistentProfileError(
                    f"identity file {identity_path} is not readable by the current user"
                ) from exc
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise PersistentProfileError(
                    f"identity file {identity_path} is not a regular file"
                )
            if st.st_uid != os.getuid():
                raise PersistentProfileError(
                    f"identity file {identity_path} is owned by another user"
                )
            if st.st_mode & 0o077:
                raise PersistentProfileError(
                    f"identity file {identity_path} is accessible to group or others"
                )
            if st.st_size > MAX_IDENTITY_BYTES:
                raise PersistentProfileError(
                    f"identity file {identity_path} exceeds the "
                    f"{MAX_IDENTITY_BYTES} byte limit"
                )
            data = _read_bounded(fd, st.st_size)
        finally:
            os.close(fd)
        try:
            identity = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PersistentProfileError(
                f"identity file {identity_path} is corrupt: {exc}"
            ) from exc
        if not isinstance(identity, dict):
            raise PersistentProfileError(
                f"identity file {identity_path} must contain a JSON object"
            )
        version = identity.get("formatVersion")
        if isinstance(version, bool) or not isinstance(version, int) or version != FORMAT_VERSION:
            raise PersistentProfileError(
                f"identity file {identity_path} has unsupported formatVersion {version!r}"
            )
        browser_path = identity.get("browserPath")
        if not isinstance(browser_path, str) or not os.path.isabs(browser_path):
            raise PersistentProfileError(
                f"identity file {identity_path} has no browserPath"
            )
        return identity

    def _publish_identity(self, identity_path: Path, payload: bytes) -> None:
        tmp_path = identity_path.with_name(
            f"{identity_path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        )
        fd: Optional[int] = None
        try:
            fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            try:
                os.link(tmp_path, identity_path)
            except FileExistsError as exc:
                raise PersistentProfileError(
                    f"identity file {identity_path} appeared during publish; "
                    "refusing to overwrite"
                ) from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _extract_config(options: Dict[str, Any]) -> Dict[str, Any]:
        env = options.get("env")
        if not isinstance(env, dict):
            raise PersistentProfileError("options['env'] must be a dict")
        chunks: Dict[int, str] = {}
        for key, value in env.items():
            if not isinstance(key, str):
                continue
            match = CAMOU_CONFIG_CHUNK_RE.match(key)
            if match is None:
                continue
            if not isinstance(value, str):
                raise PersistentProfileError(
                    f"environment chunk {key} must be a string"
                )
            index = int(match.group(1))
            if index in chunks or match.group(1) != str(index):
                raise PersistentProfileError("generated Camoufox config has duplicate or noncanonical chunk indices")
            chunks[index] = value
        if not chunks:
            raise PersistentProfileError(
                "options['env'] contains no CAMOU_CONFIG_<n> chunks"
            )
        indices = sorted(chunks)
        if indices != list(range(1, len(indices) + 1)):
            raise PersistentProfileError(
                f"options['env'] CAMOU_CONFIG_<n> chunks are not contiguous "
                f"from 1: {indices}"
            )
        raw = "".join(chunks[index] for index in indices)
        if len(raw.encode("utf-8")) > MAX_IDENTITY_BYTES:
            raise PersistentProfileError("generated Camoufox config exceeds the identity size limit")
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PersistentProfileError(
                f"generated Camoufox config is corrupt: {exc}"
            ) from exc
        if not isinstance(config, dict) or not config:
            raise PersistentProfileError(
                "generated Camoufox config must be a JSON object"
            )
        config.pop("humanize", None)
        config.pop("humanize:maxTime", None)
        return config

    @staticmethod
    def _extract_prefs(options: Dict[str, Any]) -> Dict[str, Any]:
        raw = options.get("firefox_user_prefs")
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise PersistentProfileError(
                "options['firefox_user_prefs'] must be a dict"
            )
        return {
            key: value for key, value in raw.items() if key in ALLOWED_FIREFOX_PREFS
        }

    @staticmethod
    def _resolve_browser_path(executable_path: str) -> str:
        if not isinstance(executable_path, str):
            raise PersistentProfileError("executable_path must be a string")
        if not executable_path:
            raise PersistentProfileError("executable_path must not be empty")
        return os.path.realpath(executable_path)

    @staticmethod
    def _release_fd(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _read_bounded(fd: int, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
