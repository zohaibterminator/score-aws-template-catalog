#!/usr/bin/env bash
# Render the EKS Score provisioner with score-k8s in a throwaway project and check the Terraform CR it produces.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCORE_K8S="${SCORE_K8S:-score-k8s}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

"$SCORE_K8S" init --no-sample >/dev/null 2>&1
cp "$ROOT/.score-k8s/20-eks.provisioners.yaml" .score-k8s/
cat > score.yaml <<'EOF'
apiVersion: score.dev/v1b1
metadata:
  name: eks-smoke
containers:
  main:
    image: nginx:alpine
resources:
  cluster:
    type: eks
    class: aws-terraform
    params:
      cluster_name: score-dev-eks
      aws_account_id: "123456789012"
      region: us-east-1
      kubernetes_version: "1.34"
      api_allowed_cidrs: ["203.0.113.10/32"]
EOF
"$SCORE_K8S" generate score.yaml -o manifests.yaml >/dev/null 2>&1

python - <<'EOF'
import yaml
docs = [d for d in yaml.safe_load_all(open("manifests.yaml")) if d]
[cr] = [d for d in docs if d["kind"] == "Terraform"]
spec = cr["spec"]
assert cr["apiVersion"] == "infra.contrib.fluxcd.io/v1alpha2"
assert "namespace" not in cr["metadata"], "the applier sets the namespace"
assert cr["metadata"]["name"].startswith("eks-")
assert spec["approvePlan"] == "auto" and spec["destroyResourcesOnDeletion"] is True
assert spec["path"] == "./templates/eks"
assert spec["sourceRef"] == {"kind": "GitRepository", "name": "score-aws-template-catalog", "namespace": "flux-system"}
assert spec["runnerPodTemplate"]["metadata"]["annotations"]["vault.hashicorp.com/role"] == "tf-runner-dev-role"
v = {x["name"]: x["value"] for x in spec["vars"]}
assert v["api_allowed_cidrs"] == ["203.0.113.10/32"] and v["node_desired_size"] == 2 and v["azs"] == []
assert "#" not in v["tags"]["score_resource_uid"], "AWS tag values cannot contain #"
assert spec["writeOutputsToSecret"]["name"] == "tf-output-" + cr["metadata"]["name"][len("eks-"):]
print("EKS Score provisioner renders the expected Terraform CR.")
EOF

# A missing required param must fail rendering.
sed -i '/kubernetes_version/d' score.yaml
if "$SCORE_K8S" generate score.yaml -o manifests.yaml >/dev/null 2>&1; then
  echo "rendering without kubernetes_version should have failed" >&2
  exit 1
fi
echo "EKS Score provisioner rejects a missing required param."
