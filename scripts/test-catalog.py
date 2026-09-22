#!/usr/bin/env python3
"""Offline input and safeguard tests; no provider calls, plan, or apply."""
from pathlib import Path
import copy
import ipaddress
import json
import re

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
schema = json.loads((ROOT / "schemas/application-stack-inputs.schema.json").read_text(encoding="utf-8"))

def load_tfvars(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, raw = (part.strip() for part in line.split("=", 1))
        values[key] = json.loads(raw)
    return values

def check_cidrs(values):
    for key, value in values.items():
        if "cidr" not in key:
            continue
        for cidr in value if isinstance(value, list) else [value]:
            network = ipaddress.ip_network(cidr, strict=False)
            assert str(network) != "0.0.0.0/0", key

cases = {}
for name in ("minimal", "complete", "existing-vpc"):
    values = load_tfvars(ROOT / "examples" / name / "terraform.tfvars.example")
    values["db_password"] = "test-only-value"
    if values.get("environment") == "prod" and values.get("enable_cache"):
        values["cache_auth_token"] = "test-only-token"
    jsonschema.validate(values, schema)
    check_cidrs(values)
    cases[name] = values
assert cases["complete"]["environment"] == "prod"
assert all(cases["complete"][f"enable_{name}"] for name in ("rds", "s3", "cache", "sqs"))
assert cases["existing-vpc"]["create_vpc"] is False
missing_cache_token = copy.deepcopy(cases["complete"])
missing_cache_token.pop("cache_auth_token")
assert not jsonschema.Draft202012Validator(schema).is_valid(missing_cache_token)
contract = yaml.safe_load((ROOT / "templates/application-stack/contract.yaml").read_text(encoding="utf-8"))
output_types = {item["name"]: item["type"] for item in contract["outputs"]}
assert all(output_types[name] == "list(string)" for name in ("private_subnet_ids", "database_subnet_ids", "cache_subnet_ids"))
assert all(output_types[name] == "number|null" for name in ("db_port", "cache_port"))
assert all(output_types[name] == "string|null" for name in ("db_host", "s3_bucket_name", "cache_endpoint", "queue_url"))
input_defaults = {item["name"]: item.get("default_hcl") for item in contract["inputs"]}
assert input_defaults["allow_prod_destroy"] == "false"
assert input_defaults["allow_nonprod_bucket_force_destroy"] == "false"
all_disabled = copy.deepcopy(cases["minimal"])
all_disabled.pop("db_password")
all_disabled.update({f"enable_{name}": False for name in ("rds", "s3", "cache", "sqs")})
jsonschema.validate(all_disabled, schema)
invalid = copy.deepcopy(cases["minimal"])
invalid["database_client_cidr_blocks"] = ["0.0.0.0/0"]
assert not jsonschema.Draft202012Validator(schema).is_valid(invalid)
try:
    check_cidrs(invalid)
except AssertionError:
    pass
else:
    raise AssertionError("open client CIDR accepted")
invalid["database_client_cidr_blocks"] = ["not-a-cidr"]
assert not jsonschema.Draft202012Validator(schema).is_valid(invalid)
try:
    check_cidrs(invalid)
except ValueError:
    pass
else:
    raise AssertionError("invalid CIDR accepted")

root = (ROOT / "templates/application-stack")
locals_tf = (root / "locals.tf").read_text(encoding="utf-8")
main_tf = (root / "main.tf").read_text(encoding="utf-8")
postgres_tf = (ROOT / "templates/postgres/main.tf").read_text(encoding="utf-8")
bucket_tf = (ROOT / "templates/object-storage/main.tf").read_text(encoding="utf-8")
assert 'substr(replace(var.resource_guid, "-", ""), 20, 12)' in locals_tf
assert 'trim(substr(var.stack_name, 0, 20), "-")' in locals_tf
assert '"platform.company/plane" = var.plane' in locals_tf
assert 'default_tags {' in main_tf and 'tags = local.common_tags' in main_tf
assert re.search(r'deletion_protection\s*=\s*local\.production && !var\.allow_prod_destroy', postgres_tf)
assert re.search(r'skip_final_snapshot\s*=\s*!local\.production', postgres_tf)
assert 'var.environment != "prod" && var.allow_nonprod_force_destroy' in (ROOT / "templates/object-storage/locals.tf").read_text(encoding="utf-8")
assert 'block_public_acls' in bucket_tf and 'attach_deny_insecure_transport_policy' in bucket_tf
assert re.search(r'publicly_accessible\s*=\s*false', postgres_tf)
assert not re.search(r'(?m)^\s*backend\s+"', "\n".join(path.read_text(encoding="utf-8") for path in ROOT.glob("templates/**/*.tf")))
print("Offline cases passed: minimal, complete, existing VPC, all disabled, invalid CIDRs, protection defaults, stable naming, tags, privacy.")
