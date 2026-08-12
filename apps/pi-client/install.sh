#!/usr/bin/env bash
# Install the frame kiosk onto a Raspberry Pi. Run on the Pi, as a sudoer.
#
# Reconstructed from the install heredocs that had leaked into
# kiosk-webhook.service upstream (TcDrozd/pi-dash @ 00c69ad) — it has NOT been
# re-run end to end on a fresh Pi. Read it before trusting it.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIOSK_USER="${KIOSK_USER:-kiosk}"

echo "==> Installing frame kiosk from $SRC (user: $KIOSK_USER)"

# --- packages -------------------------------------------------------------
sudo apt-get update
sudo apt-get install -y \
  xserver-xorg xinit openbox chromium-browser unclutter python3-venv

# --- user + directories ---------------------------------------------------
id -u "$KIOSK_USER" >/dev/null 2>&1 || sudo useradd -m -s /bin/bash "$KIOSK_USER"
sudo install -d -o "$KIOSK_USER" -g "$KIOSK_USER" -m 755 /var/lib/kiosk
sudo install -d -o root -g root -m 755 /opt/kiosk
sudo install -d -o root -g "$KIOSK_USER" -m 750 /etc/kiosk

# --- webhook app + venv ---------------------------------------------------
sudo install -o root -g root -m 755 "$SRC/kiosk-webhook.py" /opt/kiosk/kiosk-webhook.py
sudo -u "$KIOSK_USER" python3 -m venv "/home/$KIOSK_USER/.venv"
sudo -u "$KIOSK_USER" "/home/$KIOSK_USER/.venv/bin/pip" install -r "$SRC/requirements.txt"

# --- openbox autostart ----------------------------------------------------
sudo install -d -o "$KIOSK_USER" -g "$KIOSK_USER" -m 755 \
  "/home/$KIOSK_USER/.config/openbox"
sudo install -o "$KIOSK_USER" -g "$KIOSK_USER" -m 755 \
  "$SRC/openbox-autostart" "/home/$KIOSK_USER/.config/openbox/autostart"

# --- token + initial URL (never overwrite existing) -----------------------
if [[ ! -f /etc/kiosk/kiosk.env ]]; then
  echo "KIOSK_TOKEN=$(head -c 32 /dev/urandom | base64)" | \
    sudo tee /etc/kiosk/kiosk.env >/dev/null
  sudo chown root:"$KIOSK_USER" /etc/kiosk/kiosk.env
  sudo chmod 640 /etc/kiosk/kiosk.env
  echo "    wrote a fresh token to /etc/kiosk/kiosk.env"
else
  echo "    /etc/kiosk/kiosk.env exists — left alone"
fi

if [[ ! -f /var/lib/kiosk/url.txt ]]; then
  sudo install -o "$KIOSK_USER" -g "$KIOSK_USER" -m 644 \
    "$SRC/url.txt.example" /var/lib/kiosk/url.txt
  echo "    seeded /var/lib/kiosk/url.txt"
else
  echo "    /var/lib/kiosk/url.txt exists — left alone"
fi

# --- systemd --------------------------------------------------------------
for unit in kiosk-browser.service kiosk-webhook.service kiosk-url.path kiosk-url.service; do
  sudo install -o root -g root -m 644 "$SRC/systemd/$unit" "/etc/systemd/system/$unit"
done

sudo systemctl daemon-reload
sudo systemctl enable --now kiosk-browser.service kiosk-webhook.service kiosk-url.path

echo "==> Done. Check: systemctl status kiosk-browser kiosk-webhook kiosk-url.path"
