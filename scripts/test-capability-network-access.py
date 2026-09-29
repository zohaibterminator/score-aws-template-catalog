#!/usr/bin/env python3
"""Check the capability agent's network-access support against the real network-access module and Score
provisioner (sibling checkouts of score-tf-modules and score-gp-aws-rds): the provision_network_access
tool, its validation, and execution through /cgi-bin/network-access. Runs without network, cluster or an API key."""
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import httpx
from capability_fixtures import MAIN_TF, PROVISIONER, make_repo
from starlette.testclient import TestClient

from a2a_server import _outcome, build_app
from score_api import ScoreApi
from store import CapabilityStore

REPOS = Path(os.environ.get("REPOS_ROOT", Path(__file__).resolve().parents[2]))
MODULE_DIR = REPOS / "score-tf-modules" / "network-access"
PROVISIONER_FILE = REPOS / "score-gp-aws-rds" / "network-access" / ".score-k8s" / "network-access.provisioners.yaml"
if not (MODULE_DIR.is_dir() and PROVISIONER_FILE.is_file()):
    print(f"Skipped network-access checks: {MODULE_DIR} or {PROVISIONER_FILE} is not checked out.")
    sys.exit(0)

TOKEN, APP_SECRET = "test-token", "app-secret"
TOOL = "infra.aws_terraform.provision_network_access"
calls: list[tuple[str, dict]] = []


async def fake_score_api(request: httpx.Request) -> httpx.Response:
    endpoint, body = request.url.path.removeprefix("/cgi-bin/"), json.loads(request.content)
    calls.append((endpoint, body))
    guid = "5e1a2b3c-0000-4000-8000-000000000002"
    return httpx.Response(200, json={"status": "ok", "guid": guid, "terraform_cr": f"network-access-{guid}",
                                     "namespace": "default", "output_secret": f"tf-output-{guid}"})


def rpc(client: TestClient, data: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"data": data}]}}}
    response = client.post("/", json=body, headers={"A2A-Version": "1.0", "Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200 and "result" in response.json(), response.text
    return response.json()["result"]


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    module_files = {f"network-access/{p.name}": p.read_text(encoding="utf-8") for p in MODULE_DIR.glob("*.tf")}
    make_repo(tmp / "modules", {"terraform-aws/main.tf": MAIN_TF, **module_files}, "v1")
    make_repo(tmp / "score", {".score-k8s/pg.provisioners.yaml": PROVISIONER,
                              "network-access/.score-k8s/network-access.provisioners.yaml":
                                  PROVISIONER_FILE.read_text(encoding="utf-8"),
                              "network-access/.score-k8s/state.yaml": "workloads: {}\nresources: {}\n"}, "v1")
    store = CapabilityStore(str(tmp / "modules"), None, str(tmp / "score"), None, tmp / "work",
                            describe=lambda facts, roots: facts, check_interval=0)
    score_api = ScoreApi("http://score-api", APP_SECRET, transport=httpx.MockTransport(fake_score_api))
    app = build_app(store, public_url="http://agent/", auth_token=TOKEN, warm_up=False,
                    score_api=score_api)

    with TestClient(app) as client:
        manifest = rpc(client, {"skill": "list_capabilities"})["message"]["parts"][1]["data"]
        tools = {t["id"]: t for t in manifest["tools"]}
        assert TOOL in tools, tools.keys()
        schema = tools[TOOL]["inputSchema"]
        assert set(schema["required"]) == {"workload", "name", "aws_account_id", "region", "vpc_id", "subnet_id",
                                           "vpc_cidr", "cluster_name", "cluster_security_group_id"}, schema["required"]
        props = schema["properties"]
        assert props["bastion_access_mode"]["enum"] == ["ssm", "ssh"] and props["bastion_access_mode"]["default"] == "ssm"
        # Only what a user supplies is an input; bastion size, endpoints, ingress settings keep platform defaults.
        assert set(props) == {"workload", "name", "aws_account_id", "region", "environment", "cluster_name", "vpc_id",
                              "vpc_cidr", "subnet_id", "cluster_security_group_id", "bastion_access_mode",
                              "ssh_key_name", "public_subnet_id"}, sorted(props)
        # The allow-lists are fixed in score-tf-modules/access-lists: no tool offers them as arguments.
        assert not {"ssh_allowed_cidrs", "ingress_allowed_cidrs", "api_allowed_cidrs"} & set(props), sorted(props)
        assert "tags" not in props and "image" not in props
        assert "bastion" in tools["infra.score_api.delete_all_resources"]["description"]
        assert "network-access" in tools["infra.score_api.resource_status"]["inputSchema"]["properties"]["capability"]["enum"]

        request = {"workload": "platform-access", "name": "score-dev-access", "aws_account_id": "412662188858",
                   "region": "us-east-1", "vpc_id": "vpc-0123456789abcdef0", "subnet_id": "subnet-0123456789abcdef0",
                   "vpc_cidr": "10.90.0.0/16", "cluster_name": "score-dev-eks", "bastion_access_mode": "SSM",
                   "cluster_security_group_id": "sg-0123456789abcdef0"}
        task = rpc(client, {"skill": "call_tool", "tool": TOOL, "arguments": request})["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]
        assert "network-access-5e1a2b3c" in task["status"]["message"]["parts"][0]["text"]
        endpoint, body = calls[-1]
        assert endpoint == "network-access", calls[-1]
        assert body["workload"] == "platform-access" and body["bastion_access_mode"] == "ssm"
        assert body["enable_bastion"] is True and "ssh_allowed_cidrs" not in body
        assert "runner_vault_role" not in body and "image" not in body

        # Name patterns (can(regex(...))) are left to score-api and the Terraform plan; the agent checks enums,
        # contains() rules and required params.
        before = len(calls)
        for bad, field in (({**request, "bastion_access_mode": "rdp"}, "bastion_access_mode"),
                           ({**request, "ssh_allowed_cidrs": ["0.0.0.0/0"]}, "ssh_allowed_cidrs"),
                           ({**request, "enable_ssm_vpc_endpoints": True}, "enable_ssm_vpc_endpoints"),
                           ({**request, "name": "Kuch Bhi"}, "name"),
                           ({**request, "vpc_id": "vpc-xyz"}, "vpc_id"),
                           ({**request, "bastion_access_mode": "ssh", "ssh_key_name": "platform-bastion"},
                            "public_subnet_id"),
                           ({k: v for k, v in request.items() if k != "cluster_security_group_id"},
                            "cluster_security_group_id"),
                           ({k: v for k, v in request.items() if k != "vpc_id"}, "vpc_id")):
            data = rpc(client, {"skill": "call_tool", "tool": TOOL, "arguments": bad})["message"]["parts"][1]["data"]
            assert data["verdict"] == "rejected" and field in {i["field"] for i in data["issues"]}, (field, data)
        assert len(calls) == before, "invalid requests must not reach score-api"

        # Stray spaces from a form are trimmed, and the patterns are advertised for forms to validate.
        rpc(client, {"skill": "call_tool", "tool": TOOL, "arguments": {
            **request, "bastion_access_mode": "ssh", "public_subnet_id": "subnet-0b892efbdce3dd1a9  "}})
        assert calls[-1][1]["public_subnet_id"] == "subnet-0b892efbdce3dd1a9", calls[-1][1]
        assert calls[-1][1]["ssh_key_name"] == "platform-bastion", "ssh mode defaults to the platform key pair"
        assert props["name"]["pattern"] == "^[a-z][a-z0-9-]{1,38}[a-z0-9]$"

        data = rpc(client, {"skill": "call_tool", "tool": "infra.score_api.resource_status",
                            "arguments": {"capability": "network-access"}})
        assert calls[-1] == ("status", {"capability": "network-access"}), calls[-1]

summary = _outcome({"status": "partial", "wiped_workloads": "", "wiped_eks_workloads": "team-eks",
                    "wiped_network_access_workloads": "platform-access", "destroyed_by_terraform": "",
                    "eks_destroyed_by_terraform": "", "network_access_destroyed_by_terraform": "network-access-1 ",
                    "eks_destroy_in_progress": "eks-2 ", "network_access_destroy_in_progress": "",
                    "left_for_retry": "", "manual_cleanup_required": "", "msg": ""})
assert "network-access workloads [platform-access]" in summary and "[network-access-1]" in summary, summary
assert "[eks-2]" in summary, summary
dry = _outcome({"status": "dry_run", "would_delete_workloads": "", "would_delete_network_access_workloads": "a"})
assert "network-access bastions [a]" in dry, dry

print("Capability network-access checks passed: real module and provisioner, provision_network_access manifest, "
      "access-mode and 0.0.0.0/0 validation before score-api, execution via /cgi-bin/network-access, status filter.")
