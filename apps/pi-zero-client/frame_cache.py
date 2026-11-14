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
import math
import os
import posixpath
import struct
import warnings
from PIL import Image
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
    # Immutable content generations keep the prior manifest playable while a
    # changed ETag is downloading, including across a killed sync/reboot.
    identity = ident
    if photo.get("etag"):
        identity = repr((ident, str(photo["etag"])))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
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


def valid_manifest(value) -> bool:
    """Reject malformed input as a whole; never compact the ordered slots."""
    if not isinstance(value, dict):
        return False
    photos = value.get("photos")
    if not isinstance(photos, list) or not photos:
        return False
    if any(not local_name(photo) for photo in photos):
        return False
    try:
        for field in ("slide_seconds", "start_epoch"):
            number = value.get(field)
            if number is not None and (isinstance(number, bool) or
                    not isinstance(number, (int, float)) or not math.isfinite(number)):
                return False
    except OverflowError:
        return False
    return True


# Display formats only. Anything else (HEIC, TIFF, BMP, video, ...) is
# unidentified, i.e. a permanent per-photo failure rather than a decode attempt.
IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")

# What a corrupt/unsupported/hostile file can raise from Pillow. MemoryError is
# deliberately absent: memory pressure on a 512 MB device is not corruption and
# must not be recorded as a permanent per-photo failure.
DECODE_ERRORS = (OSError, SyntaxError, ValueError, EOFError, struct.error,
                 Image.DecompressionBombError, Image.DecompressionBombWarning)


# draft() only shrinks JPEG decodes. PNG/WebP/GIF always decode at full size
# (measured peak RSS: WebP 12 MP ~223 MB, 48 MP ~812 MB), and on a 512 MB Pi
# the kernel OOM-kills the process - no catchable MemoryError - so anything
# larger is refused from its header alone and becomes a permanent hole.
MAX_NON_JPEG_PIXELS = 8_000_000


def _webp_header_size(head: bytes):
    """(width, height) from a RIFF/WebP header, or None if not parseable.

    Pillow builds without WebP-anim support decode the whole bitstream inside
    Image.open(), before any size check could run, so WebP is sized here.
    """
    if len(head) < 30 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    chunk = head[12:16]
    if chunk == b"VP8X":
        return (1 + int.from_bytes(head[24:27], "little"),
                1 + int.from_bytes(head[27:30], "little"))
    if chunk == b"VP8L" and head[20] == 0x2F:
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":
        return (int.from_bytes(head[26:28], "little") & 0x3FFF,
                int.from_bytes(head[28:30], "little") & 0x3FFF)
    return None


# Pillow opens a JPEG carrying a multi-picture (MP) APP2 header - Samsung and
# Fujifilm cameras, phone depth maps / previews - as format "MPO". Its
# MpoImageFile subclasses JpegImageFile: draft() and the coefficient estimate
# apply to the primary picture exactly as for a plain JPEG, and nothing here
# seeks past frame 0, so only the primary picture is ever decoded.
JPEG_FORMATS = ("JPEG", "MPO")


def _refuse_pixels(fmt, size) -> None:
    width, height = size
    if fmt not in JPEG_FORMATS and width * height > MAX_NON_JPEG_PIXELS:
        raise ValueError(f"{fmt} {width}x{height} exceeds {MAX_NON_JPEG_PIXELS} pixels "
                         "(non-JPEG decodes at full size)")


# draft()/DCT scaling bounds only the OUTPUT of a JPEG decode. libjpeg keeps
# every DCT coefficient of the full-resolution image in memory (2 bytes each),
# whatever the draft scale, whenever the file has multiple scans: progressive
# JPEGs, and also SEQUENTIAL (SOF0/SOF1) JPEGs whose first scan carries fewer
# components than the frame (non-interleaved, e.g. `jpegtran -scans`; libjpeg's
# has_multiple_scans). Reviewer-measured peaks (macOS arm64, Pillow 13; check /
# viewer render): 30 MP 4:2:0 117/153 MB, 48 MP 4:2:0 170/194 MB, 48 MP 4:4:4
# 314/338 MB (progressive and non-interleaved baseline alike), 89 MP 4:4:4
# 565/600 MB - each an OOM kill (no catchable MemoryError) on the 512 MB Pi.
# 80 MiB admits the common progressive sources (<= 24 MP 4:2:0 web/camera
# exports need ~72 MB, <= 12 MP 4:4:4 the same) and refuses 30 MP+ 4:2:0 and
# >= 16 MP 4:4:4. The syncer and the viewer can decode at the same moment, so
# the worst case is roughly 2 x (80 MiB + ~50 MB interpreter, canvas and
# framebuffer) ~= 270 MB of the ~440 MB usable. Interleaved baseline JPEGs
# (single first scan with every component - what cameras and phones write)
# need no such buffer and are unaffected.
MAX_BUFFERED_JPEG_COEF_BYTES = 80 * 1024 * 1024

# The header walk to the first SOS seeks over segment payloads and stops after
# this many bytes; a header that long is costed as worst case, not parsed.
MAX_JPEG_HEADER_BYTES = 16 * 1024 * 1024
_MAX_JPEG_MARKERS = 4096

# SOF markers of progressive coding (huffman / differential / arithmetic).
_PROGRESSIVE_SOF = (0xC2, 0xC6, 0xCA, 0xCE)
_SOF = tuple(m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC))
_STANDALONE = (0x01, 0xD8) + tuple(range(0xD0, 0xD8))  # TEM, SOI, RST0-7


def jpeg_scan_layout(fp):
    """(progressive, frame_components, first_scan_components) or None.

    Walks the markers from SOI to the FIRST SOS of the JPEG at the current
    position of the binary file `fp` (fill bytes, standalone markers and
    segment lengths handled; payloads are skipped by seeking, so the walk is
    bounded by MAX_JPEG_HEADER_BYTES / marker count rather than file size).
    None means malformed, truncated or over the bound - callers must cost
    that as the worst case, never as "no buffer". Never raises on bad bytes.
    """
    try:
        start = fp.tell()
        if fp.read(2) != b"\xff\xd8":
            return None
        progressive = frame_components = None
        for _ in range(_MAX_JPEG_MARKERS):
            if fp.tell() - start > MAX_JPEG_HEADER_BYTES:
                return None
            byte = fp.read(1)
            if byte != b"\xff":
                return None  # garbage between segments / truncated
            while byte == b"\xff":  # fill bytes
                byte = fp.read(1)
            if not byte:
                return None
            marker = byte[0]
            if marker in _STANDALONE:
                continue
            raw = fp.read(2)
            if len(raw) != 2:
                return None
            length = struct.unpack(">H", raw)[0]
            if length < 2 or marker == 0xD9:  # EOI before any scan
                return None
            if marker in _SOF:
                payload = fp.read(length - 2)
                if len(payload) < 6 or frame_components is not None:
                    return None
                progressive = marker in _PROGRESSIVE_SOF
                frame_components = payload[5]
                if frame_components < 1:
                    return None
            elif marker == 0xDA:
                payload = fp.read(1)
                if length < 3 or not payload or frame_components is None or payload[0] < 1:
                    return None
                return progressive, frame_components, payload[0]
            else:
                fp.seek(length - 2, os.SEEK_CUR)
        return None
    except (OSError, ValueError, struct.error, TypeError, AttributeError):
        return None


def _image_scan_layout(image):
    """jpeg_scan_layout of the primary picture behind an open Pillow image.

    Reads from the image's own file object at offset 0 (where frame 0 of an
    MPO starts) and restores the file position afterwards.
    """
    fp = getattr(image, "fp", None)
    if fp is None:
        return None
    try:
        position = fp.tell()
    except (OSError, ValueError, AttributeError):
        return None
    try:
        fp.seek(0)
        return jpeg_scan_layout(fp)
    except (OSError, ValueError, AttributeError):
        return None
    finally:
        try:
            fp.seek(position)
        except (OSError, ValueError):
            pass


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def buffered_coefficient_bytes(image) -> int:
    """Bytes of DCT coefficients libjpeg must buffer to decode `image`.

    0 for non-JPEGs and for single-pass JPEGs (sequential, first scan carries
    every component). Charged when the file is progressive (SOF2/6/10/14 or
    Pillow's progressive info) or its first scan carries fewer components than
    the frame, and - worst case - whenever that header cannot be read.

    Header-only, from the SOF sampling factors Pillow exposes in
    `image.layer` as (id, h, v, table): per component, the block grid
    ceil(w*h_c/h_max/8) x ceil(h*v_c/v_max/8), padded to whole MCUs, times
    64 coefficients x 2 bytes - i.e. ~ w*h*2*sum(h_c*v_c)/(h_max*v_max).
    An unreadable layer table is costed as four full-resolution components
    (CMYK, the most a JPEG decodable here can carry).
    """
    if getattr(image, "format", None) not in JPEG_FORMATS:
        return 0
    info = getattr(image, "info", None) or {}
    if not (info.get("progressive") or info.get("progression")):
        layout = _image_scan_layout(image)
        if layout is not None:
            progressive, frame_components, scan_components = layout
            if not progressive and scan_components >= frame_components:
                return 0
    width, height = image.size
    try:
        factors = [(int(entry[1]), int(entry[2])) for entry in image.layer]
        if not factors or any(not (1 <= h <= 4 and 1 <= v <= 4) for h, v in factors):
            raise ValueError("bad sampling factors")
    except (AttributeError, IndexError, TypeError, ValueError):
        factors = [(1, 1)] * 4
    h_max = max(h for h, _ in factors)
    v_max = max(v for _, v in factors)
    total = 0
    for h, v in factors:
        cols = _ceil_div(_ceil_div(width * h, h_max), 8)
        rows = _ceil_div(_ceil_div(height * v, v_max), 8)
        total += _ceil_div(cols, h) * h * _ceil_div(rows, v) * v * 64 * 2
    return total


def _refuse_buffered(image) -> None:
    need = buffered_coefficient_bytes(image)
    if need > MAX_BUFFERED_JPEG_COEF_BYTES:
        width, height = image.size
        info = getattr(image, "info", None) or {}
        kind = "progressive" if info.get("progressive") or info.get("progression") else "multi-scan"
        raise ValueError(f"{kind} {image.format} {width}x{height} needs {need / 2**20:.0f} MiB of "
                         f"coefficient buffers (cap {MAX_BUFFERED_JPEG_COEF_BYTES / 2**20:.0f} MiB; "
                         "draft() cannot shrink a multi-scan decode)")


def open_image(path: Path):
    """Image.open restricted to display formats, refusing what cannot fit in RAM.

    Header-only: nothing is decoded before these guards. Refused: non-JPEGs
    above MAX_NON_JPEG_PIXELS (they decode at full size) and multi-scan
    JPEGs/MPOs (progressive, or non-interleaved first scan) whose coefficient
    buffer exceeds MAX_BUFFERED_JPEG_COEF_BYTES. Single-scan JPEGs and MPO
    primaries are decoded through draft(), which bounds their memory;
    Pillow's decompression-bomb check bounds pixel COUNT, not memory. Used by
    both the syncer and the viewer.
    """
    with open(path, "rb") as f:
        size = _webp_header_size(f.read(30))
    if size is not None:
        _refuse_pixels("WEBP", size)
    image = Image.open(path, formats=IMAGE_FORMATS)
    try:
        _refuse_pixels(image.format, image.size)
        _refuse_buffered(image)
    except BaseException:
        image.close()
        raise
    return image


def check_image(path: Path) -> None:
    """Raise one of DECODE_ERRORS unless the file is a playable image.

    The header is checked first (open_image): oversized non-JPEGs and
    multi-scan JPEGs over the coefficient budget are refused without
    decoding. verify() checks structure (PNG CRCs etc.); a second open then
    forces a real pixel decode, because JPEG verify() accepts truncated
    scans. That decode uses a 1/8-scale JPEG draft: libjpeg still has to walk
    every entropy-coded scan, so truncated baseline AND progressive files are
    still rejected. For single-scan JPEGs the draft keeps peak RSS ~26 MB for
    a 48 MP photo instead of ~219 MB; a multi-scan JPEG still buffers its full
    coefficient image (up to MAX_BUFFERED_JPEG_COEF_BYTES). Do not enable
    LOAD_TRUNCATED_IMAGES: incomplete transfers must never replace good
    bytes. Photos above Pillow's decompression-bomb threshold are refused
    (the warning is escalated) rather than decoded.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with open_image(path) as image:
            image.verify()
        with open_image(path) as image:
            image.draft("RGB", (max(1, image.width // 8), max(1, image.height // 8)))
            image.load()
            if not (image.width > 0 and image.height > 0):
                raise ValueError("image has no pixels")


def valid_image(path: Path) -> bool:
    """True when check_image accepts the file; MemoryError propagates."""
    try:
        check_image(path)
        return True
    except DECODE_ERRORS:
        return False


def referenced_names(photos: list) -> set[str]:
    """Every filename the viewer could resolve for these entries.

    Matches viewer.resolve_image: the versioned key name, the pre-ETag key
    name and the legacy basename. Failure-path pruning keeps all of them so a
    device mid-migration never loses files its last-good manifest plays.
    """
    names = set()
    for photo in photos or []:
        if not isinstance(photo, dict):
            continue
        unversioned = {k: v for k, v in photo.items() if k != "etag"}
        for name in (local_name(photo), local_name(unversioned), legacy_name(photo)):
            if name:
                names.add(name)
    return names


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
