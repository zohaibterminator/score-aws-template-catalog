locals { force_destroy = var.environment != "prod" && var.allow_nonprod_force_destroy }
