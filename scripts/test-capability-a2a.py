#!/usr/bin/env python3
"""Check the capability agent's A2A server against throwaway Git repositories.

The LLM crawl and the Q&A agent are replaced by stubs, so this runs without network or an API key;
it checks the wiring (crawl on request, cache per commit, failures not cached) and the skills.
"""
from pathlib import Path
import tempfile
import uuid

from capability_fixtures import MAIN_TF, commit, make_fixture
from starlette.testclient import TestClient

import report
import skills
from a2a_server import build_app
from hcl_eval import UNKNOWN, evaluate
from store import CapabilityStore

assert evaluate('var.environment == "prod" ? 7 : 0', {"environment": "dev"}) == 0
assert evaluate("tonumber(var.s) <= 20", {"s": "50"}) is False
assert evaluate('contains(["a", "b"], var.x) && !var.y', {"x": "b", "y": False}) is True
assert evaluate("local.name", {}, {"name": "appdb"}) == "appdb"
assert evaluate("jsondecode(var.ids)", {"ids": '["sg-1"]'}) == ["sg-1"]
assert evaluate('var.missing == "prod"', {}) is UNKNOWN
assert evaluate("somefunc(var.x)", {"x": 1}) is UNKNOWN
assert evaluate('"a${var.x}"', {"x": 1}) is UNKNOWN

TOKEN = "test-token"


def send(client: TestClient, part: dict, token: str | None = TOKEN) -> dict:
    headers = {"A2A-Version": "1.0"} | ({"Authorization": f"Bearer {token}"} if token else {})
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [part]}}}
    response = client.post("/", json=body, headers=headers)
    return {"status": response.status_code, "body": response.json()}


def reply(result: dict) -> tuple[str, dict]:
    parts = result["body"]["result"]["message"]["parts"]
    return parts[0]["text"], (parts[1]["data"] if len(parts) > 1 else {})


crawls: list[str] = []
fail_next_crawl = False


def fake_llm_crawl(facts: dict, roots: dict) -> dict:
    """Stands in for describe_with_llm: records the crawl and merges a draft the way the real agent's is merged."""
    if fail_next_crawl:
        raise RuntimeError("The model did not submit capabilities.")
    crawls.append(facts["sources"]["terraform_modules"]["commit"])
    draft = {"capabilities": [{
        "id": "terraform-aws", "summary": "PostgreSQL on Amazon RDS", "aws_service": "Amazon RDS",
        "parameters": [{"terraform_variable": "environment", "effect": "prod enables Multi-AZ and backups"}],
        "behaviours": [{"resource": "aws_db_instance.this", "attribute": "multi_az", "description": "prod only"}],
        "constraints": [{"text": "Engine is PostgreSQL", "evidence": "modules:terraform-aws/main.tf"}]}]}
    return report.merge_llm(facts, draft, roots, "fake")


def fake_answer(snapshot, question: str):
    return f"stub answer to: {question}", [{"skill": "list_capabilities",
                                            "result": skills.list_capabilities(snapshot.report)}]


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    module_repo, score_repo = make_fixture(tmp)
    store = CapabilityStore(str(module_repo), None, str(score_repo), None, tmp / "work",
                            describe=fake_llm_crawl, check_interval=0)
    app = build_app(store, public_url="http://agent/", auth_token=TOKEN, answer=fake_answer, warm_up=False)

    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503

        # A failed agent crawl is reported to the caller and not cached.
        fail_next_crawl = True
        text, _ = reply(send(client, {"data": {"skill": "list_capabilities"}}))
        assert text.startswith("Could not crawl the repositories: RuntimeError"), text
        assert store.snapshot is None and crawls == []
        fail_next_crawl = False

        # The first question triggers the crawl; later questions at the same commit reuse it.
        text, data = reply(send(client, {"text": "What capabilities do you have?"}))
        assert text == "stub answer to: What capabilities do you have?"
        assert data["evidence"][0]["result"]["capabilities"][0]["summary"] == "PostgreSQL on Amazon RDS"
        text, data = reply(send(client, {"data": {"skill": "list_capabilities", "detail": "summary"}}))
        assert data["capabilities"][0]["aws_service"] == "Amazon RDS"
        assert len(crawls) == 1, crawls
        first_commit = store.snapshot.commits[0]
        assert client.get("/readyz").status_code == 200

        card = client.get("/.well-known/agent-card.json").json()
        assert [s["id"] for s in card["skills"]] == ["list_capabilities", "check_request"]
        assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
        assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"

        assert send(client, {"data": {"skill": "list_capabilities"}}, token=None)["status"] == 401
        assert send(client, {"data": {"skill": "list_capabilities"}}, token="wrong")["status"] == 401

        # Default listing is the tool manifest other agents discover.
        text, manifest = reply(send(client, {"data": {"skill": "list_capabilities"}}))
        assert set(manifest) == {"provider", "discoveryMode", "manifestDigest", "tools"}
        assert manifest["provider"] == "valueops" and manifest["discoveryMode"] == "agent-discovery-live"
        assert manifest["manifestDigest"].startswith("sha256:") and len(manifest["manifestDigest"]) == 71
        [tool] = manifest["tools"]
        assert list(tool) == ["agent", "provider", "name", "id", "description", "inputSchema", "annotations"] or \
            set(tool) == {"agent", "provider", "name", "id", "description", "inputSchema", "annotations"}
        assert tool["id"] == "infra.aws_terraform.provision_postgres"
        assert (tool["agent"], tool["provider"], tool["name"]) == ("infra", "aws_terraform", "provision_postgres")
        assert tool["description"] == "PostgreSQL on Amazon RDS"
        schema = tool["inputSchema"]
        assert set(schema["properties"]) == {"region", "environment", "instance_class", "storage_gb"}
        assert schema["properties"]["environment"] == {"type": "string", "default": "dev",
                                                       "description": "prod enables Multi-AZ and backups"}
        assert schema["required"] == [] and schema["additionalProperties"] is False
        assert tool["annotations"] == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                                       "openWorldHint": True}
        assert "- infra.aws_terraform.provision_postgres: PostgreSQL on Amazon RDS" in text
        again = reply(send(client, {"data": {"skill": "list_capabilities"}}))[1]
        assert again["manifestDigest"] == manifest["manifestDigest"], "digest must be stable for the same crawl"

        _, data = reply(send(client, {"data": {"skill": "list_capabilities", "detail": "summary"}}))
        [item] = data["capabilities"]
        assert set(item) == {"capability", "summary", "aws_service", "params", "outputs"}, item.keys()
        assert item["capability"] == "postgres.aws-terraform"
        assert {p["name"] for p in item["params"]} == {"region", "environment", "instance_class", "storage_gb"}
        assert next(p for p in item["params"] if p["name"] == "environment")["effect"].startswith("prod enables")
        _, data = reply(send(client, {"data": {"skill": "list_capabilities", "detail": "detailed"}}))
        [item] = data["capabilities"]
        assert item["constraints"][0]["text"] == "Engine is PostgreSQL" and item["conditional_behaviours"]

        text, data = reply(send(client, {"data": {"skill": "check_request", "request": {
            "score_type": "postgres", "params": {"environment": "prod", "storage_gb": 10},
            "expect": {"multi_az": True, "backup_retention_period": 7}}}}))
        assert text == "accepted", text
        assert data["resolved_attributes"]["aws_db_instance.this.db_name"] == "appdb"
        assert data["resolved_attributes"]["aws_db_instance.this.publicly_accessible"] is True
        assert data["score_resource"]["params"]["storage_gb"] == 10

        text, data = reply(send(client, {"data": {"skill": "check_request", "request": {
            "score_type": "postgres", "params": {"environment": "dev", "storage_gb": 50, "publicly_accessible": False,
                                                 "colour": "red"},
            "expect": {"multi_az": True}}}}))
        problems = {i["field"]: i["problem"] for i in data["issues"]}
        assert data["verdict"] == "rejected"
        assert problems["storage_gb"].startswith("fails Terraform validation")
        assert problems["publicly_accessible"] == "not settable from Score; set by platform"
        assert problems["colour"] == "unknown parameter"
        assert problems["expect.multi_az"] == "would be False, not True"

        _, data = reply(send(client, {"data": {"skill": "check_request", "request": {"score_type": "redis"}}}))
        assert data["verdict"] == "unsupported" and data["issues"][0]["available"] == ["postgres.aws-terraform"]

        assert len(crawls) == 1, "same commit must not be re-crawled"

        # A new commit adds a required variable the provisioner does not set; the next request re-crawls.
        commit(module_repo, {"terraform-aws/main.tf": MAIN_TF + 'variable "subnet_group" { type = string }\n'}, "v2")
        text, data = reply(send(client, {"data": {"skill": "check_request", "request": {"score_type": "postgres"}}}))
        assert len(crawls) == 2 and store.snapshot.commits[0] != first_commit
        assert data["verdict"] == "rejected" and data["issues"][0]["field"] == "subnet_group", data
        [cap] = store.snapshot.report["capabilities"]
        assert any("required variable `subnet_group`" in i for i in cap["score"]["binding_issues"])

    assert skills.check_request(store.snapshot.report, {"capability_id": "terraform-aws"})["verdict"] == "rejected"

print("Capability A2A checks passed: crawl on request, cache per commit, failed crawls not cached, agent card, "
      "bearer auth, list/check skills.")
