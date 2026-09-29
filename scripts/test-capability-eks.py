#!/usr/bin/env python3
"""Check the capability agent's EKS support: a Score project in a sub-folder (eks/), list and number
defaults, the provision_eks tool, execution through /cgi-bin/eks, and skipping re-crawls when score-api
only committed request state. Runs without network, cluster or an API key."""
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

import httpx
from capability_fixtures import MAIN_TF, PROVISIONER, commit, make_repo
from starlette.testclient import TestClient

import score_bindings
from a2a_server import _outcome, build_app
from hcl_eval import evaluate
from score_api import ScoreApi
from store import CapabilityStore

TOKEN, APP_SECRET = "test-token", "app-secret"
EKS = "infra.aws_terraform.provision_eks"
POSTGRES = "infra.aws_terraform.provision_postgres"

EKS_TF = """
variable "cluster_name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,26}[a-z0-9]$", var.cluster_name))
    error_message = "bad name"
  }
}
variable "aws_account_id" { type = string }
variable "region" { type = string }
variable "kubernetes_version" { type = string }
variable "environment" {
  type    = string
  default = "dev"
  validation {
    condition     = contains(["dev", "staging", "uat", "dr", "prod"], var.environment)
    error_message = "bad environment"
  }
}
variable "azs" {
  type    = list(string)
  default = []
}
variable "node_instance_types" {
  type    = list(string)
  default = ["t3.small"]
}
variable "node_min_size" {
  type    = number
  default = 1
}
variable "tags" {
  type    = map(string)
  default = {}
}
resource "aws_eks_cluster" "this" {
  name    = var.cluster_name
  version = var.kubernetes_version
}
output "cluster_endpoint" { value = aws_eks_cluster.this.endpoint }
"""

EKS_PROVISIONER = """
- uri: template://test/eks
  type: eks
  class: aws-terraform
  init: |
    clusterName: {{ .Params.cluster_name | quote }}
    awsAccountId: {{ .Params.aws_account_id | quote }}
    region: {{ .Params.region | quote }}
    kubernetesVersion: {{ .Params.kubernetes_version | quote }}
    environment: {{ .Params.environment | default "dev" | quote }}
    azs: {{ .Params.azs | default list | toJson }}
    nodeInstanceTypes: {{ .Params.node_instance_types | default (list "t3.small") | toJson }}
    nodeMinSize: {{ .Params.node_min_size | default 1 }}
    runnerVaultRole: {{ .Params.runner_vault_role | default "tf-runner-role" | quote }}
  outputs: |
    endpoint: {{ encodeSecretRef (printf "tf-output-%s" .Guid) "cluster_endpoint" }}
  manifests: |
    - apiVersion: infra.contrib.fluxcd.io/v1alpha2
      kind: Terraform
      metadata:
        name: eks-{{ .Guid }}
      spec:
        approvePlan: auto
        path: ./eks
        sourceRef:
          kind: GitRepository
          name: modules
        vars:
          - name: cluster_name
            value: {{ .Init.clusterName | quote }}
          - name: aws_account_id
            value: {{ .Init.awsAccountId | quote }}
          - name: region
            value: {{ .Init.region | quote }}
          - name: kubernetes_version
            value: {{ .Init.kubernetesVersion | quote }}
          - name: environment
            value: {{ .Init.environment | quote }}
          - name: azs
            value: {{ .Init.azs | toJson }}
          - name: node_instance_types
            value: {{ .Init.nodeInstanceTypes | toJson }}
          - name: node_min_size
            value: {{ .Init.nodeMinSize }}
          - name: tags
            value: {{ dict "uid" .Uid | toJson }}
"""

calls: list[tuple[str, dict]] = []


async def fake_score_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-App-Secret"] == APP_SECRET
    endpoint, body = request.url.path.removeprefix("/cgi-bin/"), json.loads(request.content)
    calls.append((endpoint, body))
    if endpoint == "eks":
        guid = "3f1a2b3c-0000-4000-8000-000000000001"
        return httpx.Response(200, json={"status": "ok", "guid": guid, "terraform_cr": f"eks-{guid}",
                                         "namespace": "default", "output_secret": f"tf-output-{guid}"})
    return httpx.Response(200, json={"status": "ok", "run_id": 1, "guid": "g-1", "db_persisted": True})


def rpc(client: TestClient, data: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"data": data}]}}}
    response = client.post("/", json=body, headers={"A2A-Version": "1.0", "Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200 and "result" in response.json(), response.text
    return response.json()["result"]


crawls: list[str] = []


def fake_crawl(facts: dict, roots: dict) -> dict:
    crawls.append(facts["sources"]["score_workloads"]["commit"])
    return facts


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    # Modules repo: RDS module at the root path, EKS module in eks/. Score repo: RDS project at the
    # root, EKS project in eks/ -- the layout of score-tf-modules and score-gp-aws-rds.
    make_repo(tmp / "modules", {"terraform-aws/main.tf": MAIN_TF, "eks/main.tf": EKS_TF}, "v1")
    make_repo(tmp / "score", {".score-k8s/pg.provisioners.yaml": PROVISIONER,
                              "eks/.score-k8s/eks.provisioners.yaml": EKS_PROVISIONER,
                              "eks/.score-k8s/state.yaml": "workloads: {}\nresources: {}\n"}, "v1")

    # Provisioners are found in every project folder, and every default form is read.
    bindings = {b["score_type"]: b for b in score_bindings.collect(tmp / "score")}
    assert set(bindings) == {"postgres", "eks"}, bindings.keys()
    assert bindings["eks"]["file"] == "eks/.score-k8s/eks.provisioners.yaml"
    defaults = {v["terraform_variable"]: v.get("default") for v in bindings["eks"]["vars"]}
    assert defaults["azs"] == [] and defaults["node_instance_types"] == ["t3.small"] and defaults["node_min_size"] == 1
    assert defaults["environment"] == "dev" and defaults["cluster_name"] is None
    assert {v["terraform_variable"]: v.get("default") for v in bindings["postgres"]["vars"]}["storage_gb"] == "20"

    # python-hcl2 renders list literals without quotes; those words must still be read as strings.
    assert evaluate('contains([dev, staging, uat, dr, prod], var.environment)', {"environment": "dev"}) is True
    assert evaluate('contains([dev, staging, uat, dr, prod], var.environment)', {"environment": "qa"}) is False

    store = CapabilityStore(str(tmp / "modules"), None, str(tmp / "score"), None, tmp / "work",
                            describe=fake_crawl, check_interval=0)
    score_api = ScoreApi("http://score-api", APP_SECRET, transport=httpx.MockTransport(fake_score_api))
    app = build_app(store, public_url="http://agent/", auth_token=TOKEN, warm_up=False,
                    score_api=score_api)

    with TestClient(app) as client:
        # Tool calls and checks run on deterministic facts: before any list_capabilities they never crawl with the LLM.
        first = {"workload": "early-eks", "cluster_name": "early-eks", "aws_account_id": "412662188858",
                 "region": "us-east-1", "kubernetes_version": "1.34"}
        task = rpc(client, {"skill": "call_tool", "tool": EKS, "arguments": first})["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]
        rpc(client, {"skill": "check_request", "request": {"score_type": "eks", "params": first}})
        assert crawls == [], f"call_tool/check_request must not trigger the LLM crawl: {crawls}"
        calls.clear()

        # The manifest offers EKS next to RDS, with the right required and optional inputs.
        manifest = rpc(client, {"skill": "list_capabilities"})["message"]["parts"][1]["data"]
        tools = {t["id"]: t for t in manifest["tools"]}
        assert {EKS, POSTGRES} <= set(tools), tools.keys()
        schema = tools[EKS]["inputSchema"]
        assert set(schema["required"]) == {"workload", "cluster_name", "aws_account_id", "region",
                                           "kubernetes_version"}, schema["required"]
        # Only what a user supplies is an input; the rest (network, nodes, ingress) keeps its platform default.
        assert set(schema["properties"]) == {"workload", "cluster_name", "aws_account_id", "region",
                                             "kubernetes_version", "environment"}, sorted(schema["properties"])
        assert schema["properties"]["environment"]["enum"] == ["dev", "staging", "uat", "dr", "prod"]
        assert "tags" not in schema["properties"], "tags are set by Score, not by callers"
        assert set(tools[POSTGRES]["inputSchema"]["required"]) == {"workload", "image"}
        assert "EKS" in tools["infra.score_api.delete_all_resources"]["description"]

        # Provisioning calls /cgi-bin/eks with the workload and the params flat, defaults filled in.
        request = {"workload": "team-eks", "cluster_name": "team-eks", "aws_account_id": "412662188858",
                   "region": "us-east-1", "kubernetes_version": "1.34", "environment": "Dev"}
        task = rpc(client, {"skill": EKS, **request})["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]
        assert "eks-3f1a2b3c" in task["status"]["message"]["parts"][0]["text"]
        endpoint, body = calls[-1]
        assert endpoint == "eks", calls[-1]
        assert body["workload"] == "team-eks" and body["kubernetes_version"] == "1.34" and body["environment"] == "dev"
        # Only the user's inputs go to score-api; the provisioner applies the platform defaults.
        assert "azs" not in body and "node_instance_types" not in body, body
        assert "image" not in body and "runner_vault_role" not in body

        # A request relying on the default environment passes the module's contains([...]) rule.
        defaulted = {k: v for k, v in request.items() if k != "environment"}
        task = rpc(client, {"skill": EKS, **defaulted})["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]
        assert "environment" not in calls[-1][1], "left to the provisioner default (dev)"

        # Invalid requests never reach score-api.
        before = len(calls)
        for bad, field in (({**request, "api_allowed_cidrs": ["0.0.0.0/0"]}, "api_allowed_cidrs"),
                           ({**request, "node_min_size": 3}, "node_min_size"),
                           ({**request, "kubernetes_version": 1.3}, "kubernetes_version"),
                           ({k: v for k, v in request.items() if k != "cluster_name"}, "cluster_name"),
                           ({k: v for k, v in request.items() if k != "workload"}, "workload")):
            data = rpc(client, {"skill": "call_tool", "tool": EKS, "arguments": bad})["message"]["parts"][1]["data"]
            assert data["verdict"] == "rejected" and field in {i["field"] for i in data["issues"]}, (field, data)
        assert len(calls) == before

        # RDS still goes to /cgi-bin/score.
        rpc(client, {"skill": "call_tool", "tool": POSTGRES, "arguments": {"workload": "orders", "image": "nginx"}})
        assert calls[-1][0] == "score"

        # score-api committing request state must not trigger an LLM re-crawl...
        assert len(crawls) == 1, crawls
        commit(tmp / "score", {"eks/.score-k8s/state.yaml": "workloads: {team-eks: {}}\nresources: {}\n",
                               "eks/workloads/team-eks.yaml": "x: 1\n", "eks/generated/manifests.yaml": "[]\n"},
               "eks request: team-eks")
        rpc(client, {"skill": "list_capabilities"})
        assert len(crawls) == 1, "a state-only commit must reuse the crawl"
        assert store.snapshot.report["sources"]["score_workloads"]["commit"] == store.snapshot.commits[1]
        # ...but a provisioner change must.
        commit(tmp / "score", {"eks/.score-k8s/eks.provisioners.yaml": EKS_PROVISIONER.replace("t3.small", "t3.medium")},
               "change eks default instance type")
        rpc(client, {"skill": "list_capabilities"})
        assert len(crawls) == 2, crawls


# delete-all summaries describe the moment the call ended, once, without repeating score-api's msg.
summary = _outcome({"status": "partial", "wiped_workloads": "rds-smoke", "wiped_eks_workloads": "eks-smoke",
                    "destroyed_by_terraform": "rds-1 ", "eks_destroyed_by_terraform": "",
                    "eks_destroy_in_progress": "eks-2 ", "left_for_retry": "", "manual_cleanup_required": "",
                    "msg": "RDS teardown done; EKS destroys are still running in the background"})
assert "Still being destroyed when this task finished" in summary and "not updated later" in summary, summary
assert "[rds-1]" in summary and "[eks-2]" in summary and "Warning" not in summary, summary
assert summary.count("EKS destroys are still running") == 0, summary

print("Capability EKS checks passed: nested Score project, list/number defaults, provision_eks manifest, "
      "execution via /cgi-bin/eks, validation before score-api, crawl reuse for state-only commits.")
