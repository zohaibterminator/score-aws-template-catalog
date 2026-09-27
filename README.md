# score-aws-template-catalog

This repository hosts the **Score capability agent**: an A2A agent that crawls the platform's Git repositories, works out what Score workloads can deploy through Terraform (RDS PostgreSQL and EKS today), returns those capabilities as a tool manifest, and executes them through score-api. See [`agents/capability-agent`](agents/capability-agent/README.md).

## Where the platform's pieces live

| What | Repository | Path |
| --- | --- | --- |
| Terraform modules | [score-tf-modules](https://github.com/zohaibterminator/score-tf-modules) (tagged; Flux source `score-provisioner-modules`) | `terraform-aws/` (RDS), `eks/` (EKS) |
| Score provisioners, state and generated manifests | [score-gp-aws-rds](https://github.com/zohaibterminator/score-gp-aws-rds) (`main`; Flux source `score-gp-aws-rds-main`) | `rds/`, `eks/` — one Score project per capability, each applied by its own Flux Kustomization |
| Request handlers | [score-api](https://github.com/zohaibterminator/score-api) | `endpoints/score`, `endpoints/eks`, `endpoints/delete-all`, `endpoints/update-aws-creds` |

The EKS template was built here and then moved to `score-tf-modules/eks`; its upstream module versions and commits are recorded in [`source-manifest.yaml`](source-manifest.yaml).

## Tests

`bash scripts/validate.sh` runs the capability agent's test suites (no network, cluster or API key needed). CI runs the same.

## Legacy material

`agents/provisioner-agent`, `docs/`, `examples/` and `schemas/` belong to the retired application-stack template flow. Its templates were removed from this repository; these files are kept for reference only and are not used by the running platform.
