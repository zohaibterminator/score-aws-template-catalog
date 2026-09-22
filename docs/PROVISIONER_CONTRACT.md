# Blueprint for `application-stack.aws`

This is an exact input/output blueprint for a later agent, not an applied provisioner. Select `application-stack` from `catalog.yaml` and read `templates/application-stack/contract.yaml`. Keep the existing Score state file and GUID behavior. Use `type: application-stack`, `class: aws`. A new resource must produce exactly one input Secret `secret-<guid>`, one Terraform CR `stack-<guid>`, and one controller-written output Secret `tf-output-<guid>` in namespace `default`.

Create Score state with a stable `stackName` and reuse it on every generate. Generate `adminPassword` once in Score state when RDS is enabled, following the existing PostgreSQL provisioner pattern. Map it to the Secret key `db_password`; do not generate another password in Terraform. For a production cache, generate a distinct stable token once in Score state and map it to the sensitive Secret key `cache_auth_token`. The Secret metadata needs `k8s.score.dev/resource-uid`, `k8s.score.dev/source-workload`, `platform.company/plane`, and the annotation below:

```yaml
kustomize.toolkit.fluxcd.io/prune: disabled
```

Map Score init/params to the root variable names in the contract exactly. `vars` hold nonsecret values, including JSON-encoded lists and booleans. `varsFrom` supplies only sensitive keys. For default new-VPC mode provide all four CIDR lists and AZs; for existing-VPC mode provide VPC and subnet IDs. Include explicit database/cache client CIDRs where enabled. The existing cluster may need VPC peering, Transit Gateway, or VPN to reach private endpoints. Do not emit `0.0.0.0/0` ingress.

The current Flux GitRepository is `score-provisioner-modules` in `flux-system`, serving `./terraform-aws`. The new `./templates/application-stack` path requires a separate **tag-pinned** GitRepository named `score-aws-template-catalog` in the same `flux-system` namespace; do not retarget the existing object while old RDS CRs use it. Create that source as part of a later controlled integration step. The example below assumes it exists at a reviewed tag. The CR namespace remains `default`.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: secret-<guid>
  namespace: default
  labels:
    platform.company/plane: application
  annotations:
    k8s.score.dev/resource-uid: application-stack.aws#orders-api.stack
    k8s.score.dev/source-workload: orders-api
    kustomize.toolkit.fluxcd.io/prune: disabled
type: Opaque
stringData:
  db_password: <Score-state-adminPassword-at-render-time>
---
apiVersion: infra.contrib.fluxcd.io/v1alpha2
kind: Terraform
metadata:
  name: stack-<guid>
  namespace: default
  labels:
    platform.company/plane: application
  annotations:
    k8s.score.dev/resource-uid: application-stack.aws#orders-api.stack
    k8s.score.dev/source-workload: orders-api
spec:
  interval: 10m
  approvePlan: auto
  destroyResourcesOnDeletion: true
  path: ./templates/application-stack
  sourceRef:
    kind: GitRepository
    name: score-aws-template-catalog
    namespace: flux-system
  runnerPodTemplate:
    metadata:
      annotations:
        vault.hashicorp.com/agent-inject: "true"
        vault.hashicorp.com/role: tf-runner-role
        vault.hashicorp.com/agent-inject-secret-aws: secret/data/score-api/aws-creds
        vault.hashicorp.com/agent-inject-template-aws: |
          {{- with secret "secret/data/score-api/aws-creds" -}}
          [default]
          aws_access_key_id={{ .Data.data.AWS_ACCESS_KEY_ID }}
          aws_secret_access_key={{ .Data.data.AWS_SECRET_ACCESS_KEY }}
          {{- end -}}
    spec:
      env:
      - name: AWS_SHARED_CREDENTIALS_FILE
        value: /vault/secrets/aws
      - name: AWS_REGION
        value: us-east-1
  vars:
  - name: stack_name
    value: orders
  - name: resource_guid
    value: <guid>
  - name: workload
    value: orders-api
  - name: environment
    value: dev
  - name: plane
    value: application
  - name: region
    value: us-east-1
  - name: create_vpc
    value: "true"
  - name: availability_zones
    value: '["us-east-1a","us-east-1b"]'
  - name: public_subnet_cidrs
    value: '["10.80.0.0/24","10.80.1.0/24"]'
  - name: private_subnet_cidrs
    value: '["10.80.10.0/24","10.80.11.0/24"]'
  - name: database_subnet_cidrs
    value: '["10.80.20.0/24","10.80.21.0/24"]'
  - name: cache_subnet_cidrs
    value: '["10.80.30.0/24","10.80.31.0/24"]'
  - name: database_client_cidr_blocks
    value: '["10.90.0.0/20"]'
  - name: enable_rds
    value: "true"
  - name: enable_s3
    value: "true"
  - name: enable_cache
    value: "false"
  - name: enable_sqs
    value: "false"
  varsFrom:
  - kind: Secret
    name: secret-<guid>
    varsKeys:
    - db_password
  writeOutputsToSecret:
    name: tf-output-<guid>
    outputs:
    - stack_name
    - vpc_id
    - private_subnet_ids
    - database_subnet_ids
    - cache_subnet_ids
    - db_host
    - db_port
    - db_name
    - db_username
    - s3_bucket_name
    - s3_bucket_arn
    - cache_endpoint
    - cache_port
    - queue_url
    - queue_arn
    - dead_letter_queue_url
```

The example uses placeholders and no credentials. The Vault annotation template is code that runs in the cluster; it does not embed values. The current runner CR has no requests or limits, so cluster-specific resource sizing remains to be verified before integration. In a real Score template, escape the Vault `{{ ... }}` delimiters from Score's Go template, as the current provisioner does. Render and inspect the manifests before committing them.

For Score outputs, use `encodeSecretRef (printf "tf-output-%s" .Guid) "db_host"` and equivalent references for service endpoints/names/URLs. Use `encodeSecretRef (printf "secret-%s" .Guid) "db_password"` for the password. `db_port` and `cache_port` are numbers in Terraform but encoded as Secret data by tofu-controller; consumers that need numbers should parse the Secret string. Disabled component outputs are null and may be absent from controller-written Secret data; only expose references for enabled components. Verify null serialization with the installed controller before rollout. Do not expose the password as a Terraform output.
