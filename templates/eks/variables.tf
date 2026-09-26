variable "cluster_name" {
  description = "EKS cluster name. Also prefixes the VPC and IAM role names."
  type        = string

  validation {
    # The module derives IAM role name prefixes from this name; AWS caps name_prefix at 38 characters.
    condition     = can(regex("^[a-z][a-z0-9-]{1,26}[a-z0-9]$", var.cluster_name))
    error_message = "cluster_name must be 3-28 characters: lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "aws_account_id" {
  description = "AWS account the cluster must be created in. The AWS provider refuses to run against any other account."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "region" {
  description = "AWS region, e.g. us-east-1."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.region))
    error_message = "region must be an AWS region name such as us-east-1."
  }
}

variable "environment" {
  description = "Environment label, used in tags."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "uat", "dr", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, uat, dr, prod."
  }
}

variable "plane" {
  description = "Platform plane label, used in tags."
  type        = string
  default     = "resource"

  validation {
    condition     = contains(["resource", "dev", "observability", "integration", "security", "unspecified"], var.plane)
    error_message = "plane must be one of resource, dev, observability, integration, security, unspecified."
  }
}

variable "kubernetes_version" {
  description = "EKS Kubernetes <major>.<minor> version, e.g. 1.34. Must be a version EKS currently offers in the region."
  type        = string

  validation {
    condition     = can(regex("^1\\.[0-9]{2}$", var.kubernetes_version))
    error_message = "kubernetes_version must look like 1.34."
  }
}

variable "api_allowed_cidrs" {
  description = "CIDRs allowed to reach the public Kubernetes API endpoint. Include the egress IP of anyone who runs kubectl."
  type        = list(string)

  validation {
    condition     = length(var.api_allowed_cidrs) > 0 && alltrue([for c in var.api_allowed_cidrs : can(cidrhost(c, 0))])
    error_message = "api_allowed_cidrs must contain at least one valid CIDR."
  }

  validation {
    condition     = !contains(var.api_allowed_cidrs, "0.0.0.0/0")
    error_message = "api_allowed_cidrs must not contain 0.0.0.0/0; list the networks that need API access."
  }
}

variable "vpc_cidr" {
  description = "CIDR of the new VPC. A /16 to /20 leaves room for the derived subnets."
  type        = string
  default     = "10.90.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && tonumber(split("/", var.vpc_cidr)[1]) >= 16 && tonumber(split("/", var.vpc_cidr)[1]) <= 20
    error_message = "vpc_cidr must be a valid IPv4 CIDR between /16 and /20."
  }
}

variable "azs" {
  description = "Exactly two availability zones. Leave empty to use the first two available zones in the region."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.azs) == 0 || length(var.azs) == 2
    error_message = "azs must be empty or contain exactly two availability zones."
  }
}

variable "public_subnet_cidrs" {
  description = "Two public subnet CIDRs inside vpc_cidr (load balancers, NAT). Leave empty to derive them from vpc_cidr."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.public_subnet_cidrs) == 0 || (length(var.public_subnet_cidrs) == 2 && alltrue([for c in var.public_subnet_cidrs : can(cidrhost(c, 0))]))
    error_message = "public_subnet_cidrs must be empty or two valid CIDRs."
  }
}

variable "private_subnet_cidrs" {
  description = "Two private subnet CIDRs inside vpc_cidr (nodes, control plane ENIs). Leave empty to derive them from vpc_cidr."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.private_subnet_cidrs) == 0 || (length(var.private_subnet_cidrs) == 2 && alltrue([for c in var.private_subnet_cidrs : can(cidrhost(c, 0))]))
    error_message = "private_subnet_cidrs must be empty or two valid CIDRs."
  }
}

variable "node_instance_types" {
  description = "Instance types for the managed node group. t3.small is the smallest practical size and is free-plan eligible."
  type        = list(string)
  default     = ["t3.small"]

  validation {
    condition     = length(var.node_instance_types) > 0
    error_message = "node_instance_types must list at least one instance type."
  }
}

variable "node_min_size" {
  description = "Minimum number of nodes."
  type        = number
  default     = 1

  validation {
    condition     = var.node_min_size >= 1 && floor(var.node_min_size) == var.node_min_size
    error_message = "node_min_size must be a whole number of at least 1."
  }
}

variable "node_max_size" {
  description = "Maximum number of nodes."
  type        = number
  default     = 2

  validation {
    condition     = var.node_max_size >= 1 && var.node_max_size <= 10 && floor(var.node_max_size) == var.node_max_size
    error_message = "node_max_size must be a whole number from 1 to 10."
  }
}

variable "node_desired_size" {
  description = "Desired number of nodes, between node_min_size and node_max_size."
  type        = number
  default     = 2

  validation {
    condition     = floor(var.node_desired_size) == var.node_desired_size
    error_message = "node_desired_size must be a whole number."
  }
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}
