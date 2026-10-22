#!/bin/bash
# Build catalog images ON THE HOST and publish them to the host-agent's image dir with a signed manifest + catalog.pub.
#   bash build-images.sh verify ubuntu24.04            (what the bare-metal VM test needs; ~30-60 min, mostly NVIDIA downloads)
#   RELEASE=v2026.10.1 DRIVER=580.82.07 IMAGE_DIR=/var/lib/racksdock/images PYTHON=/opt/racksdock/venv/bin/python
# Afterwards the marketplace is pointed at $IMAGE_DIR/catalog.json so it only offers images that exist on this host.
#   bash build-images.sh                               (no names: re-sign the manifest for the images already there)
set -euo pipefail
trap 'echo "build-images.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
# no arguments: only (re)build, sign and verify the manifest for the images already in IMAGE_DIR
[ $# -ge 1 ] || echo "no image names given: publishing the manifest for the images already in ${IMAGE_DIR:-/var/lib/racksdock/images}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPOS="${REPOS:-$(cd "$HERE/../../.." && pwd)}"
IMAGE_DIR="${IMAGE_DIR:-/var/lib/racksdock/images}"; RELEASE="${RELEASE:-v2026.10.1}"; DRIVER="${DRIVER:-580.82.07}"
PY="${PYTHON:-/opt/racksdock/venv/bin/python}"
IMAGES="$(dirname "$PY")/racksdock-images"
mkdir -p "$IMAGE_DIR"
# libguestfs needs a readable kernel for its appliance; Ubuntu installs /boot/vmlinuz-* as 0600
for k in /boot/vmlinuz-*; do [ -r "$k" ] || chmod 0644 "$k"; done
command -v virt-customize >/dev/null || { echo "virt-customize missing: apt-get install -y libguestfs-tools"; exit 1; }
if [ $# -ge 1 ] && virsh list --name 2>/dev/null | grep -q '^racksdock-'; then
  echo "a racksdock VM is running and holds the catalog images open; stop the test (or: virsh destroy <name>) before rebuilding"; exit 1
fi
for os in "$@"; do
  CUDA=""; case "$os" in *cuda12*) CUDA="${CUDA12:-12.8}";; *cuda13*) CUDA="${CUDA13:-13.0}";; esac
  echo "== building $os ($RELEASE, driver $DRIVER${CUDA:+, CUDA $CUDA})"
  OUT_DIR="$IMAGE_DIR" PYTHON="$PY" bash "$REPOS/racksdock-node/image-build/build.sh" "$os" "$RELEASE" "$DRIVER" "$CUDA"
done
echo "== manifest"
"$IMAGES" manifest build "$IMAGE_DIR" "$RELEASE" --out "$IMAGE_DIR/manifest.json" --driver "$DRIVER"
"$IMAGES" manifest sign "$IMAGE_DIR/manifest.json" "${CATALOG_KEY:-/etc/racksdock/catalog.key}" --out "$IMAGE_DIR/manifest.signed.json" --pub-out "$IMAGE_DIR/catalog.pub"
"$IMAGES" manifest verify "$IMAGE_DIR/manifest.signed.json" "$(cat "$IMAGE_DIR/catalog.pub")" --image-dir "$IMAGE_DIR"
"$IMAGES" manifest os-options "$IMAGE_DIR/manifest.signed.json" "$(cat "$IMAGE_DIR/catalog.pub")" > "$IMAGE_DIR/catalog.json"
echo "published to $IMAGE_DIR:"; ls -la "$IMAGE_DIR"
