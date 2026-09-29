#!/usr/bin/env python3
"""Check the capability agent against throwaway Git repositories, without network or an LLM."""
import json
from pathlib import Path
import tempfile

from anthropic import beta_tool
from capability_fixtures import make_fixture

import capability_agent
import report
from git_source import checkout

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    module_repo, score_repo = make_fixture(tmp)
    # Uncommitted work must not leak into the report: capabilities come from Git.
    (module_repo / "terraform-aws/extra.tf").write_text('resource "aws_s3_bucket" "b" {}\n', encoding="utf-8")

    modules = checkout(str(module_repo), "v1.0.0", tmp / "work/modules")
    score = checkout(str(score_repo), None, tmp / "work/score")
    assert len(modules.commit) == 40 and modules.ref == "v1.0.0"
    result = report.build(modules, score)

    [cap] = result["capabilities"]
    assert cap["id"] == "terraform-aws"
    assert cap["terraform"]["resources"] == ["aws_db_instance.this"], "uncommitted file leaked"
    params = {p["terraform_variable"]: p for p in cap["parameters"]}
    assert {"resource": "provider.aws", "attribute": "region", "kind": "variable"} in params["region"]["drives"]
    assert any(d["attribute"] == "identifier" for d in params["resource_guid"]["drives"]), "locals not traced"
    assert params["storage_gb"]["validations"] == ["tonumber(var.storage_gb) <= 20"]
    source = lambda name: params[name]["score_sources"][0]  # noqa: E731
    assert source("region") == {"provisioner": "template://test/postgres", "set_by": "developer",
                                "score_param": "region", "default": "us-east-1"}
    assert source("publicly_accessible")["set_by"] == "platform" and source("publicly_accessible")["value"] == "true"
    assert source("password") == {"provisioner": "template://test/postgres", "set_by": "secret",
                                  "detail": "Kubernetes Secret secret-{{ .Guid }}"}
    assert source("resource_guid")["set_by"] == "score"
    assert [b["attribute"] for b in cap["conditional_behaviours"]] == ["multi_az", "backup_retention_period"]
    assert {"resource": "aws_db_instance.this", "attribute": "engine", "value": "postgres"} in cap["fixed_settings"]
    entry = cap["score"]["entrypoints"][0]
    assert entry["outputs"] == [
        {"name": "host", "source": "terraform_output", "terraform_output": "host"},
        {"name": "name", "source": "literal", "value": "appdb"},
    ]
    assert entry["example"]["resources"]["<name>"]["params"] == {
        "region": "us-east-1", "environment": "dev", "instance_class": "db.t3.micro", "storage_gb": "20"}
    issues = cap["score"]["binding_issues"]
    assert issues == [".score-k8s/pg.provisioners.yaml sets `not_declared`, which terraform-aws does not declare"]

    roots = {"modules": modules.path, "score": score.path}
    draft = {"capabilities": [
        {"id": "terraform-aws", "summary": "PostgreSQL on RDS", "aws_service": "Amazon RDS",
         "parameters": [{"terraform_variable": "environment", "effect": "prod enables Multi-AZ"},
                        {"terraform_variable": "invented", "effect": "x"}],
         "behaviours": [{"resource": "aws_db_instance.this", "attribute": "multi_az", "description": "prod only"},
                        {"resource": "aws_db_instance.this", "attribute": "storage_encrypted", "description": "x"}],
         "constraints": [{"text": "Engine is PostgreSQL", "evidence": "modules:terraform-aws/main.tf"},
                         {"text": "Made up", "evidence": "modules:nope.tf"},
                         {"text": "Escapes", "evidence": "modules:../score/.score-k8s/pg.provisioners.yaml"}]},
        {"id": "ghost", "summary": "x", "aws_service": "x", "parameters": [], "behaviours": [], "constraints": []},
    ]}
    merged = report.merge_llm(result, draft, roots, "test-model")
    cap = merged["capabilities"][0]
    assert cap["summary"] == "PostgreSQL on RDS"
    assert {p["terraform_variable"]: p for p in cap["parameters"]}["environment"]["effect"] == "prod enables Multi-AZ"
    assert cap["conditional_behaviours"][0]["description"] == "prod only"
    assert [c["text"] for c in cap["constraints"]] == ["Engine is PostgreSQL"]
    warnings = " | ".join(merged["extraction"]["warnings"])
    for dropped in ("ghost", "invented", "storage_encrypted", "Made up", "Escapes"):
        assert dropped in warnings, dropped

    # Wrap the tools exactly as the Claude tool runner does, so schemas and argument validation are exercised.
    captured: dict = {}
    tools = {t.name: t for t in map(beta_tool, capability_agent.crawl_tools(roots, result, captured))}
    assert set(tools) == {"list_files", "read_file", "get_facts", "submit_capabilities"}
    assert tools["submit_capabilities"].to_dict()["input_schema"]["required"] == ["capabilities"]
    assert "terraform-aws/main.tf" in tools["list_files"].call({"repo": "modules"})
    assert 'resource "aws_db_instance"' in tools["read_file"].call({"repo": "modules", "path": "terraform-aws/main.tf"})
    for bad in ("../score/.score-k8s/pg.provisioners.yaml", ".git/config"):
        try:
            tools["read_file"].call({"repo": "modules", "path": bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f"read_file allowed {bad}")
    assert tools["submit_capabilities"].to_dict()["input_schema"]["properties"]["capabilities"]["type"] == "array"
    tools["submit_capabilities"].call({"capabilities": draft["capabilities"][:1]})
    assert captured["draft"]["capabilities"][0]["id"] == "terraform-aws"
    # Some models send the list as a JSON string, or the whole {"capabilities": [...]} object as one.
    for encoded in (json.dumps(draft["capabilities"][:1]), json.dumps({"capabilities": draft["capabilities"][:1]})):
        captured.clear()
        tools["submit_capabilities"].call({"capabilities": encoded})
        assert captured["draft"]["capabilities"][0]["id"] == "terraform-aws", encoded[:40]
    try:
        tools["submit_capabilities"].call({"capabilities": [{"id": "x"}]})
    except Exception:
        pass
    else:
        raise AssertionError("an incomplete submission passed validation")

print("Capability agent checks passed: Git checkout, Terraform facts, Score bindings, LLM grounding, tool sandbox.")

# ---- Cost controls: skipped built-in provisioners, prompt caching, token usage logging ----
import logging
from types import SimpleNamespace

import anthropic

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    root = Path(tmp)
    (root / ".score-k8s").mkdir()
    (root / ".score-k8s" / "zz-default.provisioners.yaml").write_text("- huge: defaults\n", encoding="utf-8")
    (root / ".score-k8s" / "pg.provisioners.yaml").write_text("- uri: x\n", encoding="utf-8")
    list_files, read_file = capability_agent.file_tools({"score": root})
    assert "zz-default" not in list_files("score") and "pg.provisioners.yaml" in list_files("score")
    assert read_file("score", ".score-k8s/zz-default.provisioners.yaml").startswith("Skipped:")

sent: dict = {}


def fake_message(i: int, stop: str):
    usage = SimpleNamespace(input_tokens=100 * i, cache_read_input_tokens=1000 * i,
                            cache_creation_input_tokens=10, output_tokens=5)
    return SimpleNamespace(usage=usage, stop_reason=stop, stop_details=None, content=[])


class FakeStream:
    def __init__(self, message):
        self.message = message

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, *args, **kwargs):
        sent["client"] = kwargs
        runner = lambda **kw: sent.update(kw) or iter([FakeStream(fake_message(1, "tool_use")),  # noqa: E731
                                                       FakeStream(fake_message(2, "end_turn"))])
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=runner))


class Capture(logging.Handler):
    records: list = []

    def emit(self, record):
        Capture.records.append(record.getMessage())


real_client = anthropic.Anthropic
anthropic.Anthropic = FakeClient
handler = Capture()
logging.getLogger("capability_agent").addHandler(handler)
logging.getLogger("capability_agent").setLevel(logging.INFO)
try:
    capability_agent.run_claude("sys", "prompt", [], "claude-sonnet-5", max_iterations=3, label="crawl")
finally:
    anthropic.Anthropic = real_client
assert sent["cache_control"] == {"type": "ephemeral"}, sent.keys()
# A gateway timeout must never re-send and re-bill a request: no SDK retries, streamed rounds, long timeout.
assert sent["client"]["max_retries"] == 0 and sent["stream"] is True, (sent["client"], sent.get("stream"))
assert sent["client"]["timeout"].read == 900.0
assert "fallbacks" not in sent  # sonnet gets no refusal fallback
line = next(r for r in Capture.records if r.startswith("Claude crawl"))
assert "2 rounds, input 300, cache read 3000, cache write 20, output 10 tokens" in line, line

print("Capability cost controls passed: built-in provisioners skipped, prompt caching on, token usage logged.")
