"""Automatic photo selection: the "no curated playlist" fallback.

Ports the v1 publisher's selection semantics (tools/publish_manifest.py) to
the serverless publisher: interleave the library across collections so a
bounded window is not dominated by whichever folder sorts first, shuffle
deterministically with the UTC date as seed, then take the first N.

Determinism is the point: every scheduled run within a given UTC day selects
the same membership, so the URL-refresh cadence never churns photos on the
frames — the window rotates once, at midnight UTC. New uploads join the pool
on the next rotation.
"""
from __future__ import annotations

import hashlib
import random
from collections import deque
from datetime import datetime, timezone

from .s3_browse import is_media_key


def list_media_keys(s3, bucket: str, prefix: str) -> list[str]:
    """Every media key under prefix (no delimiter — full recursive listing)."""
    keys: list[str] = []
    kwargs: dict = {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**kwargs)
        for item in page.get("Contents", []):
            key = item.get("Key")
            if key and is_media_key(key):
                keys.append(key)
        token = page.get("NextContinuationToken")
        if not token:
            return keys
        kwargs["ContinuationToken"] = token


def _seeded_shuffle(items: list[str], seed_str: str) -> list[str]:
    seed = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest(), 16) % (2**32)
    out = list(items)
    random.Random(seed).shuffle(out)
    return out


def _collection(key: str, prefix: str) -> str:
    """First folder under the photos prefix (photos/2024/x.jpg -> "2024")."""
    rest = key[len(prefix):] if key.startswith(prefix) else key
    return rest.split("/", 1)[0] if "/" in rest else "__root__"


def select_window(
    keys: list[str], window: int, *, prefix: str, day: str | None = None
) -> list[str]:
    """Deterministic daily window over the pool.

    Group by collection, shuffle group order and members seeded on the UTC
    date, round-robin interleave, take the first `window`. Inputs are sorted
    before shuffling so the result is independent of S3 listing order.
    """
    if day is None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    groups: dict[str, list[str]] = {}
    for key in keys:
        groups.setdefault(_collection(key, prefix), []).append(key)

    group_order = _seeded_shuffle(sorted(groups), f"auto:groups:{day}")
    queues = deque(
        deque(_seeded_shuffle(sorted(groups[g]), f"auto:group:{g}:{day}"))
        for g in group_order
    )

    out: list[str] = []
    while queues and len(out) < window:
        q = queues.popleft()
        if q:
            out.append(q.popleft())
        if q:
            queues.append(q)
    return out
