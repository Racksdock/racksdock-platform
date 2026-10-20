#!/bin/bash
# Run the three repositories' test-suites.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
fail=0
for repo in racksdock-subnet racksdock-node racksdock-platform; do
  printf "%-16s " "$repo"
  out=$(cd "$ROOT/$repo" && "$PY" -m pytest 2>&1 | tail -1)
  echo "$out"
  echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed" || fail=1
done
exit $fail
