#!/usr/bin/env python3
"""Check provision_eks against the real eks module and Score provisioner (sibling checkouts of score-tf-modules and
score-gp-aws-rds), including the bundled network-access CR: the extra inputs it adds, that the bundled CR does not
leak its variables into the eks capability, and execution through /cgi-bin/eks. Runs without network or an API key."""
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import httpx
from capability_fixtures import MAIN_TF, PROVISIONER, make_repo
from starlette.testclient import TestClient

from a2a_server import build_app
from score_api import ScoreApi
from store import CapabilityStore

REPOS = Path(os.environ.get("REPOS_ROOT", Path(__file__).resolve().parents[2]))
MODULES = REPOS / "score-tf-modules"
PROVISIONER_FILE = REPOS / "score-gp-aws-rds" / "eks" / ".score-k8s" / "eks.provisioners.yaml"
if not ((MODULES / "eks").is_dir() and PROVISIONER_FILE.is_file()):
    print(f"Skipped eks bundle checks: {MODULES / 'eks'} or {PROVISIONER_FILE} is not checked out.")
    sys.exit(0)

TOKEN, APP_SECRET = "test-token", "app-secret"
TOOL = "infra.aws_terraform.provision_eks"
GUID = "7b1a2b3c-0000-4000-8000-000000000003"
calls: list[tuple[str, dict]] = []


async def fake_score_api(request: httpx.Request) -> httpx.Response:
    endpoint, body = request.url.path.removeprefix("/cgi-bin/"), json.loads(request.content)
    calls.append((endpoint, body))
    bundled = body.get("enable_network_access", True)
    return httpx.Response(200, json={"status": "ok", "guid": GUID, "terraform_cr": f"eks-{GUID}",
                                     "namespace": "default", "output_secret": f"tf-output-{GUID}",
                                     "network_access": bundled,
                                     **({"network_access_cr": f"access-{GUID}",
                                         "network_access_output_secret": f"tf-output-{GUID}-access"}
                                        if bundled else {})})


def rpc(client: TestClient, data: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"data": data}]}}}
    response = client.post("/", json=body, headers={"A2A-Version": "1.0", "Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200 and "result" in response.json(), response.text
    return response.json()["result"]


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    module_files = {f"{d}/{p.name}": p.read_text(encoding="utf-8")
                    for d in ("eks", "access-lists", "network-access") for p in (MODULES / d).glob("*.tf")}
    make_repo(tmp / "modules", {"terraform-aws/main.tf": MAIN_TF, **module_files}, "v1")
    make_repo(tmp / "score", {".score-k8s/pg.provisioners.yaml": PROVISIONER,
                              "eks/.score-k8s/eks.provisioners.yaml": PROVISIONER_FILE.read_text(encoding="utf-8"),
                              "eks/.score-k8s/state.yaml": "workloads: {}\nresources: {}\n"}, "v1")
    store = CapabilityStore(str(tmp / "modules"), None, str(tmp / "score"), None, tmp / "work",
                            describe=lambda facts, roots: facts, check_interval=0)
    score_api = ScoreApi("http://score-api", APP_SECRET, transport=httpx.MockTransport(fake_score_api))
    app = build_app(store, public_url="http://agent/", auth_token=TOKEN, warm_up=False, score_api=score_api)

    with TestClient(app) as client:
        facts = store.facts().report
        eks = next(c for c in facts["capabilities"] if c["id"] == "eks")
        # The bundled CR's handoff keys (vpc_id, subnet_id, ...) belong to network-access, not to ./eks.
        assert [e["score_type"] for e in eks["score"]["entrypoints"]] == ["eks"], "bound to ./eks only"
        assert eks["score"]["binding_issues"] == [], eks["score"]["binding_issues"]
        names = {p["terraform_variable"] for p in eks["parameters"]}
        assert {"enable_network_access", "bastion_access_mode"} <= names, sorted(names)
        assert not {"vpc_id", "subnet_id", "runner_vault_role"} & names, sorted(names)

        manifest = rpc(client, {"skill": "list_capabilities"})["message"]["parts"][1]["data"]
        schema = {t["id"]: t for t in manifest["tools"]}[TOOL]["inputSchema"]
        props = schema["properties"]
        assert set(props) == {"workload", "cluster_name", "aws_account_id", "region", "kubernetes_version",
                              "environment", "enable_network_access", "bastion_access_mode"}, sorted(props)
        assert props["enable_network_access"]["type"] == "boolean" and props["enable_network_access"]["default"] is True
        assert props["bastion_access_mode"]["enum"] == ["ssm", "ssh"] and props["bastion_access_mode"]["default"] == "ssm"
        assert set(schema["required"]) == {"workload", "cluster_name", "aws_account_id", "region",
                                           "kubernetes_version"}, schema["required"]

        request = {"workload": "platform-eks", "cluster_name": "platform-eks", "aws_account_id": "412662188858",
                   "region": "us-east-1", "kubernetes_version": "1.34"}
        # One request, both stacks: the reply names the bundled network-access CR and its outputs.
        task = rpc(client, {"skill": "call_tool", "tool": TOOL, "arguments": {**request, "bastion_access_mode": "SSH"}})["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]
        text = task["status"]["message"]["parts"][0]["text"]
        assert f"access-{GUID}" in text and f"tf-output-{GUID}-access" in text, text
        endpoint, body = calls[-1]
        assert endpoint == "eks" and body["bastion_access_mode"] == "ssh" and "enable_network_access" not in body, body

        rpc(client, {"skill": "call_tool", "tool": TOOL, "arguments": {**request, "enable_network_access": False}})
        assert calls[-1][1]["enable_network_access"] is False, calls[-1][1]

        before = len(calls)
        for bad, field in (({**request, "bastion_access_mode": "rdp"}, "bastion_access_mode"),
                           ({**request, "ssh_key_name": "other"}, "ssh_key_name"),
                           ({**request, "vpc_id": "vpc-0123456789abcdef0"}, "vpc_id")):
            data = rpc(client, {"skill": "call_tool", "tool": TOOL, "arguments": bad})["message"]["parts"][1]["data"]
            assert data["verdict"] == "rejected" and field in {i["field"] for i in data["issues"]}, (field, data)
        assert len(calls) == before, "invalid requests must not reach score-api"

print("Capability EKS bundle checks passed: real eks provisioner with the bundled network-access CR, extra inputs, "
      "no leaked handoff variables, execution via /cgi-bin/eks.")
