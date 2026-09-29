# Request flow: capability agent → AWS

How a request made through the capability agent becomes infrastructure in AWS account `412662188858`, and how it is removed again.

```
Caller (A2A client / another agent)
   │  A2A JSON-RPC (bearer token)
   ▼
Capability agent            score-aws-template-catalog/agents/capability-agent
   │  HTTP POST (shared secret), plain code — no LLM
   ▼
score-api                   CGI endpoints: /eks, /network-access, /rds, /delete-all
   │  writes score.yaml, runs score-k8s generate, git commit + push
   ▼
score-gp-aws-rds (Git)      folders: eks/, network-access/, rds/
   │  Flux Kustomizations: score-eks, score-network-access, score-generated
   ▼
tofu-controller             Terraform CRs (approvePlan: auto)
   │  module source: GitRepository score-tf-modules, pinned to a tag (v1.11.0)
   ▼
score-tf-modules            eks/, network-access/, terraform-aws/, access-lists/
   │  terraform apply
   ▼
AWS                         EKS, bastion, NLB, RDS, ...
   │  outputs
   ▼
Secret tf-output-<guid>     read back by score-api / the caller
```

---

## 1. Capability agent (A2A)

**Where:** `score-aws-template-catalog/agents/capability-agent`, deployed from `k8s/capability-agent.yaml`.

The agent is the front door. It tells a caller what can be provisioned and forwards valid requests to score-api.

| Request | What handles it | LLM used? |
|---|---|---|
| `list_capabilities` | `store.current()`: the Claude crawl of the module and Score repos | **Yes**, and only here |
| a tool call (e.g. create EKS, network-access, RDS, delete-all) | `store.facts()` + `call_tool` → score-api | No |
| `check_request` (validate params before sending) | `store.facts()` + `skills.check_request` | No |
| free text | not answered | No |

**Crawl and cost controls**
- The agent watches both repos with `git ls-remote`. It crawls again only when a commit has changed module or provisioner content; commits that only change state are skipped (`_reuse`).
- At most `MAX_CRAWLS_PER_HOUR=2` crawls. A failed crawl backs off for 1 to 30 minutes.
- The last crawl is saved to `/cache/crawl.json` on the PVC `score-capability-agent-cache`. A pod restart reuses it while the commits are unchanged.
- Model calls go to the LiteLLM gateway (`ANTHROPIC_BASE_URL`, model `azure_ai/claude-opus-4-8`). They are streamed, use a 900 s timeout, and have SDK retries off (`max_retries=0`), so a gateway timeout never re-sends and re-bills a request.

**Deterministic facts.** `facts()` reads the Git repos directly: the provisioner `.score-k8s/*.provisioners.yaml` files and the Terraform `variables.tf`. From these it builds each tool's parameter schema (required params, defaults, types). The allow-list params (`api_allowed_cidrs`, `ingress_allowed_cidrs`, `ssh_allowed_cidrs`) are never offered.

---

## 2. score-api

**Where:** `score-api/endpoints/*`. These are CGI shell scripts, mounted into the `score-api` Deployment from the ConfigMap `score-api-endpoints`.

Each endpoint:
1. **Authenticates** the caller with the shared secret.
2. **Validates** the JSON body with `jq`:
   - Checks the required fields and their formats (for EKS: `cluster_name`, `aws_account_id`, `region`, `kubernetes_version`).
   - Keeps only the known fields; anything else is dropped.
   - Rejects `api_allowed_cidrs`, `ingress_allowed_cidrs` and `ssh_allowed_cidrs`, because those are fixed by the platform.
   - In network-access `ssh` mode, requires `ssh_key_name` and `public_subnet_id`.
3. **Writes a Score workload** (`score.yaml`) into the right folder of `score-gp-aws-rds`. The workload has one resource of the provisioner's type, and the request fields become its `params`.
4. **Runs `score-k8s generate`**, which renders the matching provisioner (step 3) into Kubernetes manifests. `state.yaml` in each folder is score-k8s's own file and is never edited by hand.
5. **Commits and pushes** to `score-gp-aws-rds`.
6. **Returns** the request's GUID and the names of the resources to watch.

| Endpoint | Folder | Creates |
|---|---|---|
| `/rds` | `rds/` | `terraform-aws` stack (RDS etc.) |
| `/eks` | `eks/` | EKS cluster stack |
| `/network-access` | `network-access/` | bastion + cluster add-ons (LBC, NGINX, optional ExternalDNS) |
| `/delete-all` (v14) | all | removes the workloads in dependency order: **network-access first, then EKS**, then the rest |

`scripts/deploy-platform.sh` (`lists | eks | access | status | delete`) is a wrapper that calls these endpoints for the platform stacks. It keeps its own copy of the allow-lists, used only for the office-egress drift check and the NLB rule-budget check.

---

## 3. Provisioners (score-gp-aws-rds)

**Where:** `score-gp-aws-rds/<folder>/.score-k8s/<type>.provisioners.yaml`

A provisioner is a score-k8s template. It turns a Score resource into Kubernetes objects:

- **`init`**: computes values from `.Params`: defaults, names, the GUID.
- **`manifests`**: renders a **tofu-controller `Terraform` CR**:
  - `sourceRef` → GitRepository `score-provisioner-modules`
  - `path` → the module folder (`./eks`, `./network-access`, `./terraform-aws`)
  - `vars` → the params, passed as Terraform variables
  - `approvePlan: auto`
  - `writeOutputsToSecret: tf-output-<guid>` with the listed outputs. A null string output crashes the controller, so string outputs use `""` instead of null.
- **`outputs`**: what Score exposes to the workload (endpoints, secret refs).

Changes since v1.11.0:
- The eks provisioner requires only `cluster_name`, `aws_account_id`, `region` and `kubernetes_version`. It no longer passes any CIDR lists, and it exports `api_allowed_cidrs` and `ingress_allowed_cidrs` as outputs.
- The network-access provisioner no longer passes `ssh_allowed_cidrs` or `ingress_allowed_cidrs`.

---

## 4. Flux

- The Kustomizations **`score-eks`**, **`score-network-access`** and **`score-generated`** (rds) watch their folders in `score-gp-aws-rds`. They apply the generated Terraform CRs to the cluster.
- The GitRepository **`score-provisioner-modules`** points at `score-tf-modules`, **pinned to a reviewed tag**. To upgrade modules, move the tag:
  ```bash
  kubectl -n flux-system patch gitrepository score-provisioner-modules --type merge -p '{"spec":{"ref":{"tag":"v1.11.0"}}}'
  ```
- Deleting a workload removes its CR. tofu-controller's finalizer runs `terraform destroy` before the CR disappears, so **never strip the finalizer**.

---

## 5. tofu-controller

For each `Terraform` CR:
1. Fetches the module tag's artifact from `score-provisioner-modules`.
2. Starts a runner pod, which runs `terraform init/plan`. Because of `approvePlan: auto`, the pod also runs `apply`. State lives in a Kubernetes Secret in the cluster.
3. Writes the listed outputs to the Secret `tf-output-<guid>`.
4. Re-plans at every reconcile, so drift is corrected.

The runner uses **only AWS APIs**. It never talks to the EKS API itself. In-cluster work is done from the bastion (step 6).

---

## 6. Terraform modules (score-tf-modules)

### `access-lists/` (fixed allow-lists, not inputs)
| Output | Contents | Used by |
|---|---|---|
| `api_cidrs` | GlobalProtect Pakistan South (4) + office links 110.93.200.194/32, 202.125.133.221/32 | EKS public API endpoint |
| `ingress_cidrs` | same 6 | NLB security group (80, 443) → 12 rules |
| `ssh_cidrs` | GlobalProtect Pakistan South (4) | bastion SSH (TCP 22) |

To change a list, edit `access-lists/main.tf` and release a new tag.

### `eks/`
- Creates the EKS cluster and its managed node group.
- The public endpoint accepts only `access_lists.api_cidrs`.
- The node SG rule `allow_vpc_cidr_to_nodes` allows all traffic from `vpc_cidr`.
- Access entries: `cluster_admin_principal_arns` (e.g. `user/Abdurrahman`), excluding the runner's own role.
- Pod Identity roles for the AWS Load Balancer Controller and, optionally, ExternalDNS (`ingress.tf`). The Route 53 zone is optional.
- Finds the NLB's hostname through the AWS API (`data aws_lb`) once it exists.

### `network-access/`
- **Bastion** (Amazon Linux 2023):
  - `ssm` mode: private subnet.
  - `ssh` mode: public subnet, public IP or optional EIP, and an existing key pair. The SG allows port 22 only from `ssh_cidrs`. A read-only NACL check runs, and sshd is hardened. SSM stays as break-glass.
- **Cluster add-ons over SSM** (`kube-setup.tf` + `templates/kube-addons.sh.tftpl`). An SSM association runs on the bastion and installs:
  - kubectl, helm, git
  - AWS Load Balancer Controller 3.5.0
  - ExternalDNS 1.21.1 (optional)
  - F5 nginx-ingress 2.7.3, whose Service is type `LoadBalancer`. The LBC turns it into **one NLB with IP targets**, named `<cluster>-nlb`, with `loadBalancerSourceRanges = ingress_cidrs`.
- **Lambda** (`templates/teardown_lambda.py`, invoked through `aws_lambda_invocation`, `lifecycle_scope = CRUD`):
  - on create and update: checks that the install run succeeded. It retries a failed run once, then fails the CR.
  - on delete: runs the teardown document, which removes NGINX, the NLB and ExternalDNS records before EKS is destroyed.
- **Plan-time guards:** the NLB SG rule quota (`nlb_security_group_rule_quota`, default 60) against CIDRs × listeners, plus IGW and VPC checks in ssh mode.

### `terraform-aws/`
The general stack behind `/rds`.

---

## 7. Results and deletion

- **Status:** `kubectl get terraform -A` shows each CR's plan and apply state. Outputs are in `kubectl get secret tf-output-<guid> -o yaml`.
- **Delete one stack:** remove its workload through score-api. Flux prunes the CR, and the finalizer destroys the resources.
- **Delete everything:** `/delete-all` removes network-access first, so the teardown Lambda removes the NLB while the cluster still exists. EKS follows, then the rest.

---

## End-to-end example: "create an EKS cluster"

1. The caller sends an A2A tool call to the agent with `{cluster_name, aws_account_id, region, kubernetes_version}`. No LLM runs.
2. The agent checks the params against `facts()` and POSTs them to score-api `/eks`.
3. score-api validates the request, writes `eks/score.yaml`, runs `score-k8s generate`, and pushes to `score-gp-aws-rds`.
4. Flux `score-eks` applies the new `Terraform` CR.
5. tofu-controller runs `./eks` from the pinned tag. The cluster is created with the fixed API allow-list.
6. The outputs land in `tf-output-<guid>`.
7. A follow-up `/network-access` request creates the bastion. SSM installs the LBC and NGINX, the NLB appears with 12 SG rules, and the Lambda confirms the install.
