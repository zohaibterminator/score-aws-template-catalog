variable "name" { type = string }
variable "tags" {
  type    = map(string)
  default = {}
}
variable "max_receive_count" {
  type    = number
  default = 5
  validation {
    condition     = var.max_receive_count >= 1 && var.max_receive_count <= 1000
    error_message = "Invalid receive count."
  }
}
