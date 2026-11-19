#!/usr/bin/env python3
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import frame_cache

MANIFEST_URL = os.environ.get("FRAME_MANIFEST_URL", "").strip()

LOCAL_MANIFEST = Path("/opt/frame/manifest.json")
STATE_FILE = Path("/opt/frame/.sync_state.json")

IMAGES_DIR = Path("/opt/frame/images")
STAGING_DIR = Path("/opt/frame/staging")

TIMEOUT = 25

# Abort any photo body above this; a 48 MP JPEG is ~15-25 MB. Oversized
# objects are permanent per-photo failures (holes), never a reason to stall.
MAX_PHOTO_BYTES = 50 * 1024 * 1024
COPY_CHUNK = 64 * 1024

# Permanent failures retry on (key, etag) with exponential backoff, capped.
PERMANENT_BACKOFF_BASE = 30 * 60
PERMANENT_BACKOFF_MAX = 24 * 60 * 60

# A decode the kernel OOM-kills leaves no exception behind, only the
# state['validating'] marker persisted before it. This many such deaths on the
# same file make it a permanent failure (hole + backoff) instead of a loop that
# kills every tick before promote/prune/state-save can run.
MAX_DECODE_CRASHES = 2

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}

def atomic_json(path, value):
    """Same-directory, flushed replacement: readers see old OR complete new JSON."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as f:
            temporary = Path(f.name)
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_state(state):
    atomic_json(STATE_FILE, state)

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
        "photos": [{k: p.get(k) for k in ("id", "key", "url", "etag", "sha256")}
                   for p in manifest.get("photos", [])],
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


class PermanentPhotoError(Exception):
    """This exact object (key, etag) will not become playable by retrying.

    Undecodable/unsupported bytes, decompression bombs and oversized bodies.
    Network errors, HTTP statuses and Content-Length mismatches are NOT
    permanent: they keep the last-good manifest and retry next tick.
    """


def _now():
    return time.time()


def download_to(tmp_path: Path, url: str, validate=None):
    validate = validate or frame_cache.check_image
    req = Request(url, headers={"User-Agent": "frame-pi/1.0"})
    try:
        response = urlopen(req, timeout=TIMEOUT)
    except HTTPError as e:
        e.close()
        raise
    with response as resp:
        if getattr(resp, "status", 200) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status', '?')}")
        length = resp.headers.get("Content-Length")
        if length is not None and length.strip().isdigit() and int(length) > MAX_PHOTO_BYTES:
            raise PermanentPhotoError(f"declared size {length} exceeds cap {MAX_PHOTO_BYTES}")
        tmp_file = tmp_path.with_suffix(tmp_path.suffix + ".tmp")
        try:
            total = 0
            with open(tmp_file, "wb") as f:
                while True:
                    chunk = resp.read(COPY_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PHOTO_BYTES:
                        raise PermanentPhotoError(f"body size exceeds cap {MAX_PHOTO_BYTES}")
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            if length is not None and total != int(length):
                raise ValueError("incomplete photo Content-Length")
            try:
                validate(tmp_file)
            except frame_cache.DECODE_ERRORS as error:
                raise PermanentPhotoError(f"undecodable photo: {error!r}") from error
            tmp_file.replace(tmp_path)
        finally:
            tmp_file.unlink(missing_ok=True)

def ensure_dirs():
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)


# ---- validated-file records -------------------------------------------------
# A file is fully decoded once, when it is downloaded (or first seen without a
# record). Afterwards the shortcut paths only stat() it against the recorded
# [size, mtime_ns]; one rolling full re-validation per tick catches bit rot.

def _stamp(path: Path):
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def record_validated(state, name):
    try:
        state.setdefault("validated", {})[name] = _stamp(IMAGES_DIR / name)
    except OSError:
        state.setdefault("validated", {}).pop(name, None)


# ---- crash marker around every full decode --------------------------------

class StatePersistError(RuntimeError):
    """.sync_state.json could not be written (ENOSPC, EIO, read-only fs...).

    Raised instead of the bare OSError so it can never be mistaken for a
    decode error (OSError is in frame_cache.DECODE_ERRORS): a full SD card is
    not evidence that a photo is corrupt. main() aborts the run on it without
    recording holes (and, for any write before the end of the run, without
    promoting or pruning), leaving the last-good window intact. Every state
    write in the syncer goes through persist_state, so a full disk is always
    this clean exit 1, never a traceback.
    """


def persist_state(state):
    """save_state with OSError -> StatePersistError (all syncer state writes)."""
    try:
        save_state(state)
    except OSError as error:
        raise StatePersistError(f"cannot write {STATE_FILE}: {error}") from error


def guarded_check(state, path: Path, name: str, fid):
    """frame_cache.check_image with a persisted crash marker around it.

    The marker is written (atomically, fsynced) BEFORE the decode and cleared
    after it returns or raises a decode error / MemoryError. An OOM kill (or
    any death mid-decode) runs no handler, so the marker survives on disk and
    recover_crash_marker() attributes the death to this file on the next run.
    Only check_image's own exceptions are decode verdicts; a failed marker
    write raises StatePersistError, which callers must not classify.
    """
    state["validating"] = {"name": name, "fid": fid}
    persist_state(state)
    try:
        frame_cache.check_image(path)
    except (MemoryError,) + frame_cache.DECODE_ERRORS:
        _clear_marker(state, name, fid)
        raise
    _clear_marker(state, name, fid)


def _clear_marker(state, name, fid):
    state.pop("validating", None)
    state.setdefault("decode_crashes", {}).pop(fid or name, None)
    persist_state(state)


def guarded_valid(state, path: Path, name: str, fid) -> bool:
    try:
        guarded_check(state, path, name, fid)
        return True
    except frame_cache.DECODE_ERRORS:
        return False


def _still_validated(state, name) -> bool:
    """The file on disk is exactly the bytes recorded as fully validated."""
    recorded = state.get("validated", {}).get(name)
    if not recorded or "/" in name or name in ("", ".", ".."):
        return False
    try:
        return list(recorded) == _stamp(IMAGES_DIR / name)
    except (OSError, TypeError):
        return False


def recover_crash_marker(state, now, playing=()):
    """Attribute a leftover marker (previous run died mid-decode) to its file.

    `playing` is every name the last-good manifest resolves to. A death while
    re-validating such a file whose recorded [size, mtime_ns] still match is
    as likely a SIGTERM at shutdown or a power cut as a bad photo, and the
    file already passed a full decode: deleting it would punch a hole into a
    working show. It is kept (still validated, still playable) and only
    dropped from the rolling re-validation (`no_revalidate`).
    """
    marker = state.pop("validating", None)
    if not isinstance(marker, dict) or not marker.get("name"):
        return
    name, fid = str(marker["name"]), marker.get("fid")
    crashes = state.setdefault("decode_crashes", {})
    key = fid or name
    count = int(crashes.get(key, 0)) + 1
    print(f"Previous run died while decoding {name} (crash {count}/{MAX_DECODE_CRASHES})")
    if count < MAX_DECODE_CRASHES:
        crashes[key] = count
    elif name in playing and _still_validated(state, name):
        crashes.pop(key, None)
        state.setdefault("no_revalidate", {})[name] = list(state["validated"][name])
        print(f"Keeping {name}: validated and playing; excluded from re-validation")
    else:
        crashes.pop(key, None)
        if fid:
            _record_failure_id(state, fid, f"decode killed the sync process {count} times", now)
        # Out of the re-validation rotation and off the playable set: the slot
        # becomes a hole (backoff) instead of a file that kills every tick.
        state.setdefault("validated", {}).pop(name, None)
        if "/" not in name and name not in ("", ".", ".."):
            (IMAGES_DIR / name).unlink(missing_ok=True)
    persist_state(state)


def cached_ok(name, state, fid=None) -> bool:
    validated = state.setdefault("validated", {})
    path = IMAGES_DIR / name
    try:
        stamp = _stamp(path)
    except OSError:
        validated.pop(name, None)
        return False
    if validated.get(name) == stamp:
        return True
    if guarded_valid(state, path, name, fid):
        validated[name] = stamp
        return True
    validated.pop(name, None)
    return False


def _fid_for_name(name):
    manifest = local_window()
    if frame_cache.valid_manifest(manifest):
        for photo in manifest["photos"]:
            if frame_cache.local_name(photo) == name:
                return failure_id(photo)
    return None


def rolling_revalidate(state):
    """Fully decode at most one previously validated file per tick.

    Files in `no_revalidate` (see recover_crash_marker) are skipped while
    their recorded stamp still matches; a changed file rejoins the rotation.
    """
    validated = state.setdefault("validated", {})
    skip = state.setdefault("no_revalidate", {})
    for name in list(skip):
        if validated.get(name) != skip[name]:
            del skip[name]
    names = sorted(n for n in validated if n not in skip)
    if not names:
        return
    cursor = state.get("revalidate_cursor") or ""
    name = next((n for n in names if n > cursor), names[0])
    state["revalidate_cursor"] = name
    path = IMAGES_DIR / name
    if not guarded_valid(state, path, name, _fid_for_name(name)):
        print(f"Re-validation failed for {name}; scheduling repair")
        validated.pop(name, None)
        path.unlink(missing_ok=True)
    else:
        record_validated(state, name)


# ---- permanent per-photo failures ------------------------------------------

def failure_id(photo) -> str:
    ident = str(photo.get("key") or "").strip() or str(photo.get("id") or "").strip()
    return json.dumps([ident, str(photo.get("etag") or "")])


def _has_url(photo):
    url = photo.get("url") if isinstance(photo, dict) else None
    return isinstance(url, str) and bool(url)


def hole_pending(photo, state, now) -> bool:
    record = state.get("permanent_failures", {}).get(failure_id(photo))
    if not record:
        return False
    if record.get("no_url") and _has_url(photo):
        return False  # the publisher fixed the entry: retry now, not after backoff
    remaining = record.get("retry_at", 0) - now
    # A retry further away than the cap was written under a wrong clock (no
    # RTC; fake-hwclock/NTP jumps): treat it as expired, never pin a hole.
    return 0 < remaining <= PERMANENT_BACKOFF_MAX


def _record_failure_id(state, fid, reason, now, **extra):
    failures = state.setdefault("permanent_failures", {})
    attempts = int(failures.get(fid, {}).get("attempts", 0)) + 1
    delay = min(PERMANENT_BACKOFF_BASE * 2 ** (attempts - 1), PERMANENT_BACKOFF_MAX)
    failures[fid] = {"attempts": attempts, "retry_at": now + delay, "reason": reason[:200], **extra}
    return delay


def record_failure(state, photo, reason, now):
    extra = {} if _has_url(photo) else {"no_url": True}
    return _record_failure_id(state, failure_id(photo), reason, now, **extra)


def complete_window(manifest, state=None):
    """Every slot is a validated file or a hole still inside its backoff."""
    state = {} if state is None else state
    if not frame_cache.valid_manifest(manifest):
        return False
    now = _now()
    for photo in manifest["photos"]:
        name = frame_cache.local_name(photo)
        if not cached_ok(name, state, failure_id(photo)) and not hole_pending(photo, state, now):
            return False
    return True


def local_window():
    try:
        return json.loads(LOCAL_MANIFEST.read_text())
    except (OSError, ValueError):
        return None


def fetch_manifest(etag, state):
    try:
        return fetch_bytes(MANIFEST_URL, etag)
    except HTTPError as error:
        error.close()
        if error.code != 304:
            raise
        if complete_window(local_window(), state):
            return None, etag
        # Disk loss/corruption (or an expired permanent-failure backoff) is
        # independent of publisher identity. Get fresh URLs as well as the
        # manifest; cached presigned URLs may have expired.
        return fetch_bytes(MANIFEST_URL, None)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    if not MANIFEST_URL:
        raise SystemExit("FRAME_MANIFEST_URL env var is required")
    try:
        _sync()
    except StatePersistError as error:
        # Nothing was promoted or pruned and no hole recorded: the last-good
        # manifest and its files are untouched. Exit non-zero so systemd and
        # the journal show the failed run; the next tick retries.
        print(f"Sync aborted, state not persisted (disk full or failing?): {error}")
        raise SystemExit(1) from error


def _sync(_allow_refresh=True):
    ensure_dirs()
    state = load_state()
    etag = state.get("etag")
    # Read before any promotion: failure-path pruning must keep this window.
    last_good = local_window()
    last_good_photos = last_good["photos"] if frame_cache.valid_manifest(last_good) else []

    recover_crash_marker(state, _now(), frame_cache.referenced_names(last_good_photos))
    rolling_revalidate(state)

    # ---- fetch manifest (ETag-aware) ----
    try:
        raw, new_etag = fetch_manifest(etag, state)
    except HTTPError as e:
        e.close()
        persist_state(state)
        if e.code == 304:
            print("Manifest unchanged (304).")
            return
        print(f"Manifest fetch HTTP error: {e}")
        return
    except (URLError, OSError, ValueError) as e:
        persist_state(state)
        print(f"Manifest fetch failed (offline?): {e}")
        return

    if raw is None:
        persist_state(state)
        print("Manifest unchanged; local window verified playable.")
        return

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        persist_state(state)
        print("Invalid manifest JSON; keeping last good manifest.")
        return
    if not frame_cache.valid_manifest(manifest):
        persist_state(state)
        print("Invalid or empty manifest; keeping last good manifest.")
        return

    # Basic validation. Schema 2 is what the live publisher emits (it added
    # the full `key` per photo); schema 1 is the older shape and still reads.
    if manifest.get("schema") not in (1, 2):
        print(f"Unexpected schema={manifest.get('schema')}; continuing anyway.")
    photos = manifest["photos"]

    sig = stable_manifest_sig(manifest)
    if (state.get("sig") == sig and local_window() == manifest and
            complete_window(manifest, state)):
        # Even if ETag changed, content we care about didn't
        print("Manifest content signature unchanged.")
        state["etag"] = new_etag or etag
        state["last_check"] = int(time.time())
        persist_state(state)
        return

    # ---- download/update images ----
    # Names come from frame_cache so the viewer looks for the same files;
    # they are derived from each photo's full S3 key, not its basename (see
    # frame_cache for why that distinction is load-bearing).
    now = _now()
    downloaded = 0
    skipped = 0
    failed = 0      # transient: network/HTTP/short body -> keep last good
    holes = 0       # permanent: undecodable/unsupported/oversized -> hole
    expired_url = False
    keep = set()
    failures = state.setdefault("permanent_failures", {})

    for p in photos:
        name = frame_cache.local_name(p)  # valid_manifest guarantees a name
        url = p.get("url")
        fid = failure_id(p)
        keep.add(name)
        dest = IMAGES_DIR / name
        if cached_ok(name, state, fid):
            skipped += 1
            failures.pop(fid, None)
            continue
        if hole_pending(p, state, now):
            holes += 1
            continue

        try:
            if not _has_url(p):
                # A manifest property, not a network condition: retrying every
                # tick cannot fix it, so it is a hole (hole_pending lets a fixed
                # entry with the same identity download immediately).
                raise PermanentPhotoError("missing photo URL")
            tmp = STAGING_DIR / name
            # Flushed + fsynced + fully validated under a crash marker.
            download_to(tmp, url, validate=lambda path, n=name, f=fid: guarded_check(state, path, n, f))
            tmp.replace(dest)
            record_validated(state, name)
            failures.pop(fid, None)
            downloaded += 1
        except PermanentPhotoError as e:
            holes += 1
            delay = record_failure(state, p, str(e), now)
            print(f"Permanent failure for {name} (retry in {delay // 60:.0f} min): {e}")
        except (MemoryError, StatePersistError):
            # Neither is a verdict on this photo; abort the run unclassified.
            raise
        except Exception as e:
            failed += 1
            expired_url = expired_url or (isinstance(e, HTTPError) and e.code in (403, 404))
            print(f"Download failed for {name}: {e}")

    # A changed ETag (or a photo leaving the window) clears its record.
    wanted_failures = {failure_id(p) for p in photos}
    for fid in list(failures):
        if fid not in wanted_failures:
            del failures[fid]
    crashes = state.setdefault("decode_crashes", {})
    for key in list(crashes):
        if key not in wanted_failures and key not in keep:
            del crashes[key]

    # Promote only a window whose every slot is playable or a permanent hole,
    # and which can actually show something: an all-hole window would replace
    # a playable show with a frozen frame. Holes keep their slot (lockstep):
    # the viewer holds the previous frame for a slot with no file.
    promote = failed == 0 and (downloaded + skipped) > 0
    if promote:
        _fsync_dir(IMAGES_DIR)
        atomic_json(LOCAL_MANIFEST, manifest)
        prune_keep = keep
    else:
        # Bounded growth: keep this candidate's downloads (the retry reuses
        # them) plus everything the last-good manifest can resolve to.
        prune_keep = keep | frame_cache.referenced_names(last_good_photos)
        print(f"{failed or holes} photo(s) not playable; keeping last good manifest")

    # ---- prune images no kept manifest entry claims ----
    removed, freed = frame_cache.prune_dir(IMAGES_DIR, prune_keep)
    if removed:
        print(f"Pruned {removed} image(s), freed {freed / 1e6:.1f} MB")
    validated = state.setdefault("validated", {})
    for name in list(validated):
        if not (IMAGES_DIR / name).exists():
            del validated[name]

    # Staging is per-run scratch. Clearing it collects the partial .tmp files
    # a killed or timed-out download leaves behind.
    frame_cache.prune_dir(STAGING_DIR, set())

    # Save sync state. Without promotion, persist neither the ETag nor the
    # signature: both are early-return short-circuits, so keeping them would
    # make every run for the rest of this publish window skip straight past
    # the photos that just failed. (Permanent holes are retried on their own
    # backoff schedule via complete_window, not every tick.)
    if promote:
        state["etag"] = new_etag or etag
        state["sig"] = sig
        state.pop("last_failed", None)
        state["holes"] = holes
    else:
        state.pop("etag", None)
        state.pop("sig", None)
        state["last_failed"] = failed or holes
    state["last_sync"] = int(time.time())
    # Runs after promote+prune, so a failed write here (StatePersistError ->
    # main exits 1) leaves a consistent disk: the promoted manifest and its
    # files are in place; only etag/sig/validated stamps are not recorded, so
    # the next tick re-fetches and re-validates rather than short-circuiting.
    persist_state(state)

    print(f"Sync done: downloaded={downloaded} skipped={skipped} failed={failed} holes={holes}")
    if expired_url and _allow_refresh:
        # Failure cleared conditional-fetch state. One bounded retry refreshes
        # signed URLs without ever pruning/publishing the failed candidate.
        _sync(_allow_refresh=False)

    # Apply: writes .order and updates slide seconds + restarts if needed
    # (Run as root via systemd if necessary; if this script runs as tcd, sudo will be needed)
    # rc = os.system("sudo /usr/bin/python3 /opt/frame/apply_manifest.py")
    # if rc != 0:
    #     print("apply_manifest.py failed (see logs).")

if __name__ == "__main__":
    main()
