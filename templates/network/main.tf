resource "terraform_data" "network_contract" {
  lifecycle {
    precondition {
      condition = var.create_vpc ? (
        length(var.availability_zones) >= 2 &&
        length(var.public_subnet_cidrs) == length(var.availability_zones) &&
        length(var.private_subnet_cidrs) == length(var.availability_zones) &&
        length(var.database_subnet_cidrs) == length(var.availability_zones) &&
        length(var.cache_subnet_cidrs) == length(var.availability_zones) &&
        alltrue([for cidr in concat(var.public_subnet_cidrs, var.private_subnet_cidrs, var.database_subnet_cidrs, var.cache_subnet_cidrs) : can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0"])
      ) : (var.existing_vpc_id != null && length(var.existing_database_subnet_ids) >= 2 && length(var.existing_cache_subnet_ids) >= 2 && length(var.existing_private_subnet_ids) >= 1)
      error_message = "New VPC requires at least two AZs and one valid CIDR per subnet tier/AZ; existing VPC requires its ID, two database/cache subnets and application private subnets."
    }
  }
}

module "vpc" {
  count   = var.create_vpc ? 1 : 0
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.21.0"

  name                            = var.name
  cidr                            = var.vpc_cidr
  azs                             = var.availability_zones
  public_subnets                  = var.public_subnet_cidrs
  private_subnets                 = var.private_subnet_cidrs
  database_subnets                = var.database_subnet_cidrs
  elasticache_subnets             = var.cache_subnet_cidrs
  create_database_subnet_group    = false
  create_elasticache_subnet_group = false
  enable_dns_hostnames            = true
  enable_dns_support              = true
  enable_nat_gateway              = var.enable_nat_gateway
  single_nat_gateway              = var.single_nat_gateway
  tags                            = var.tags
  depends_on                      = [terraform_data.network_contract]
}
