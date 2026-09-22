# Catalog agent instructions

Read `catalog.yaml`, then the selected template's `contract.yaml` and `docs/PROVISIONER_CONTRACT.md` before producing a Score provisioner. Treat contracts as an API: preserve output names and types. Never run `score-k8s init` against an existing workload repository. Do not modify existing RDS state, generate or commit credentials, deploy, or apply infrastructure without explicit user instruction. Keep the input Secret prune protected until its Terraform CR is gone. Pin the Flux source to a reviewed Git tag before use.
