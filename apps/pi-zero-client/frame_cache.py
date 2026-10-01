#!/usr/bin/env python3
"""Cache filenames and pruning — shared by the syncer and the viewer.

The two halves of the client must agree on exactly one thing: the name a
manifest entry takes under `/opt/frame/images`. That agreement used to be
"the entry's `id`", i.e. the S3 basename, which is not unique — basenames
collide across folders (`photos/IMG_0274.jpeg` vs
`photos/04_phone_modern/IMG_0274.jpeg`). The collision was silent and
permanent: the syncer skips any photo whose file already exists, so the
frame showed whichever photo cached that name first, forever. Daily window
rotation churns ~50 photos a day through the cache, so the collision
surface is the whole library rather than one curated playlist.

The name is now derived from the entry's full S3 `key` (manifest schema 2),
keeping the basename as a readable suffix:

    photos/04_phone_modern/IMG_0274.jpeg -> a1b2c3d4e5f6_IMG_0274.jpeg

The digest supplies uniqueness; the suffix is sanitized and no manifest
string is ever used as a path component, so a malformed or hostile manifest
cannot write outside the images directory. Schema-1 manifests carry only
`id`, so they hash the basename and keep the older ambiguity — such a
manifest holds nothing that could tell two same-basename photos apart.

Pruning lives here too, because it is the same agreement seen from the
other side: a file whose name no manifest entry claims is unreachable, and
before this the syncer never deleted one.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
from pathlib import Path

DIGEST_CHARS = 12
MAX_STEM = 48
MAX_EXT = 10
_SAFE_PUNCT = ("-", "_", ".")


def _sanitize(name: str) -> str:
    """ASCII-only, path-separator-free rendering of a basename.

    The extension is preserved across truncation: the viewer's no-manifest
    fallback path filters the image directory by suffix, so a name that
    lost its `.jpg` would be invisible there.
    """
    stem, ext = posixpath.splitext(name)

    def clean(part: str) -> str:
        return "".join(
            ch if (ch.isascii() and ch.isalnum()) or ch in _SAFE_PUNCT else "_"
            for ch in part
        )

    stem = clean(stem).strip(".")[:MAX_STEM]
    ext = clean(ext)[:MAX_EXT]
    return f"{stem}{ext}".strip(".")


def local_name(photo: dict) -> str | None:
    """Filename under the images directory for a manifest photo entry.

    None means the entry carries neither `key` nor `id` and the caller
    should skip it.
    """
    if not isinstance(photo, dict):
        return None
    ident = str(photo.get("key") or "").strip() or str(photo.get("id") or "").strip()
    if not ident:
        return None
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
    suffix = _sanitize(posixpath.basename(ident.rstrip("/")))
    return f"{digest}_{suffix}" if suffix else digest


def legacy_name(photo: dict) -> str | None:
    """Pre-migration filename: the entry's `id`, used as-is.

    The viewer falls back to this so a device whose `viewer.py` is updated
    ahead of its `frame_sync.py` keeps playing its existing cache instead
    of going black. Transitional only — the first successful sync fetches
    the window under the new names and prunes these. Ids containing a path
    separator are refused; the old code used them raw.
    """
    if not isinstance(photo, dict):
        return None
    pid = str(photo.get("id") or "").strip()
    if not pid or pid in (".", "..") or "/" in pid or "\\" in pid:
        return None
    return pid


def wanted_names(photos: list) -> set[str]:
    """Every local filename the given manifest photos resolve to."""
    names = set()
    for photo in photos or []:
        name = local_name(photo)
        if name:
            names.add(name)
    return names


def prune_dir(directory: Path, keep: set[str]) -> tuple[int, int]:
    """Delete files in `directory` whose name is not in `keep`.

    Returns (files_removed, bytes_freed). Subdirectories are left alone —
    the images and staging directories have always been flat — and an entry
    that cannot be statted or unlinked is skipped rather than raising, so a
    permissions oddity on one file cannot abort a sync run.
    """
    removed = 0
    freed = 0
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0, 0
    for entry in entries:
        if entry.name in keep:
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            size = entry.stat(follow_symlinks=False).st_size
            os.unlink(entry.path)
        except OSError:
            continue
        removed += 1
        freed += size
    return removed, freed
