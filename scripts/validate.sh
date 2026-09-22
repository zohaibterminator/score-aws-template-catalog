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

export TF_IN_AUTOMATION=1 TF_INPUT=0 AWS_EC2_METADATA_DISABLED=true
"$TF" -chdir="$ROOT" fmt -check -recursive
for template in network postgres object-storage cache queue application-stack; do
  echo "Validating $template with $TF"
  "$TF" -chdir="$ROOT/templates/$template" init -backend=false -input=false -lockfile=readonly -no-color
  "$TF" -chdir="$ROOT/templates/$template" validate -no-color
done

python "$ROOT/scripts/validate-contracts.py"
python "$ROOT/scripts/test-catalog.py"
python "$ROOT/scripts/test-console.py" "$TF"
if command -v tflint >/dev/null 2>&1; then
  for template in network postgres object-storage cache queue application-stack; do
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
