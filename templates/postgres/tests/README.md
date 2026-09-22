# PostgreSQL checks

`scripts/validate.sh` initializes this directory without a backend and validates the pinned RDS and security-group modules. Production protection and password sensitivity are checked by `scripts/test-catalog.py` and the contract validator.
