#!/usr/bin/env python3
"""Re-apply the Camoufox input-dispatch guards to the installed browser.

A runtime reinstall or `camoufox fetch` replaces the browser asset and drops the
guards, which reintroduces the click-hang/poisoning failure. Run this after any
such reinstall. It is idempotent: if the installed archive is already guarded it
does nothing.

The guarded Juggler sources are read from the Camoufox fork checkout. Override
with CAMOUFOX_GUARD_SOURCE, and the runtime with AGENT_BROWSER_CAMOUFOX_RUNTIME.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

GUARD_SENTINEL = "ensureEventWithin"
SWAP = {
    "Helper.js": "chrome/juggler/content/Helper.js",
    "protocol/PageHandler.js": "chrome/juggler/content/protocol/PageHandler.js",
    "TargetRegistry.js": "chrome/juggler/content/TargetRegistry.js",
    "input/MouseDispatch.js": "chrome/juggler/content/input/MouseDispatch.js",
}


def default_runtime_dir() -> Path:
    env = os.environ.get("AGENT_BROWSER_CAMOUFOX_RUNTIME")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "agent-browser" / "camoufox-v1"


def default_source_dir() -> Path:
    env = os.environ.get("CAMOUFOX_GUARD_SOURCE")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "camoufox" / "additions" / "juggler"


def resolve_omni(runtime_dir: Path) -> Path:
    record = json.loads((runtime_dir / "runtime.json").read_text("utf-8"))
    executable = Path(record["browser"]["executablePath"])
    if executable.parent.name == "MacOS" and executable.parent.parent.name == "Contents":
        resources = executable.parent.parent / "Resources"
    else:
        resources = executable.parent
    omni = resources / "omni.ja"
    if not omni.is_file():
        raise SystemExit(f"installed omni.ja not found at {omni}")
    return omni


def is_guarded(omni: Path) -> bool:
    with zipfile.ZipFile(omni) as archive:
        try:
            helper = archive.read("chrome/juggler/content/Helper.js").decode("utf-8")
        except KeyError:
            return False
    return GUARD_SENTINEL in helper


def build_patched(omni: Path, source: Path, destination: Path) -> int:
    sources = {name: source / name for name in SWAP}
    for name, path in sources.items():
        if not path.is_file():
            raise SystemExit(f"guarded source missing: {path}")
        text = path.read_text("utf-8")
        if name == "Helper.js" and GUARD_SENTINEL not in text:
            raise SystemExit(f"{path} does not contain the guard")

    template = None
    written = 0
    with zipfile.ZipFile(omni) as src, zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as out:
        existing = {info.filename: info for info in src.infolist()}
        template = existing.get("chrome/juggler/content/Helper.js")
        for info in src.infolist():
            if info.filename in SWAP.values():
                continue
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.external_attr = info.external_attr
            clone.create_system = info.create_system
            clone.compress_type = zipfile.ZIP_STORED
            out.writestr(clone, src.read(info.filename))
        for name, target in SWAP.items():
            clone = zipfile.ZipInfo(target, date_time=template.date_time)
            clone.external_attr = template.external_attr
            clone.create_system = template.create_system
            clone.compress_type = zipfile.ZIP_STORED
            out.writestr(clone, sources[name].read_bytes())
            written += 1
    return written


def verify_patched(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise SystemExit("patched archive failed its integrity check")
        helper = archive.read("chrome/juggler/content/Helper.js").decode("utf-8")
        handler = archive.read("chrome/juggler/content/protocol/PageHandler.js").decode("utf-8")
        if GUARD_SENTINEL not in helper:
            raise SystemExit("Helper.js guard missing after patch")
        if "MouseDispatch" not in handler:
            raise SystemExit("PageHandler.js MouseDispatch routing missing after patch")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default=None)
    parser.add_argument("--source", default=None)
    parser.add_argument("--omni", help="override the omni.ja path (for testing on a copy)")
    args = parser.parse_args(argv)

    runtime_dir = Path(args.runtime_dir or default_runtime_dir()).expanduser().resolve()
    source = Path(args.source or default_source_dir()).expanduser().resolve()
    omni = Path(args.omni).expanduser().resolve() if args.omni else resolve_omni(runtime_dir)

    if is_guarded(omni):
        print(json.dumps({"status": "already-guarded", "omni": str(omni)}))
        return 0

    backup = omni.with_name(omni.name + ".guards-backup")
    if not backup.exists():
        shutil.copy2(omni, backup)

    staged = omni.with_name(omni.name + ".guards-new")
    if staged.exists():
        staged.unlink()
    written = build_patched(omni, source, staged)
    verify_patched(staged)

    shutil.copy2(staged, omni.with_name(omni.name + ".guards-install"))
    os.replace(omni.with_name(omni.name + ".guards-install"), omni)
    staged.unlink()

    print(json.dumps({
        "status": "patched",
        "omni": str(omni),
        "backup": str(backup),
        "files": written,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
