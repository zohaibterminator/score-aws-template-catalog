#!/usr/bin/env python3
"""Check contract schema and drift against Terraform declarations."""
from pathlib import Path
import json
import re
import sys

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
schema = json.loads((ROOT / "schemas/template-contract.schema.json").read_text(encoding="utf-8"))
catalog = yaml.safe_load((ROOT / "catalog.yaml").read_text(encoding="utf-8"))
expected = {"network", "postgres", "object-storage", "cache", "queue", "application-stack"}
assert {item["name"] for item in catalog["templates"]} == expected
for item in catalog["templates"]:
    folder = ROOT / item["path"]
    contract = yaml.safe_load((folder / "contract.yaml").read_text(encoding="utf-8"))
    jsonschema.validate(contract, schema)
    assert contract["template_name"] == item["name"]
    assert contract["template_path"] == item["path"]
    assert contract["direct_terraform_cr_root"] == (item["name"] == "application-stack")
    assert contract["terraform_cr_source_path"] == "./templates/application-stack"
    variables = set(re.findall(r'(?m)^variable "([^"]+)"', (folder / "variables.tf").read_text(encoding="utf-8")))
    outputs = set(re.findall(r'(?m)^output "([^"]+)"', (folder / "outputs.tf").read_text(encoding="utf-8")))
    assert {entry["name"] for entry in contract["inputs"]} == variables, item["name"]
    assert {entry["name"] for entry in contract["outputs"]} == outputs, item["name"]
    for module in contract["upstream_modules"]:
        assert re.fullmatch(r"\d+\.\d+\.\d+", module["version"])
root = yaml.safe_load((ROOT / "templates/application-stack/contract.yaml").read_text(encoding="utf-8"))
manifest = yaml.safe_load((ROOT / "source-manifest.yaml").read_text(encoding="utf-8"))
declared_sources = {(entry["module_registry_address"], entry["exact_version"]) for entry in manifest["sources"] if entry["module_registry_address"]}
assert declared_sources == {(entry["address"], entry["version"]) for entry in root["upstream_modules"]}
assert all(entry["files_copied"] == [] and entry["use"] == "referenced" for entry in manifest["sources"])
assert root["score_resource_type"] == "application-stack" and root["score_class"] == "aws"
assert {x["name"] for x in root["outputs"]} == {"stack_name", "vpc_id", "private_subnet_ids", "database_subnet_ids", "cache_subnet_ids", "db_host", "db_port", "db_name", "db_username", "s3_bucket_name", "s3_bucket_arn", "cache_endpoint", "cache_port", "queue_url", "queue_arn", "dead_letter_queue_url"}
assert {x["name"] for x in root["inputs"] if x["sensitive"]} == {"db_password", "cache_auth_token"}
print("Contract schema and Terraform interface checks passed for six templates.")
