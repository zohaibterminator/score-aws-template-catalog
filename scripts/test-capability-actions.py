#!/usr/bin/env python3
"""Check that the capability agent executes manifest tools through score-api.

score-api is replaced by an in-process fake, so this runs without a cluster, network or an API key.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import uuid

import httpx
from capability_fixtures import make_fixture
from starlette.testclient import TestClient

from a2a_server import build_app
from score_api import ScoreApi
from store import CapabilityStore

TOKEN, APP_SECRET = "test-token", "app-secret"
PROVISION = "infra.aws_terraform.provision_postgres"
CREDS, DELETE = "infra.score_api.update_aws_credentials", "infra.score_api.delete_all_resources"
AKID, SECRET_KEY = "AKIAABCDEFGHIJKLMNOP", "abcdEFGHijklMNOPqrstUVWXyz0123456789/+=="
calls: list[tuple[str, dict]] = []
score_api_status = "ok"


async def fake_score_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-App-Secret"] == APP_SECRET
    endpoint, body = request.url.path.removeprefix("/cgi-bin/"), json.loads(request.content)
    calls.append((endpoint, body))
    if endpoint == "score":
        if score_api_status == "error":
            return httpx.Response(200, json={"status": "error", "stage": "push_main", "msg": "rejected"})
        return httpx.Response(200, json={"status": "ok", "run_id": 1700000000, "guid": "g-1", "db_persisted": True})
    if endpoint == "update-aws-creds":
        return httpx.Response(200, json={"status": "ok", "verified": True, "reconciled": "rds-g-1 "})
    if endpoint == "delete-all":
        if body["confirm"] != "DELETE-ALL":
            return httpx.Response(200, json={"status": "dry_run", "would_delete_workloads": "checkout-api",
                                             "would_delete_guids": "g-1", "msg": "resend"})
        await asyncio.sleep(0.5)  # delete-all holds the connection open while Terraform destroys everything
        return httpx.Response(200, json={"status": "ok", "wiped_workloads": "checkout-api",
                                         "msg": "Full synchronous deletion completed"})
    return httpx.Response(404, text="not found")


def rpc(client: TestClient, method: str, params: dict) -> dict:
    headers = {"A2A-Version": "1.0", "Authorization": f"Bearer {TOKEN}"}
    response = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                           headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    return body["result"]


def call(client: TestClient, tool: str, arguments: dict, return_immediately: bool = False) -> dict:
    params = {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [
        {"data": {"skill": "call_tool", "tool": tool, "arguments": arguments}}]}}
    if return_immediately:
        params["configuration"] = {"returnImmediately": True}
    return rpc(client, "SendMessage", params)


def task_outcome(task: dict) -> tuple[str, str, dict]:
    status = task["status"]
    artifacts = task.get("artifacts") or []
    data = artifacts[0]["parts"][0]["data"] if artifacts else {}
    return status["state"], status["message"]["parts"][0]["text"], data


def message(result: dict) -> tuple[str, dict]:
    parts = result["message"]["parts"]
    return parts[0]["text"], (parts[1]["data"] if len(parts) > 1 else {})


def fake_crawl(facts: dict, roots: dict) -> dict:
    return facts


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    module_repo, score_repo = make_fixture(tmp)
    store = CapabilityStore(str(module_repo), None, str(score_repo), None, tmp / "work", describe=fake_crawl)
    score_api = ScoreApi("http://score-api", APP_SECRET, transport=httpx.MockTransport(fake_score_api))
    app = build_app(store, public_url="http://agent/", auth_token=TOKEN, answer=None, warm_up=False,
                    score_api=score_api)

    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()
        assert [s["id"] for s in card["skills"]] == ["list_capabilities", "check_request", "call_tool"]

        # The manifest advertises what the agent can execute.
        _, manifest = message(rpc(client, "SendMessage", {"message": {
            "messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"data": {"skill": "list_capabilities"}}]}}))
        tools = {t["id"]: t for t in manifest["tools"]}
        assert set(tools) == {PROVISION, CREDS, DELETE}
        schema = tools[PROVISION]["inputSchema"]
        assert schema["required"] == ["workload", "image"] and {"workload", "image"} <= set(schema["properties"])
        assert tools[DELETE]["annotations"]["destructiveHint"] is True

        # Provisioning: validated, defaults filled from the manifest, submitted to /cgi-bin/score as a task.
        task = call(client, PROVISION, {"workload": "checkout-api", "image": "nginx:latest",
                                         "environment": "prod", "storage_gb": 10})["task"]
        state, text, data = task_outcome(task)
        assert state == "TASK_STATE_COMPLETED", task
        assert "pushed run 1700000000" in text and data["guid"] == "g-1"
        assert calls[-1] == ("score", {"workload": "checkout-api", "image": "nginx:latest", "db": {
            "region": "us-east-1", "environment": "prod", "instance_class": "db.t3.micro", "storage_gb": 10}}), calls[-1]

        # Invalid requests are rejected before score-api is called.
        before = len(calls)
        text, data = message(call(client, PROVISION, {"workload": "Checkout_API", "storage_gb": 50, "colour": "red"}))
        problems = {i["field"]: i["problem"] for i in data["issues"]}
        assert text.startswith("rejected, nothing was provisioned"), text
        assert set(problems) == {"workload", "image", "storage_gb", "colour"}, problems
        text, data = message(call(client, "infra.aws_terraform.provision_redis", {}))
        assert data["verdict"] == "unsupported"
        assert len(calls) == before, "score-api must not be called for invalid requests"

        # A score-api error fails the task and passes the stage and message through.
        score_api_status = "error"
        state, text, data = task_outcome(call(client, PROVISION, {"workload": "checkout-api", "image": "nginx"})["task"])
        assert state == "TASK_STATE_FAILED" and text == "score-api failed at push_main: rejected", text
        score_api_status = "ok"

        # Credentials: validated, answered as a message (not stored in a task), never echoed back.
        text, _ = message(call(client, CREDS, {"access_key_id": "nope", "secret_access_key": SECRET_KEY}))
        assert text.startswith("rejected: access_key_id") and calls[-1][0] != "update-aws-creds"
        result = call(client, CREDS, {"access_key_id": AKID, "secret_access_key": SECRET_KEY})
        text, data = message(result)
        assert calls[-1] == ("update-aws-creds", {"access_key_id": AKID, "secret_access_key": SECRET_KEY,
                                                  "region": "us-east-1"})
        assert "Reconciled: rds-g-1." in text and SECRET_KEY not in json.dumps(result)

        # Delete-all without confirm is a dry run.
        state, text, _ = task_outcome(call(client, DELETE, {})["task"])
        assert state == "TASK_STATE_COMPLETED" and text.startswith("Dry run: would delete workloads [checkout-api]")
        assert calls[-1] == ("delete-all", {"confirm": ""})
        text, _ = message(call(client, DELETE, {"confirm": "yes"}))
        assert text.startswith('confirm must be "DELETE-ALL"')

        # The real delete returns immediately when asked; the caller polls GetTask until it finishes.
        task = call(client, DELETE, {"confirm": "DELETE-ALL"}, return_immediately=True)["task"]
        assert task["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"), task["status"]
        deadline = time.time() + 10
        while task["status"]["state"] not in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED") and time.time() < deadline:
            time.sleep(0.1)
            task = rpc(client, "GetTask", {"id": task["id"]})
        state, text, data = task_outcome(task)
        assert state == "TASK_STATE_COMPLETED" and data["wiped_workloads"] == "checkout-api", task
        assert calls[-1] == ("delete-all", {"confirm": "DELETE-ALL"})

    # Without score-api the agent still lists capabilities, but refuses to execute.
    app = build_app(store, public_url="http://agent/", auth_token=TOKEN, answer=None, warm_up=False)
    with TestClient(app) as client:
        text, _ = message(call(client, PROVISION, {"workload": "checkout-api", "image": "nginx"}))
        assert "not connected to score-api" in text

print("Capability action checks passed: manifest tools, validation before score-api, provision and delete-all "
      "tasks, returnImmediately + GetTask, credentials handling.")
