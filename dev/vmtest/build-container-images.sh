#!/bin/bash
# Build container-tier catalog images ON THE HOST (docker) and publish a signed manifest + catalog.pub in the host-agent's image dir.
#   bash build-container-images.sh verify ubuntu24.04     (what a container-tier node needs; the verify image builds nccl-tests/nvbandwidth)
#   RELEASE=v2026.10.1 IMAGE_DIR=/var/lib/racksdock/images PYTHON=/opt/racksdock/venv/bin/python
set -euo pipefail
trap 'echo "build-container-images.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
HERE="$(cd "$(dirname "$0")" && pwd)"
REPOS="${REPOS:-$(cd "$HERE/../../.." && pwd)}"
IMAGE_DIR="${IMAGE_DIR:-/var/lib/racksdock/images}"; RELEASE="${RELEASE:-v2026.10.1}"
PY="${PYTHON:-/opt/racksdock/venv/bin/python}"
IMAGES="$(dirname "$PY")/racksdock-images"
mkdir -p "$IMAGE_DIR"
for os in "$@"; do
  echo "== building container image $os ($RELEASE)"
  PYTHON="$PY" bash "$REPOS/racksdock-node/container-image/build.sh" "$os" "$RELEASE"
done
echo "== manifest (container images on this host + any VM images in $IMAGE_DIR)"
"$IMAGES" manifest build "$IMAGE_DIR" "$RELEASE" --out "$IMAGE_DIR/manifest.json" --containers
"$IMAGES" manifest sign "$IMAGE_DIR/manifest.json" "${CATALOG_KEY:-/etc/racksdock/catalog.key}" --out "$IMAGE_DIR/manifest.signed.json" --pub-out "$IMAGE_DIR/catalog.pub"
"$IMAGES" manifest verify "$IMAGE_DIR/manifest.signed.json" "$(cat "$IMAGE_DIR/catalog.pub")" --image-dir "$IMAGE_DIR"
"$IMAGES" manifest os-options "$IMAGE_DIR/manifest.signed.json" "$(cat "$IMAGE_DIR/catalog.pub")" > "$IMAGE_DIR/catalog.json"
echo "published to $IMAGE_DIR:"; ls -la "$IMAGE_DIR"; docker images "racksdock/*:$RELEASE"
