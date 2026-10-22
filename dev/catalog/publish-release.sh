#!/bin/bash
# Publish a staged release ON THE PLATFORM SERVER: pull the files from the build host, sign the manifest with the platform's
# catalog key, verify, and point /catalog/current at it. Hosts then fetch it (racksdock-images fetch / host-agent auto-sync).
#   bash publish-release.sh v2026.10.2 miner-test-8x4090 /var/lib/racksdock/release/v2026.10.2
#   STORE=/var/lib/racksdock/catalog CATALOG_KEY=/etc/racksdock/catalog.key SSH_CONFIG=/workspace/nodes.config
set -euo pipefail
trap 'echo "publish-release.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
RELEASE="$1"; BUILD_HOST="$2"; SRC="$3"
STORE="${STORE:-/var/lib/racksdock/catalog}"; KEY="${CATALOG_KEY:-/etc/racksdock/catalog.key}"
PY="${PYTHON:-/workspace/.venv/bin/python}"; IMAGES="$(dirname "$PY")/racksdock-images"
SSH_OPTS=(-o BatchMode=yes); [ -n "${SSH_CONFIG:-}" ] && SSH_OPTS+=(-F "$SSH_CONFIG")
DEST="$STORE/$RELEASE"; mkdir -p "$DEST"
echo "== pulling $BUILD_HOST:$SRC -> $DEST"
for f in $(ssh "${SSH_OPTS[@]}" "$BUILD_HOST" "cd $SRC && ls manifest.json *.qcow2 *.container.tar 2>/dev/null"); do
  if [ -f "$DEST/$f" ] && [ "$f" != manifest.json ]; then
    R=$(ssh "${SSH_OPTS[@]}" "$BUILD_HOST" "stat -c %s $SRC/$f"); L=$(stat -c %s "$DEST/$f")
    [ "$R" = "$L" ] && { echo "   $f already here"; continue; }
  fi
  echo "   $f"; scp "${SSH_OPTS[@]}" -q "$BUILD_HOST:$SRC/$f" "$DEST/$f"
done
echo "== signing with $KEY"
"$IMAGES" manifest sign "$DEST/manifest.json" "$KEY" --out "$DEST/manifest.signed.json" --pub-out "$DEST/catalog.pub"
"$IMAGES" manifest verify "$DEST/manifest.signed.json" "$(cat "$DEST/catalog.pub")" --image-dir "$DEST"
ln -sfn "$RELEASE" "$STORE/current"
echo "== published: $STORE/current -> $RELEASE"; ls -la "$DEST"
echo "point RACKSDOCK_CATALOG_MANIFEST at $STORE/current/manifest.signed.json and restart the marketplace if the release changed"
