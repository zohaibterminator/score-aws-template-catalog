locals {
  production     = var.environment == "prod"
  engine_version = var.engine_version != null ? var.engine_version : (var.engine == "valkey" ? "7.2" : "7.1")
}
