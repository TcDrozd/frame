#!/usr/bin/env bash
set -euo pipefail

SRC="${SRC:-$HOME/frame/apps/publisher-api}"
DST="${DST:-/opt/frame/apps/publisher-api}"
VERSION_FILE="/opt/frame/VERSION"

EXCLUDES=(
  "--exclude=.git/"
  "--exclude=.venv/"
  "--exclude=venv/"
  "--exclude=__pycache__/"
  "--exclude=*.pyc"
  "--exclude=.DS_Store"
  "--exclude=.env"          # keep secrets local or in /etc/..., not in /opt by default
  "--exclude=var/"          # runtime var lives in /opt/frame/var/publisher-api/*
  "--exclude=instance/"     # if used by flask
  "--exclude=.publish_state.json"  # optional: decide if state belongs in /opt/frame/var instead
)

echo "==> Deploying PUBLISHER-API"
echo "    SRC: $SRC"
echo "    DST: $DST"

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: Source not found: $SRC" >&2
  exit 1
fi

sudo install -d -o root -g root -m 755 /opt/frame/apps
sudo install -d -o root -g root -m 755 "$DST"

sudo rsync -a --delete "${EXCLUDES[@]}" "$SRC/" "$DST/"

STAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ') publisher-api=$(cd "$SRC" && (git rev-parse --short HEAD 2>/dev/null || echo 'nogit'))"
echo "$STAMP" | sudo tee -a "$VERSION_FILE" >/dev/null

echo "==> Publisher API deploy complete."
echo "NOTE: No service restart done by this script."
