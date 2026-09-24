#!/usr/bin/env python3
"""Check the capability agent against throwaway Git repositories, without network or an LLM."""
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
    tools["submit_capabilities"].call({"capabilities": draft["capabilities"][:1]})
    assert captured["draft"]["capabilities"][0]["id"] == "terraform-aws"
    try:
        tools["submit_capabilities"].call({"capabilities": [{"id": "x"}]})
    except Exception:
        pass
    else:
        raise AssertionError("an incomplete submission passed validation")

print("Capability agent checks passed: Git checkout, Terraform facts, Score bindings, LLM grounding, tool sandbox.")
