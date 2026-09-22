# Stack lifecycle and deletion runbook

The input Secret has `kustomize.toolkit.fluxcd.io/prune: disabled`. Flux may remove the Terraform CR from `generated-latest`, but the Secret must remain while tofu-controller destroys AWS resources. If it is pruned first, the runner can fail with `VarsGenerationFailed`. Delete `secret-<guid>` **only after** `stack-<guid>` has disappeared and AWS verification is complete; then remove `tf-output-<guid>`. The current generated RDS manifest lacks this prune annotation even though the provisioner template has it, so inspect actual generated output before any future rollout.

Deletion order:

1. Stop producers and consumers, drain or archive SQS messages, and preserve needed S3 data and RDS snapshots.
2. For production RDS, review a CR update that sets `allow_prod_destroy=true` so Terraform disables AWS deletion protection. Wait for reconciliation before deleting the CR. A production final snapshot is configured; verify its identifier and retention policy.
3. Empty versioned S3 buckets (objects, versions, delete markers, multipart uploads) unless a reviewed nonproduction force destroy is intentionally enabled. A non-empty production bucket will block destruction.
4. Remove the Terraform CR manifest from the Git source branch that Flux reconciles. Confirm no other Kustomization or generated manifest still contains it; otherwise Flux can resurrect the CR.
5. Keep the input Secret. Wait for the tofu-controller finalizer and Terraform destroy. Check conditions, runner logs, and state locks. Resolve a stale Kubernetes backend lock only after proving that no runner is active; `spec.force`/force-unlock is an exceptional recovery step and must not race an active destroy.
6. Verify RDS, ElastiCache, S3, SQS/DLQ, security groups, NAT gateways, ENIs, subnets, and VPC individually in AWS. ElastiCache and NAT deletion can take many minutes; ENIs can hold subnets and VPC, and queue/security-group dependencies can delay cleanup.
7. When the CR is gone and AWS deletion is verified, delete the input and output Secrets and retire the Score state entry through the normal lifecycle.

Do not automatically strip the Terraform finalizer from a complete application-stack CR. That skips Terraform destruction and can orphan RDS, Redis/Valkey, S3, SQS, NAT gateways, security groups, and VPC resources. The current API deletion script has an RDS-only emergency path that may remove the finalizer; do not reuse it for full stacks. The current 20-minute wait is likely too short: make the stack deletion wait configurable, with a 45–60 minute starting range and per-service AWS verification. A larger single root state increases the impact of a stale Kubernetes backend lock, though this catalog does not change the backend strategy.
