provider "aws" {
  region = var.region

  # Refuse to touch any account other than the requested one, before any resource is read or created.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "runner" {
  lifecycle {
    postcondition {
      condition     = self.account_id == var.aws_account_id
      error_message = "The runner's AWS credentials belong to a different account than aws_account_id."
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

# Checks that span several inputs. They fail the plan, so nothing is created when an input is inconsistent.
resource "terraform_data" "input_checks" {
  lifecycle {
    precondition {
      condition     = var.node_min_size <= var.node_desired_size && var.node_desired_size <= var.node_max_size
      error_message = "Node sizes must satisfy node_min_size <= node_desired_size <= node_max_size."
    }
    precondition {
      # A subnet is inside the VPC when its network address, masked to the VPC prefix, is the VPC network address.
      condition = alltrue([
        for c in concat(local.public_subnet_cidrs, local.private_subnet_cidrs) :
        tonumber(split("/", c)[1]) >= local.vpc_prefix && cidrhost("${cidrhost(c, 0)}/${local.vpc_prefix}", 0) == cidrhost(var.vpc_cidr, 0)
      ])
      error_message = "Every public and private subnet CIDR must be inside vpc_cidr."
    }
    precondition {
      condition     = length(distinct(concat(local.public_subnet_cidrs, local.private_subnet_cidrs))) == 4
      error_message = "The four subnet CIDRs must be different."
    }
    precondition {
      condition     = length(local.azs) == 2
      error_message = "The region must offer at least two availability zones."
    }
  }
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.3"

  name = var.cluster_name
  cidr = var.vpc_cidr
  azs  = local.azs

  public_subnets  = local.public_subnet_cidrs
  private_subnets = local.private_subnet_cidrs

  # One NAT gateway keeps cost down; nodes in both AZs egress through it.
  enable_nat_gateway     = true
  single_nat_gateway     = true
  one_nat_gateway_per_az = false

  enable_dns_hostnames = true

  # Let the AWS load balancer integrations find the right subnets.
  public_subnet_tags  = { "kubernetes.io/role/elb" = "1" }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = "1" }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.26.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnets
  control_plane_subnet_ids = module.vpc.private_subnets

  endpoint_private_access      = true
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.api_allowed_cidrs

  # Access entries only; the identity that runs Terraform becomes cluster admin so the cluster can be inspected.
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true

  # Disposable test cluster: allow deletion, keep logs briefly, and delete the secrets KMS key after the minimum wait.
  deletion_protection                    = false
  cloudwatch_log_group_retention_in_days = 7
  kms_key_deletion_window_in_days        = 7

  addons = {
    vpc-cni                = { before_compute = true }
    eks-pod-identity-agent = { before_compute = true }
    kube-proxy             = {}
    coredns                = {}
  }

  eks_managed_node_groups = {
    (local.node_group_name) = {
      name            = local.node_group_name
      use_name_prefix = false
      ami_type        = "AL2023_x86_64_STANDARD"
      # Let EKS choose the latest AMI release for the Kubernetes version, instead of looking it up in the
      # public SSM parameter, so the runner does not need ssm:GetParameter.
      use_latest_ami_release_version = false
      instance_types                 = var.node_instance_types
      min_size                       = var.node_min_size
      max_size                       = var.node_max_size
      desired_size                   = var.node_desired_size
      disk_size                      = 20
    }
  }
}
