output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_arn" {
  description = "EKS cluster ARN."
  value       = module.eks.cluster_arn
}

output "cluster_endpoint" {
  description = "Kubernetes API server endpoint."
  value       = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  description = "Base64-encoded cluster CA certificate, for kubeconfig."
  value       = module.eks.cluster_certificate_authority_data
}

output "cluster_version" {
  description = "Kubernetes version the control plane runs."
  value       = module.eks.cluster_version
}

output "oidc_provider_arn" {
  description = "IAM OIDC provider ARN, for IAM roles for service accounts."
  value       = module.eks.oidc_provider_arn
}

output "node_group_name" {
  description = "Managed node group name."
  value       = local.node_group_name
}

output "node_group_status" {
  description = "Managed node group status reported by EKS."
  value       = module.eks.eks_managed_node_groups[local.node_group_name].node_group_status
}

output "vpc_id" {
  description = "ID of the VPC created for the cluster."
  value       = module.vpc.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs (nodes and control plane network interfaces)."
  value       = module.vpc.private_subnets
}

output "public_subnet_ids" {
  description = "Public subnet IDs (NAT gateway and internet-facing load balancers)."
  value       = module.vpc.public_subnets
}

output "region" {
  description = "AWS region of the cluster."
  value       = var.region
}

output "aws_account_id" {
  description = "AWS account the runner's credentials belong to (checked against aws_account_id)."
  value       = data.aws_caller_identity.runner.account_id
}

output "kubeconfig_command" {
  description = "Command that writes a kubeconfig entry for the cluster."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}
