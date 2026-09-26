#!/usr/bin/env python3
"""Check contract schema and drift against Terraform declarations for every template in catalog.yaml."""
from pathlib import Path
import json
import re

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
schema = json.loads((ROOT / "schemas/template-contract.schema.json").read_text(encoding="utf-8"))
catalog = yaml.safe_load((ROOT / "catalog.yaml").read_text(encoding="utf-8"))
manifest = yaml.safe_load((ROOT / "source-manifest.yaml").read_text(encoding="utf-8"))
declared_sources = {(e["module_registry_address"], e["exact_version"]) for e in manifest["sources"] if e["module_registry_address"]}
assert all(e["files_copied"] == [] and e["use"] == "referenced" for e in manifest["sources"])

names = [item["name"] for item in catalog["templates"]]
assert catalog["default_template"] in names
for item in catalog["templates"]:
    folder = ROOT / item["path"]
    contract = yaml.safe_load((folder / "contract.yaml").read_text(encoding="utf-8"))
    jsonschema.validate(contract, schema)
    assert contract["template_name"] == item["name"]
    assert contract["template_path"] == item["path"]
    assert contract["direct_terraform_cr_root"] == (item["composition"] == "root"), item["name"]
    assert contract["terraform_cr_source_path"] == item["terraform_cr_source_path"], item["name"]
    variables = set(re.findall(r'(?m)^variable "([^"]+)"', (folder / "variables.tf").read_text(encoding="utf-8")))
    outputs = set(re.findall(r'(?m)^output "([^"]+)"', (folder / "outputs.tf").read_text(encoding="utf-8")))
    assert {entry["name"] for entry in contract["inputs"]} == variables, item["name"]
    assert {entry["name"] for entry in contract["outputs"]} == outputs, item["name"]
    # Every upstream module is pinned exactly, declared in the source manifest, and pinned the same way in the code.
    code = "".join(p.read_text(encoding="utf-8") for p in folder.glob("*.tf"))
    for module in contract["upstream_modules"]:
        assert re.fullmatch(r"\d+\.\d+\.\d+", module["version"])
        assert (module["address"], module["version"]) in declared_sources, (item["name"], module)
    for address, version in re.findall(r'module\s+"[^"]+"\s*\{\s*source\s*=\s*"([^"./][^"]*/aws)"\s*version\s*=\s*"([^"]+)"', code):
        assert {"address": address, "version": version} in contract["upstream_modules"], (item["name"], address, version)
    pinned_aws = re.search(r'aws\s*=\s*\{\s*source\s*=\s*"hashicorp/aws"\s*version\s*=\s*"([^"]+)"', code)
    assert pinned_aws and pinned_aws.group(1) == contract["required_aws_provider_version"], item["name"]
    provisioner = item.get("score_provisioner")
    if provisioner:
        entries = yaml.safe_load((ROOT / provisioner).read_text(encoding="utf-8"))
        assert any(e["type"] == contract["score_resource_type"] and e["class"] == contract["score_class"] for e in entries)
        manifests = "".join(e["manifests"] for e in entries)
        assert f"path: {item['terraform_cr_source_path']}" in manifests and "approvePlan: auto" in manifests

if "application-stack" in names:
    root = yaml.safe_load((ROOT / "templates/application-stack/contract.yaml").read_text(encoding="utf-8"))
    assert root["score_resource_type"] == "application-stack" and root["score_class"] == "aws"
    assert {x["name"] for x in root["inputs"] if x["sensitive"]} == {"db_password", "cache_auth_token"}
print(f"Contract schema and Terraform interface checks passed for {len(names)} template(s): {', '.join(names)}.")
