"""Assemble capabilities from parsed facts, and merge LLM descriptions only where they match those facts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import score_bindings
import terraform_facts
from git_source import Checkout


def _parameter(variable: dict[str, Any], module: dict[str, Any], bindings: list[dict[str, Any]] | None) -> dict[str, Any]:
    name = variable["name"]
    drives = [
        {"resource": resource["address"], "attribute": attr["attribute"], "kind": attr["kind"]}
        for resource in module["resources"]
        for attr in resource["attributes"]
        if name in attr.get("variables", [])
    ]
    parameter = {
        "terraform_variable": name,
        "type": variable["type"],
        "required": variable["required"],
        "terraform_default": variable["default"],
        "sensitive": variable["sensitive"],
        "description": variable["description"],
        "validations": variable["validations"],
        "drives": drives,
    }
    if bindings is None:
        return parameter
    sources = []
    for binding in bindings:
        match = next((v for v in binding["vars"] if v["terraform_variable"] == name), None)
        if match is None:
            match = {"set_by": "terraform_default" if not variable["required"] else "missing"}
        sources.append({"provisioner": binding["uri"], **{k: v for k, v in match.items() if k != "terraform_variable"}})
    parameter["score_sources"] = sources
    return parameter


def _score_example(binding: dict[str, Any]) -> dict[str, Any]:
    params = {
        var["score_param"]: var.get("default") or f"<{var['score_param']}>"
        for var in binding["vars"] if var["set_by"] == "developer"
    }
    return {"resources": {"<name>": {"type": binding["score_type"], "class": binding["score_class"], "params": params}}}


def _issues(module: dict[str, Any], bindings: list[dict[str, Any]]) -> list[str]:
    declared = {variable["name"] for variable in module["variables"]}
    required = {variable["name"] for variable in module["variables"] if variable["required"]}
    issues = []
    for binding in bindings:
        provided = {var["terraform_variable"] for var in binding["vars"]}
        for name in sorted(provided - declared):
            issues.append(f"{binding['file']} sets `{name}`, which {module['path']} does not declare")
        for name in sorted(required - provided):
            issues.append(f"{binding['file']} does not set required variable `{name}`")
    return issues


def _capability(module: dict[str, Any], bindings: list[dict[str, Any]] | None) -> dict[str, Any]:
    matched = [b for b in bindings or [] if b["terraform_path"] == module["path"]]
    managed = [r for r in module["resources"] if r["kind"] == "resource"]
    capability = {
        "id": module["path"],
        "summary": None,
        "aws_service": None,
        "terraform": {
            "module_path": module["path"],
            "required_terraform_version": module["required_terraform_version"],
            "required_providers": module["required_providers"],
            "resources": [r["address"] for r in managed],
            "data_sources": [r["address"] for r in module["resources"] if r["kind"] == "data"],
            "child_modules": module["child_modules"],
        },
        "parameters": [_parameter(v, module, matched if bindings is not None else None) for v in module["variables"]],
        "fixed_settings": [
            {"resource": r["address"], "attribute": a["attribute"], "value": a["value"]}
            for r in managed for a in r["attributes"] if a["kind"] == "fixed"
        ],
        "conditional_behaviours": [
            {"resource": r["address"], "attribute": a["attribute"], "expression": a["expression"],
             "variables": a["variables"], "description": None}
            for r in managed for a in r["attributes"] if a["kind"] == "conditional"
        ],
        "locals": module["locals"],
        "resource_attributes": [
            {"address": r["address"], "attributes": r["attributes"]}
            for r in module["resources"] if r["kind"] in ("resource", "provider")
        ],
        "outputs": module["outputs"],
        "constraints": [],
    }
    if bindings is not None:
        capability["score"] = {
            "entrypoints": [
                {key: b[key] for key in ("score_type", "score_class", "uri", "file", "flux_source", "approve_plan", "destroy_on_deletion")}
                | {"outputs": b["outputs"], "example": _score_example(b)}
                for b in matched
            ],
            "binding_issues": _issues(module, matched),
        }
    return capability


def build(modules: Checkout, score: Checkout | None) -> dict[str, Any]:
    facts = terraform_facts.collect(modules.path)
    bindings = score_bindings.collect(score.path) if score else None
    module_paths = {module["path"] for module in facts}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {"terraform_modules": modules.describe()} | ({"score_workloads": score.describe()} if score else {}),
        "extraction": {"method": "deterministic", "warnings": []},
        "capabilities": [_capability(module, bindings) for module in facts],
    }
    if bindings is not None:
        report["unmatched_score_provisioners"] = [
            {"file": b["file"], "uri": b["uri"], "terraform_path": b["terraform_path"], "flux_source": b["flux_source"]}
            for b in bindings if b["terraform_path"] not in module_paths
        ]
    return report


def merge_llm(report: dict[str, Any], draft: dict[str, Any], roots: dict[str, Path], model: str) -> dict[str, Any]:
    """Apply LLM prose onto the factual report; anything not grounded in the facts is dropped with a warning."""
    warnings = report["extraction"]["warnings"]
    by_id = {capability["id"]: capability for capability in report["capabilities"]}
    for item in draft.get("capabilities", []):
        capability = by_id.get(item["id"])
        if capability is None:
            warnings.append(f"Dropped capability `{item['id']}`: no such Terraform module")
            continue
        capability["summary"] = item["summary"]
        capability["aws_service"] = item["aws_service"]
        parameters = {p["terraform_variable"]: p for p in capability["parameters"]}
        for note in item.get("parameters", []):
            if note["terraform_variable"] in parameters:
                parameters[note["terraform_variable"]]["effect"] = note["effect"]
            else:
                warnings.append(f"Dropped effect for unknown variable `{note['terraform_variable']}` in `{item['id']}`")
        behaviours = {(b["resource"], b["attribute"]): b for b in capability["conditional_behaviours"]}
        for note in item.get("behaviours", []):
            key = (note["resource"], note["attribute"])
            if key in behaviours:
                behaviours[key]["description"] = note["description"]
            else:
                warnings.append(f"Dropped behaviour for unknown attribute `{key[0]}.{key[1]}` in `{item['id']}`")
        for constraint in item.get("constraints", []):
            repo, _, rel = constraint["evidence"].partition(":")
            root = roots.get(repo)
            evidence = (root / rel).resolve() if root and rel else None
            if evidence and evidence.is_relative_to(root.resolve()) and evidence.is_file():
                capability["constraints"].append(constraint)
            else:
                warnings.append(f"Dropped constraint without valid evidence in `{item['id']}`: {constraint['text']}")
    missing = [c["id"] for c in report["capabilities"] if c["summary"] is None]
    warnings.extend(f"LLM did not describe capability `{cid}`" for cid in missing)
    report["extraction"]["method"] = "llm"
    report["extraction"]["model"] = model
    return report
