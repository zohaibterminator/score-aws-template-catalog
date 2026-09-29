"""Map Score provisioners (.score-k8s/*.provisioners.yaml) onto the Terraform variables they set."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

PARAM_REF = re.compile(r"""\.Params\.(\w+)|index\s+\.Params\s+"(\w+)\"""")
INIT_REF = re.compile(r"\.Init\.(\w+)")
STATE_REF = re.compile(r"\.State\.(\w+)")
# `| default <value>` in a template: a quoted string, a number, a boolean, `list` or `(list "a" "b")`.
DEFAULT = re.compile(r'\|\s*default\s+(?:"(?P<str>[^"]*)"|(?P<num>-?\d+(?:\.\d+)?)\b|(?P<bool>true|false)\b|\(list(?P<list>(?:\s+"[^"]*")*)\s*\)|(?P<empty>list)\b)')

# `{{ if hasKey .Params "x" }}{{ .Params.x }}{{ else }}<value>{{ end }}`: how a template defaults a boolean to
# true, since `| default true` would also replace an explicit false.
HASKEY_DEFAULT = re.compile(r'hasKey\s+\.Params\s+"\w+".*\{\{-?\s*else\s*-?\}\}\s*(?P<value>true|false|-?\d+(?:\.\d+)?|"[^"]*")\s*\{\{-?\s*end')


def _default(value: str) -> Any:
    """The default a template applies to a param, or None when it has none (the param is required)."""
    if keyed := HASKEY_DEFAULT.search(value):
        return _default(f'| default {keyed.group("value")}')
    match = DEFAULT.search(value)
    if not match:
        return None
    if match.group("bool") is not None:
        return match.group("bool") == "true"
    if match.group("str") is not None:
        return match.group("str")
    if match.group("num") is not None:
        number = float(match.group("num"))
        return int(number) if number.is_integer() else number
    if match.group("list") is not None:
        return re.findall(r'"([^"]*)"', match.group("list"))
    return []
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
        return {"set_by": "developer", "score_param": match.group(1) or match.group(2), "default": _default(value)}
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


# Params the platform sets (score-api), never a caller: not advertised even when a template reads them.
PLATFORM_PARAMS = {"runner_vault_role"}


def _primary(manifests: str) -> str:
    """The first Terraform document of a manifests template: the CR whose module the provisioner is bound to.

    A provisioner may render more than one CR (the eks provisioner bundles a network-access CR after its own); the
    later ones must not lend their path, vars or varsFrom to the primary module."""
    docs, current = [], []
    for line in manifests.splitlines():
        if re.match(r"^\s*-\s*apiVersion:", line) and current:
            docs.append("\n".join(current))
            current = []
        current.append(line)
    docs.append("\n".join(current))
    return next((doc for doc in docs if "kind: Terraform" in doc), manifests)


def _provisioner(item: dict[str, Any], file: str) -> dict[str, Any] | None:
    manifests = item.get("manifests") or ""
    if "kind: Terraform" not in manifests:
        return None
    manifests = _primary(manifests)
    init, state = _keyed_lines(item.get("init")), _keyed_lines(item.get("state"))

    tf_vars = []
    for name, value in re.findall(r"-\s*name:\s*(\S+)\s*\n\s*value:\s*(.*)", _block(manifests, "vars")):
        tf_vars.append({"terraform_variable": name, **_trace(value, init, state)})
    varsfrom = _block(manifests, "varsFrom")
    secret = re.search(r"name:\s*(.+?)\s*$", varsfrom, re.M)
    for key in re.findall(r"^\s*-\s*(\w+)\s*$", _block(varsfrom, "varsKeys"), re.M):
        tf_vars.append({"terraform_variable": key, "set_by": "secret",
                        "detail": f"Kubernetes Secret {secret.group(1) if secret else '?'}"})

    # Params the template reads that no variable of the primary module takes, e.g. the eks provisioner's
    # enable_network_access and bastion_access_mode, which drive its bundled network-access CR.
    traced = {v.get("score_param") for v in tf_vars if v.get("set_by") == "developer"}
    extra_params: list[dict[str, Any]] = []
    for value in init.values():
        for match in PARAM_REF.finditer(value):
            name = match.group(1) or match.group(2)
            if name in traced or name in PLATFORM_PARAMS or any(p["name"] == name for p in extra_params):
                continue
            extra_params.append({"name": name, "default": _default(value)})

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
        "extra_params": extra_params,
        "vars": tf_vars,
        "outputs": [{"name": key, **_output_source(value, init, state)}
                    for key, value in _keyed_lines(item.get("outputs")).items()],
    }


def collect(root: Path) -> list[dict[str, Any]]:
    """Provisioners of every Score project in the repo: the root .score-k8s/ and project folders such as eks/."""
    bindings = []
    paths = [p for p in root.glob("**/.score-k8s/*.provisioners.yaml") if ".git" not in p.relative_to(root).parts]
    for path in sorted(paths):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and (binding := _provisioner(item, path.relative_to(root).as_posix())):
                bindings.append(binding)
    return bindings
