# Quick runbook

**Current status:** This is a validated template catalog, not a deployed stack. Test integration with a new disposable workload. Leave existing RDS state alone, and never run `score-k8s init` against the existing workload repository.

## How it works

```text
Score resource (application-stack.aws)
  → input Secret + Terraform CR in Git
  → Flux applies them
  → tofu-controller runs templates/application-stack
  → AWS resources are created in one Terraform state
  → output Secret supplies endpoints back to Score
```

`application-stack` is the only root used by the Terraform CR. It calls five child templates: `network` (VPC/subnets/NAT or existing network), `postgres` (private RDS), `object-storage` (private S3), `cache` (Redis/Valkey), and `queue` (SQS and DLQ). RDS, S3, cache, and SQS can each be switched on or off. The stack uses the existing Vault runner credentials and tofu-controller Kubernetes state.

## What the files are for

| Location | Purpose |
| --- | --- |
| `catalog.yaml` | Short index for an agent choosing a template. |
| `templates/*/main.tf`, `variables.tf`, `outputs.tf`, `locals.tf`, `versions.tf` | Resources, inputs, outputs, names, and pinned provider versions. |
| `templates/*/contract.yaml` | Exact machine-readable inputs, outputs, dependencies, and safeguards. |
| `templates/*/.terraform.lock.hcl` | Exact downloaded provider versions and checksums. |
| `templates/*/README.md`, `terraform.tfvars.example` | Template guidance and non-secret sample inputs. |
| `examples/` | Minimal, full, existing-VPC, and controller examples. |
| `schemas/` | Rules for contracts and application inputs. |
| `scripts/`, `.github/workflows/validate.yaml`, `requirements-dev.txt` | Local checks, CI checks, and Python check dependencies. |
| `source-manifest.yaml`, `renovate.json`, `LICENSE`, `.gitignore`, `.gitattributes`, `AGENTS.md` | Upstream attribution, update policy, licensing, Git hygiene, and agent rules. |
| `docs/` | Detailed compatibility, security, deletion, migration, integration, and validation notes. |

The `.terraform/` folders are **downloaded module caches** created by `terraform init`. They are ignored by Git. Do not edit them; edit the files directly under `templates/` instead.

## Prepare a new stack

1. Choose `examples/minimal`, `examples/complete`, or `examples/existing-vpc`. Set a stable `stack_name`, Score GUID, workload, environment, plane, and AWS region.
2. For a new VPC, provide two or more AZs and public, private, database, and cache subnet CIDRs. For an existing VPC, provide its ID and subnet IDs. Supply **routable, restricted client CIDRs** for enabled database/cache access. The Kubernetes cluster may need peering, Transit Gateway, or VPN to reach those private endpoints.
3. Have a later agent generate the Score provisioner from `catalog.yaml`, the root `contract.yaml`, and `PROVISIONER_CONTRACT.md`. It must reuse the Score-created database password through `secret-<guid>`; a production cache also needs a stable secret token. Keep secrets out of Git.
4. Create a separate, tag-pinned Flux GitRepository for this catalog. Keep the old `score-provisioner-modules` source for existing RDS CRs. Render and review the new manifests before using Flux.

## Check and operate

Install `requirements-dev.txt`, then run `bash scripts/validate.sh` in Git Bash or another Bash shell with `tofu` or `terraform` installed. This formats/checks, initializes without a backend, and validates all six templates. It does **not** plan, apply, or deploy. Results and untested areas are in `VALIDATION.md`.

For deletion, remove the Terraform CR from the Git branch Flux watches, but keep its prune-protected input Secret until the CR has disappeared. First disable production RDS deletion protection through a reviewed update and empty any non-empty S3 bucket. Allow roughly 45–60 minutes and verify each AWS service. Do not strip the finalizer to force deletion; that can leave the stack behind. Follow `LIFECYCLE.md` for the full order.
