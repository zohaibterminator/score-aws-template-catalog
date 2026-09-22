module "sqs" {
  source                      = "terraform-aws-modules/sqs/aws"
  version                     = "4.3.1"
  name                        = var.name
  create_dlq                  = true
  dlq_name                    = local.dlq_name
  redrive_policy              = { maxReceiveCount = var.max_receive_count }
  sqs_managed_sse_enabled     = true
  dlq_sqs_managed_sse_enabled = true
  tags                        = var.tags
  dlq_tags                    = var.tags
}
