# Tofu-controller integration example

The complete Secret and Terraform CR blueprint is in [`docs/PROVISIONER_CONTRACT.md`](../../docs/PROVISIONER_CONTRACT.md). Render it only through a later Score provisioner so GUID, resource UID, password, network inputs, and labels come from stable Score state. A distinct `score-aws-template-catalog` GitRepository in `flux-system` must point to a reviewed catalog tag; keep the legacy `score-provisioner-modules` source for existing RDS CRs.
