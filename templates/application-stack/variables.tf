variable "stack_name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,38}[a-z0-9]$", var.stack_name)) && !strcontains(var.stack_name, "--")
    error_message = "stack_name must be 3–40 lowercase AWS-safe characters."
  }
}
variable "resource_guid" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.resource_guid))
    error_message = "resource_guid must be a lowercase UUID."
  }
}
variable "workload" { type = string }
variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "uat", "dr", "prod"], var.environment)
    error_message = "Unsupported environment."
  }
}
variable "plane" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,62}$", var.plane))
    error_message = "Invalid plane label."
  }
}
variable "region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.region))
    error_message = "Invalid AWS region."
  }
}
variable "create_vpc" {
  type    = bool
  default = true
}
variable "vpc_cidr" {
  type    = string
  default = "10.80.0.0/16"
  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && var.vpc_cidr != "0.0.0.0/0"
    error_message = "Invalid VPC CIDR."
  }
}
variable "availability_zones" {
  type    = list(string)
  default = []
}
variable "public_subnet_cidrs" {
  type    = list(string)
  default = []
}
variable "private_subnet_cidrs" {
  type    = list(string)
  default = []
}
variable "database_subnet_cidrs" {
  type    = list(string)
  default = []
}
variable "cache_subnet_cidrs" {
  type    = list(string)
  default = []
}
variable "enable_nat_gateway" {
  type    = bool
  default = false
}
variable "single_nat_gateway" {
  type    = bool
  default = true
}
variable "existing_vpc_id" {
  type    = string
  default = null
}
variable "existing_public_subnet_ids" {
  type    = list(string)
  default = []
}
variable "existing_private_subnet_ids" {
  type    = list(string)
  default = []
}
variable "existing_database_subnet_ids" {
  type    = list(string)
  default = []
}
variable "existing_cache_subnet_ids" {
  type    = list(string)
  default = []
}
variable "database_client_cidr_blocks" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for cidr in var.database_client_cidr_blocks : can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0"])
    error_message = "Invalid or open database client CIDR."
  }
}
variable "cache_client_cidr_blocks" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for cidr in var.cache_client_cidr_blocks : can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0"])
    error_message = "Invalid or open cache client CIDR."
  }
}
variable "enable_rds" {
  type    = bool
  default = true
}
variable "enable_s3" {
  type    = bool
  default = true
}
variable "enable_cache" {
  type    = bool
  default = false
}
variable "enable_sqs" {
  type    = bool
  default = false
}
variable "db_name" {
  type    = string
  default = "appdb"
}
variable "db_username" {
  type    = string
  default = "scoreadmin"
}
variable "db_password" {
  type      = string
  sensitive = true
  default   = null
}
variable "db_instance_class" {
  type    = string
  default = "db.t3.medium"
  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.db_instance_class))
    error_message = "Invalid RDS instance class."
  }
}
variable "db_allocated_storage" {
  type    = number
  default = 20
  validation {
    condition     = var.db_allocated_storage >= 20 && var.db_allocated_storage <= 65536
    error_message = "RDS storage must be 20–65536 GiB."
  }
}
variable "db_engine_version" {
  type    = string
  default = "16"
}
variable "cache_engine" {
  type    = string
  default = "redis"
  validation {
    condition     = contains(["redis", "valkey"], var.cache_engine)
    error_message = "Use redis or valkey."
  }
}
variable "cache_engine_version" {
  type    = string
  default = null
}
variable "cache_node_type" {
  type    = string
  default = "cache.t4g.small"
  validation {
    condition     = can(regex("^cache\\.[a-z0-9]+\\.[a-z0-9]+$", var.cache_node_type))
    error_message = "Invalid cache node type."
  }
}
variable "cache_auth_token" {
  type      = string
  sensitive = true
  default   = null
}
variable "allow_prod_destroy" {
  type    = bool
  default = false
}
variable "allow_nonprod_bucket_force_destroy" {
  type    = bool
  default = false
}
variable "extra_tags" {
  type    = map(string)
  default = {}
}
