resource "terraform_data" "subnet_contract" {
  lifecycle {
    precondition {
      condition     = length(var.subnet_ids) >= 2
      error_message = "RDS requires subnets in at least two AZs."
    }
  }
}

module "security_group" {
  source      = "terraform-aws-modules/security-group/aws"
  version     = "5.3.1"
  name        = "${var.name}-db"
  description = "PostgreSQL clients for ${var.name}"
  vpc_id      = var.vpc_id
  ingress_with_cidr_blocks = [for cidr in var.client_cidr_blocks : {
    from_port   = var.port
    to_port     = var.port
    protocol    = "tcp"
    cidr_blocks = cidr
    description = "Approved application client"
  }]
  egress_rules = []
  tags         = var.tags
}

module "rds" {
  source                           = "terraform-aws-modules/rds/aws"
  version                          = "6.13.1"
  identifier                       = "${var.name}-db"
  engine                           = "postgres"
  engine_version                   = var.engine_version
  family                           = "postgres${split(".", var.engine_version)[0]}"
  major_engine_version             = split(".", var.engine_version)[0]
  instance_class                   = var.instance_class
  allocated_storage                = var.allocated_storage
  storage_encrypted                = true
  db_name                          = var.db_name
  username                         = var.db_username
  password                         = var.db_password
  manage_master_user_password      = false
  port                             = var.port
  create_db_subnet_group           = true
  db_subnet_group_name             = "${var.name}-db"
  db_subnet_group_use_name_prefix  = false
  subnet_ids                       = var.subnet_ids
  vpc_security_group_ids           = [module.security_group.security_group_id]
  publicly_accessible              = false
  multi_az                         = local.production
  backup_retention_period          = local.production ? 7 : 1
  deletion_protection              = local.production && !var.allow_prod_destroy
  skip_final_snapshot              = !local.production
  final_snapshot_identifier_prefix = "${var.name}-final"
  copy_tags_to_snapshot            = true
  create_monitoring_role           = false
  tags                             = var.tags
  depends_on                       = [terraform_data.subnet_contract]
}
