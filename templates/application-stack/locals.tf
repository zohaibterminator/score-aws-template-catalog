locals {
  guid_suffix = substr(replace(var.resource_guid, "-", ""), 20, 12)
  aws_name    = "${trim(substr(var.stack_name, 0, 20), "-")}-${local.guid_suffix}"
  common_tags = merge(var.extra_tags, {
    Application              = var.stack_name
    Workload                 = var.workload
    Environment              = var.environment
    Plane                    = var.plane
    ManagedBy                = "score-tofu-controller"
    ResourceGuid             = var.resource_guid
    "platform.company/plane" = var.plane
  })
}
