terraform {
  required_version = ">= 1.7.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.66.0"
    }
    # Required by terraform-aws-modules/eks/aws 21.26.0 (OIDC thumbprint, waits, node user data).
    tls = {
      source  = "hashicorp/tls"
      version = "= 4.4.1"
    }
    time = {
      source  = "hashicorp/time"
      version = "= 0.14.2"
    }
    cloudinit = {
      source  = "hashicorp/cloudinit"
      version = "= 2.4.1"
    }
    null = {
      source  = "hashicorp/null"
      version = "= 3.3.2"
    }
  }
}
