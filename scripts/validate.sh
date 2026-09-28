#!/usr/bin/env bash
# Checks for what this repository still owns: the Score capability agent.
# The Terraform modules live in score-tf-modules and the Score provisioners in score-gp-aws-rds.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT/agents/capability-agent${PYTHONPATH:+:$PYTHONPATH}"

python "$ROOT/scripts/test-capability-agent.py"
python "$ROOT/scripts/test-capability-a2a.py"
python "$ROOT/scripts/test-capability-actions.py"
python "$ROOT/scripts/test-capability-eks.py"
python "$ROOT/scripts/test-capability-network-access.py"
