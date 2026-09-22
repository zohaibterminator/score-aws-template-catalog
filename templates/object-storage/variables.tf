variable "name" { type = string }
variable "environment" { type = string }
variable "allow_nonprod_force_destroy" {
  type    = bool
  default = false
}
variable "tags" {
  type    = map(string)
  default = {}
}
