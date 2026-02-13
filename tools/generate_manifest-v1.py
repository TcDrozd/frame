#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}  # keep it simple


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos-dir", default="photos", help="Directory containing photo files")
    ap.add_argument("--out", default="manifest.json", help="Output manifest path")
    ap.add_argument("--url-prefix", default="/photos/", help="URL prefix for photos (e.g. /photos/ or https://bucket/.../photos/)")
    ap.add_argument("--mode", default="inventory", choices=["inventory", "sync"])
    ap.add_argument("--slide-seconds", type=int, default=3600)
    ap.add_argument("--schema", type=int, default=1)
    ap.add_argument("--version", default=None, help="If omitted, auto-generate a timestamped version")
    ap.add_argument("--include-sha256", action="store_true")
    args = ap.parse_args()

    photos_dir = Path(args.photos_dir)
    if not photos_dir.exists():
        raise SystemExit(f"photos dir not found: {photos_dir.resolve()}")

    url_prefix = args.url_prefix
    if not url_prefix.endswith("/"):
        url_prefix += "/"

    files = []
    for p in photos_dir.iterdir():
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            files.append(p)

    files.sort(key=lambda p: p.name.lower())  # stable & predictable

    photos = []
    for p in files:
        entry = {
            "id": p.name,
            "url": f"{url_prefix}{p.name}",
            "name": p.stem.replace("_", " "),
            "bytes": p.stat().st_size,
        }
        if args.include_sha256:
            entry["sha256"] = sha256_file(p)
        photos.append(entry)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    version = args.version or datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%SZ")

    manifest = {
        "schema": args.schema,
        "version": version,
        "generated_at": now,
        "mode": args.mode,
        "slide_seconds": args.slide_seconds,
        "photos": photos,
    }

    out_path = Path(args.out)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, out_path)  # atomic write

    print(f"Wrote {out_path} with {len(photos)} photos (mode={args.mode}, slide_seconds={args.slide_seconds})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())