#!/bin/bash
# Create the shared venv and install the three repos editable (expects racksdock-subnet, racksdock-node, racksdock-platform side by side).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv --without-pip "$VENV"
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && "$VENV/bin/python" /tmp/get-pip.py -q
fi
PIP="$VENV/bin/python -m pip"
$PIP install -q --upgrade pip
for repo in racksdock-subnet racksdock-node racksdock-platform; do echo "installing $repo"; $PIP install -q -e "$ROOT/$repo"; done
for repo in racksdock-subnet racksdock-node racksdock-platform; do $PIP install -q -e "$ROOT/$repo[test]"; done
echo "done: activate with 'source $VENV/bin/activate'"
