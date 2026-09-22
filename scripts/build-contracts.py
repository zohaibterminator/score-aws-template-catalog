#!/usr/bin/env python3
"""Regenerate the agent contracts from Terraform declarations and catalog metadata."""
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {
    "vpc": ("terraform-aws-modules/vpc/aws", "5.21.0"),
    "security-group": ("terraform-aws-modules/security-group/aws", "5.3.1"),
    "rds": ("terraform-aws-modules/rds/aws", "6.13.1"),
    "s3-bucket": ("terraform-aws-modules/s3-bucket/aws", "4.11.0"),
    "elasticache": ("terraform-aws-modules/elasticache/aws", "1.11.1"),
    "sqs": ("terraform-aws-modules/sqs/aws", "4.3.1"),
}
INFO = {
    "network": {
        "purpose": "New four-tier VPC or references to an existing VPC and subnets",
        "modules": ["vpc"], "dependencies": [],
        "optional_components": ["new_vpc", "nat_gateway"],
        "destructive": ["VPC and four subnet tiers", "NAT gateways and elastic IPs"],
        "teardown": ["NAT gateway deletion", "ENIs can hold subnet and VPC deletion"],
        "connectivity": ["Existing VPC routing remains caller managed", "Kubernetes clients may need peering, Transit Gateway, or VPN"],
        "safeguards": ["Two AZs in create mode", "No open ingress", "NAT disabled unless requested"],
        "iam": {"ec2": ["Describe*", "CreateVpc", "DeleteVpc", "CreateSubnet", "DeleteSubnet", "CreateRouteTable", "DeleteRouteTable", "CreateRoute", "DeleteRoute", "CreateNatGateway", "DeleteNatGateway", "AllocateAddress", "ReleaseAddress", "CreateInternetGateway", "DeleteInternetGateway", "CreateTags", "DeleteTags"]},
    },
    "postgres": {
        "purpose": "Private encrypted PostgreSQL RDS with DB security group",
        "modules": ["security-group", "rds"], "dependencies": ["network"],
        "optional_components": [],
        "destructive": ["RDS instance", "DB subnet group", "database security group"],
        "teardown": ["Production deletion protection must be disabled in a reviewed update", "Final snapshot on production destroy"],
        "connectivity": ["Caller supplied client CIDRs must be routable to database subnets"],
        "safeguards": ["Private endpoint", "Encrypted storage", "Production Multi-AZ, backups, deletion protection and final snapshot"],
        "iam": {"rds": ["Describe*", "CreateDBInstance", "ModifyDBInstance", "DeleteDBInstance", "CreateDBSubnetGroup", "ModifyDBSubnetGroup", "DeleteDBSubnetGroup", "CreateDBParameterGroup", "ModifyDBParameterGroup", "DeleteDBParameterGroup", "CreateDBOptionGroup", "DeleteDBOptionGroup", "AddTagsToResource", "RemoveTagsFromResource"], "ec2": ["CreateSecurityGroup", "DeleteSecurityGroup", "AuthorizeSecurityGroupIngress", "RevokeSecurityGroupIngress", "DescribeSecurityGroups", "CreateTags"]},
    },
    "object-storage": {
        "purpose": "Private versioned S3 bucket with encryption and TLS-only access",
        "modules": ["s3-bucket"], "dependencies": [],
        "optional_components": [],
        "destructive": ["S3 bucket and its objects if force destroy is explicitly enabled in nonproduction"],
        "teardown": ["Non-empty versioned buckets must be emptied before default destruction"],
        "connectivity": ["Kubernetes clients need S3 access through NAT, VPC endpoint, or existing egress"],
        "safeguards": ["Public access blocked", "SSE-S3", "TLS-only policy", "Production force destroy disabled"],
        "iam": {"s3": ["CreateBucket", "DeleteBucket", "GetBucket*", "PutBucket*", "ListBucket", "ListBucketVersions", "DeleteObject", "DeleteObjectVersion", "PutBucketTagging"]},
    },
    "cache": {
        "purpose": "Private encrypted Redis or Valkey replication group and security group",
        "modules": ["security-group", "elasticache"], "dependencies": ["network"],
        "optional_components": ["auth_token"],
        "destructive": ["ElastiCache replication group", "cache subnet group", "cache security group"],
        "teardown": ["ElastiCache deletion and final snapshots can take many minutes"],
        "connectivity": ["Caller supplied client CIDRs must be routable to cache subnets", "Clients must use TLS"],
        "safeguards": ["Private endpoints", "At-rest and transit encryption", "Production auth token, Multi-AZ and snapshots"],
        "iam": {"elasticache": ["Describe*", "CreateReplicationGroup", "ModifyReplicationGroup", "DeleteReplicationGroup", "CreateCacheSubnetGroup", "ModifyCacheSubnetGroup", "DeleteCacheSubnetGroup", "AddTagsToResource", "RemoveTagsFromResource"], "ec2": ["CreateSecurityGroup", "DeleteSecurityGroup", "AuthorizeSecurityGroupIngress", "RevokeSecurityGroupIngress", "DescribeSecurityGroups", "CreateTags"]},
    },
    "queue": {
        "purpose": "Encrypted SQS queue with dead-letter queue",
        "modules": ["sqs"], "dependencies": [],
        "optional_components": [],
        "destructive": ["Main queue and dead-letter queue, including undelivered messages"],
        "teardown": ["Drain or archive messages before deletion"],
        "connectivity": ["Clients need AWS SQS API egress or an existing VPC endpoint"],
        "safeguards": ["SQS-managed encryption on both queues", "Redrive to DLQ"],
        "iam": {"sqs": ["CreateQueue", "DeleteQueue", "GetQueueAttributes", "SetQueueAttributes", "GetQueueUrl", "ListQueueTags", "TagQueue", "UntagQueue"]},
    },
}
INFO["application-stack"] = {
    "purpose": "Primary tofu-controller root composing all application infrastructure in one state",
    "modules": list(VERSIONS), "dependencies": list(INFO),
    "optional_components": ["rds", "s3", "cache", "sqs", "new_vpc", "nat_gateway"],
    "destructive": ["All enabled resources in the stack, including network resources owned by this state"],
    "teardown": ["Input Secret must outlive Terraform CR", "Production RDS deletion protection and final snapshot", "Non-empty S3 buckets", "ElastiCache and NAT deletion time", "ENIs blocking subnet/VPC deletion"],
    "connectivity": ["The Kubernetes cluster is not assumed to be in the created VPC", "Supply restricted client CIDRs plus peering, Transit Gateway, VPN, or equivalent private routing", "S3 and SQS clients need AWS API egress"],
    "safeguards": ["One state and root provider", "Production RDS deletion protection and final snapshot", "Production S3 force destroy disabled", "Production cache requires auth token", "No default database or cache ingress", "No plaintext password output"],
    "iam": {service: sorted(set(actions for info in INFO.values() for actions in info["iam"].get(service, []))) for service in ("ec2", "rds", "s3", "elasticache", "sqs")},
}

TYPES = {
    "vpc_id": "string", "public_subnet_ids": "list(string)", "private_subnet_ids": "list(string)",
    "database_subnet_ids": "list(string)", "cache_subnet_ids": "list(string)",
    "db_port": "number|null", "cache_port": "number|null", "port": "number",
}
for name in ("db_host", "db_name", "db_username", "s3_bucket_name", "s3_bucket_arn", "cache_endpoint", "queue_url", "queue_arn", "dead_letter_queue_url"):
    TYPES[name] = "string|null"

def blocks(source: str, kind: str):
    pattern = re.compile(rf'{kind} "([^"]+)"\s*\{{')
    for match in pattern.finditer(source):
        level, quoted, escaped = 1, False, False
        index = match.end()
        while level and index < len(source):
            char = source[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                level += 1
            elif char == "}":
                level -= 1
            index += 1
        yield match.group(1), source[match.end():index-1]

def attribute(body: str, key: str):
    match = re.search(rf'(?m)^\s*{key}\s*=\s*([^\n]+)', body)
    return match.group(1).strip() if match else None

def template_contract(name: str):
    info = INFO[name]
    folder = ROOT / "templates" / name
    inputs = []
    for input_name, body in blocks((folder / "variables.tf").read_text(encoding="utf-8"), "variable"):
        default = attribute(body, "default")
        validation = re.findall(r'error_message\s*=\s*"([^"]+)"', body)
        entry = {"name": input_name, "type": attribute(body, "type"), "required": default is None, "sensitive": attribute(body, "sensitive") == "true", "validations": validation}
        if default is not None:
            entry["default_hcl"] = default
        inputs.append(entry)
    outputs = []
    for output_name, body in blocks((folder / "outputs.tf").read_text(encoding="utf-8"), "output"):
        output_type = TYPES.get(output_name, "string")
        if name != "application-stack" and output_type.endswith("|null"):
            output_type = output_type.removesuffix("|null")
        outputs.append({"name": output_name, "type": output_type, "sensitive": attribute(body, "sensitive") == "true"})
    return {
        "template_name": name,
        "template_path": f"./templates/{name}",
        "purpose": info["purpose"],
        "template_version": "1.0.0",
        "required_terraform_version": ">= 1.7.0, < 2.0.0",
        "required_aws_provider_version": "= 5.100.0",
        "required_random_provider_version": "= 3.9.1" if name in ("postgres", "cache", "application-stack") else None,
        "upstream_modules": [{"address": VERSIONS[module][0], "version": VERSIONS[module][1]} for module in info["modules"]],
        "inputs": inputs,
        "outputs": outputs,
        "optional_components": info["optional_components"],
        "dependencies": info["dependencies"],
        "estimated_destructive_operations": info["destructive"],
        "special_teardown_resources": info["teardown"],
        "score_resource_type": "application-stack",
        "score_class": "aws",
        "direct_terraform_cr_root": name == "application-stack",
        "terraform_cr_source_path": "./templates/application-stack",
        "suggested_input_secret_name": "secret-<guid>",
        "suggested_output_secret_name": "tf-output-<guid>",
        "required_iam_actions": info["iam"],
        "connectivity_assumptions": info["connectivity"],
        "production_safeguards": info["safeguards"],
    }

def main():
    for name in INFO:
        path = ROOT / "templates" / name / "contract.yaml"
        path.write_text(yaml.safe_dump(template_contract(name), sort_keys=False, allow_unicode=True), encoding="utf-8")
    catalog = {
        "catalog_version": "1.0.0",
        "default_template": "application-stack",
        "recommended_score_resource": {"type": "application-stack", "class": "aws"},
        "templates": [{"name": name, "path": f"./templates/{name}", "contract": f"./templates/{name}/contract.yaml", "purpose": info["purpose"], "terraform_cr_source_path": "./templates/application-stack", "composition": "root" if name == "application-stack" else "child"} for name, info in INFO.items()],
    }
    (ROOT / "catalog.yaml").write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")

if __name__ == "__main__":
    main()
