# Compatibility discovery — 2026-09-22

Inspected local `score-tf-modules`, `score-gp-aws-rds`, and `score-api` without changing their files. No `versions.tf` or `.terraform.lock.hcl` was present in those repositories. `score-tf-modules/terraform-aws/main.tf` declares Terraform `>= 1.7.0` and AWS provider `~> 5.60`; there is no exact runner binary or installed provider version recorded. This catalog validated with Terraform CLI 1.10.5 and AWS provider 5.100.0 plus random 3.9.1. It has **not** been validated with the cluster's actual OpenTofu binary.

Agent policy:

```agent-policy-yaml
source_name: score-aws-template-catalog
source_namespace: flux-system
terraform_namespace: default
vault_role: tf-runner-role
vault_secret_path: secret/data/score-api/aws-creds
```

The current provisioner uses `infra.contrib.fluxcd.io/v1alpha2`, Terraform CR namespace `default` (the generated manifest omits namespace and the API deletion script queries `default`), `sourceRef.kind: GitRepository`, `sourceRef.name: score-provisioner-modules`, and `sourceRef.namespace: flux-system`. Its source path is `./terraform-aws` in the existing module repository. The current input and output Secret names are `secret-<guid>` and `tf-output-<guid>`; the CR is `rds-<guid>`. The password is created once by Score state (`adminPassword`) and passed through the input Secret's `password` key. The output Secret currently writes `host`; Score uses an encoded reference for the password in the input Secret. Existing labels and tags include `platform.company/plane`; the old module also tags `managed_by`, `resource_uid`, `environment`, and `plane`.

Runner credentials are injected by Vault role `tf-runner-role` from `secret/data/score-api/aws-creds` into `/vault/secrets/aws`; `AWS_SHARED_CREDENTIALS_FILE` points there. No static key is needed in Git. The current CR contains no resource requests or limits. The cluster's actual resource admission requirements and runner OpenTofu version were not discoverable from local files; verify them before rollout. Terraform state uses tofu-controller's Kubernetes backend and destruction is enabled with `destroyResourcesOnDeletion: true`.

## Selected compatible set

| Component | Pin | Minimum AWS provider declared upstream |
| --- | --- | --- |
| VPC | 5.21.0 | 5.79 |
| Security group | 5.3.1 | 3.29 |
| RDS | 6.13.1 | 5.92 |
| S3 bucket | 4.11.0 | 5.83 |
| ElastiCache | 1.11.1 | 5.93 |
| SQS | 4.3.1 | 4.36 |

The compatible provider intersection is AWS `>= 5.93, < 6.0`; the catalog pins the initialized selection `5.100.0`. This is inside the existing `~> 5.60` constraint. The selected modules all declare Terraform `>= 1.0`, so the existing `>= 1.7.0` baseline is sufficient. ElastiCache and RDS also use random; the catalog pins 3.9.1. Exact release source and license metadata are in `source-manifest.yaml`. These are the newest releases in the respective AWS-provider-5-compatible major lines found in Terraform Registry metadata on 2026-09-22.

## Preferred upgrade set for a later migration

Current upstream VPC 6.x, security-group 6.x, RDS 7.x, and S3 bucket 5.x require AWS provider 6; RDS 7 also requires Terraform >=1.11 and switches away from the traditional password argument. A separate, coordinated AWS provider 6 / OpenTofu-or-Terraform >=1.11 upgrade with new module majors and a credential strategy review is preferable to mixing those releases into the current AWS 5 platform. Test exact versions and provider lock files in a disposable workload first. This catalog intentionally does not change the existing runner or provider major version.

The current Flux source object cannot serve this standalone catalog path while it still points to `score-tf-modules`. Retargeting it would break `./terraform-aws` for existing RDS CRs. Create a **separate** pinned GitRepository named `score-aws-template-catalog` in `flux-system` for new CRs; retain `score-provisioner-modules` for old CRs. This is the one deliberate sourceRef-name difference from the legacy CR.
