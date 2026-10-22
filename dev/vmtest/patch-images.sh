#!/bin/bash
# Copy the in-image overlay (systemd units, /usr/local/bin tools, cloud-init drop-ins) into the catalog images ALREADY built on
# this host and re-sign the manifest. Minutes instead of a full rebuild; use it while iterating on in-image tooling. Packages, the
# NVIDIA stack and the node-agent venv are NOT touched: changes to those need build-images.sh.
#   bash patch-images.sh                 (all images in IMAGE_DIR)
set -euo pipefail
trap 'echo "patch-images.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
HERE="$(cd "$(dirname "$0")" && pwd)"
REPOS="${REPOS:-$(cd "$HERE/../../.." && pwd)}"
IMAGE_DIR="${IMAGE_DIR:-/var/lib/racksdock/images}"
OVERLAY="$REPOS/racksdock-node/image-build/overlay"
for k in /boot/vmlinuz-*; do [ -r "$k" ] || chmod 0644 "$k"; done
# a running racksdock VM keeps its catalog image open (QEMU image lock) and virt-customize cannot start; the test VMs are disposable
for vm in $(virsh list --all --name 2>/dev/null | grep '^racksdock-' || true); do
  echo "== destroying leftover VM $vm"; virsh destroy "$vm" >/dev/null 2>&1 || true; virsh undefine --nvram "$vm" >/dev/null 2>&1 || true
done
for img in "$IMAGE_DIR"/*.qcow2; do
  echo "== patching $(basename "$img")"
  virt-customize -a "$img" --copy-in "$OVERLAY/usr:/" --copy-in "$OVERLAY/etc:/" \
    --run-command "systemctl enable racksdock-node-agent racksdock-launch" --selinux-relabel
done
echo "== re-signing the manifest"
bash "$HERE/build-images.sh"
