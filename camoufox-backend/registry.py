from __future__ import annotations

import hashlib
import importlib.util
import inspect
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

SUPPORTED_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items",
    "oneOf", "enum", "minimum", "maximum", "minLength", "maxLength",
    "minItems", "maxItems", "description", "default",
}
SUPPORTED_TYPES = {"object", "array", "string", "boolean", "number", "integer"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class RegistryError(Exception):
    pass


@dataclass
class GestureSpec:
    name: str
    description: str
    schema: Dict[str, Any]
    examples: List[Any]
    run: Callable[..., Any]
    module: str
    source: str


def _is_object_like(node: Dict[str, Any]) -> bool:
    node_type = node.get("type")
    if node_type == "object":
        return True
    if isinstance(node_type, list) and "object" in node_type:
        return True
    return "properties" in node


def validate_schema(schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise RegistryError(f"{path}: schema must be an object")
    unknown = set(schema.keys()) - SUPPORTED_KEYWORDS
    if unknown:
        raise RegistryError(
            f"{path}: unsupported schema keyword(s): {', '.join(sorted(unknown))}; "
            "the validator implements a bounded JSON Schema subset only"
        )
    node_type = schema.get("type")
    if node_type is not None:
        types = node_type if isinstance(node_type, list) else [node_type]
        for entry in types:
            if entry not in SUPPORTED_TYPES:
                raise RegistryError(f"{path}: unsupported type '{entry}'")
    if "oneOf" in schema:
        siblings = set(schema.keys()) - {"oneOf", "description", "default"}
        if siblings:
            raise RegistryError(f"{path}: oneOf cannot be combined with {', '.join(sorted(siblings))}")
        branches = schema["oneOf"]
        if not isinstance(branches, list) or not branches:
            raise RegistryError(f"{path}: oneOf must be a non-empty array of schemas")
        for index, branch in enumerate(branches):
            validate_schema(branch, f"{path}.oneOf[{index}]")
        return
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            raise RegistryError(f"{path}: properties must be an object")
        for key, child in properties.items():
            validate_schema(child, f"{path}.properties.{key}")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise RegistryError(f"{path}: required must be an array of strings")
        if len(set(required)) != len(required):
            raise RegistryError(f"{path}: required contains duplicates")
        properties = schema.get("properties", {})
        for key in required:
            if key not in properties:
                raise RegistryError(f"{path}: required key '{key}' is not declared in properties")
    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if not isinstance(additional, (bool, dict)):
            raise RegistryError(f"{path}: additionalProperties must be false or a schema")
        if isinstance(additional, dict):
            validate_schema(additional, f"{path}.additionalProperties")
    if "items" in schema:
        if not isinstance(schema["items"], dict):
            raise RegistryError(f"{path}: items must be a schema object")
        validate_schema(schema["items"], f"{path}.items")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            raise RegistryError(f"{path}: enum must be a non-empty array")
    for keyword in ("minimum", "maximum"):
        if keyword in schema:
            value = schema[keyword]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise RegistryError(f"{path}: {keyword} must be a finite number")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        raise RegistryError(f"{path}: minimum is greater than maximum")
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        if keyword in schema:
            value = schema[keyword]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RegistryError(f"{path}: {keyword} must be a non-negative integer")
    for low, high in (("minLength", "maxLength"), ("minItems", "maxItems")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise RegistryError(f"{path}: {low} is greater than {high}")
    if _is_object_like(schema) and schema.get("additionalProperties") is not False:
        raise RegistryError(
            f"{path}: object schemas must set additionalProperties to false (strict objects)"
        )


def _check_type(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return (not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value))
    if expected == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and math.isfinite(value) and value.is_integer()
    return False


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if not (math.isfinite(left) and math.isfinite(right)):
            return False
        return float(left) == float(right)
    return type(left) is type(right) and left == right


def _validate(schema: Dict[str, Any], value: Any, path: str) -> List[str]:
    errors: List[str] = []
    if "oneOf" in schema:
        matches = 0
        for index, branch in enumerate(schema["oneOf"]):
            if not _validate(branch, value, f"{path}<{index}>"):
                matches += 1
        if matches != 1:
            errors.append(f"{path}: must match exactly one allowed shape")
        return errors
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_check_type(entry, value) for entry in types):
            errors.append(f"{path}: expected {' or '.join(types)}")
            return errors
    if "enum" in schema and not any(_json_equal(value, allowed) for allowed in schema["enum"]):
        errors.append(f"{path}: value is not one of the allowed values")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value is above the maximum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: string is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: string is longer than maxLength")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: array has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: array has more than maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(_validate(schema["items"], item, f"{path}[{index}]"))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required parameter is missing")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                errors.extend(_validate(properties[key], item, f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}.{key}: unexpected parameter")
            elif isinstance(additional, dict):
                errors.extend(_validate(additional, item, f"{path}.{key}"))
    return errors


def validate_params(schema: Dict[str, Any], params: Any) -> List[str]:
    return _validate(schema, params, "$")


def _load_module(path: Path, source: str) -> GestureSpec:
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    module_name = f"agent_browser_gesture_{path.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RegistryError(f"cannot load gesture module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise RegistryError(f"gesture module failed to import: {path}: {exc}") from exc
    name = getattr(module, "NAME", None)
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise RegistryError(
            f"gesture module {path} must export NAME matching ^[a-z][a-z0-9_]{{0,63}}$"
        )
    description = getattr(module, "DESCRIPTION", "")
    if not isinstance(description, str):
        raise RegistryError(f"gesture module {path}: DESCRIPTION must be a string")
    schema = getattr(module, "SCHEMA", None)
    if not isinstance(schema, dict):
        raise RegistryError(f"gesture module {path}: SCHEMA must be an object")
    validate_schema(schema, f"{path}:$")
    roots = schema.get("oneOf", [schema])
    if not all(root.get("type") == "object" and root.get("additionalProperties") is False for root in roots):
        raise RegistryError(
            f"gesture module {path}: parameter schema root must be a strict object or a oneOf of strict objects"
        )
    examples = getattr(module, "EXAMPLES", [])
    if not isinstance(examples, list):
        raise RegistryError(f"gesture module {path}: EXAMPLES must be an array")
    run = getattr(module, "run", None)
    if run is None or not callable(run) or not inspect.iscoroutinefunction(run):
        raise RegistryError(f"gesture module {path}: run must be an async def run(ctx, params)")
    return GestureSpec(
        name=name,
        description=description,
        schema=schema,
        examples=examples,
        run=run,
        module=str(path),
        source=source,
    )


def discover(directories: Sequence[Tuple[Any, str]]) -> Dict[str, GestureSpec]:
    specs: Dict[str, GestureSpec] = {}
    seen: set = set()
    for directory, source in directories:
        if directory is None:
            continue
        path = Path(directory).expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        if not path.is_dir():
            raise RegistryError(f"gesture directory does not exist: {path}")
        if str(path) not in sys.path:
            # Gesture helpers must not shadow installed packages, including Click with gestures/click.py.
            sys.path.append(str(path))
        for file_path in sorted(path.glob("*.py"), key=lambda item: item.name):
            if file_path.name.startswith("_"):
                continue
            spec = _load_module(file_path, source)
            existing = specs.get(spec.name)
            if existing is not None:
                if existing.source == "builtin" and spec.source != "builtin":
                    raise RegistryError(
                        f"external gesture '{spec.name}' from {file_path} shadows a built-in gesture"
                    )
                raise RegistryError(
                    f"duplicate gesture name '{spec.name}' from {file_path} "
                    f"(already registered by {existing.module})"
                )
            specs[spec.name] = spec
    return specs


def schema_summary(spec: GestureSpec) -> Dict[str, Any]:
    properties = spec.schema.get("properties", {})
    required = spec.schema.get("required", [])
    return {
        "name": spec.name,
        "description": spec.description,
        "source": spec.source,
        "params": sorted(properties.keys()),
        "required": sorted(required),
    }


def schema_description(spec: GestureSpec) -> Dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "source": spec.source,
        "module": spec.module,
        "schema": spec.schema,
        "examples": spec.examples,
    }
