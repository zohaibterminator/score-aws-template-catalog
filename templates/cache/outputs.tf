output "endpoint" { value = module.elasticache.replication_group_primary_endpoint_address }
output "port" { value = var.port }
output "security_group_id" { value = module.security_group.security_group_id }
