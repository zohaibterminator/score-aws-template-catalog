#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v tofu >/dev/null 2>&1; then
  TF=tofu
elif command -v terraform >/dev/null 2>&1; then
  TF=terraform
else
  echo "Neither tofu nor terraform is installed" >&2
  exit 2
fi

# Templates come from catalog.yaml, so adding or removing one needs no change here.
mapfile -t TEMPLATES < <(python -c "import yaml; [print(t['path'].removeprefix('./templates/')) for t in yaml.safe_load(open('$ROOT/catalog.yaml'))['templates']]")

export TF_IN_AUTOMATION=1 TF_INPUT=0 AWS_EC2_METADATA_DISABLED=true
"$TF" -chdir="$ROOT" fmt -check -recursive
for template in "${TEMPLATES[@]}"; do
  echo "Validating $template with $TF"
  lock=()
  [ -f "$ROOT/templates/$template/.terraform.lock.hcl" ] && lock=(-lockfile=readonly)
  "$TF" -chdir="$ROOT/templates/$template" init -backend=false -input=false "${lock[@]}" -no-color
  "$TF" -chdir="$ROOT/templates/$template" validate -no-color
done

python "$ROOT/scripts/validate-contracts.py"
if [ -d "$ROOT/templates/application-stack" ]; then
  python "$ROOT/scripts/test-catalog.py"
  python "$ROOT/scripts/test-console.py" "$TF"
fi
python "$ROOT/scripts/test-capability-agent.py"
python "$ROOT/scripts/test-capability-a2a.py"
python "$ROOT/scripts/test-capability-actions.py"

# Render the Score provisioners in a throwaway project (never in a workload repository).
if command -v score-k8s >/dev/null 2>&1; then
  bash "$ROOT/scripts/test-score-provisioners.sh"
else
  echo "score-k8s unavailable; Score provisioner rendering skipped"
fi

if command -v tflint >/dev/null 2>&1; then
  for template in "${TEMPLATES[@]}"; do
    tflint --chdir "$ROOT/templates/$template"
  done
else
  echo "tflint unavailable; skipped"
fi
if command -v checkov >/dev/null 2>&1; then
  checkov -d "$ROOT/templates" --framework terraform --quiet
else
  echo "checkov unavailable; skipped"
fi
