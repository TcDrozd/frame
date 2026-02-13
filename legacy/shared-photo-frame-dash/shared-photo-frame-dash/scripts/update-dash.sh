#!/usr/bin/env bash
set -euo pipefail

SERVICE="shared-photo-frame-dash.service"

SRC_DIR="/home/tcd/shared-photo-frame-dash"
DST_DIR="/opt/shared-photo-frame-dash"
OWNER="spf:spf"

echo "[shared-photo-frame-dash] Deploying from: $SRC_DIR"
echo "[shared-photo-frame-dash] Deploying to:   $DST_DIR"

if [[ ! -d "$SRC_DIR" ]]; then
  echo "ERROR: Source directory not found: $SRC_DIR" >&2
  exit 1
fi

# Make sure destination exists
sudo mkdir -p "$DST_DIR"

# Stop service before swapping code (simple + safe)
echo "[shared-photo-frame-dash] Stopping service..."
sudo systemctl stop "$SERVICE" || true

# Rsync into place:
# - Delete removed files
# - Preserve permissions/times where reasonable
# - Exclude venv so we can rebuild cleanly under /opt
#   (this avoids dependency drift and weird binary paths)
echo "[shared-photo-frame-dash] Syncing files..."
sudo rsync -a --delete \
  --exclude ".git/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude "venv/" \
  "$SRC_DIR/" "$DST_DIR/"

# Ensure ownership for runtime
echo "[shared-photo-frame-dash] Setting ownership to $OWNER..."
sudo chown -R "$OWNER" "$DST_DIR"

# Recreate venv under /opt as spf and install requirements
echo "[shared-photo-frame-dash] Rebuilding venv + installing requirements..."
sudo -u spf -H bash -lc "
  cd '$DST_DIR'
  python3 -m venv venv
  ./venv/bin/pip install --upgrade pip
  ./venv/bin/pip install -r requirements.txt
"

# Start service back up
echo "[shared-photo-frame-dash] Starting service..."
sudo systemctl start "$SERVICE"

echo "[shared-photo-frame-dash] Status:"
sudo systemctl --no-pager --full status "$SERVICE" || true

echo "[shared-photo-frame-dash] Done."
