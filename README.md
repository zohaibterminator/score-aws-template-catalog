# Score AWS template catalog

Reusable, pinned Terraform modules for the existing Score → Flux → tofu-controller platform. `templates/application-stack` is the single tofu-controller root and one state. The other five templates are child modules and can also be initialized independently for validation. No AWS or Kubernetes deployment is performed by this repository.

Start with [`catalog.yaml`](catalog.yaml), then the selected [`contract.yaml`](templates/application-stack/contract.yaml). The recommended Score resource is `application-stack.aws`. An agent should use [`docs/PROVISIONER_CONTRACT.md`](docs/PROVISIONER_CONTRACT.md) to write a provisioner later. This catalog does not create one automatically.

## Inputs and use

The root requires a stable `stack_name`, Score `resource_guid`, `workload`, `environment`, `plane`, `region`, and the Score-created `db_password` when RDS is enabled. Supply the password through the prune-protected input Secret; never put it in tfvars. New-VPC mode requires two or more AZs and four CIDR lists. Existing-VPC mode requires the VPC ID and database, cache, and application subnet IDs. Both modes require explicit routable client CIDRs for enabled RDS/cache. NAT is optional and off by default.

`examples/minimal`, `examples/complete`, and `examples/existing-vpc` provide non-secret example inputs. The existing Kubernetes cluster is not assumed to share the new VPC: arrange VPC peering, Transit Gateway, VPN, or other private routing and DNS before using private endpoints.

## Validation

Install `requirements-dev.txt`, then run `bash scripts/validate.sh` on a machine with OpenTofu or Terraform. It initializes with `-backend=false`, validates the root and leaf templates, checks contracts, and runs tflint/checkov if installed. It never plans or applies. CI runs the same checks. The latest local results and limits are in [`docs/VALIDATION.md`](docs/VALIDATION.md). The root `.terraform.lock.hcl` pins AWS 5.100.0 and random 3.9.1; the registry modules are pinned in source and listed in [`source-manifest.yaml`](source-manifest.yaml).

## Boundaries

The current RDS state remains separate. No backend is configured here; tofu-controller keeps its Kubernetes backend. No static AWS credentials are present. Use a separate pinned Flux GitRepository artifact for this catalog while `score-provisioner-modules` continues to serve existing RDS workloads. See [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md), [`docs/LIFECYCLE.md`](docs/LIFECYCLE.md), and [`docs/MIGRATION.md`](docs/MIGRATION.md).

The repository's original wrapper code and documentation are MIT licensed. All six referenced upstream modules are Apache-2.0 and are downloaded by Terraform; no upstream source is vendored.
