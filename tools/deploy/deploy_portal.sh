#!/usr/bin/env bash
set -euo pipefail

SRC="${SRC:-$HOME/frame/apps/portal}"
DST="${DST:-/opt/frame/apps/portal}"
VERSION_FILE="/opt/frame/VERSION"

# rsync exclusions: keep repo/dev junk out of /opt
EXCLUDES=(
  "--exclude=.git/"
  "--exclude=.venv/"
  "--exclude=venv/"
  "--exclude=__pycache__/"
  "--exclude=*.pyc"
  "--exclude=.DS_Store"
  "--exclude=.env"              # IMPORTANT: keep secrets out of /opt unless you intentionally deploy them
  "--exclude=data/"             # dev DB lives here; prod DB is /opt/frame/var/portal/portal.db
  "--exclude=instance/"         # if you use flask instance locally
)

echo "==> Deploying PORTAL"
echo "    SRC: $SRC"
echo "    DST: $DST"

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: Source not found: $SRC" >&2
  exit 1
fi

sudo install -d -o root -g root -m 755 /opt/frame/apps
sudo install -d -o root -g root -m 755 "$DST"

# Sync code
sudo rsync -a --delete "${EXCLUDES[@]}" "$SRC/" "$DST/"

# Optional: stamp version
STAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ') portal=$(cd "$SRC" && (git rev-parse --short HEAD 2>/dev/null || echo 'nogit'))"
echo "$STAMP" | sudo tee -a "$VERSION_FILE" >/dev/null

echo "==> Portal deploy complete."
echo "NOTE: No service restart done by this script."
