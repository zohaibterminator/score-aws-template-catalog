#!/usr/bin/env python3
"""Evaluate stable root expressions with Terraform/OpenTofu console, without planning."""
from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TF = sys.argv[1]
WORKDIR = ROOT / "templates/application-stack"

def evaluate(case, expression):
    example = ROOT / "examples" / case / "terraform.tfvars.example"
    result = subprocess.run(
        [TF, "console", f"-var-file={example}", "-no-color"],
        input=expression + "\n", text=True, capture_output=True, cwd=WORKDIR, check=True
    )
    return json.loads(result.stdout.strip())

assert evaluate("minimal", "local.aws_name") == "orders-000000000001"
assert evaluate("complete", "local.aws_name") == "orders-000000000002"
assert evaluate("existing-vpc", "local.aws_name") == "orders-000000000003"
assert evaluate("complete", 'local.common_tags["platform.company/plane"]') == "application"
assert evaluate("complete", 'local.common_tags["Environment"]') == "prod"
assert evaluate("complete", "var.allow_prod_destroy") is False
assert evaluate("complete", "var.allow_nonprod_bucket_force_destroy") is False
print("Console checks passed: stable names, required tags, production deletion defaults.")
