#!/usr/bin/env python3
"""A plain curl to the capability agent's URL must never return capabilities or start an LLM crawl.

Also checks that list_capabilities follows the current commit's variables when the LLM crawl is older (crawl budget
used up): a variable removed from the modules disappears from the tools without a new crawl.
"""
from pathlib import Path
import json
import tempfile
import uuid

from capability_fixtures import MAIN_TF, commit, make_fixture
from starlette.testclient import TestClient

import report
from a2a_server import build_app
from store import CapabilityStore

TOKEN = "test-token"
crawls: list[str] = []


def fake_llm_crawl(facts: dict, roots: dict) -> dict:
    crawls.append(facts["sources"]["terraform_modules"]["commit"])
    draft = {"capabilities": [{"id": "terraform-aws", "summary": "PostgreSQL on Amazon RDS", "aws_service": "Amazon RDS",
                               "parameters": [{"terraform_variable": "environment", "effect": "prod enables Multi-AZ"}]}]}
    return report.merge_llm(facts, draft, roots, "fake")


def rpc(parts: list, token: str | None = TOKEN) -> dict:
    return {"url": "/", "method": "POST",
            "headers": {"A2A-Version": "1.0", **({"Authorization": f"Bearer {token}"} if token else {})},
            "json": {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                     "params": {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": parts}}}}


# What someone does to "test the URL", with and without the token.
PROBES = [
    {"url": "/", "method": "GET"},
    {"url": "/", "method": "POST"},
    {"url": "/", "method": "POST", "json": {}},
    {"url": "/", "method": "GET", "headers": {"Authorization": f"Bearer {TOKEN}"}},
    {"url": "/", "method": "POST", "headers": {"Authorization": f"Bearer {TOKEN}"}},
    {"url": "/", "method": "POST", "headers": {"Authorization": f"Bearer {TOKEN}"}, "json": {}},
    {"url": "/", "method": "POST", "headers": {"Authorization": f"Bearer {TOKEN}"}, "content": "hello"},
    {"url": "/healthz", "method": "GET"},
    {"url": "/readyz", "method": "GET"},
    {"url": "/.well-known/agent-card.json", "method": "GET"},
    {"url": "/tools", "method": "GET"},
    rpc([{"data": {"skill": "list_capabilities"}}], token=None),
    rpc([{"data": {"skill": "list_capabilities"}}], token="wrong"),
    rpc([{"text": "hello"}]),
    rpc([{"text": "What capabilities do you have?"}]),
    rpc([{"data": {}}]),
    rpc([{"data": {"skill": "unknown"}}]),
    rpc([]),
]
# Anything that would reveal a capability: tool ids, module names, Terraform variables, repo URLs.
LEAKS = ["provision_", "terraform-aws", "aws_terraform", "instance_class", "storage_gb", "cluster_name", "module-repo",
         "score-repo", '"tools"']

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    module_repo, score_repo = make_fixture(tmp)
    # A shared helper module with only locals and outputs, like access-lists.
    commit(module_repo, {"helper-lists/main.tf": 'locals { cidrs = ["10.0.0.0/8"] }' + chr(10)
                                      + 'output "cidrs" { value = local.cidrs }' + chr(10)},
           "helper module")
    store = CapabilityStore(str(module_repo), None, str(score_repo), None, tmp / "work",
                            describe=fake_llm_crawl, check_interval=0, max_crawls_per_hour=1)
    app = build_app(store, public_url="http://agent/", auth_token=TOKEN, warm_up=False,
                    score_api=object())  # connected to score-api, so the card also lists call_tool

    with TestClient(app) as client:
        store.facts()  # as at start-up: facts loaded, so readyz is "ready"
        for probe in PROBES:
            probe = dict(probe)
            response = client.request(probe.pop("method"), probe.pop("url"), **probe)
            body = response.text
            leaked = [word for word in LEAKS if word in body]
            assert not leaked, (response.request.method, response.request.url, response.status_code, leaked, body[:300])
        assert crawls == [], f"a probe started an LLM crawl: {crawls}"
        assert client.get("/readyz").json() == {"status": "ready"}

        # Only an authenticated list_capabilities returns them, crawling once.
        request = rpc([{"data": {"skill": "list_capabilities"}}])
        manifest = client.post("/", json=request["json"], headers=request["headers"]).json()
        tools = manifest["result"]["message"]["parts"][1]["data"]["tools"]
        schema = tools[0]["inputSchema"]["properties"]
        assert "instance_class" in schema and len(crawls) == 1
        assert [t["id"] for t in tools if "helper" in t["id"]] == [], "a module without resources is not a tool"

        # A commit removes a variable while the crawl budget is used up: it disappears at once, prose is kept.
        commit(module_repo, {"terraform-aws/main.tf": MAIN_TF.replace(
            'variable "instance_class" { default = "db.t3.micro" }', "")}, "drop instance_class")
        store._checked_at = store._facts_checked_at = 0
        manifest = client.post("/", json=request["json"], headers=request["headers"]).json()
        tool = manifest["result"]["message"]["parts"][1]["data"]["tools"][0]
        assert "instance_class" not in tool["inputSchema"]["properties"], json.dumps(tool)[:400]
        assert tool["description"] == "PostgreSQL on Amazon RDS"
        assert tool["inputSchema"]["properties"]["environment"]["description"] == "prod enables Multi-AZ"
        assert len(crawls) == 1, "must not crawl again while the budget is used up"
        text = manifest["result"]["message"]["parts"][0]["text"]
        assert store.facts().commits[0][:12] in text, text

print("probe checks passed")
