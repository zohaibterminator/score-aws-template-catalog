provider "aws" {
  region = var.region
  default_tags { tags = local.common_tags }
}

data "aws_caller_identity" "current" {}

resource "terraform_data" "stack_contract" {
  lifecycle {
    precondition {
      condition     = !var.enable_rds || var.db_password != null
      error_message = "db_password must come from the Score input Secret when RDS is enabled."
    }
    precondition {
      condition     = !var.enable_rds || length(var.database_client_cidr_blocks) > 0
      error_message = "Provide explicit database client CIDRs; no ingress is created by default."
    }
    precondition {
      condition     = !var.enable_cache || length(var.cache_client_cidr_blocks) > 0
      error_message = "Provide explicit cache client CIDRs; no ingress is created by default."
    }
    precondition {
      condition     = !var.enable_cache || var.environment != "prod" || var.cache_auth_token != null
      error_message = "Production cache requires cache_auth_token from the input Secret."
    }
  }
}

module "network" {
  source                       = "../network"
  name                         = local.aws_name
  create_vpc                   = var.create_vpc
  vpc_cidr                     = var.vpc_cidr
  availability_zones           = var.availability_zones
  public_subnet_cidrs          = var.public_subnet_cidrs
  private_subnet_cidrs         = var.private_subnet_cidrs
  database_subnet_cidrs        = var.database_subnet_cidrs
  cache_subnet_cidrs           = var.cache_subnet_cidrs
  enable_nat_gateway           = var.enable_nat_gateway
  single_nat_gateway           = var.single_nat_gateway
  existing_vpc_id              = var.existing_vpc_id
  existing_public_subnet_ids   = var.existing_public_subnet_ids
  existing_private_subnet_ids  = var.existing_private_subnet_ids
  existing_database_subnet_ids = var.existing_database_subnet_ids
  existing_cache_subnet_ids    = var.existing_cache_subnet_ids
  tags                         = local.common_tags
}

module "postgres" {
  count              = var.enable_rds ? 1 : 0
  source             = "../postgres"
  name               = local.aws_name
  vpc_id             = module.network.vpc_id
  subnet_ids         = module.network.database_subnet_ids
  client_cidr_blocks = var.database_client_cidr_blocks
  db_name            = var.db_name
  db_username        = var.db_username
  db_password        = var.db_password
  instance_class     = var.db_instance_class
  allocated_storage  = var.db_allocated_storage
  engine_version     = var.db_engine_version
  environment        = var.environment
  allow_prod_destroy = var.allow_prod_destroy
  tags               = local.common_tags
  depends_on         = [terraform_data.stack_contract]
}

module "object_storage" {
  count                       = var.enable_s3 ? 1 : 0
  source                      = "../object-storage"
  name                        = "score-${local.aws_name}-${data.aws_caller_identity.current.account_id}"
  environment                 = var.environment
  allow_nonprod_force_destroy = var.allow_nonprod_bucket_force_destroy
  tags                        = local.common_tags
}

module "cache" {
  count              = var.enable_cache ? 1 : 0
  source             = "../cache"
  name               = local.aws_name
  vpc_id             = module.network.vpc_id
  subnet_ids         = module.network.cache_subnet_ids
  client_cidr_blocks = var.cache_client_cidr_blocks
  engine             = var.cache_engine
  engine_version     = var.cache_engine_version
  node_type          = var.cache_node_type
  auth_token         = var.cache_auth_token
  environment        = var.environment
  tags               = local.common_tags
  depends_on         = [terraform_data.stack_contract]
}

module "queue" {
  count  = var.enable_sqs ? 1 : 0
  source = "../queue"
  name   = "${local.aws_name}-queue"
  tags   = local.common_tags
}
