"""Fail closed when catalog, docs, or Score provisioner drift from policy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

import jsonschema
import yaml

from score_provisioner import ROOT, build_provisioner, load_catalog


def policies() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("PROVISIONER_CONTRACT", "SECURITY", "COMPATIBILITY", "LIFECYCLE"):
        document = (ROOT / "docs" / f"{name}.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```agent-policy-yaml\n(.*?)\n```", document, flags=re.S)
        if len(blocks) != 1:
            raise ValueError(f"docs/{name}.md must have exactly one agent policy block.")
        policy = yaml.safe_load(blocks[0])
        if not isinstance(policy, dict) or set(result) & set(policy):
            raise ValueError(f"Invalid or duplicate agent policy in docs/{name}.md.")
        result.update(policy)
    return result


def check() -> tuple[str, dict[str, Any]]:
    catalog, root, children = load_catalog()
    policy = policies()
    schema = json.loads((ROOT / "schemas" / "template-contract.schema.json").read_text(encoding="utf-8"))
    entries = catalog["templates"]
    if catalog["default_template"] != "application-stack" or len(children) != 5:
        raise ValueError("Catalog composition changed; review the agent mapping.")
    if {item["name"] for item in children} != {"network", "postgres", "object-storage", "cache", "queue"}:
        raise ValueError("Child template set changed; review the agent mapping.")
    for item in entries:
        contract = yaml.safe_load((ROOT / item["contract"]).read_text(encoding="utf-8"))
        jsonschema.validate(contract, schema)
        if contract["template_name"] != item["name"] or contract["template_path"] != item["path"]:
            raise ValueError(f"Catalog/contract mismatch for {item['name']}.")
        if contract["terraform_cr_source_path"] != root["terraform_cr_source_path"]:
            raise ValueError(f"Terraform root path mismatch for {item['name']}.")
        if not (ROOT / item["path"]).is_dir():
            raise ValueError(f"Missing template directory: {item['name']}.")

    if root["score_resource_type"] != "application-stack" or root["score_class"] != "aws":
        raise ValueError("Score resource identity changed.")
    if root["dependencies"] != [item["name"] for item in children]:
        raise ValueError("Root dependencies differ from catalog children.")
    if root["direct_terraform_cr_root"] is not True:
        raise ValueError("Application stack is no longer a direct Terraform root.")
    if policy["publish_repo"] != "application-stack" or not policy["publish_file"].startswith(".score-k8s/"):
        raise ValueError("Publishing destination changed outside reviewed policy.")
    if policy["allow_existing_provisioner_replacement"] is not False or policy["allow_state_file_edits"] is not False:
        raise ValueError("Lifecycle policy would allow unsafe replacement or state edits.")
    if policy["approve_plan"] != "" or policy["forbidden_manifest_kinds"] != ["Secret"]:
        raise ValueError("Security policy must require manual plan approval and no Git Secret.")
    if policy["secrets_mode"] != "existing-kubernetes-secret" or policy["terraform_kind"] != "Terraform":
        raise ValueError("Provisioner contract policy changed.")

    rendered = build_provisioner()
    provisioners = yaml.safe_load(rendered)
    if len(provisioners) != 1:
        raise ValueError("Expected one Score provisioner.")
    provisioner = provisioners[0]
    if provisioner["type"] != root["score_resource_type"] or provisioner["class"] != root["score_class"]:
        raise ValueError("Score provisioner identity differs from root contract.")
    if provisioner["uri"] != policy["score_provisioner_uri"]:
        raise ValueError("Score provisioner URI differs from blueprint.")
    if not provisioner["uri"].startswith("template://"):
        raise ValueError("Only Score template provisioners are allowed.")
    expected_params = {item["name"] for item in root["inputs"] if not item["sensitive"]}
    expected_params -= {"resource_guid", "workload"}
    expected_params -= set(policy["forbidden_terraform_inputs"])
    expected_params.add("input_secret_name")
    if set(provisioner["supported_params"]) != expected_params:
        raise ValueError("Score parameters differ from the application-stack contract.")
    for forbidden in policy["forbidden_request_fields"] + policy["forbidden_terraform_inputs"]:
        if forbidden in provisioner["supported_params"]:
            raise ValueError(f"Unsafe Score parameter: {forbidden}.")
    manifest = provisioner["manifests"]
    required = [
        "kind: Terraform", "approvePlan: \"\"", "path: " + root["terraform_cr_source_path"],
        "name: " + policy["source_name"], "namespace: " + policy["source_namespace"],
        "namespace: " + policy["terraform_namespace"],
        "vault.hashicorp.com/role: " + policy["vault_role"],
        policy["vault_secret_path"], "varsFrom:", "writeOutputsToSecret:",
        "k8s.score.dev/resource-uid", ".Guid", ".State.stackName",
    ]
    if any(token not in manifest for token in required):
        raise ValueError("Generated manifest is missing a compatibility or security control.")
    if any(re.search(rf"(?m)^  kind: {re.escape(kind)}$", manifest) for kind in policy["forbidden_manifest_kinds"]):
        raise ValueError("A forbidden Kubernetes manifest kind would be generated.")
    if "approvePlan: auto" in manifest or "forceUnlock" in manifest or "kubectl" in manifest:
        raise ValueError("Provisioner contains automatic apply or cluster control.")
    if "db_password" not in manifest or "cache_auth_token" not in manifest:
        raise ValueError("Sensitive Terraform inputs must come from a referenced Secret.")
    return rendered, policy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", action="store_true", dest="print_yaml")
    args = parser.parse_args()
    try:
        rendered, _ = check()
    except (ValueError, jsonschema.ValidationError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(rendered if args.print_yaml else "Provisioner guardrails passed.", end="" if args.print_yaml else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
