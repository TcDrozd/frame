#!/usr/bin/env bash
set -euo pipefail

SRC="${SRC:-$HOME/frame/apps/client}"

TARGET="${TARGET:-v2}"  # v2 (default) or v1
if [[ "$TARGET" == "v1" ]]; then
  DST="/opt/shared-photo-frame/client"
else
  DST="/opt/frame/apps/client"
fi

VERSION_FILE="/opt/frame/VERSION"

EXCLUDES=(
  "--exclude=.git/"
  "--exclude=node_modules/"
  "--exclude=dist/"
  "--exclude=.DS_Store"
  "--exclude=.env"
)

echo "==> Deploying CLIENT ($TARGET)"
echo "    SRC: $SRC"
echo "    DST: $DST"

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: Source not found: $SRC" >&2
  exit 1
fi

if [[ "$TARGET" == "v2" ]]; then
  sudo install -d -o root -g root -m 755 /opt/frame/apps
  sudo install -d -o root -g root -m 755 "$DST"
else
  # v1 runtime is owned by spf; keep it that way
  sudo install -d -o spf -g spf -m 755 "$DST"
fi

# For static client files, rsync is perfect
sudo rsync -a --delete "${EXCLUDES[@]}" "$SRC/" "$DST/"

# Stamp (only stamps /opt/frame/VERSION)
STAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ') client(${TARGET})=$(cd "$SRC" && (git rev-parse --short HEAD 2>/dev/null || echo 'nogit'))"
echo "$STAMP" | sudo tee -a "$VERSION_FILE" >/dev/null

echo "==> Client deploy complete."
echo "NOTE: No service restart done by this script."
