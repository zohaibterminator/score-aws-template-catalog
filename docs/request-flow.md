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
| `/eks` | `eks/` | EKS cluster stack, **plus the network-access stack in the same request** (default; `enable_network_access: false` for the cluster only) |
| `/network-access` | `network-access/` | bastion + cluster add-ons (LBC, NGINX, optional ExternalDNS) |
| `/delete-all` (v15) | all | removes the workloads in dependency order: **network-access first, then EKS**, then the rest |

`scripts/deploy-platform.sh` (`lists | eks | access | status | delete`) is a wrapper that calls these endpoints for the platform stacks. It keeps its own copy of the allow-lists, used only for the office-egress drift check and the NLB rule-budget check.

---

## 2a. One request for EKS and network access (bundled)

`provision_eks` / `/eks` creates the cluster **and** its network access by default. The EKS provisioner renders two
Terraform CRs from the one request:

```
/eks {cluster_name, aws_account_id, region, kubernetes_version, [bastion_access_mode]}
   │
   ├─ eks-<guid>             ./eks             VPC, subnets, EKS, node group, LBC/ExternalDNS IAM
   │     └─ writes tf-output-<guid>: vpc_id, subnet_id, public_subnet_id, cluster_security_group_id, ...
   │
   └─ access-<guid>          ./network-access  bastion, cluster-admin access, LBC + NGINX + one NLB
         dependsOn: eks-<guid>          (plans only when the cluster stack is Ready)
         varsFrom:  tf-output-<guid>    (vpc_id, subnet_id, cluster_security_group_id, public_subnet_id in ssh mode)
         name:      <cluster_name>-access, vpc_cidr/region/account/ingress settings from the same request
         writes tf-output-<guid>-access: ssm_start_session_command, ssh_command, ingress_load_balancer_hostname, ...
```

- **Nothing is copied by hand.** The IDs AWS generates for the VPC, subnets and security group flow from the cluster
  stack's output Secret into the network-access stack.
- **Inputs:** `bastion_access_mode` (`ssm` default, or `ssh` with key pair `platform-bastion` and an Elastic IP,
  so the SSH address survives stop/start and instance replacement; with score-api's BASTION_EIP_ALLOCATION_ID and
  BASTION_HOST_KEY_PARAMETER set, every bastion takes the same pre-allocated IP and SSH host key) and
  `enable_network_access` (default true) are the only extra fields.
- **One network-access stack per cluster.** `/eks` refuses to bundle when the cluster already has a separate stack
  (from `/network-access`), and `/network-access` refuses a cluster that already has a bundled one.
- **Deletion order is kept.** tofu-controller puts a dependency finalizer on `eks-<guid>`, so the cluster stack cannot be destroyed while `access-<guid>` exists. delete-all first drops only the `access-<guid>` document from
  `eks/generated/manifests.yaml` (Flux prunes it; the teardown removes NGINX and the NLB), waits until it is gone,
  then wipes `eks/`.
- **The separate path still works.** The `network-access/` provisioner, module and `/network-access` endpoint are
  unchanged, as a backup or for clusters requested with `enable_network_access: false`. Clusters requested before
  bundling keep their shape: the provisioner's own default is false, and score-api sends true only for new requests.

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
| `ssh_cidrs` | GlobalProtect Pakistan South (4) + office links (2) + private admin hosts 10.100.142.31-36 (6 x /32) | bastion SSH (TCP 22) |

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

## 6a. Following a request: /cgi-bin/status

Poll `infra.score_api.resource_status` (or `GET /cgi-bin/status`), with no input, until `overall.done` is true.
The verdict covers everything provisioned; `requests` has one verdict per request (cluster + its network access):

| `overall.state` | Meaning |
|---|---|
| `in_progress` | something is pending, waiting on a dependency, planning or applying (`overall.summary` says which) |
| `succeeded` | every resource of the request is ready (for `operation: delete`: every one is gone) |
| `none` | nothing is provisioned (e.g. after delete-all finished) |
| `failed` | one resource failed; `overall.error` names it, the step (`plan_failed`, `apply_failed`, ...), the reason, the full message, error lines from its runner and warning events |

Each resource also reports its `step`, conditions, revisions, runner pod (phase, restarts, log tail) and last events.
A bundled cluster counts as one request: `eks-<guid>` names `access-<guid>` in an annotation, so the request is not
done until both are ready. Runner pods, logs and events need `score-api/k8s/rbac-status.yaml`; without it the verdict
still works from the Terraform CRs.

## 7. Results and deletion

- **Status:** `kubectl get terraform -A` shows each CR's plan and apply state. Outputs are in `kubectl get secret tf-output-<guid> -o yaml`.
- **Delete one stack:** remove its workload through score-api. Flux prunes the CR, and the finalizer destroys the resources.
- **Delete everything:** `/delete-all` removes network-access first, so the teardown Lambda removes the NLB while the cluster still exists. EKS follows, then the rest.

---

## End-to-end example: "create an EKS cluster with access" (bundled)

1. The caller sends `provision_eks` with `{workload, cluster_name, aws_account_id, region, kubernetes_version}`
   (optionally `bastion_access_mode: "ssh"`).
2. score-api writes `eks/workloads/<workload>.yaml` with `enable_network_access: true`, runs `score-k8s generate` and
   pushes. `eks/generated/manifests.yaml` now holds `eks-<guid>` and `access-<guid>`.
3. Flux `score-eks` applies both. `eks-<guid>` builds the cluster (about 15-20 minutes); `access-<guid>` waits.
4. When the cluster stack is Ready, `access-<guid>` reads its IDs from `tf-output-<guid>` and builds the
   bastion, the load balancer controller, NGINX and the NLB (about 10 more minutes).
5. Results: `tf-output-<guid>` (cluster) and `tf-output-<guid>-access` (bastion commands, NLB hostname).

## End-to-end example: "create an EKS cluster" (separate requests)

1. The caller sends an A2A tool call to the agent with `{cluster_name, aws_account_id, region, kubernetes_version}`. No LLM runs.
2. The agent checks the params against `facts()` and POSTs them to score-api `/eks`.
3. score-api validates the request, writes `eks/score.yaml`, runs `score-k8s generate`, and pushes to `score-gp-aws-rds`.
4. Flux `score-eks` applies the new `Terraform` CR.
5. tofu-controller runs `./eks` from the pinned tag. The cluster is created with the fixed API allow-list.
6. The outputs land in `tf-output-<guid>`.
7. A follow-up `/network-access` request creates the bastion. SSM installs the LBC and NGINX, the NLB appears with 12 SG rules, and the Lambda confirms the install.
