# EKS

A one-shot tofu-controller root that creates, in one Terraform state:

- a VPC across two availability zones, with two public and two private subnets, an internet gateway and one NAT gateway
- an Amazon EKS control plane with a KMS key for secret encryption, an OIDC provider and CloudWatch control plane logs
- the core add-ons: `vpc-cni`, `eks-pod-identity-agent`, `kube-proxy` and `coredns`
- one managed node group (`default`) in the private subnets
- the IAM roles, policies and security groups those need, created by the official modules

It is built from pinned official modules: `terraform-aws-modules/vpc/aws` 6.7.3 and `terraform-aws-modules/eks/aws` 21.26.0, which itself pins `terraform-aws-modules/kms/aws` 4.0.0. Exact commits are in [`source-manifest.yaml`](../../source-manifest.yaml). Inputs, outputs and IAM needs are in [`contract.yaml`](contract.yaml).

## Inputs

Required: `cluster_name`, `aws_account_id`, `region`, `kubernetes_version` and `api_allowed_cidrs`. Everything else has a default. See [`terraform.tfvars.example`](terraform.tfvars.example).

- **Account check.** The AWS provider refuses to run against any account other than `aws_account_id` (`allowed_account_ids`). A postcondition on the caller identity repeats the check with a clearer message. A plan with the wrong credentials fails before anything is created.
- **API access.** The public endpoint only accepts `api_allowed_cidrs`, and `0.0.0.0/0` is rejected. Add the egress IP of every machine that runs `kubectl`. Terraform itself never calls the Kubernetes API, so the runner does not need to be in the list.
- **Subnets.** Leave `azs`, `public_subnet_cidrs` and `private_subnet_cidrs` empty to use the region's first two zones, two `/20` private subnets and two `/24` public subnets carved from `vpc_cidr`. If you set them, they are checked to be inside `vpc_cidr` and distinct.
- **Kubernetes version.** Use a version EKS offers in the region: `aws eks describe-cluster-versions --region <region> --query 'clusterVersions[].clusterVersion'`.
- **Access.** Authentication mode is `API` (access entries). The identity that runs Terraform becomes cluster admin, so the cluster can be inspected with the runner's AWS credentials.

## Cost

This is tuned for disposable test clusters, not production. It uses one NAT gateway, two `t3.small` nodes, 7-day log retention, a 7-day KMS deletion window and no deletion protection. The EKS control plane and NAT gateway are billed hourly and are not covered by the AWS free tier. AWS free-plan accounts may refuse some instance types or EKS itself (`FreeTierRestrictionError`).

## Score and tofu-controller

The Score provisioner is [`.score-k8s/20-eks.provisioners.yaml`](../../.score-k8s/20-eks.provisioners.yaml) (type `eks`, class `aws-terraform`). It renders one Terraform CR, `eks-<guid>`, that points at `./templates/eks` in the `score-aws-template-catalog` GitRepository in `flux-system`. The CR sets `approvePlan: auto` and `destroyResourcesOnDeletion: true`. The runner gets AWS credentials from Vault through the `tf-runner-dev-role` role. Outputs are written to `tf-output-<guid>`.

The CR has no `metadata.namespace`. The namespace is set by whatever applies it: a Flux Kustomization with `targetNamespace: score-dev`, or `kubectl apply -n "$SCORE_NAMESPACE"`. Its state (`tfstate-default-eks-<guid>`) stays in that namespace, apart from the RDS pipeline.

## Deletion

Deleting the Terraform CR destroys the stack. Delete load balancers and volumes created by workloads in the cluster first. They are not in Terraform state and block VPC deletion.
