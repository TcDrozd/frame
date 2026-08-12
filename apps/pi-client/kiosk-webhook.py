import ipaddress, os, re
from flask import Flask, request, jsonify

app = Flask(__name__)

TOKEN = os.environ.get("KIOSK_TOKEN", "no-go")
URL_FILE = os.environ.get("KIOSK_URL_FILE", "/var/lib/kiosk/url.txt")

# Only allow these URL prefixes (edit for your LAN/Tailscale).
# Note: https:// URLs are rejected unless a prefix below is https — the frame
# client is served over plain HTTP on the LAN, so that is deliberate.
ALLOW_PREFIXES = [
    # frame client (home server, LAN/tailnet)
    "http://frame.local", "http://frame.lan",
    # homelab dashboards
    "http://frigate.lan", "http://192.168.50.", "http://wikijs.lan",
    "http://gpu-vm.", "http://custom-dash.", "http://prox-dash.",
    "http://homeassistant.", "http://frigate.", "http://wiki.", "http://apps.",
    "http://dashboards.", "http://10.", "http://172.16.", "http://172.17.",
    "http://172.18.", "http://172.19.", "http://172.20.", "http://172.21.",
    "http://172.22.", "http://172.23.", "http://172.24.", "http://172.25.",
    "http://172.26.", "http://172.27.", "http://172.28.", "http://172.29.",
    "http://172.30.", "http://172.31.", "http://127.0.0.1", "http://localhost"
]

def allowed(url: str) -> bool:
    if not re.match(r'^https?://', url):  # force scheme
        return False
    return any(url.startswith(p) for p in ALLOW_PREFIXES)

@app.route("/kiosk/set", methods=["POST"])
def set_url():
    # Auth
    if request.headers.get("X-Kiosk-Token") != TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()

    if not allowed(url):
        return jsonify({"ok": False, "error": "url_not_allowed"}), 400

    # Save URL
    try:
        with open(URL_FILE, "w") as f:
            f.write(url + "\n")
        # Touch file to trigger systemd.path if needed
        os.utime(URL_FILE, None)
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/kiosk/get")
def get_url():
    try:
        with open(URL_FILE) as f:
            return jsonify({"ok": True, "url": f.read().strip()})
    except FileNotFoundError:
        return jsonify({"ok": True, "url": None})

if __name__ == "__main__":
    # kiosk-webhook.service runs this file directly, so it needs an entrypoint.
    # Binds all interfaces by design: the whole point is remote control from the
    # homelab. Keep it behind the LAN/tailnet and the X-Kiosk-Token check.
    app.run(
        host=os.environ.get("KIOSK_BIND", "0.0.0.0"),
        port=int(os.environ.get("KIOSK_PORT", "5000")),
    )
