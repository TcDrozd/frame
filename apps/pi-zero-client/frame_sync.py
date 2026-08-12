#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

MANIFEST_URL = os.environ.get("FRAME_MANIFEST_URL", "").strip()

LOCAL_MANIFEST = Path("/opt/frame/manifest.json")
STATE_FILE = Path("/opt/frame/.sync_state.json")

IMAGES_DIR = Path("/opt/frame/images")
STAGING_DIR = Path("/opt/frame/staging")

TIMEOUT = 25

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")

def stable_manifest_sig(manifest: dict) -> str:
    """
    Signature to detect meaningful changes even if ETag isn't stable.
    Only include fields that matter to the client.
    """
    slim = {
        "schema": manifest.get("schema"),
        "version": manifest.get("version"),
        "generated_at": manifest.get("generated_at"),
        "mode": manifest.get("mode"),
        "slide_seconds": manifest.get("slide_seconds"),
        "start_epoch": manifest.get("start_epoch"),
        "photos": [{"id": p.get("id"), "url": p.get("url")} for p in manifest.get("photos", [])],
    }
    raw = json.dumps(slim, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def fetch_bytes(url: str, etag: str | None):
    headers = {"User-Agent": "frame-pi/1.0"}
    if etag:
        headers["If-None-Match"] = etag

    req = Request(url, headers=headers)
    with urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read()
        new_etag = resp.headers.get("ETag")
        return data, new_etag

def download_to(tmp_path: Path, url: str):
    req = Request(url, headers={"User-Agent": "frame-pi/1.0"})
    with urlopen(req, timeout=TIMEOUT) as resp:
        if getattr(resp, "status", 200) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status', '?')}")
        tmp_file = tmp_path.with_suffix(tmp_path.suffix + ".tmp")
        with open(tmp_file, "wb") as f:
            shutil.copyfileobj(resp, f)
        tmp_file.replace(tmp_path)

def ensure_dirs():
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

def main():
    if not MANIFEST_URL:
        raise SystemExit("FRAME_MANIFEST_URL env var is required")

    ensure_dirs()
    state = load_state()
    etag = state.get("etag")

    # ---- fetch manifest (ETag-aware) ----
    try:
        raw, new_etag = fetch_bytes(MANIFEST_URL, etag=etag)
    except HTTPError as e:
        if e.code == 304:
            print("Manifest unchanged (304).")
            return
        print(f"Manifest fetch HTTP error: {e}")
        return
    except URLError as e:
        print(f"Manifest fetch failed (offline?): {e}")
        return

    manifest = json.loads(raw.decode("utf-8"))

    # Basic validation
    if manifest.get("schema") != 1:
        print(f"Unexpected schema={manifest.get('schema')}; continuing anyway.")
    photos = manifest.get("photos", [])
    if not isinstance(photos, list) or not photos:
        print("Manifest has no photos; leaving current images alone.")
        return

    sig = stable_manifest_sig(manifest)
    if state.get("sig") == sig:
        # Even if ETag changed, content we care about didn't
        print("Manifest content signature unchanged.")
        state["etag"] = new_etag or etag
        state["last_check"] = int(time.time())
        save_state(state)
        return

    # ---- download/update images ----
    downloaded = 0
    skipped = 0
    failed = 0

    for p in photos:
        pid = str(p.get("id") or "").strip()
        url = p.get("url")

        if not pid or not url:
            skipped += 1
            continue

        dest = IMAGES_DIR / pid
        if dest.exists():
            # If your publisher later adds sha256, we can validate and refresh on mismatch.
            skipped += 1
            continue

        try:
            tmp = STAGING_DIR / pid
            download_to(tmp, url)
            tmp.replace(dest)
            downloaded += 1
        except Exception as e:
            failed += 1
            print(f"Download failed for {pid}: {e}")

    # Write local manifest (so apply_manifest can read it consistently)
    LOCAL_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")

    # Save sync state
    state["etag"] = new_etag or etag
    state["sig"] = sig
    state["last_sync"] = int(time.time())
    save_state(state)

    print(f"Sync done: downloaded={downloaded} skipped={skipped} failed={failed}")

    # Apply: writes .order and updates slide seconds + restarts if needed
    # (Run as root via systemd if necessary; if this script runs as tcd, sudo will be needed)
    # rc = os.system("sudo /usr/bin/python3 /opt/frame/apply_manifest.py")
    # if rc != 0:
    #     print("apply_manifest.py failed (see logs).")

if __name__ == "__main__":
    main()
