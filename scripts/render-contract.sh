#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name="${1:-application-stack}"
case "$name" in
  network|postgres|object-storage|cache|queue|application-stack) ;;
  *) echo "Unknown template: $name" >&2; exit 2 ;;
esac
cat "$ROOT/templates/$name/contract.yaml"
