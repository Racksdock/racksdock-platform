#!/bin/bash
# Publish a PATCH release ON THE PLATFORM SERVER: a new release that rebuilds only some templates and keeps every other image
# of the previous release byte for byte (hard links in the store, same `version`), so hosts download only what changed.
#   bash publish-patch-release.sh v2026.10.2 miner-test-a100 /var/lib/racksdock/release/v2026.10.2 verify
# The build host staged `manifest.json` + archives for the rebuilt templates (container-image/build.sh + `racksdock-images
# manifest build ... --containers --archive`). For a rebuilt template whose VM image was NOT rebuilt, the previous qcow2 is
# carried over under the NEW version (entry.file keeps the old file name, so hosts that have it do not download it again).
#   STORE=/var/lib/racksdock/catalog CATALOG_KEY=/etc/racksdock/catalog.key SSH_CONFIG=/workspace/nodes.config
set -euo pipefail
trap 'echo "publish-patch-release.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
RELEASE="$1"; BUILD_HOST="$2"; SRC="$3"; shift 3; REBUILT=("$@")
STORE="${STORE:-/var/lib/racksdock/catalog}"; KEY="${CATALOG_KEY:-/etc/racksdock/catalog.key}"
PY="${PYTHON:-/workspace/.venv/bin/python}"; IMAGES="$(dirname "$PY")/racksdock-images"
SSH_OPTS=(-o BatchMode=yes); [ -n "${SSH_CONFIG:-}" ] && SSH_OPTS+=(-F "$SSH_CONFIG")
PREV="$(readlink -f "$STORE/current")"; DEST="$STORE/$RELEASE"; mkdir -p "$DEST"
[ -f "$PREV/manifest.json" ] || { echo "no previous release at $STORE/current"; exit 1; }
echo "== previous release: $PREV"
echo "== pulling rebuilt templates (${REBUILT[*]}) from $BUILD_HOST:$SRC"
for f in $(ssh "${SSH_OPTS[@]}" "$BUILD_HOST" "cd $SRC && ls manifest.json *.qcow2 *.container.tar 2>/dev/null"); do
  if [ -f "$DEST/$f" ] && [ "$f" != manifest.json ]; then
    R=$(ssh "${SSH_OPTS[@]}" "$BUILD_HOST" "stat -c %s $SRC/$f"); L=$(stat -c %s "$DEST/$f"); [ "$R" = "$L" ] && { echo "   $f already here"; continue; }
  fi
  echo "   $f"; scp "${SSH_OPTS[@]}" -q "$BUILD_HOST:$SRC/$f" "$DEST/$f"
done
mv "$DEST/manifest.json" "$DEST/manifest.rebuilt.json"
echo "== carrying over unchanged images from $PREV (hard links)"
for f in "$PREV"/*.qcow2 "$PREV"/*.container.tar; do [ -f "$f" ] && [ ! -e "$DEST/$(basename "$f")" ] && ln "$f" "$DEST/$(basename "$f")" || true; done
"$PY" - "$PREV/manifest.json" "$DEST/manifest.rebuilt.json" "$DEST/manifest.json" "$RELEASE" "${REBUILT[@]}" <<'PYEOF'
import json, sys
prev, rebuilt, out, release, *names = sys.argv[1:]
prev_m, new_m = json.load(open(prev)), json.load(open(rebuilt))
new_by_os = {}
for e in new_m["images"]:
    new_by_os.setdefault(e["os"], {})[e.get("kind", "vm")] = e
images = []
for e in prev_m["images"]:
    if e["os"] not in names:
        images.append(e)                                   # unchanged: same version, same file, same digest
        continue
    kind = e.get("kind", "vm")
    fresh = new_by_os.get(e["os"], {}).get(kind)
    if fresh:
        images.append(fresh)                               # rebuilt for this kind
    else:
        # not rebuilt for this kind: carry the previous image over under the new version, keeping its file name
        version = next(iter(new_by_os[e["os"]].values()))["version"]
        images.append({**e, "version": version})
        print(f"   {e['os']} {kind}: carried over {e['file']} as {version}")
for os_, kinds in new_by_os.items():
    for kind, e in kinds.items():
        if not any(i["os"] == os_ and i.get("kind", "vm") == kind for i in images):
            images.append(e)
out_m = {**prev_m, "release": release, "images": images}
if new_m.get("host_image"):
    out_m["host_image"] = new_m["host_image"]
json.dump(out_m, open(out, "w"), indent=1)
print(f"   {len(images)} entries: " + ", ".join(f"{i['os']}/{i.get('kind','vm')}={i['version']}" for i in images))
PYEOF
echo "== signing with $KEY"
"$IMAGES" manifest sign "$DEST/manifest.json" "$KEY" --out "$DEST/manifest.signed.json" --pub-out "$DEST/catalog.pub"
"$IMAGES" manifest verify "$DEST/manifest.signed.json" "$(cat "$DEST/catalog.pub")" --image-dir "$DEST"
ln -sfn "$RELEASE" "$STORE/current"
echo "== published: $STORE/current -> $RELEASE"; ls -la "$DEST"
echo "restart the marketplace so it serves the new release; hosts pick it up within RACKSDOCK_CATALOG_SYNC_S"
