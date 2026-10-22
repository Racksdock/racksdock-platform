#!/bin/bash
# Build a catalog release ON A BUILD HOST (KVM + libguestfs + docker): VM images (qcow2) and container images for the given
# templates, then one manifest.json with template metadata and docker-save archives, ready for publish-release.sh on the platform.
#   bash build-release.sh v2026.10.2 ubuntu24.04 pytorch-2.9-cuda13.0 verify
#   OUT_DIR=/var/lib/racksdock/release KINDS=vm,container PYTHON=/opt/racksdock/venv/bin/python
set -euo pipefail
trap 'echo "build-release.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
RELEASE="$1"; shift
HERE="$(cd "$(dirname "$0")" && pwd)"; REPOS="${REPOS:-$(cd "$HERE/../../.." && pwd)}"
OUT_DIR="${OUT_DIR:-/var/lib/racksdock/release/$RELEASE}"; KINDS="${KINDS:-vm,container}"
PY="${PYTHON:-/opt/racksdock/venv/bin/python}"; IMAGES="$(dirname "$PY")/racksdock-images"; DRIVER="${DRIVER:-580.82.07}"
mkdir -p "$OUT_DIR"
for os in "$@"; do
  if [[ ",$KINDS," == *",vm,"* ]]; then
    [ -f "$OUT_DIR/${os}_${RELEASE}.qcow2" ] && echo "== vm $os already built" || { echo "== vm image $os"; OUT_DIR="$OUT_DIR" PYTHON="$PY" bash "$REPOS/racksdock-node/image-build/build.sh" "$os" "$RELEASE" "$DRIVER"; }
  fi
  if [[ ",$KINDS," == *",container,"* ]]; then
    echo "== container image $os"; PYTHON="$PY" bash "$REPOS/racksdock-node/container-image/build.sh" "$os" "$RELEASE"
  fi
done
echo "== manifest + archives"
ARGS=(--driver "$DRIVER"); [[ ",$KINDS," == *",container,"* ]] && ARGS+=(--containers --archive)
"$IMAGES" manifest build "$OUT_DIR" "$RELEASE" --out "$OUT_DIR/manifest.json" "${ARGS[@]}"
echo "release $RELEASE staged in $OUT_DIR:"; ls -la "$OUT_DIR"
echo "next, on the platform server: bash dev/catalog/publish-release.sh $RELEASE <build-host-ssh-alias> $OUT_DIR"
