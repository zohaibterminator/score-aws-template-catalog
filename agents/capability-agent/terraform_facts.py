"""Deterministic facts about every Terraform module in a checkout, parsed with python-hcl2."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import hcl2

VAR_REF = re.compile(r"\bvar\.([A-Za-z_][\w-]*)")
LOCAL_REF = re.compile(r"\blocal\.([A-Za-z_][\w-]*)")
CONDITIONAL = re.compile(r"\?|==|!=|<=|>=|\bcontains\(|\bcan\(|\btry\(")
SKIP_DIRS = {".git", ".terraform"}


def _unwrap(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return value[2:-1]
    return value


def _is_expression(value: Any) -> bool:
    return isinstance(value, str) and "${" in value


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return hcl2.load(handle)


def _named(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for block in blocks:
        merged.update(block)
    return merged


def _flatten(attrs: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in attrs.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}."))
        elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            for index, item in enumerate(value):
                flat.update(_flatten(item, f"{name}[{index}]."))
        else:
            flat[name] = value
    return flat


def _variables_used(expression: str, locals_: dict[str, Any], seen: frozenset[str] = frozenset()) -> set[str]:
    found = set(VAR_REF.findall(expression))
    for local in LOCAL_REF.findall(expression):
        if local in locals_ and local not in seen:
            found |= _variables_used(str(locals_[local]), locals_, seen | {local})
    return found


def _attribute(name: str, value: Any, locals_: dict[str, Any]) -> dict[str, Any]:
    if not _is_expression(value):
        return {"attribute": name, "kind": "fixed", "value": value}
    expression = _unwrap(value)
    variables = sorted(_variables_used(expression, locals_))
    if not variables:
        kind = "computed"
    elif CONDITIONAL.search(expression):
        kind = "conditional"
    else:
        kind = "variable"
    return {"attribute": name, "kind": kind, "expression": expression, "variables": variables}


def module_dirs(root: Path) -> list[Path]:
    dirs = {
        path.parent for path in root.rglob("*.tf")
        if not SKIP_DIRS.intersection(path.relative_to(root).parts)
    }
    return sorted(dirs)


def parse_module(root: Path, module_dir: Path) -> dict[str, Any]:
    docs = [_load(path) for path in sorted(module_dir.glob("*.tf"))]

    def blocks(kind: str) -> list[dict[str, Any]]:
        return [block for doc in docs for block in doc.get(kind, [])]

    locals_ = {key: value for block in blocks("locals") for key, value in block.items()}
    settings = _named(blocks("terraform"))
    required = settings.get("required_providers", [])
    providers_required = _named(required if isinstance(required, list) else [required])

    variables = []
    for name, spec in _named(blocks("variable")).items():
        variables.append({
            "name": name,
            "type": _unwrap(spec.get("type", "any")),
            "required": "default" not in spec,
            "default": spec.get("default"),
            "sensitive": bool(spec.get("sensitive", False)),
            "description": spec.get("description"),
            "validations": [_unwrap(rule.get("condition")) for rule in spec.get("validation", [])],
        })

    resources = []
    for block in blocks("resource") + [{f"data.{k}": v for k, v in b.items()} for b in blocks("data")]:
        for rtype, instances in block.items():
            for rname, attrs in instances.items():
                attributes = [_attribute(k, v, locals_) for k, v in _flatten(attrs).items()]
                resources.append({
                    "address": f"{rtype}.{rname}",
                    "type": rtype.removeprefix("data."),
                    "kind": "data" if rtype.startswith("data.") else "resource",
                    "attributes": attributes,
                })

    for block in blocks("provider"):
        for pname, attrs in block.items():
            resources.append({
                "address": f"provider.{pname}",
                "type": pname,
                "kind": "provider",
                "attributes": [_attribute(k, v, locals_) for k, v in _flatten(attrs).items()],
            })

    outputs = []
    for name, spec in _named(blocks("output")).items():
        outputs.append({
            "name": name,
            "value": _unwrap(spec.get("value")),
            "sensitive": bool(spec.get("sensitive", False)),
            "description": spec.get("description"),
        })

    child_modules = [
        {"name": name, "source": spec.get("source"), "version": spec.get("version")}
        for name, spec in _named(blocks("module")).items()
    ]

    return {
        "path": module_dir.relative_to(root).as_posix() or ".",
        "required_terraform_version": settings.get("required_version"),
        "required_providers": {
            name: {"source": spec.get("source"), "version": spec.get("version")}
            for name, spec in providers_required.items() if isinstance(spec, dict)
        },
        "variables": variables,
        "locals": locals_,
        "resources": resources,
        "outputs": outputs,
        "child_modules": child_modules,
    }


def collect(root: Path) -> list[dict[str, Any]]:
    return [parse_module(root, module_dir) for module_dir in module_dirs(root)]
