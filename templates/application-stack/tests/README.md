# Root contract tests

`scripts/test-catalog.py` exercises minimal, complete, existing-VPC, all-disabled, invalid-CIDR, production guard, sensitivity, and output-type cases against this template and its input schema. `scripts/test-console.py` evaluates stable names and common tags in the real Terraform expression engine without a plan. `scripts/validate.sh` runs both after root initialization.
