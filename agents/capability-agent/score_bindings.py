"""Map Score provisioners (.score-k8s/*.provisioners.yaml) onto the Terraform variables they set."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

PARAM_REF = re.compile(r"""\.Params\.(\w+)|index\s+\.Params\s+"(\w+)\"""")
INIT_REF = re.compile(r"\.Init\.(\w+)")
STATE_REF = re.compile(r"\.State\.(\w+)")
DEFAULT = re.compile(r'default\s+"([^"]*)"')
SCORE_GENERATED = {".Guid": "resource guid", ".Uid": "resource uid", ".SourceWorkload": "workload name"}
SECRET_OUTPUT = re.compile(r'encodeSecretRef\s+\(printf\s+"([\w-]+)-%s"\s+\.Guid\)\s+"(\w+)"')


def _keyed_lines(template: str | None) -> dict[str, str]:
    lines: dict[str, str] = {}
    for line in (template or "").splitlines():
        if match := re.match(r"^\s*(\w+):\s*(.*)$", line):
            lines[match.group(1)] = match.group(2)
    return lines


def _block(text: str, key: str) -> str:
    """Return the YAML list under `key:` in a Go-templated manifest that cannot be parsed as YAML."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^\s*{key}:\s*$", line):
            indent = len(line) - len(line.lstrip())
            body = []
            for nxt in lines[index + 1:]:
                stripped = nxt.strip()
                current = len(nxt) - len(nxt.lstrip())
                if stripped and current <= indent and not stripped.startswith(("-", "{{")):
                    break
                body.append(nxt)
            return "\n".join(body)
    return ""


def _trace(value: str, init: dict[str, str], state: dict[str, str], depth: int = 0) -> dict[str, Any]:
    if match := PARAM_REF.search(value):
        default = DEFAULT.search(value)
        return {"set_by": "developer", "score_param": match.group(1) or match.group(2),
                "default": default.group(1) if default else None}
    if depth < 4 and (match := INIT_REF.search(value)) and match.group(1) in init:
        return _trace(init[match.group(1)], init, state, depth + 1)
    if depth < 4 and (match := STATE_REF.search(value)) and match.group(1) in state:
        return _trace(state[match.group(1)], init, state, depth + 1)
    if "randAlphaNum" in value:
        return {"set_by": "generated", "detail": "random value stored in Score state"}
    for token, label in SCORE_GENERATED.items():
        if token in value:
            return {"set_by": "score", "detail": label}
    if "{{" not in value:
        return {"set_by": "platform", "value": value.strip().strip("'\"")}
    return {"set_by": "template", "expression": value.strip()}


def _output_source(value: str, init: dict[str, str], state: dict[str, str]) -> dict[str, Any]:
    if match := SECRET_OUTPUT.search(value):
        prefix, key = match.groups()
        if prefix == "tf-output":
            return {"source": "terraform_output", "terraform_output": key}
        return {"source": "kubernetes_secret", "secret_key": key}
    traced = _trace(value, init, state)
    if traced["set_by"] == "platform":
        return {"source": "literal", "value": traced["value"]}
    return {"source": "score", **traced}


def _provisioner(item: dict[str, Any], file: str) -> dict[str, Any] | None:
    manifests = item.get("manifests") or ""
    if "kind: Terraform" not in manifests:
        return None
    init, state = _keyed_lines(item.get("init")), _keyed_lines(item.get("state"))

    tf_vars = []
    for name, value in re.findall(r"-\s*name:\s*(\S+)\s*\n\s*value:\s*(.*)", _block(manifests, "vars")):
        tf_vars.append({"terraform_variable": name, **_trace(value, init, state)})
    varsfrom = _block(manifests, "varsFrom")
    secret = re.search(r"name:\s*(.+?)\s*$", varsfrom, re.M)
    for key in re.findall(r"^\s*-\s*(\w+)\s*$", _block(varsfrom, "varsKeys"), re.M):
        tf_vars.append({"terraform_variable": key, "set_by": "secret",
                        "detail": f"Kubernetes Secret {secret.group(1) if secret else '?'}"})

    source = re.search(r"sourceRef:\s*\n(?:\s*\w+:.*\n)*?\s*name:\s*(\S+)", manifests)
    path = re.search(r"^\s*path:\s*(\S+)\s*$", manifests, re.M)
    approve = re.search(r"^\s*approvePlan:\s*(.*)$", manifests, re.M)
    return {
        "file": file,
        "uri": item.get("uri"),
        "score_type": item.get("type"),
        "score_class": item.get("class"),
        "description": item.get("description"),
        "terraform_path": path.group(1).removeprefix("./") if path else None,
        "flux_source": source.group(1) if source else None,
        "approve_plan": approve.group(1).strip() if approve else None,
        "destroy_on_deletion": "destroyResourcesOnDeletion: true" in manifests,
        "supported_params": item.get("supported_params"),
        "vars": tf_vars,
        "outputs": [{"name": key, **_output_source(value, init, state)}
                    for key, value in _keyed_lines(item.get("outputs")).items()],
    }


def collect(root: Path) -> list[dict[str, Any]]:
    bindings = []
    for path in sorted((root / ".score-k8s").glob("*.provisioners.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and (binding := _provisioner(item, path.relative_to(root).as_posix())):
                bindings.append(binding)
    return bindings
