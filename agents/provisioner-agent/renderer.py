#!/usr/bin/env python3
"""Render a tofu-controller manifest for the application-stack template."""
from __future__ import annotations

from pathlib import Path
import ipaddress
import json
import re
import sys
from typing import Any

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "templates" / "application-stack" / "contract.yaml"

SENSITIVE_KEYS = {"db_password", "cache_auth_token"}
COMPONENT_FLAGS = {
    "network": "create_vpc",
    "postgres": "enable_rds",
    "object-storage": "enable_s3",
    "cache": "enable_cache",
    "queue": "enable_sqs",
}
class ManifestDumper(yaml.SafeDumper):
    pass


def str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.nodes.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


ManifestDumper.add_representer(str, str_presenter)


def load_request(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("Request YAML must contain a mapping.")
    return data


def contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def resolved_request(request: dict[str, Any]) -> dict[str, Any]:
    result = dict(request)
    components = result.pop("components", None)
    if components is not None:
        if not isinstance(components, list) or any(not isinstance(item, str) for item in components) or len(components) != len(set(components)):
            raise ValueError("components must be a list without duplicates.")
        unknown = set(components) - set(COMPONENT_FLAGS)
        if unknown:
            raise ValueError(f"Unknown components: {', '.join(sorted(unknown))}")
        for component, flag in COMPONENT_FLAGS.items():
            enabled = component in components
            if flag in result and result[flag] != enabled:
                raise ValueError(f"{flag} conflicts with components.")
            result[flag] = enabled
    return result


def validate_request(request: dict[str, Any]) -> None:
    allowed = {entry["name"] for entry in contract()["inputs"]} | {"resource_uid", "input_secret_name", "components"}
    unknown = set(request) - allowed
    if unknown:
        raise ValueError(f"Unknown request fields: {', '.join(sorted(unknown))}")
    if set(request) & SENSITIVE_KEYS:
        raise ValueError("Do not put secret values in the request or Git. Use input_secret_name.")
    request = resolved_request(request)
    schema = json.loads((ROOT / "schemas/application-stack-inputs.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(request, schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"Application stack input schema: {exc.message}") from exc
    required = ["stack_name", "resource_guid", "workload", "resource_uid", "environment", "plane", "region"]
    missing = [name for name in required if not request.get(name)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]", str(request["stack_name"])):
        raise ValueError("stack_name must be 3-40 lowercase AWS-safe characters.")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(request["resource_guid"])):
        raise ValueError("resource_guid must be a lowercase UUID.")

    for key in ("database_client_cidr_blocks", "cache_client_cidr_blocks"):
        for cidr in request.get(key, []) or []:
            network = ipaddress.ip_network(cidr, strict=False)
            if str(network) == "0.0.0.0/0":
                raise ValueError(f"{key} must not include 0.0.0.0/0.")

    needs_secret = request.get("enable_rds", True) or (request.get("enable_cache") and request["environment"] == "prod")
    if needs_secret and not request.get("input_secret_name"):
        raise ValueError("input_secret_name is required for RDS or production cache.")
    if request.get("input_secret_name") and not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", request["input_secret_name"]):
        raise ValueError("input_secret_name must be a Kubernetes DNS name.")


def tf_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def build_terraform_cr(request: dict[str, Any]) -> dict[str, Any]:
    guid = request["resource_guid"]
    metadata = contract()
    input_names = {entry["name"] for entry in metadata["inputs"]}
    vars_list = []
    for key in sorted(input_names - SENSITIVE_KEYS):
        if key in request and request[key] is not None:
            vars_list.append({"name": key, "value": tf_value(request[key])})

    vars_keys = []
    if request.get("enable_rds", True):
        vars_keys.append("db_password")
    if request.get("enable_cache") and request.get("environment") == "prod":
        vars_keys.append("cache_auth_token")
    return {
        "apiVersion": "infra.contrib.fluxcd.io/v1alpha2",
        "kind": "Terraform",
        "metadata": {
            "name": f"stack-{guid}",
            "namespace": "default",
            "labels": {"platform.company/plane": request["plane"]},
            "annotations": {
                "k8s.score.dev/resource-uid": request["resource_uid"],
                "k8s.score.dev/source-workload": request["workload"],
            },
        },
        "spec": {
            "interval": "10m",
            "approvePlan": "",
            "destroyResourcesOnDeletion": True,
            "path": metadata["terraform_cr_source_path"],
            "sourceRef": {
                "kind": "GitRepository",
                "name": "score-aws-template-catalog",
                "namespace": "flux-system",
            },
            "runnerPodTemplate": {
                "metadata": {
                    "annotations": {
                        "vault.hashicorp.com/agent-inject": "true",
                        "vault.hashicorp.com/role": "tf-runner-role",
                        "vault.hashicorp.com/agent-inject-secret-aws": "secret/data/score-api/aws-creds",
                        "vault.hashicorp.com/agent-inject-template-aws": (
                            '{{- with secret "secret/data/score-api/aws-creds" -}}\n'
                            "[default]\n"
                            "aws_access_key_id={{ .Data.data.AWS_ACCESS_KEY_ID }}\n"
                            "aws_secret_access_key={{ .Data.data.AWS_SECRET_ACCESS_KEY }}\n"
                            "{{- end -}}\n"
                        ),
                    }
                },
                "spec": {
                    "env": [
                        {"name": "AWS_SHARED_CREDENTIALS_FILE", "value": "/vault/secrets/aws"},
                        {"name": "AWS_REGION", "value": request["region"]},
                    ]
                },
            },
            "vars": vars_list,
            **({"varsFrom": [{"kind": "Secret", "name": request["input_secret_name"], "varsKeys": vars_keys}]} if vars_keys else {}),
            "writeOutputsToSecret": {"name": f"tf-output-{guid}", "outputs": [entry["name"] for entry in metadata["outputs"] if not entry["sensitive"]]},
        },
    }


def render_manifests(request: dict[str, Any]) -> str:
    validate_request(request)
    return yaml.dump(build_terraform_cr(resolved_request(request)), Dumper=ManifestDumper, sort_keys=False, explicit_start=True)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: renderer.py <request.yaml>", file=sys.stderr)
        return 2
    print(render_manifests(load_request(Path(sys.argv[1]))), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
