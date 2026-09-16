from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from runtime import _sanitize_fingerprint_config


class FingerprintConfigSanitizerTests(unittest.TestCase):
    def test_strips_all_locale_spoof_keys(self) -> None:
        options = {
            "config": {
                "locale:language": "en",
                "locale:region": "US",
                "locale:script": "Latn",
                "locale:all": "en-US",
                "navigator.language": "en-US",
                "navigator.userAgent": "kept",
                "screen.width": 1920,
            }
        }
        _sanitize_fingerprint_config(options)
        config = options["config"]
        for key in ("locale:language", "locale:region", "locale:script", "locale:all", "navigator.language"):
            self.assertNotIn(key, config)
        self.assertEqual(config["navigator.userAgent"], "kept")

    def test_strips_locale_keys_from_env_config_chunks(self) -> None:
        config = {
            "locale:language": "en",
            "locale:region": "US",
            "navigator.userAgent": "kept",
        }
        encoded = json.dumps(config, separators=(",", ":"))
        options = {"env": {"CAMOU_CONFIG_1": encoded[:100], "CAMOU_CONFIG_2": encoded[100:]}}
        _sanitize_fingerprint_config(options)
        env = options["env"]
        chunks = sorted(k for k in env if k.startswith("CAMOU_CONFIG_"))
        raw = "".join(env[k] for k in chunks)
        decoded = json.loads(raw)
        self.assertNotIn("locale:language", decoded)
        self.assertNotIn("locale:region", decoded)
        self.assertEqual(decoded["navigator.userAgent"], "kept")

    def test_tolerates_missing_or_non_dict_config(self) -> None:
        _sanitize_fingerprint_config({})
        _sanitize_fingerprint_config({"config": None})
        _sanitize_fingerprint_config({"config": "not-a-dict"})

    def test_leaves_other_fingerprint_values_untouched(self) -> None:
        options = {
            "config": {
                "canvas:seed": 1234,
                "fonts": ["Arial"],
                "webGl:renderer": "ANGLE (NVIDIA)",
                "timezone": "America/New_York",
            }
        }
        before = dict(options["config"])
        _sanitize_fingerprint_config(options)
        self.assertEqual(options["config"], before)


if __name__ == "__main__":
    unittest.main()