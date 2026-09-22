#!/usr/bin/env python3
"""Render tofu-controller manifests for the application-stack template."""
from __future__ import annotations

from pathlib import Path
import ipaddress
import json
import re
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "templates" / "application-stack" / "contract.yaml"

SENSITIVE_KEYS = {"db_password", "cache_auth_token"}
OUTPUTS = [
    "stack_name",
    "vpc_id",
    "private_subnet_ids",
    "database_subnet_ids",
    "cache_subnet_ids",
    "db_host",
    "db_port",
    "db_name",
    "db_username",
    "s3_bucket_name",
    "s3_bucket_arn",
    "cache_endpoint",
    "cache_port",
    "queue_url",
    "queue_arn",
    "dead_letter_queue_url",
]


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


def contract_inputs() -> set[str]:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return {entry["name"] for entry in contract["inputs"]}


def validate_request(request: dict[str, Any]) -> None:
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

    if request.get("enable_rds", True) and not request.get("db_password"):
        raise ValueError("db_password is required when enable_rds is true.")
    if request.get("environment") == "prod" and request.get("enable_cache") and not request.get("cache_auth_token"):
        raise ValueError("cache_auth_token is required for production cache.")


def tf_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def build_secret(request: dict[str, Any]) -> dict[str, Any]:
    guid = request["resource_guid"]
    string_data = {key: str(request[key]) for key in SENSITIVE_KEYS if request.get(key) is not None}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"secret-{guid}",
            "namespace": "default",
            "labels": {"platform.company/plane": request["plane"]},
            "annotations": {
                "k8s.score.dev/resource-uid": request["resource_uid"],
                "k8s.score.dev/source-workload": request["workload"],
                "kustomize.toolkit.fluxcd.io/prune": "disabled",
            },
        },
        "type": "Opaque",
        "stringData": string_data,
    }


def build_terraform_cr(request: dict[str, Any]) -> dict[str, Any]:
    guid = request["resource_guid"]
    input_names = contract_inputs()
    vars_list = []
    for key in sorted(input_names - SENSITIVE_KEYS):
        if key in request and request[key] is not None:
            vars_list.append({"name": key, "value": tf_value(request[key])})

    vars_keys = [key for key in ("db_password", "cache_auth_token") if request.get(key) is not None]
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
            "approvePlan": "auto",
            "destroyResourcesOnDeletion": True,
            "path": "./templates/application-stack",
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
            "varsFrom": [{"kind": "Secret", "name": f"secret-{guid}", "varsKeys": vars_keys}],
            "writeOutputsToSecret": {"name": f"tf-output-{guid}", "outputs": OUTPUTS},
        },
    }


def render_manifests(request: dict[str, Any]) -> str:
    validate_request(request)
    docs = [build_secret(request), build_terraform_cr(request)]
    return yaml.dump_all(docs, Dumper=ManifestDumper, sort_keys=False, explicit_start=True)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: renderer.py <request.yaml>", file=sys.stderr)
        return 2
    print(render_manifests(load_request(Path(sys.argv[1]))), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
