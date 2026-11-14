#!/usr/bin/env bash
set -euo pipefail
# These runtime deployments overwrite files with rsync --delete.
if [[ "${FRAME_PRODUCTION_INTENT:-}" != frame-runtime || "$#" != 1 || "$1" != --apply ]]; then
  echo 'Refused: production intent FRAME_PRODUCTION_INTENT=frame-runtime and --apply required.' >&2
  exit 2
fi

SRC="${SRC:-$HOME/frame/apps/portal}"

# rsync --delete mirrors SRC: an empty or wrong SRC would wipe DST. Require a
# real checkout of this repository containing the expected entry file.
[[ -n "$SRC" && -f "$SRC/app/main.py" ]] || { echo "Refused: SRC must contain app/main.py: ${SRC:-<empty>}" >&2; exit 2; }
git -C "$SRC" rev-parse --show-toplevel >/dev/null 2>&1 || { echo "Refused: SRC is not inside a git checkout: $SRC" >&2; exit 2; }
DST="${DST:-/opt/frame/apps/portal}"
[[ "$DST" == /opt/frame/apps/portal ]] || { echo 'Refused: portal destination must be /opt/frame/apps/portal' >&2; exit 2; }
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
