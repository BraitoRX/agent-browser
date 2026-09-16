from __future__ import annotations

import os
from typing import Any, Dict, List

from input_context import BackendError, CODE_INVALID, CODE_UNSUPPORTED

from gestures._common import bounded, resolved_target

NAME = "upload"
DESCRIPTION = (
    "Set one or more files on an <input type=file> element via Playwright "
    "set_input_files, bypassing native file-picker dialogs. Paths resolve inside "
    "the bubble container when running in os-native mode."
)

MAX_PATH_LEN = 4096
MAX_FILES = 20


def _validate_paths(value: Any) -> List[Dict[str, Any]]:
    raw_list: List[Any]
    if isinstance(value, list):
        raw_list = value
    else:
        raw_list = [value]
    if not raw_list or len(raw_list) > MAX_FILES:
        raise BackendError(
            CODE_INVALID,
            f"files accepts 1 to {MAX_FILES} entries",
        )
    resolved: List[Dict[str, Any]] = []
    for entry in raw_list:
        if isinstance(entry, dict):
            path = entry.get("path")
            payload = entry.get("payload")
            mime = entry.get("mimeType")
            name = entry.get("name")
        else:
            path = entry
            payload = None
            mime = None
            name = None
        if payload is not None:
            if path is not None:
                raise BackendError(
                    CODE_INVALID, "provide either 'path' or 'payload', not both"
                )
            if not isinstance(payload, str) or not payload:
                raise BackendError(
                    CODE_INVALID, "'payload' must be a non-empty string"
                )
            resolved.append({
                "payload": payload,
                "mimeType": mime if isinstance(mime, str) else None,
                "name": name if isinstance(name, str) and name else "upload.bin",
            })
        elif isinstance(path, str) and path:
            if len(path) > MAX_PATH_LEN:
                raise BackendError(CODE_INVALID, "path exceeds maximum length")
            if not os.path.isabs(path):
                raise BackendError(
                    CODE_INVALID, f"path must be absolute: {path!r}"
                )
            resolved.append({"path": path})
        else:
            raise BackendError(
                CODE_INVALID, "each file entry needs an absolute 'path' or a 'payload'"
            )
    return resolved


def _mime_for_path(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    table = {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
        ".csv": "text/csv", ".json": "application/json", ".xml": "application/xml",
        ".html": "text/html", ".zip": "application/zip",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".m4a": "audio/mp4", ".flac": "audio/flac",
    }
    return table.get(ext, "application/octet-stream")


EXAMPLES = [
    {"target": "input[type=file]", "files": ["/tmp/v.mp4"]},
    {"target": {"selector": "@e12"}, "files": [{"path": "/tmp/photo.png"}]},
    {
        "target": "#dropzone-input",
        "files": [{"payload": "hello", "name": "note.txt", "mimeType": "text/plain"}],
    },
]

SCHEMA = {
    "type": "object",
    "description": (
        "Attach files directly to a file input element without opening a native "
        "file-picker dialog. Path entries resolve in the worker's filesystem "
        "(inside the bubble container for os-native sessions)."
    ),
    "properties": {
        "target": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 4096}},
                    "required": ["selector"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "coordinates": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "captureId": {"type": "string", "minLength": 1, "maxLength": 128},
                            },
                            "required": ["x", "y", "captureId"],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["coordinates"],
                    "additionalProperties": False,
                },
            ]
        },
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "payload": {"type": "string", "maxLength": 4194304},
                    "name": {"type": "string", "minLength": 1, "maxLength": 255},
                    "mimeType": {"type": "string", "maxLength": 255},
                },
                "additionalProperties": False,
            },
        },
        "timeoutMs": {"type": "integer", "minimum": 100, "maximum": 22000, "default": 5000},
    },
    "required": ["target", "files"],
    "additionalProperties": False,
}


async def run(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    spec = resolved_target(ctx, params)
    if spec.kind == "selector":
        if spec.ref is not None and spec.frame_prefix is not None:
            raise BackendError(
                CODE_UNSUPPORTED,
                "upload supports main-frame targets only; iframe file inputs are not reachable",
            )
        _scope, locator = await ctx.action_locator(spec)
        label = f"selector {('@' + spec.ref) if spec.ref else spec.selector!r}"
    else:
        raise BackendError(
            CODE_UNSUPPORTED,
            "upload requires a file-input element target, not coordinates",
        )

    files = _validate_paths(params.get("files"))
    timeout_ms = params.get("timeoutMs")
    timeout_ms = timeout_ms if isinstance(timeout_ms, int) else 5000

    ctx.set_stage("resolve")
    count = await bounded(ctx, locator.count(), f"resolve {label}", timeout_ms)
    if count != 1:
        raise BackendError(
            CODE_UNSUPPORTED if count == 0 else CODE_INVALID,
            f"{label} matched {count} elements; upload needs exactly one <input type=file>",
        )
    input_kind = await bounded(
        ctx, locator.evaluate("el => el.type || el.tagName"), f"inspect {label}", timeout_ms
    )
    if input_kind != "file":
        raise BackendError(
            CODE_UNSUPPORTED,
            f"{label} is not a file input (got {input_kind!r})",
        )

    ctx.set_stage("dispatch")
    MAX_INLINE_BUFFER = 48 * 1024 * 1024
    playwright_files: List[Dict[str, Any]] = []
    for entry in files:
        if "path" in entry:
            if not os.path.isfile(entry["path"]):
                raise BackendError(
                    CODE_INVALID, f"file not found: {entry['path']}"
                )
            size = os.path.getsize(entry["path"])
            if size <= MAX_INLINE_BUFFER:
                item: Dict[str, Any] = {
                    "name": os.path.basename(entry["path"]),
                    "mimeType": _mime_for_path(entry["path"]),
                }
                try:
                    with open(entry["path"], "rb") as handle:
                        item["buffer"] = handle.read()
                except OSError as exc:
                    raise BackendError(CODE_INVALID, f"cannot read {entry['path']}: {exc}") from exc
                playwright_files.append(item)
            else:
                playwright_files.append(entry["path"])
        else:
            playwright_files.append({
                "name": entry["name"],
                "mimeType": entry.get("mimeType") or "application/octet-stream",
                "buffer": entry["payload"].encode("utf-8"),
            })

    try:
        await bounded(
            ctx,
            locator.set_input_files(playwright_files, timeout=timeout_ms),
            "set input files",
            timeout_ms + 1000,
        )
    except TypeError:
        await bounded(ctx, locator.set_input_files(playwright_files), "set input files", timeout_ms)

    ctx.note_input_dispatched()
    return {
        "action": "upload",
        "target": label,
        "files": [
            entry.get("name") or os.path.basename(entry.get("path", ""))
            for entry in files
        ],
        "count": len(files),
        "diagnostics": ctx.diagnostics(),
    }