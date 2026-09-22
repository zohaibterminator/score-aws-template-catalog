variable "name" { type = string }
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "client_cidr_blocks" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for cidr in var.client_cidr_blocks : can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0"])
    error_message = "Database clients need valid restricted IPv4 CIDRs."
  }
}
variable "db_name" {
  type    = string
  default = "appdb"
  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,62}$", var.db_name))
    error_message = "Invalid PostgreSQL database name."
  }
}
variable "db_username" {
  type    = string
  default = "scoreadmin"
}
variable "db_password" {
  type      = string
  sensitive = true
}
variable "port" {
  type    = number
  default = 5432
  validation {
    condition     = var.port >= 1 && var.port <= 65535
    error_message = "Invalid port."
  }
}
variable "instance_class" {
  type    = string
  default = "db.t3.medium"
  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.instance_class))
    error_message = "Invalid RDS instance class."
  }
}
variable "allocated_storage" {
  type    = number
  default = 20
  validation {
    condition     = var.allocated_storage >= 20 && var.allocated_storage <= 65536
    error_message = "RDS storage must be 20–65536 GiB."
  }
}
variable "engine_version" {
  type    = string
  default = "16"
}
variable "environment" {
  type    = string
  default = "dev"
}
variable "allow_prod_destroy" {
  type    = bool
  default = false
}
variable "tags" {
  type    = map(string)
  default = {}
}
