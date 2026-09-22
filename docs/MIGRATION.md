# Existing RDS migration boundary

Existing `postgres.aws-terraform` resources retain their Score GUID, `rds-<guid>` Terraform CR, Kubernetes backend state, and current RDS instance. The new `application-stack.aws` resource is a **different Score identity** and creates a separate `stack-<guid>` CR and state. Do not switch an existing workload automatically: doing so may create a second database and later destroy the old one.

Test the catalog first with a new disposable workload and a fresh GUID. Leave all existing RDS CRs and `score-provisioner-modules` source intact. A future migration must include an explicit Terraform import or state-move plan, ownership handoff, rollback plan, database endpoint/cutover plan, and confirmation that no two states claim the same AWS resource. No import, move, or destroy is performed here. Never run `score-k8s init` against the existing repository; that can change resource identity.
