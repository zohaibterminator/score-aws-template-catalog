locals {
  azs = length(var.azs) == 2 ? var.azs : slice(data.aws_availability_zones.available.names, 0, 2)

  # Defaults carve the VPC into two large private subnets (nodes and pods use VPC IPs) and two small public ones.
  private_subnet_cidrs = length(var.private_subnet_cidrs) == 2 ? var.private_subnet_cidrs : [for i in range(2) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnet_cidrs  = length(var.public_subnet_cidrs) == 2 ? var.public_subnet_cidrs : [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, 240 + i)]

  vpc_prefix      = tonumber(split("/", var.vpc_cidr)[1])
  node_group_name = "default"

  tags = merge(var.tags, {
    cluster     = var.cluster_name
    environment = var.environment
    plane       = var.plane
    managed_by  = "score"
  })
}
