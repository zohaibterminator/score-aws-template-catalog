output "host" { value = module.rds.db_instance_address }
output "port" { value = var.port }
output "name" { value = var.db_name }
output "username" { value = var.db_username }
output "security_group_id" { value = module.security_group.security_group_id }
