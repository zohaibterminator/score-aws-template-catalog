variable "name" { type = string }
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "client_cidr_blocks" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for cidr in var.client_cidr_blocks : can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0"])
    error_message = "Cache clients need valid restricted IPv4 CIDRs."
  }
}
variable "engine" {
  type    = string
  default = "redis"
  validation {
    condition     = contains(["redis", "valkey"], var.engine)
    error_message = "Use redis or valkey."
  }
}
variable "engine_version" {
  type    = string
  default = null
}
variable "node_type" {
  type    = string
  default = "cache.t4g.small"
  validation {
    condition     = can(regex("^cache\\.[a-z0-9]+\\.[a-z0-9]+$", var.node_type))
    error_message = "Invalid cache node type."
  }
}
variable "port" {
  type    = number
  default = 6379
  validation {
    condition     = var.port >= 1 && var.port <= 65535
    error_message = "Invalid port."
  }
}
variable "auth_token" {
  type      = string
  sensitive = true
  default   = null
}
variable "environment" {
  type    = string
  default = "dev"
}
variable "tags" {
  type    = map(string)
  default = {}
}
