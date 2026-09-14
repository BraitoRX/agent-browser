import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from persistent_profile import (
    ALLOWED_FIREFOX_PREFS,
    FORMAT_VERSION,
    IDENTITY_FILENAME,
    LOCK_FILENAME,
    MAX_IDENTITY_BYTES,
    PersistentProfile,
    PersistentProfileError,
)

BROWSER_PATH = "/opt/camoufox/camoufox-bin"
OTHER_BROWSER_PATH = "/opt/camoufox/other-camoufox-bin"
BASE_CONFIG = {
    "navigator.userAgent": "Mozilla/5.0 (X11; Linux x86_64) CamoufoxTest/1.0",
    "navigator.platform": "Linux x86_64",
    "screen.width": 1280,
}
BASE_PREFS = {
    "webgl.enable-webgl2": True,
    "webgl.force-enabled": True,
}


def build_options(config=None, prefs=None, env_extra=None):
    raw = json.dumps(BASE_CONFIG if config is None else config)
    env = {"CAMOU_CONFIG_1": raw}
    if env_extra:
        env.update(env_extra)
    return {
        "env": env,
        "firefox_user_prefs": dict(BASE_PREFS if prefs is None else prefs),
    }


def mode_of(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def write_json_file(path, payload, mode=0o600):
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)


class ProfileTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="persistent-profile-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


class PathValidationTests(ProfileTestCase):
    def test_rejects_relative_empty_tilde_and_non_string(self):
        for bad in [
            "",
            "profiles/x",
            "./profiles/x",
            "../profiles/x",
            "~",
            "~/profiles",
            "~user/profiles",
            None,
            123,
        ]:
            with self.subTest(path=bad):
                with self.assertRaises(PersistentProfileError):
                    PersistentProfile(bad)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_constructor_does_not_create_profile_dir(self):
        profile_path = self.root / "profile"
        PersistentProfile(str(profile_path))
        self.assertFalse(profile_path.exists())


class AcquireTests(ProfileTestCase):
    def test_private_creation_modes(self):
        profile_path = self.root / "profile"
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        try:
            self.assertTrue(profile_path.is_dir())
            self.assertEqual(mode_of(profile_path) & 0o777, 0o700)
            lock_path = profile_path / LOCK_FILENAME
            self.assertTrue(stat.S_ISREG(os.lstat(lock_path).st_mode))
            self.assertEqual(mode_of(lock_path) & 0o777, 0o600)
            self.assertFalse((profile_path / IDENTITY_FILENAME).exists())
        finally:
            profile.release()
        self.assertTrue(lock_path.exists())

    def test_rejects_symlink_profile_and_leaves_target_untouched(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real)
        profile = PersistentProfile(str(link))
        with self.assertRaises(PersistentProfileError) as ctx:
            profile.acquire()
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(list(real.iterdir()), [])
        self.assertEqual(mode_of(real) & 0o777, 0o700)

    def test_rejects_wide_permission_dir_without_chmod(self):
        for wide_mode in (0o755, 0o750):
            with self.subTest(mode=oct(wide_mode)):
                profile_path = self.root / ("shared-%o" % wide_mode)
                profile_path.mkdir(mode=0o700)
                os.chmod(profile_path, wide_mode)
                profile = PersistentProfile(str(profile_path))
                with self.assertRaises(PersistentProfileError):
                    profile.acquire()
                self.assertEqual(mode_of(profile_path) & 0o777, wide_mode)

    def test_lock_collision_release_reacquire(self):
        profile_path = self.root / "profile"
        first = PersistentProfile(str(profile_path))
        second = PersistentProfile(str(profile_path))
        first.acquire()
        with self.assertRaises(PersistentProfileError) as ctx:
            second.acquire()
        self.assertIn("already locked", str(ctx.exception))
        first.release()
        second.acquire()
        with self.assertRaises(PersistentProfileError):
            first.acquire()
        second.release()
        first.acquire()
        first.release()
        self.assertTrue((profile_path / LOCK_FILENAME).exists())
        self.assertFalse((profile_path / IDENTITY_FILENAME).exists())

    def test_double_acquire_on_same_instance_rejected(self):
        profile = PersistentProfile(str(self.root / "profile"))
        profile.acquire()
        try:
            with self.assertRaises(PersistentProfileError):
                profile.acquire()
        finally:
            profile.release()

    def test_release_without_acquire_is_noop_and_profile_is_kept(self):
        profile_path = self.root / "profile"
        profile = PersistentProfile(str(profile_path))
        profile.release()
        profile.acquire()
        marker = profile_path / "user-data.txt"
        marker.write_text("keep me")
        profile.release()
        profile.release()
        self.assertTrue(profile_path.is_dir())
        self.assertTrue(marker.exists())


class IdentityLifecycleTests(ProfileTestCase):
    def test_missing_identity_with_existing_profile_data_is_refused(self):
        profile = PersistentProfile(str(self.root / "profile"))
        profile.acquire()
        try:
            marker = profile.path / "prefs.js"
            marker.write_text("existing browser data")
            with self.assertRaises(PersistentProfileError):
                profile.launch_kwargs(BROWSER_PATH)
            self.assertEqual(marker.read_text(), "existing browser data")
            self.assertFalse((profile.path / IDENTITY_FILENAME).exists())
        finally:
            profile.release()

    def test_missing_identity_returns_empty_kwargs(self):
        profile = PersistentProfile(str(self.root / "profile"))
        profile.acquire()
        try:
            self.assertEqual(profile.launch_kwargs(BROWSER_PATH), {})
            self.assertFalse((self.root / "profile" / IDENTITY_FILENAME).exists())
        finally:
            profile.release()

    def test_save_and_launch_require_lock(self):
        profile = PersistentProfile(str(self.root / "profile"))
        with self.assertRaises(PersistentProfileError):
            profile.save_identity(build_options(), BROWSER_PATH)
        with self.assertRaises(PersistentProfileError):
            profile.launch_kwargs(BROWSER_PATH)
        self.assertEqual(list(self.root.joinpath("profile").iterdir()) if self.root.joinpath("profile").exists() else [], [])

    def test_saved_identity_contains_only_allowed_data(self):
        profile_path = self.root / "profile"
        options = build_options(
            config={**BASE_CONFIG, "humanize": True, "humanize:maxTime": 1.5},
            prefs={**BASE_PREFS, "privacy.resistFingerprinting": True},
            env_extra={"SECRET_TOKEN": "s3cr3t-value", "FONTCONFIG_PATH": "/fonts"},
        )
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        try:
            profile.save_identity(options, BROWSER_PATH)
        finally:
            profile.release()
        identity_path = profile_path / IDENTITY_FILENAME
        self.assertEqual(mode_of(identity_path) & 0o777, 0o600)
        text = identity_path.read_text(encoding="utf-8")
        stored = json.loads(text)
        self.assertEqual(
            set(stored),
            {"formatVersion", "browserPath", "config", "firefox_user_prefs"},
        )
        self.assertEqual(stored["formatVersion"], FORMAT_VERSION)
        self.assertEqual(stored["browserPath"], os.path.realpath(BROWSER_PATH))
        self.assertEqual(stored["config"], BASE_CONFIG)
        self.assertNotIn("humanize", stored["config"])
        self.assertNotIn("humanize:maxTime", stored["config"])
        self.assertEqual(stored["firefox_user_prefs"], BASE_PREFS)
        self.assertTrue(set(stored["firefox_user_prefs"]) <= ALLOWED_FIREFOX_PREFS)
        self.assertNotIn("s3cr3t-value", text)
        self.assertNotIn("SECRET_TOKEN", text)
        self.assertNotIn("FONTCONFIG_PATH", text)
        for entry in profile_path.iterdir():
            self.assertNotIn("s3cr3t-value", entry.read_text(encoding="utf-8", errors="replace"))

    def test_restart_reuses_identity_unchanged_bytes(self):
        profile_path = self.root / "profile"
        options = build_options(
            config={**BASE_CONFIG, "humanize": True, "humanize:maxTime": 1.5},
        )
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        profile.save_identity(options, BROWSER_PATH)
        identity_path = profile_path / IDENTITY_FILENAME
        saved = identity_path.read_bytes()
        kwargs = profile.launch_kwargs(BROWSER_PATH)
        self.assertEqual(kwargs["config"], BASE_CONFIG)
        self.assertEqual(kwargs["firefox_user_prefs"], BASE_PREFS)
        self.assertIs(kwargs["i_know_what_im_doing"], True)
        profile.release()

        restarted = PersistentProfile(str(profile_path))
        restarted.acquire()
        try:
            self.assertEqual(restarted.launch_kwargs(BROWSER_PATH), kwargs)
            with self.assertRaises(PersistentProfileError):
                restarted.save_identity(options, BROWSER_PATH)
            self.assertEqual(identity_path.read_bytes(), saved)
        finally:
            restarted.release()
        self.assertEqual(identity_path.read_bytes(), saved)

    def test_exact_browser_mismatch_rejected(self):
        profile_path = self.root / "profile"
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        try:
            profile.save_identity(build_options(), BROWSER_PATH)
            with self.assertRaises(PersistentProfileError):
                profile.launch_kwargs(OTHER_BROWSER_PATH)
            kwargs = profile.launch_kwargs(BROWSER_PATH)
            self.assertEqual(kwargs["config"], BASE_CONFIG)
        finally:
            profile.release()

    def test_corrupt_identity_rejected_and_never_replaced(self):
        profile_path = self.root / "profile"
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        profile.save_identity(build_options(), BROWSER_PATH)
        profile.release()
        identity_path = profile_path / IDENTITY_FILENAME
        identity_path.write_bytes(b"{not json")
        os.chmod(identity_path, 0o600)
        reopened = PersistentProfile(str(profile_path))
        reopened.acquire()
        try:
            with self.assertRaises(PersistentProfileError):
                reopened.launch_kwargs(BROWSER_PATH)
            with self.assertRaises(PersistentProfileError):
                reopened.save_identity(build_options(), BROWSER_PATH)
            self.assertEqual(identity_path.read_bytes(), b"{not json")
        finally:
            reopened.release()

    def test_incomplete_identity_variants_rejected(self):
        cases = [
            {"browserPath": BROWSER_PATH, "config": BASE_CONFIG, "firefox_user_prefs": {}},
            {
                "formatVersion": FORMAT_VERSION + 1,
                "browserPath": BROWSER_PATH,
                "config": BASE_CONFIG,
                "firefox_user_prefs": {},
            },
            {
                "formatVersion": True,
                "browserPath": BROWSER_PATH,
                "config": BASE_CONFIG,
                "firefox_user_prefs": {},
            },
            {"formatVersion": FORMAT_VERSION, "config": BASE_CONFIG, "firefox_user_prefs": {}},
            {
                "formatVersion": FORMAT_VERSION,
                "browserPath": "relative/browser",
                "config": BASE_CONFIG,
                "firefox_user_prefs": {},
            },
            {"formatVersion": FORMAT_VERSION, "browserPath": BROWSER_PATH, "firefox_user_prefs": {}},
            {
                "formatVersion": FORMAT_VERSION,
                "browserPath": BROWSER_PATH,
                "config": {},
                "firefox_user_prefs": {},
            },
            {
                "formatVersion": FORMAT_VERSION,
                "browserPath": BROWSER_PATH,
                "config": BASE_CONFIG,
                "firefox_user_prefs": {"webgl.force-enabled": 1},
            },
            {
                "formatVersion": FORMAT_VERSION,
                "browserPath": BROWSER_PATH,
                "config": BASE_CONFIG,
                "firefox_user_prefs": {"privacy.resistFingerprinting": True},
            },
            {
                "formatVersion": FORMAT_VERSION,
                "browserPath": BROWSER_PATH,
                "config": {**BASE_CONFIG, "humanize": True},
                "firefox_user_prefs": {},
            },
            {
                "formatVersion": FORMAT_VERSION,
                "browserPath": BROWSER_PATH,
                "config": {**BASE_CONFIG, "humanize:maxTime": 1.5},
                "firefox_user_prefs": {},
            },
        ]
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                profile_path = self.root / ("case-%d" % index)
                profile_path.mkdir(mode=0o700)
                write_json_file(profile_path / IDENTITY_FILENAME, payload)
                profile = PersistentProfile(str(profile_path))
                profile.acquire()
                try:
                    with self.assertRaises(PersistentProfileError):
                        profile.launch_kwargs(BROWSER_PATH)
                finally:
                    profile.release()

    def test_wide_permission_identity_rejected(self):
        profile_path = self.root / "profile"
        profile_path.mkdir(mode=0o700)
        payload = {
            "formatVersion": FORMAT_VERSION,
            "browserPath": BROWSER_PATH,
            "config": BASE_CONFIG,
            "firefox_user_prefs": {},
        }
        write_json_file(profile_path / IDENTITY_FILENAME, payload, mode=0o644)
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        try:
            with self.assertRaises(PersistentProfileError):
                profile.launch_kwargs(BROWSER_PATH)
        finally:
            profile.release()

    def test_symlink_identity_rejected(self):
        profile_path = self.root / "profile"
        profile_path.mkdir(mode=0o700)
        payload = {
            "formatVersion": FORMAT_VERSION,
            "browserPath": BROWSER_PATH,
            "config": BASE_CONFIG,
            "firefox_user_prefs": {},
        }
        target = self.root / "elsewhere.json"
        write_json_file(target, payload)
        (profile_path / IDENTITY_FILENAME).symlink_to(target)
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        try:
            with self.assertRaises(PersistentProfileError):
                profile.launch_kwargs(BROWSER_PATH)
        finally:
            profile.release()

    def test_oversized_identity_rejected(self):
        profile_path = self.root / "profile"
        profile_path.mkdir(mode=0o700)
        payload = {
            "formatVersion": FORMAT_VERSION,
            "browserPath": BROWSER_PATH,
            "config": {"pad": "x" * (MAX_IDENTITY_BYTES + 64)},
            "firefox_user_prefs": {},
        }
        write_json_file(profile_path / IDENTITY_FILENAME, payload)
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        try:
            with self.assertRaises(PersistentProfileError):
                profile.launch_kwargs(BROWSER_PATH)
        finally:
            profile.release()

    def test_corrupt_or_gapped_config_chunks_rejected(self):
        profile_path = self.root / "profile"
        profile = PersistentProfile(str(profile_path))
        profile.acquire()
        try:
            broken = {"env": {"CAMOU_CONFIG_1": "{broken"}, "firefox_user_prefs": {}}
            with self.assertRaises(PersistentProfileError):
                profile.save_identity(broken, BROWSER_PATH)
            gapped = {
                "env": {"CAMOU_CONFIG_1": '{"a": 1}', "CAMOU_CONFIG_3": "{}"},
                "firefox_user_prefs": {},
            }
            with self.assertRaises(PersistentProfileError):
                profile.save_identity(gapped, BROWSER_PATH)
            missing = {"env": {"PATH": "/usr/bin"}, "firefox_user_prefs": {}}
            with self.assertRaises(PersistentProfileError):
                profile.save_identity(missing, BROWSER_PATH)
            self.assertFalse((profile_path / IDENTITY_FILENAME).exists())
        finally:
            profile.release()


if __name__ == "__main__":
    unittest.main()
