"""Throwaway Git repositories shared by the capability agent tests."""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents/capability-agent"))

MAIN_TF = """
terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}
provider "aws" { region = var.region }
locals {
  suffix  = substr(var.resource_guid, 0, 8)
  db_name = "appdb"
}
resource "aws_db_instance" "this" {
  identifier              = "score-${local.suffix}"
  engine                  = "postgres"
  db_name                 = local.db_name
  instance_class          = var.instance_class
  allocated_storage       = tonumber(var.storage_gb)
  multi_az                = var.environment == "prod"
  backup_retention_period = var.environment == "prod" ? 7 : 0
  publicly_accessible     = var.publicly_accessible
  password                = var.password
}
variable "region" { default = "us-east-1" }
variable "resource_guid" { type = string }
variable "instance_class" { default = "db.t3.micro" }
variable "environment" { default = "dev" }
variable "storage_gb" {
  type    = string
  default = "20"
  validation {
    condition     = tonumber(var.storage_gb) <= 20
    error_message = "Free tier allows at most 20 GB."
  }
}
variable "publicly_accessible" {
  type    = bool
  default = false
}
variable "password" {
  type      = string
  sensitive = true
}
output "host" { value = aws_db_instance.this.address }
"""

PROVISIONER = """
- uri: template://test/postgres
  type: postgres
  class: aws-terraform
  init: |
    region: {{ .Params.region | default "us-east-1" }}
    environment: {{ .Params.environment | default "dev" }}
    instanceClass: {{ .Params.instance_class | default "db.t3.micro" }}
    storageGb: "{{ .Params.storage_gb | default "20" }}"
  state: |
    dbName: appdb
  outputs: |
    host: {{ encodeSecretRef (printf "tf-output-%s" .Guid) "host" }}
    name: {{ .State.dbName }}
  manifests: |
    - apiVersion: infra.contrib.fluxcd.io/v1alpha2
      kind: Terraform
      spec:
        path: ./terraform-aws
        sourceRef:
          kind: GitRepository
          name: modules
        runnerPodTemplate:
          spec:
            env:
              - name: AWS_REGION
                value: us-east-1
        vars:
          - name: resource_guid
            value: {{ .Guid | quote }}
          - name: region
            value: {{ .Init.region | quote }}
          - name: environment
            value: {{ .Init.environment | quote }}
          - name: instance_class
            value: {{ .Init.instanceClass | quote }}
          - name: storage_gb
            value: {{ .Init.storageGb | quote }}
          - name: publicly_accessible
            value: "true"
          - name: not_declared
            value: "x"
        varsFrom:
          - kind: Secret
            name: secret-{{ .Guid }}
            varsKeys:
              - password
"""


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


def commit(root: Path, files: dict[str, str], message: str = "fixture") -> None:
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


def make_repo(root: Path, files: dict[str, str], tag: str) -> None:
    root.mkdir(parents=True)
    git(root, "init", "-q")
    commit(root, files)
    git(root, "tag", tag)


def make_fixture(tmp: Path) -> tuple[Path, Path]:
    make_repo(tmp / "modules", {"terraform-aws/main.tf": MAIN_TF}, "v1.0.0")
    make_repo(tmp / "score", {".score-k8s/pg.provisioners.yaml": PROVISIONER}, "v1")
    return tmp / "modules", tmp / "score"
