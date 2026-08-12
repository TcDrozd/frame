#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3


IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch_now_utc() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def is_image_key(key: str) -> bool:
    k = key.lower()
    return any(k.endswith(ext) for ext in IMG_EXTS)


@dataclass
class ObjInfo:
    key: str
    size: Optional[int]


def list_objects(s3, bucket: str, prefix: str) -> list[ObjInfo]:
    """List all image objects under prefix."""
    out: list[ObjInfo] = []
    token: Optional[str] = None

    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token

        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            if not is_image_key(key):
                continue
            out.append(ObjInfo(key=key, size=obj.get("Size")))

        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    return out


def normalize_pin(pin: str, photos_prefix: str) -> tuple[str, str]:
    """Return (pin_key, pin_filename). Accepts filename or full key."""
    pin = pin.strip()
    if not pin:
        return ("", "")

    if "/" in pin:
        pin_key = pin
    else:
        pin_key = photos_prefix.rstrip("/") + "/" + pin

    pin_filename = pin_key.split("/")[-1]
    return (pin_key, pin_filename)


def reorder_with_pin_first(objs: list[ObjInfo], pin_key: str, pin_filename: str) -> list[ObjInfo]:
    if not pin_key and not pin_filename:
        return objs

    pinned: Optional[ObjInfo] = None

    # Prefer exact key match; fallback to filename match
    for o in objs:
        if o.key == pin_key:
            pinned = o
            break
    if pinned is None:
        for o in objs:
            if o.key.split("/")[-1] == pin_filename:
                pinned = o
                break

    if pinned is None:
        print(f'WARN: --pin-first "{pin_key or pin_filename}" not found in bucket listing; ignoring.')
        return objs

    rest = [o for o in objs if o.key != pinned.key]
    return [pinned] + rest


def _seeded_shuffle(items: list[ObjInfo], seed_str: str) -> list[ObjInfo]:
    # stable deterministic shuffle
    seed = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest(), 16) % (2**32)
    rnd = random.Random(seed)
    out = list(items)
    rnd.shuffle(out)
    return out


def apply_shuffle(items: list[ObjInfo], shuffle: str, seed_key: str) -> list[ObjInfo]:
    if shuffle == "none":
        return items

    if shuffle == "random":
        out = list(items)
        random.shuffle(out)
        return out

    # daily deterministic shuffle
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _seeded_shuffle(items, f"{seed_key}:{today}")


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text("utf-8"))
    except Exception:
        return {}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, state_path)


def select_window(items: list[ObjInfo], limit: int, offset: int) -> list[ObjInfo]:
    """Select a wrap-around window of size limit starting at offset."""
    if limit <= 0:
        return []
    if not items:
        return []
    n = len(items)
    offset = offset % n
    if limit >= n:
        return list(items)

    end = offset + limit
    if end <= n:
        return list(items[offset:end])

    # wrap
    first = items[offset:]
    second = items[: end % n]
    return list(first) + list(second)


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish a bounded working-set manifest.json from S3 photos/ using presigned URLs")
    ap.add_argument("--bucket", default="trevor-shared-photo-stream")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--photos-prefix", default="photos/")
    ap.add_argument("--manifest-key", default="manifest.json")
    ap.add_argument("--expires", type=int, default=12 * 60 * 60, help="presign expiry seconds (default 12h)")

    ap.add_argument("--mode", default="inventory", choices=["inventory", "sync"])
    ap.add_argument("--slide-seconds", type=int, default=12 * 60 * 60)
    ap.add_argument("--schema", type=int, default=1)
    ap.add_argument("--version", default=None)

    # sync controls
    ap.add_argument(
        "--start-epoch",
        default=None,
        help='Sync start epoch in UTC seconds. Use "now" to set start to current time. Only used in --mode sync.',
    )

    # working set controls
    ap.add_argument(
        "--limit",
        type=int,
        default=30,
        help="How many photos to include in the functional manifest (bounded working set).",
    )
    ap.add_argument(
        "--state-file",
        default="./.publish_state.json",
        help="Local state file used to remember window offset across publishes.",
    )
    ap.add_argument(
        "--advance",
        type=int,
        default=0,
        help="Advance the window offset by N before selecting (e.g., 5 to rotate 5 new photos into the set).",
    )

    # ordering controls
    ap.add_argument(
        "--shuffle",
        default="daily",
        choices=["none", "daily", "random"],
        help="Shuffle ordering of the functional set (default: daily deterministic shuffle).",
    )

    ap.add_argument(
        "--pin-first",
        default=None,
        help='Pin a specific photo to index 0 (accepts filename like IMG_1234.jpg or full key like photos/IMG_1234.jpg).',
    )

    args = ap.parse_args()

    session = boto3.session.Session(region_name=args.region)
    s3 = session.client("s3")

    # 1) list library from S3 (this is the master list)
    library = list_objects(s3, args.bucket, args.photos_prefix)
    library.sort(key=lambda o: o.key.lower())  # stable base ordering

    if not library:
        raise SystemExit(f"No images found under s3://{args.bucket}/{args.photos_prefix}")

    # 2) load + update window state
    state_path = Path(args.state_file)
    state = load_state(state_path)
    offset = int(state.get("offset", 0))
    if args.advance:
        offset += int(args.advance)

    # 3) pick bounded working set
    working = select_window(library, args.limit, offset)

    # 4) pin-first should always win inside the working set.
    # If the pinned item is not in the window, we swap it in (replacing the last item).
    pin_key, pin_filename = ("", "")
    if args.pin_first:
        pin_key, pin_filename = normalize_pin(args.pin_first, args.photos_prefix)

        # try to find pinned object in full library
        pinned_obj: Optional[ObjInfo] = None
        for o in library:
            if o.key == pin_key:
                pinned_obj = o
                break
        if pinned_obj is None:
            for o in library:
                if o.key.split("/")[-1] == pin_filename:
                    pinned_obj = o
                    break

        if pinned_obj is None:
            print(f'WARN: --pin-first "{args.pin_first}" not found in bucket; ignoring.')
        else:
            if all(o.key != pinned_obj.key for o in working):
                # replace last item to keep limit fixed
                if working:
                    working[-1] = pinned_obj
                else:
                    working = [pinned_obj]
            # move it to index 0
            working = reorder_with_pin_first(working, pinned_obj.key, pinned_obj.key.split("/")[-1])

    # 5) shuffle ordering of the working set (after pin-first is placed at index 0)
    # We shuffle everything after index 0 so pinned stays pinned.
    seed_key = f"{args.bucket}/{args.photos_prefix}"
    if args.shuffle != "none" and len(working) > 1:
        head = working[0:1]
        tail = working[1:]
        tail = apply_shuffle(tail, args.shuffle, seed_key)
        working = head + tail

    # 6) persist updated offset state
    state["offset"] = offset
    state["updated_at"] = utc_now_z()
    state["bucket"] = args.bucket
    state["photos_prefix"] = args.photos_prefix
    state["limit"] = args.limit
    save_state(state_path, state)

    # 7) presign urls + build photos[]
    photos = []
    for o in working:
        key = o.key
        filename = key.split("/")[-1]
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": args.bucket, "Key": key},
            ExpiresIn=args.expires,
        )
        photos.append(
            {
                "id": filename,
                "url": url,
                "name": filename.rsplit(".", 1)[0].replace("_", " "),
                "bytes": o.size,
            }
        )

    # 8) manifest
    version = args.version or datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%SZ")
    manifest = {
        "schema": args.schema,
        "version": version,
        "generated_at": utc_now_z(),
        "mode": args.mode,
        "slide_seconds": args.slide_seconds,
        "photos": photos,
    }

    if args.mode == "sync":
        if args.start_epoch is None:
            start_epoch = epoch_now_utc()
        else:
            se = str(args.start_epoch).strip().lower()
            start_epoch = epoch_now_utc() if se == "now" else int(se)
        manifest["start_epoch"] = start_epoch

    body = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")

    s3.put_object(
        Bucket=args.bucket,
        Key=args.manifest_key,
        Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-store, max-age=0",
    )

    print(f"Uploaded s3://{args.bucket}/{args.manifest_key} with {len(photos)} photos (limit={args.limit})")
    print(f"Mode: {args.mode} | slide_seconds={args.slide_seconds} | shuffle={args.shuffle}")
    if args.mode == "sync":
        print(f"start_epoch={manifest['start_epoch']}")
    if args.pin_first:
        print(f"pin_first={args.pin_first}")
    if args.advance:
        print(f"advanced_by={args.advance} (new offset={offset})")
    print(f"State file: {state_path.resolve()}")
    print(f"Manifest URL: https://{args.bucket}.s3.{args.region}.amazonaws.com/{args.manifest_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())