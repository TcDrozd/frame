#!/usr/bin/env python3
import fcntl
import os
import signal
import struct
import time
import json
from pathlib import Path
from typing import List, Tuple, Optional

from PIL import Image

FB = "/dev/fb0"
DEFAULT_DIR = "/opt/frame/images"
DEFAULT_SECONDS = 10
MANIFEST_FILE = Path("/opt/frame/manifest.json")
MANIFEST_POLL_SEC = 2
BASE_DIR = Path(__file__).resolve().parent
ORDER_FILE = BASE_DIR / ".order"


# ioctl constants from linux/fb.h
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602

shutdown = False

def handle_signal(signum, frame):
    global shutdown
    shutdown = True

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

def get_fb_info():
    with open(FB, "rb") as f:
        vinfo = fcntl.ioctl(f, FBIOGET_VSCREENINFO, bytes(160))
        xres, yres = struct.unpack_from("II", vinfo, 0)
        bpp = struct.unpack_from("I", vinfo, 24)[0]

        finfo = fcntl.ioctl(f, FBIOGET_FSCREENINFO, bytes(68))
        line_length = struct.unpack_from("I", finfo, 48)[0]

    return {"xres": xres, "yres": yres, "bpp": bpp, "line_length": line_length}

def fit_contain(img_w: int, img_h: int, screen_w: int, screen_h: int) -> Tuple[int, int, int, int]:
    scale = min(screen_w / img_w, screen_h / img_h)
    new_w = max(1, int(img_w * scale))
    new_h = max(1, int(img_h * scale))
    x = (screen_w - new_w) // 2
    y = (screen_h - new_h) // 2
    return new_w, new_h, x, y

def rgb_to_rgb565(im: Image.Image) -> bytes:
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()
    out = bytearray(w * h * 2)
    idx = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[idx] = v & 0xFF
            out[idx + 1] = (v >> 8) & 0xFF
            idx += 2
    return bytes(out)

def render_to_framebuffer(img_path: Path, fb_info, fb_fd, cached: Optional[dict] = None):
    sw, sh, bpp = fb_info["xres"], fb_info["yres"], fb_info["bpp"]

    # Simple cache to avoid re-resizing if the same image repeats
    cache_key = (str(img_path), sw, sh, bpp)
    if cached is not None and cache_key in cached:
        fb_fd.seek(0)
        fb_fd.write(cached[cache_key])
        fb_fd.flush()
        return

    im = Image.open(img_path)
    im.load()  # force decode now

    # Resize + letterbox
    new_w, new_h, x, y = fit_contain(im.width, im.height, sw, sh)
    im = im.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGB", (sw, sh), (0, 0, 0))
    canvas.paste(im, (x, y))

    if bpp == 16:
        buf = rgb_to_rgb565(canvas)
    elif bpp == 32:
        # BGRX little-endian
        buf = canvas.convert("RGB").tobytes("raw", "BGRX")
    else:
        raise RuntimeError(f"Unsupported framebuffer bpp={bpp} (expected 16 or 32)")

    fb_fd.seek(0)
    fb_fd.write(buf)
    fb_fd.flush()

    if cached is not None:
        # Keep cache bounded
        if len(cached) > 50:
            cached.clear()
        cached[cache_key] = buf

manifest = None
manifest_mtime = 0.0

def load_manifest_if_changed():
    global manifest, manifest_mtime
    try:
        st = MANIFEST_FILE.stat()
        if st.st_mtime != manifest_mtime:
            manifest = json.loads(MANIFEST_FILE.read_text())
            manifest_mtime = st.st_mtime
    except FileNotFoundError:
        manifest = None
        manifest_mtime = 0.0
    return manifest

def compute_sync_index(start_epoch: int, slide_seconds: int, n: int, now: float) -> int:
    if n <= 0:
        return 0
    if slide_seconds <= 0:
        slide_seconds = 1
    elapsed = now - float(start_epoch)
    if elapsed < 0:
        elapsed = 0
    steps = int(elapsed // float(slide_seconds))
    return steps % n

def seconds_until_next(start_epoch: int, slide_seconds: int, now: float) -> float:
    slide_seconds = max(1, int(slide_seconds))
    elapsed = now - float(start_epoch)
    if elapsed < 0:
        elapsed = 0
    steps = int(elapsed // slide_seconds)
    next_change = float(start_epoch) + float((steps + 1) * slide_seconds)
    return max(0.2, next_change - now)

def list_images_ordered(img_dir: Path) -> List[Path]:
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}  # gif will show first frame only

    # If an order file exists, follow it
    if ORDER_FILE.exists():
        names = [line.strip() for line in ORDER_FILE.read_text().splitlines() if line.strip()]
        ordered = []
        for name in names:
            p = img_dir / name
            if p.exists() and p.is_file() and p.suffix.lower() in exts:
                ordered.append(p)

        # If order file produced at least one valid image, use it
        if ordered:
            return ordered

    # Fallback: directory scan (sorted)
    paths = [p for p in img_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    paths.sort()
    return paths

def main():
    img_dir = Path(os.environ.get("FRAME_IMG_DIR", DEFAULT_DIR))
    slide_seconds = int(os.environ.get("FRAME_SECONDS", str(DEFAULT_SECONDS)))
    slide_seconds_default = slide_seconds

    fb_info = get_fb_info()
    print(f"Framebuffer: {fb_info} | dir={img_dir} | seconds={slide_seconds}", flush=True)

    if not img_dir.exists():
        raise SystemExit(f"Image directory not found: {img_dir}")

    cache = {}

    with open(FB, "r+b", buffering=0) as fb_fd:
        idx = 0
        last_paths_sig = None
        last_scan = 0.0

        last_manifest_check = 0.0
        manifest = None

        # Local fallback list for non-sync mode
        paths: List[Path] = []
        idx = 0

        while not shutdown:
            now = time.time()

            # refresh manifest occasionally
            if now - last_manifest_check > MANIFEST_POLL_SEC:
                manifest = load_manifest_if_changed()
                last_manifest_check = now

            mode = (manifest or {}).get("mode")
            photos = (manifest or {}).get("photos") or []
            start_epoch = (manifest or {}).get("start_epoch")
            manifest_slide = (manifest or {}).get("slide_seconds", slide_seconds_default)

            if (
                mode in ("sync", "inventory")
                and isinstance(manifest_slide, (int, float))
                and isinstance(photos, list)
                and len(photos) > 0
            ):
                n = len(photos)
                slide_s = max(1, int(manifest_slide))

                if mode == "sync" and isinstance(start_epoch, (int, float)):
                    # Sync mode: deterministic index based on start_epoch
                    se = int(start_epoch)
                    idx = compute_sync_index(se, slide_s, n, now)
                    sleep_for = seconds_until_next(se, slide_s, now)
                else:
                    # Inventory mode: loop locally through manifest list
                    idx = idx % n
                    sleep_for = float(slide_s)

                entry = photos[idx] if idx < n else None
                photo_id = entry.get("id") if isinstance(entry, dict) else None

                if photo_id:
                    img_path = img_dir / str(photo_id)
                    if img_path.exists():
                        render_to_framebuffer(img_path, fb_info, fb_fd, cached=cache)
                    else:
                        # Missing file: wait briefly and retry next loop
                        time.sleep(0.5)

                # Advance for inventory mode (sync mode index is computed)
                if mode != "sync":
                    idx += 1

            else:
                # fallback: local ordered list + env-controlled seconds
                if not paths:
                    paths = list_images_ordered(img_dir)

                if not paths:
                    time.sleep(1)
                    continue

                path = paths[idx % len(paths)]
                render_to_framebuffer(path, fb_info, fb_fd, cached=cache)
                idx += 1
                sleep_for = slide_seconds_default


            end = time.time() + float(sleep_for)
            while not shutdown and time.time() < end:
                time.sleep(0.2)

if __name__ == "__main__":
    main()
