#!/usr/bin/env python3
from __future__ import annotations

# -----------------------------------------------------------------------------
# publish_manifest.py
#
# Publishes a bounded "working set" manifest.json for the shared photo frame,
# using presigned S3 URLs.
#
# Key behavior to remember:
#   S3 keys are lexicographically sorted. If we select a window (offset/limit)
#   from that sorted list, the window can get dominated by whichever folder sorts
#   first (e.g. photos/01_scans/...).
#
#   To avoid the manifest feeling "stuck" in one folder, we support --mix, which
#   interleaves the full library across groups BEFORE window selection.
#     --mix collection (default): balance across photos/<collection>/... folders
#     --mix prefix: balance across explicit --photos-prefixes values
#     --mix none: no interleaving (old behavior)
#
#   Then --shuffle applies within the selected window (pin-first stays at index 0).
# -----------------------------------------------------------------------------

from collections import deque
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
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key", "")
            if key and is_image_key(key):
                out.append(ObjInfo(key=key, size=obj.get("Size")))
    return out


def normalize_pin(pin: str, default_prefix: str) -> tuple[str, str]:
    """
    If pin looks like a full key (contains '/'), return it as key.
    If it looks like a filename, return default_prefix + filename.
    Also return filename (basename) for fallback matching.
    """
    p = pin.strip()
    if "/" in p:
        return p, p.split("/")[-1]
    pfx = default_prefix if default_prefix.endswith("/") else default_prefix + "/"
    return pfx + p, p


def reorder_with_pin_first(objs: list[ObjInfo], pin_key: str, pin_filename: str) -> list[ObjInfo]:
    """Ensure pinned object is at index 0 if present in objs. Otherwise unchanged."""
    pinned: Optional[ObjInfo] = None
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


def _mix_key_for_obj(key: str, mix: str, include_prefixes: list[str]) -> str:
    """Return the group key used for interleaving prior to window selection."""
    if mix == "none":
        return "__all__"

    if mix == "prefix":
        # Choose the most-specific matching include prefix (longest match).
        matches = [p for p in include_prefixes if key.startswith(p)]
        return max(matches, key=len) if matches else "__unknown_prefix__"

    # mix == "collection"
    # Convention: photos/<collection>/...
    parts = key.split("/")
    if len(parts) >= 2 and parts[0] == "photos":
        return parts[1] or "__no_collection__"
    return "__not_photos__"


def interleave_library(
    library: list[ObjInfo],
    mix: str,
    shuffle: str,
    seed_key: str,
    include_prefixes: list[str],
) -> list[ObjInfo]:
    """
    Interleave objects across groups so a bounded window doesn't get dominated by
    whichever prefix/folder sorts first.

    Runs BEFORE select_window(). Reuses shuffle controls (none/daily/random) so
    interleaving can be deterministic per-day if desired.
    """
    if mix == "none" or len(library) <= 1:
        return library

    groups: dict[str, list[ObjInfo]] = {}
    for o in library:
        gk = _mix_key_for_obj(o.key, mix, include_prefixes)
        groups.setdefault(gk, []).append(o)

    group_keys = sorted(groups.keys())

    # Optionally shuffle group order (deterministic daily or fully random).
    if shuffle != "none" and len(group_keys) > 1:
        if shuffle == "random":
            random.shuffle(group_keys)
        else:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            group_objs = [ObjInfo(key=k, size=None) for k in group_keys]
            group_keys = [o.key for o in _seeded_shuffle(group_objs, f"{seed_key}:groups:{today}")]

    # Optionally shuffle within each group.
    for k, items in list(groups.items()):
        if shuffle == "none" or len(items) <= 1:
            continue
        if shuffle == "random":
            random.shuffle(items)
        else:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            groups[k] = _seeded_shuffle(items, f"{seed_key}:group:{k}:{today}")

    # Round-robin interleave.
    key_order = [k for k in group_keys if groups.get(k)]
    queues = deque(deque(groups[k]) for k in key_order)
    out: list[ObjInfo] = []
    while queues:
        q = queues.popleft()
        if q:
            out.append(q.popleft())
        if q:
            queues.append(q)
    return out


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



def load_injected_keys(path: str) -> list[str]:
    """Load newline-separated S3 keys from a text file.

    - Strips whitespace
    - Ignores blank lines
    - Ignores lines starting with '#'
    - De-duplicates while preserving first-seen order
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in raw:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def inject_into_working_set(
    *,
    working: list[ObjInfo],
    library_by_key: dict[str, ObjInfo],
    injected_keys: list[str],
    placement: str,
    limit: int,
    ignore_limit: bool,
    strict: bool,
    seed_key: str,
) -> list[ObjInfo]:
    """Return a new working set containing injected keys + existing working set, with sane defaults."""
    if not injected_keys:
        return working

    injected_objs: list[ObjInfo] = []
    missing: list[str] = []
    for k in injected_keys:
        obj = library_by_key.get(k)
        if obj is None:
            missing.append(k)
            continue
        injected_objs.append(obj)

    if missing:
        msg = f"Injected keys missing from bucket listing: {len(missing)}\n" + "\n".join(f"  - {k}" for k in missing[:20])
        if len(missing) > 20:
            msg += f"\n  ... (+{len(missing)-20} more)"
        if strict:
            raise SystemExit("ERROR: " + msg)
        print("WARN: " + msg)

    injected_set = {o.key for o in injected_objs}
    base = [o for o in working if o.key not in injected_set]

    if placement == "random":
        # Deterministic-ish random insertion before shuffle is applied; shuffle can still reorder later.
        rng = _seeded_shuffle_rng(seed_key + "|inject")
        merged = list(base)
        for o in injected_objs:
            idx = rng.randrange(0, len(merged) + 1)
            merged.insert(idx, o)
        combined = merged
    else:
        combined = injected_objs + base

    if ignore_limit:
        return combined

    # Keep the manifest bounded: injected photos take priority, and we trim the remainder.
    if len(injected_objs) > limit:
        print(f"WARN: {len(injected_objs)} injected photos > --limit {limit}; only the first {limit} injected will be included.")
        return injected_objs[:limit]

    return combined[:limit]



def main() -> int:
    ap = argparse.ArgumentParser(description="Publish a bounded working-set manifest.json from S3 photos/ using presigned URLs")
    ap.add_argument("--bucket", default="trevor-shared-photo-stream")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument(
        "--photos-prefix",
        default="photos/",
        help="Single photos prefix to list (backward compatible). Ignored if --photos-prefixes is provided.",
    )
    ap.add_argument(
        "--photos-prefixes",
        default=None,
        help=(
            "Comma-separated list of prefixes to include (e.g. 'photos/02_digital-camera_family/,photos/03_dslr_early/'). "
            "If provided, overrides --photos-prefix."
        ),
    )
    ap.add_argument(
        "--exclude-prefixes",
        default=None,
        help="Comma-separated list of prefixes to exclude (applied after include list is expanded).",
    )
    ap.add_argument("--manifest-key", default="manifest.json")
    ap.add_argument("--expires", type=int, default=12 * 60 * 60, help="presign expiry seconds (default 12h)")

    ap.add_argument("--mode", default="sync", choices=["inventory", "sync"])
    ap.add_argument("--slide-seconds", type=int, default=1360)
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
        default=40,
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
        default=20,
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
        "--mix",
        default="collection",
        choices=["none", "prefix", "collection"],
        help=(
            "Interleave the full library BEFORE window selection to avoid one folder dominating. "
            "none=old behavior; prefix=balance across --photos-prefixes; collection=balance across photos/<collection>/ (default)."
        ),
    )

    # optional: inject a hand-picked list of photo keys (newline-separated) into the working set
    ap.add_argument(
        "--inject-keys-file",
        default=None,
        help=(
            "Path to a newline-separated .txt file of S3 photo keys to force-include in the manifest "
            "(e.g. 'photos/IMG_0001.jpeg'). Blank lines and lines starting with '#' are ignored."
        ),
    )
    ap.add_argument(
        "--inject-placement",
        default="top",
        choices=["top", "random"],
        help="Where to place injected photos inside the working set before shuffle is applied (default: top).",
    )
    ap.add_argument(
        "--inject-ignore-limit",
        action="store_true",
        help="If set, injected photos do NOT count toward --limit (manifest may exceed the limit).",
    )
    ap.add_argument(
        "--inject-strict",
        action="store_true",
        help="If set, fail if any injected key is missing from the bucket listing (default: warn + skip).",
    )

    ap.add_argument(
        "--pin-first",
        default=None,
        help='Pin a specific photo to index 0 (accepts filename like IMG_1234.jpg or full key like photos/IMG_1234.jpg).',
    )

    args = ap.parse_args()

    session = boto3.session.Session(region_name=args.region)
    s3 = session.client("s3")

    def _split_csv_prefixes(s: Optional[str]) -> list[str]:
        if not s:
            return []
        parts = [p.strip() for p in s.split(",") if p.strip()]
        # Normalize to always end with '/'
        out = []
        for p in parts:
            out.append(p if p.endswith("/") else p + "/")
        return out

    include_prefixes = _split_csv_prefixes(args.photos_prefixes) or [
        args.photos_prefix if args.photos_prefix.endswith("/") else args.photos_prefix + "/"
    ]
    exclude_prefixes = _split_csv_prefixes(args.exclude_prefixes)

    # Remove excluded prefixes from include list if the include list directly names them.
    include_prefixes = [p for p in include_prefixes if all(not p.startswith(x) for x in exclude_prefixes)]

    if not include_prefixes:
        raise SystemExit("No include prefixes left after applying excludes")

    # 1) list library from S3 (this is the master list)
    library: list[ObjInfo] = []
    for pfx in include_prefixes:
        library.extend(list_objects(s3, args.bucket, pfx))

    # Apply excludes as a final filter (covers nested paths as well)
    if exclude_prefixes:
        library = [o for o in library if all(not o.key.startswith(x) for x in exclude_prefixes)]

    library.sort(key=lambda o: o.key.lower())  # stable base ordering

    # IMPORTANT: mix/interleave the library BEFORE selecting the bounded window.
    # This prevents early-sorting folders (e.g. photos/01_scans/...) from dominating the window.
    seed_key = f"{args.bucket}/" + ";".join(include_prefixes)
    library = interleave_library(
        library=library,
        mix=args.mix,
        shuffle=args.shuffle,
        seed_key=seed_key,
        include_prefixes=include_prefixes,
    )

    if not library:
        raise SystemExit(f"No images found under s3://{args.bucket}/{'/'.join(include_prefixes)}")

    # 2) load + update window state
    state_path = Path(args.state_file)
    state = load_state(state_path)
    offset = int(state.get("offset", 0))
    if args.advance:
        offset += int(args.advance)

    # 3) pick bounded working set
    working = select_window(library, args.limit, offset)
    # Optional: inject a hand-picked list of keys into the working set.
    # This happens AFTER window selection but BEFORE pin-first + shuffle.
    if args.inject_keys_file:
        injected_keys = load_injected_keys(args.inject_keys_file)
        library_by_key = {o.key: o for o in library}
        working = inject_into_working_set(
            working=working,
            library_by_key=library_by_key,
            injected_keys=injected_keys,
            placement=args.inject_placement,
            limit=args.limit,
            ignore_limit=args.inject_ignore_limit,
            strict=args.inject_strict,
            seed_key=seed_key,
        )


    # 4) pin-first should always win inside the working set.
    # If the pinned item is not in the window, we swap it in (replacing the last item).
    pin_key, pin_filename = ("", "")
    if args.pin_first:
        # In multi-prefix mode, treat a bare filename as matching any prefix.
        # normalize_pin() still works for full keys.
        pin_key, pin_filename = normalize_pin(args.pin_first, include_prefixes[0])

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
            print(f'WARN: --pin-first "{args.pin_first}" not found in library; ignoring.')
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
    if args.shuffle != "none" and len(working) > 1:
        head = working[0:1]
        tail = working[1:]
        tail = apply_shuffle(tail, args.shuffle, seed_key)
        working = head + tail

    # 6) persist updated offset state
    state["offset"] = offset
    state["updated_at"] = utc_now_z()
    state["bucket"] = args.bucket
    state["photos_prefixes"] = include_prefixes
    state["exclude_prefixes"] = exclude_prefixes
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
    print(f"Mode: {args.mode} | slide_seconds={args.slide_seconds} | shuffle={args.shuffle} | mix={args.mix}")
    if args.mode == "sync":
        print(f"start_epoch={manifest['start_epoch']}")
    if args.pin_first:
        print(f"pin_first={args.pin_first}")
    if args.inject_keys_file:
        print(f"inject_keys_file={args.inject_keys_file} | placement={args.inject_placement} | ignore_limit={args.inject_ignore_limit} | strict={args.inject_strict}")
    if args.advance:
        print(f"advanced_by={args.advance} (new offset={offset})")
    print(f"State file: {state_path.resolve()}")
    print(f"Include prefixes: {include_prefixes}")
    if exclude_prefixes:
        print(f"Exclude prefixes: {exclude_prefixes}")
    print(f"Manifest URL: https://{args.bucket}.s3.{args.region}.amazonaws.com/{args.manifest_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

