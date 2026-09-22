module "bucket" {
  source                                = "terraform-aws-modules/s3-bucket/aws"
  version                               = "4.11.0"
  bucket                                = var.name
  force_destroy                         = local.force_destroy
  block_public_acls                     = true
  block_public_policy                   = true
  ignore_public_acls                    = true
  restrict_public_buckets               = true
  attach_deny_insecure_transport_policy = true
  control_object_ownership              = true
  object_ownership                      = "BucketOwnerEnforced"
  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
      bucket_key_enabled = true
    }
  }
  versioning = { enabled = true }
  tags       = var.tags
}
