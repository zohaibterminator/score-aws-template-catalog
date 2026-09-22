# Application stack

Primary tofu-controller root module. One Terraform CR and one Kubernetes-backed state compose the five child templates. Only this module configures the AWS provider. A Score-created `db_password` must be supplied through `varsFrom`; it is never generated here or output. The stack name and GUID determine stable resource names. Existing-VPC mode never takes ownership of existing network resources.

`terraform.tfvars.example` shows network shape only. Never put a real password in a tfvars file. See [the contract](contract.yaml) and [the integration blueprint](../../docs/PROVISIONER_CONTRACT.md).
