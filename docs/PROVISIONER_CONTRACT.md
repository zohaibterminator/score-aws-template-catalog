# Score application stack provisioner

Agent policy:

```agent-policy-yaml
score_provisioner_uri: template://systems-limited/application-stack-aws-tofu-controller
terraform_kind: Terraform
secrets_mode: existing-kubernetes-secret
```

The agent reads `catalog.yaml` and every `templates/*/contract.yaml`. It generates one Score `template://` provisioner for `application-stack.aws`, using `templates/application-stack` as the only Terraform CR root. The five child templates are selected by the root's `create_vpc`, `enable_rds`, `enable_s3`, `enable_cache`, and `enable_sqs` inputs.

The provisioner belongs in `score-gp-aws-rds/.score-k8s/10-application-stack.provisioners.yaml`. `score-k8s generate` loads it from that directory. Keep the existing PostgreSQL provisioner and `.score-k8s/state.yaml` unchanged.

For each Score resource, the provisioner reuses `.Guid`, `.Uid`, and a stable `stackName` in Score state. It emits one `stack-<guid>` Terraform CR in `default`, with source `score-aws-template-catalog` in `flux-system` and path `./templates/application-stack`. Nonsecret parameters go to `vars`; sensitive values come from a pre-existing Kubernetes Secret named by `input_secret_name` through `varsFrom`. The CR writes declared nonsecret outputs to `tf-output-<guid>`. Score outputs use `encodeSecretRef` for those output keys.

Create the input Secret through an approved cluster secret workflow before using RDS or production cache. It needs `db_password` for RDS and `cache_auth_token` for production cache. The agent never writes a Secret or password into Git, Score state, or the model prompt. Keep that Secret until the Terraform CR has finished destruction, as described in `LIFECYCLE.md`.

The CR leaves `approvePlan` empty, so tofu-controller creates a plan and waits for a reviewed plan name before applying. Before Git publishing, the agent requires the SHA-256 approval code printed with the exact preview. The agent pushes only a review branch; merge and Terraform plan approval are separate operations. A separate, reviewed, tag-pinned Flux GitRepository must serve this catalog. Do not retarget the existing `score-provisioner-modules` source.

Use `agents/provisioner-agent/harness.py` to check the catalog, all contracts, and the policy blocks in `SECURITY.md`, `COMPATIBILITY.md`, and `LIFECYCLE.md` before publishing. The harness blocks automatic plan approval, direct Kubernetes Secrets, destructive override inputs, state edits, and replacement of an existing provisioner file.
