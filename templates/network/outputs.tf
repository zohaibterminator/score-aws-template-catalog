output "vpc_id" { value = var.create_vpc ? module.vpc[0].vpc_id : var.existing_vpc_id }
output "public_subnet_ids" { value = local.public_ids }
output "private_subnet_ids" { value = local.private_ids }
output "database_subnet_ids" { value = local.database_ids }
output "cache_subnet_ids" { value = local.cache_ids }
