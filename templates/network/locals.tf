locals {
  public_ids   = var.create_vpc ? module.vpc[0].public_subnets : var.existing_public_subnet_ids
  private_ids  = var.create_vpc ? module.vpc[0].private_subnets : var.existing_private_subnet_ids
  database_ids = var.create_vpc ? module.vpc[0].database_subnets : var.existing_database_subnet_ids
  cache_ids    = var.create_vpc ? module.vpc[0].elasticache_subnets : var.existing_cache_subnet_ids
}
