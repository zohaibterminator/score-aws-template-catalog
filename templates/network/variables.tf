variable "name" { type = string }
variable "create_vpc" {
  type    = bool
  default = true
}
variable "vpc_cidr" {
  type    = string
  default = "10.80.0.0/16"
  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && var.vpc_cidr != "0.0.0.0/0"
    error_message = "Provide a valid non-open IPv4 VPC CIDR."
  }
}
variable "availability_zones" {
  type    = list(string)
  default = []
}
variable "public_subnet_cidrs" {
  type    = list(string)
  default = []
}
variable "private_subnet_cidrs" {
  type    = list(string)
  default = []
}
variable "database_subnet_cidrs" {
  type    = list(string)
  default = []
}
variable "cache_subnet_cidrs" {
  type    = list(string)
  default = []
}
variable "enable_nat_gateway" {
  type    = bool
  default = false
}
variable "single_nat_gateway" {
  type    = bool
  default = true
}
variable "existing_vpc_id" {
  type    = string
  default = null
}
variable "existing_public_subnet_ids" {
  type    = list(string)
  default = []
}
variable "existing_private_subnet_ids" {
  type    = list(string)
  default = []
}
variable "existing_database_subnet_ids" {
  type    = list(string)
  default = []
}
variable "existing_cache_subnet_ids" {
  type    = list(string)
  default = []
}
variable "tags" {
  type    = map(string)
  default = {}
}
