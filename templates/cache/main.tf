resource "terraform_data" "subnet_contract" {
  lifecycle {
    precondition {
      condition     = length(var.subnet_ids) >= 2
      error_message = "Cache requires subnets in at least two AZs."
    }
    precondition {
      condition     = !local.production || var.auth_token != null
      error_message = "Production cache requires a sensitive auth_token."
    }
  }
}

module "security_group" {
  source      = "terraform-aws-modules/security-group/aws"
  version     = "5.3.1"
  name        = "${var.name}-cache"
  description = "Cache clients for ${var.name}"
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

module "elasticache" {
  source                     = "terraform-aws-modules/elasticache/aws"
  version                    = "1.11.1"
  create_cluster             = false
  create_replication_group   = true
  replication_group_id       = "${var.name}-cache"
  description                = "Private application cache"
  engine                     = var.engine
  engine_version             = local.engine_version
  node_type                  = var.node_type
  port                       = var.port
  num_cache_clusters         = local.production ? 2 : 1
  automatic_failover_enabled = local.production
  multi_az_enabled           = local.production
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = var.auth_token
  create_subnet_group        = true
  subnet_group_name          = "${var.name}-cache"
  subnet_ids                 = var.subnet_ids
  create_security_group      = false
  security_group_ids         = [module.security_group.security_group_id]
  snapshot_retention_limit   = local.production ? 7 : 1
  tags                       = var.tags
  depends_on                 = [terraform_data.subnet_contract]
}
