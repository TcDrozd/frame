# Pi Zero native client

Second display client for the shared photo frame. Runs on an original
Raspberry Pi Zero W, writes decoded photos straight to `/dev/fb0` with Pillow,
and obeys the same `manifest.json` contract as `apps/client`.

**This is not `apps/pi-client/`.** That directory is a Chromium kiosk *host*
that displays `apps/client` in a browser; it was too heavy for a Pi Zero and is
kept only for provenance. Do not install it over this device.

> **Provenance:** these files were recovered from the live `frame-zero` device
> (`/opt/frame/`) on 2026-08-11. `systemd/frame-sync.timer` is the fixed
> version deployed that day (see [The sync timer bug](#the-sync-timer-bug)).
> `install.sh` does not exist yet — installation is still the manual steps
> below.
>
> **The device is behind this directory.** `frame_cache.py`, `frame_sync.py`
> and `viewer.py` carry the cache-naming and pruning changes described in
> [The image cache](#the-image-cache), which have not been deployed to
> `frame-zero` (it has been unreachable since its `tailscaled` died on
> 2026-01-17). Deploy all three together.

## Layout

```
viewer.py                              always-running framebuffer slideshow
frame_sync.py                          oneshot manifest + image syncer
frame_cache.py                         cache filenames + pruning (both import it)
.env.example                           copy to /opt/frame/.env
systemd/frame-fb.service               viewer unit (owns tty1)
systemd/frame-fb.service.d/override.conf   per-device slide seconds
systemd/frame-sync.service             oneshot sync unit
systemd/frame-sync.timer               sync schedule
```

On the device everything lives in `/opt/frame/`, owned by the service user
(`tcd`), with the units in `/etc/systemd/system/`.

## Runtime architecture

```text
              remote manifest + presigned photo URLs
                              |
                  frame-sync.timer (every 10 min)
                              |
                  frame-sync.service (oneshot)
                              |
                   /opt/frame/frame_sync.py
                              |
              atomically updates local state
                              |
        +---------------------+---------------------+
        |                                           |
 /opt/frame/manifest.json                  /opt/frame/images/
        |                                           |
        +---------------------+---------------------+
                              |
                    frame-fb.service (always up)
                              |
                     /opt/frame/viewer.py
                              |
                      Pillow -> /dev/fb0
```

The two halves are deliberately independent:

- the **viewer** always runs and never touches the network or S3;
- the **syncer** runs periodically, updates local state, and exits;
- if syncing fails the viewer keeps playing from the last good local manifest
  and cached images. **Offline playback is the property that must never
  break.**

## Playback

The authoritative order is `manifest.photos[]`. The viewer maps the selected
entry to a file under `/opt/frame/images/` via `frame_cache.local_name()` (see
[The image cache](#the-image-cache)). It must not sort the image directory or
derive the index from a filtered local list — if the image for the current
index is missing it waits for the syncer to supply that exact file rather than
shifting to another image and falling out of lockstep.

**Sync mode** (`mode: "sync"`) uses Unix epoch seconds:

```text
elapsed = now - start_epoch
steps   = floor(elapsed / slide_seconds)
index   = steps mod len(photos)
```

The next transition is computed from the same `start_epoch` phase rather than
sleeping a fixed duration from process start, so a reboot returns to whatever
slide the shared clock dictates. `time.time()` is already in seconds — do not
copy the `* 1000` conversion `apps/client` needs for `Date.now()`.

**Inventory mode** (`mode: "inventory"`, no `start_epoch`) uses the manifest's
ordering and `slide_seconds` but advances locally and restarts at the first
photo after a viewer restart. Manifest-driven, but *not* lockstep — use sync
mode when several devices must show the same photo simultaneously.

## The image cache

`frame_cache.py` holds the one thing the two halves must agree on: the local
filename for a manifest entry. Both import it, which is why they deploy
together.

**Names come from the S3 key, not the basename.** A photo cached as
`photos/04_phone_modern/IMG_0274.jpeg` lands at:

```text
/opt/frame/images/<sha256(key)[:12]>_IMG_0274.jpeg
```

When an entry includes `etag`, the digest instead covers `(key || id, etag)`.
Each content revision therefore has an immutable filename: downloading a new
ETag cannot overwrite bytes referenced by the last-good manifest. URL-only
refreshes do not change that filename. The viewer also reads pre-ETag hashed
names and legacy basenames until the first successful migration sync.

The syncer skips any photo whose file already exists, so the old
basename-keyed scheme (`images/<id>`) meant two photos sharing a basename
across folders collided: whichever cached first was displayed in place of the
other, silently and permanently. `key` is exactly what manifest schema 2 added
to make those distinguishable. Schema-1 manifests carry only `id` and keep the
old ambiguity — nothing in such a manifest can tell the two apart. No manifest
string is ever used as a path component, so a malformed or hostile manifest
cannot write outside `images/`.

**The cache is pruned to the current manifest.** With `AutoPublishMode=window`
the publisher rotates a fresh ~50-photo selection (~90 MB) every UTC day, and
the viewer only ever opens files the current manifest names, so anything else
is dead weight on the SD card. Each sync run deletes images no entry claims,
which also collects the pre-migration basename files on first run.

**Failures are classified.** *Transient* failures (network errors, HTTP
statuses, a body shorter than its `Content-Length`) keep the
last-good manifest: nothing is promoted, and neither the ETag nor the content
signature is persisted (both are early returns that would otherwise skip the
failed photos for the rest of the publish window); `last_failed` is recorded
instead. *Permanent* failures — bytes no allowed decoder accepts (only
JPEG/PNG/WebP/GIF are opened), Pillow decompression bombs, a non-JPEG above
`MAX_NON_JPEG_PIXELS` or a multi-scan (progressive or non-interleaved) JPEG
above `MAX_BUFFERED_JPEG_COEF_BYTES` (see below), an entry with no `url`, a file whose
decode killed the sync process twice (crash marker, below), or a body above
`MAX_PHOTO_BYTES` (50 MB, aborted mid-download) — are recorded in
`.sync_state.json` under `permanent_failures`, keyed by `(key || id, etag)`,
and retried with exponential backoff (30 min doubling, capped at 24 h). A
changed ETag is a new identity and clears the record; a URL-less record stops
applying as soon as the same entry carries a URL. A `retry_at` further away
than the 24 h cap can only come from a wrong clock (no RTC: `fake-hwclock`
then an NTP jump) and is treated as expired rather than pinning a hole. The window is then
promoted with that slot as a **hole**: the slot stays in `photos[]` (indices
never shift) and the viewer holds the previous frame for it. A window with no
playable slot at all is never promoted.

**Pruning is bounded on every run.** After a promotion the cache is pruned to
the new window. After a failed run it is pruned to the candidate's downloads
plus every name the last-good manifest can resolve to (including pre-ETag and
legacy basenames), so persistent failures cannot grow the SD card past about
two windows and can never delete what the viewer is playing.

**Validation is bounded.** A download is fully decoded once (baseline JPEGs
via a 1/8 scale `draft()`: truncated baseline and progressive files are still
rejected, at ~26 MB peak RSS for a 48 MP baseline photo instead of ~219 MB) and its `[size, mtime_ns]`
is recorded under `validated`. The 304 and unchanged-signature paths only
`stat()` against that record, plus one rolling full re-validation per tick
that catches same-size bit rot. `MemoryError` propagates (the run fails and
retries) rather than being mistaken for corruption. The viewer decodes with a
screen-sized `draft()`. `draft()` does **not** bound multi-scan JPEGs (see
below).

**Non-JPEG pixels are capped from the header.** `draft()` only shrinks JPEG
decodes; PNG/WebP/GIF always decode at full size (reviewer-measured peak RSS:
PNG 12 MP 75 MB / 48 MP 219 MB, WebP 12 MP 223 MB / 48 MP 812 MB from a 6 MB
file). On 512 MB the kernel OOM-kills the process, which no handler can catch,
so `frame_cache.open_image` refuses any non-JPEG above `MAX_NON_JPEG_PIXELS`
(8,000,000) after reading only its header — WebP dimensions are parsed from
the RIFF header before Pillow even opens it, because Pillow builds without
WebP-anim support decode the whole bitstream inside `Image.open`. Such a photo
is a permanent hole. The viewer uses the same guard before `load()` and holds
the previous frame. Pillow's decompression-bomb limit bounds the pixel
*count* of a JPEG, not its decode memory.

**MPO photos are JPEGs here.** Pillow opens a JPEG carrying a multi-picture
header (Samsung/Fujifilm cameras, phone depth maps and previews) as format
`MPO`. `MpoImageFile` subclasses `JpegImageFile`, so the pixel cap does not
apply, `draft()` bounds the decode and the coefficient cap below applies to
its primary picture; nothing seeks past frame 0, so only the primary picture
is ever decoded or rendered. (Treating MPO as a full-size non-JPEG turned
every >8 MP multi-picture phone photo into a permanent hole.)

**Multi-scan JPEGs are capped by coefficient memory.** libjpeg buffers every
DCT coefficient of the full-resolution image (2 bytes each), no matter how
small the `draft()` is, whenever a file has multiple scans: every progressive
JPEG, and also a *sequential* (SOF0/SOF1) JPEG whose first scan carries fewer
components than the frame (non-interleaved, e.g. `jpegtran -scans`; Pillow
reports those as plain baseline). Reviewer-measured peaks on macOS arm64 /
Pillow 13 (sync check / viewer render): 30 MP 4:2:0 117/153 MB, 48 MP 4:2:0
170/194 MB, 48 MP 4:4:4 314/338 MB (progressive and non-interleaved baseline
alike), 89 MP 4:4:4 565/600 MB — OOM kills on the Pi. `open_image` (used by
both the syncer and the viewer) walks the markers to the first SOS (bounded:
payloads are seeked over, at most `MAX_JPEG_HEADER_BYTES`), and for a
progressive or non-interleaved file estimates the buffer from the SOF header
(`w*h*2*sum(h_c*v_c)/(h_max*v_max)`, MCU-padded, from `image.layer`); a header
it cannot parse is costed as that worst case, and unreadable sampling factors
as four full-resolution components (CMYK). Anything above
`MAX_BUFFERED_JPEG_COEF_BYTES` (80 MiB) is refused before decoding: the
syncer records a permanent hole, the viewer holds the previous frame. 80 MiB
admits ≤ 24 MP 4:2:0 (~72 MB; measured 97 MB check / 119 MB render peak on
macOS) and ≤ 12 MP 4:4:4, and refuses 30 MP+ 4:2:0 and ≥ 16 MP 4:4:4; with
syncer and viewer both at the cap the device needs roughly 2 × (80 MiB +
~50 MB) of its ~440 MB. Interleaved baseline JPEGs (what cameras and phones
write) are unaffected. These figures were measured on macOS, not on the Pi's
32-bit Pillow 9.4; the coefficient estimate itself is platform-independent
(libjpeg's `JCOEF` is 16-bit everywhere), but per-process overhead on the
device is unmeasured.

**The viewer's render cache is byte-capped.** Repeated slides reuse a
rendered framebuffer, but each one is a full frame (~8.3 MB at 1080p/32 bpp),
so the cache is bounded by `RENDER_CACHE_MAX_BYTES` (16 MB, oldest evicted
first) rather than by entry count; with ~23-minute slides hits are rare.

**Decodes run under a crash marker.** Before every full decode (download
validation, stat-mismatch checks and the rolling re-validation) the syncer
atomically persists `validating: {name, fid}` in `.sync_state.json` and clears
it afterwards. A marker found at startup means the previous run died
mid-decode (e.g. OOM kill): `decode_crashes` counts it per file, a later
successful decode resets the count, and the second crash records the photo as
a permanent failure (hole + backoff), removes the file from the cache and the
re-validation rotation, and skips decoding it. Exception: a file the
last-good manifest plays whose recorded `[size, mtime_ns]` still match was
already fully validated, and two deaths during its re-validation are as
likely a shutdown SIGTERM or a power cut as a bad photo — it is kept playable
and only listed under `no_revalidate` (dropped from the rolling
re-validation until its stamp changes). `frame-sync.service` sets
`OOMScoreAdjust=500` so that under memory pressure the kernel kills the
oneshot sync (which retries next tick) in preference to the always-running
viewer.

**A state-file write failure is not a corrupt photo.** Every write of
`.sync_state.json` (the crash-marker writes around each decode and the
early-return / end-of-run saves) raises `StatePersistError` (not the bare
`OSError`, which Pillow also raises for corrupt files) when it cannot be
written (ENOSPC, EIO), and the run exits with status 1 instead of a
traceback. Before the end of the run nothing is promoted, pruned or recorded
as a hole, so the last-good window and its files stay intact; the end-of-run
save follows promotion and pruning, so a failure there leaves the promoted
window playable and only skips recording ETag/signature/validation stamps
(the next tick re-fetches and re-validates). The next tick retries.

**Manifest promotion is atomic.** After every playable slot's file exists,
the syncer flushes them, writes and fsyncs a same-directory temporary
manifest, then atomically replaces `manifest.json`. Only then may it prune
old images and save the ETag/signature. Sync state is also written by atomic
replacement. The viewer rejects malformed JSON/shape or non-finite timing
fields and retains its last successfully parsed manifest; an invalid read
does not suppress the next retry.

Because a publish re-signs every URL, the content signature changes once per
publish (~2h) rather than per sync tick (10 min); runs in between are 304s and
touch nothing.

Covered by `tests/test_frame_cache.py` and `tests/test_frame_sync.py`, which
runs the syncer end to end against a local HTTP server.

### Milestone A regression evidence (local only)

- **RED, before implementation:** the Pi regression run reported 5 failures
  and 1 error (including partial/total replacement subtests, same-key ETag
  refresh, invalid entry, non-atomic promotion, and malformed viewer JSON).
  The actual-source Node browser tests failed all 5 original regressions.
- **GREEN:** `apps/frame-dash/.venv/bin/python -m unittest discover -s tests -v`
  from the root passes 46 tests. This includes the wrapper that executes
  `node --test tests/test_browser_cache.js` (7 passing cases).
- Coverage includes loading the persisted last-good manifest into a fresh
  viewer module after total/partial replacement failure, retrying the same
  manifest, interrupted atomic promotion, schema 1/2, key collisions,
  ETag replacement, and preserving order/epoch. Browser tests execute
  `apps/client/app.js` in a fresh Node VM with an asynchronous transactional
  IndexedDB fake, including failed promotion rollback and restart reads.
- Not validated here: physical Pi reboot/framebuffer rendering, actual
  browser IndexedDB/quota behavior, SD-card power-loss behavior, or image
  decode/integrity checks. Browser replacements stage blobs in memory until
  one atomic IDB transaction; large-window memory pressure remains unmeasured.
  No device, deployment, or AWS operations were performed.
- **Consumer review follow-up (local only):** RED first — 13 failures + 6
  errors in new Pi tests (hole promotion, backoff, ETag clearing, 50 MB cap,
  MemoryError, bounded pruning, no-decode 304 path, draft, formats); 10 new
  Node browser tests failing; real Chrome accepted 70%/50%-truncated JPEGs via
  the HTMLImageElement fallback. GREEN: 75 root tests (0 skipped), 23 Node
  tests, native acquisition suite and `tests/browser-native/run.py` (5/5).
  Not measured here: RSS on actual Pi hardware (figures above are the
  reviewer's measurements).
- **Independent re-review follow-up (local only):** RED first — 10 failures
  + 7 errors across the new Pi tests (header-only PNG/WebP rejection with
  `load`/`verify` patched to prove no decode, viewer holding the previous
  frame, crash-marker persistence and 2-crash escalation via a simulated
  kill, rolling re-validation escalation, wrong-clock backoff, URL-less hole,
  `OOMScoreAdjust`). GREEN with the full root suite (0 skipped). The old
  `test_invalid_photo_cannot_promote_partial_manifest` was replaced by
  `test_photo_without_url_is_a_hole_not_a_frozen_update`: a deliberate
  contract change (URL-less entry = hole, not a promotion blocker).
- **Consumer re-review follow-up (local only):** RED first — 4 failures +
  15 errors across new Pi tests (real Pillow-generated 24 MP progressive
  4:4:4 JPEG refused before `load`/`verify`, sync hole and viewer skip;
  coefficient estimate per sampling factor; ENOSPC on the state file during
  re-validation, download marker and marker clear; byte-capped render cache;
  last-good file kept after two re-validation deaths) and the Node test
  asserting serialized `createImageBitmap`. GREEN: 108 root tests, 32 Node
  tests, `tests/browser-native/run.py` 5/5. The old
  `test_rolling_revalidation_crash_marker_escalates` became
  `test_rolling_revalidation_crashes_never_delete_a_playing_file` (deliberate
  contract change); stamp-mismatched files still escalate. Memory figures are
  macOS arm64 / Pillow 13, not the Pi's 32-bit Pillow 9.4.
- **Second consumer re-review follow-up (local only):** RED first — the
  pure-Python non-interleaved SOF0 fixture (and, where `jpegtran` exists, a
  `jpegtran -scans` one) was decoded by the viewer instead of refused; a
  12 MP Pillow MPO was refused (`MPO 4000x3000 exceeds 8000000 pixels`) and a
  24 MP progressive MPO was refused by the wrong guard; 7 early-return/end
  state saves raised a bare `OSError: [Errno 28]`; the Node test with a
  never-settling `createImageBitmap` wedged the sync. GREEN: root suite,
  Node tests and `tests/browser-native/run.py`. `progressive_coefficient_bytes`
  / `MAX_PROGRESSIVE_JPEG_COEF_BYTES` were renamed `buffered_coefficient_bytes`
  / `MAX_BUFFERED_JPEG_COEF_BYTES` (same 80 MiB value).

## The sync timer bug

Fixed 2026-08-11. Worth reading before touching `frame-sync.timer`.

**Symptom:** the Pi was online and displaying, but had not synced since
2026-02-11 — roughly six months of stale photos, entirely silent.

**Cause:** the timer was

```ini
OnBootSec=30
OnUnitActiveSec=10min
```

`OnUnitActiveSec=` cannot arm until the triggered service has been activated at
least once in the current boot. On any boot where the `OnBootSec` trigger is
missed, neither elapse source is available and the timer parks permanently in
`SubState=elapsed` with `NextElapseUSecMonotonic=infinity`. It never fires
again until something manually starts the service.

The likely reason the boot trigger was missed: this Pi has **no RTC**.
`fake-hwclock` restores a stale time at boot and `systemd-timesyncd` then jumps
the clock — on the failing boot, forward 31 minutes about 10 seconds in, before
the 30-second deadline.

**Fix**, both parts of which are needed:

1. `frame-sync.timer` now uses `OnCalendar=*:0/10`, which always has a next
   elapse point regardless of whether the service ever ran. (`Persistent=true`
   was previously a no-op — it only affects calendar timers.) `OnBootSec` was
   raised to 2 minutes so the first sync waits for wifi and NTP.
2. `frame-sync.service` is now **enabled** (`WantedBy=multi-user.target`), so a
   sync happens once per boot even if the timer were somehow broken again.

Validated across a real reboot: the timer self-armed with a populated
`NextElapseUSecRealtime`, no manual intervention.

**Diagnostic:** `systemctl list-timers frame-sync.timer` showing `NEXT: -` is
the tell. Nothing else surfaces this — a dead syncer looks identical to a
healthy one from the couch, because the viewer keeps playing from cache.

```bash
systemctl list-timers frame-sync.timer     # NEXT must not be "-"
cat /opt/frame/.sync_state.json            # last_sync recent, no last_failed
journalctl -u frame-sync.service -n 20     # "Sync done" / "unchanged (304)"
du -sh /opt/frame/images                   # ~1 window (~90 MB), not GBs
```

## Install

Raspberry Pi OS Lite 32-bit (Bookworm), console only — no desktop, X11, or
browser. An earlier Trixie image was swapped out while troubleshooting the
graphics stack; Bookworm did not make SDL usable on a Pi Zero but is the stable
minimum underneath the framebuffer client.

Device preparation:

- console boot, screen blanking disabled;
- `consoleblank=0` on the kernel command line;
- `hdmi_force_hotplug=1` and `hdmi_blanking=0` in the boot config;
- GPU memory reduced to 16 MB;
- `getty@tty1.service` disabled so a login prompt cannot repaint the display;
- the service user granted access to `/dev/fb0` (video group).

```bash
sudo apt install -y python3 python3-pil python3-pip python3-venv jq curl
```

Install Pillow from apt (`python3-pil`) rather than building it on the Pi.

```bash
sudo install -d -o tcd -g tcd /opt/frame /opt/frame/images /opt/frame/staging
sudo install -o tcd -g tcd viewer.py frame_sync.py frame_cache.py /opt/frame/
sudo install -o tcd -g tcd .env.example /opt/frame/.env   # then edit
sudo cp -r systemd/. /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now frame-fb.service
sudo systemctl enable --now frame-sync.service frame-sync.timer
```

Keep `/opt/frame` owned by the service user throughout. A previous sync job run
under `sudo` created `.sync_state.json` as root and the unprivileged service
could then never update it.

### Why the viewer attaches to tty1 directly

`frame-fb.service` sets `TTYPath=/dev/tty1` plus `TTYReset`/`TTYVHangup`/
`TTYVTDisallocate` and `StandardInput=tty`. `openvt` worked when run by hand
but failed as an `ExecStart` with *"Couldn't get a file descriptor referring to
the console"* — it expects a controlling console a normal service does not
have. systemd's TTY properties grant that ownership explicitly.

`override.conf` carries the per-device slide duration so the shipped unit stays
generic.

## Known issues and divergences

- **Stale schema check.** `frame_sync.py` validates `schema != 1` and logs
  `Unexpected schema=2; continuing anyway` on every run that sees a changed
  manifest. The publisher now emits `schema: 2`. Harmless but noisy.
- **Cache identity diverges from `apps/client`.** Schema 2 gives each photo a
  full S3 `key` alongside the basename `id`; `apps/client` caches by
  `key || id` because basenames can collide across folders. `frame_sync.py`
  stores by `id` alone, so two photos sharing a basename would collide — the
  second is skipped by the `dest.exists()` check and the wrong image displays.
  **Currently latent:** the bucket holds 295 objects with 295 distinct
  basenames (verified 2026-08-11), so there is no live exposure.
- **Future `start_epoch` handling differs.** `compute_sync_index` and
  `seconds_until_next` clamp negative elapsed time to zero; `apps/client` has a
  distinct absolute-clock fallback. Only reachable if the publisher emits a
  `start_epoch` in the future.
- **The deployed device's cache is never pruned.** 323 files locally against
  295 in the bucket (618 MB used, 11 GB free). This repo's syncer prunes on
  every run (see above); the fix is not yet deployed.
- **RGB565 conversion is pure Python**, a per-pixel double loop, which is slow
  on a Pi Zero. Converted frames are cached, but the cache clears wholesale
  once it exceeds 50 entries, so a 50-photo window sits right at the boundary.
- **`.order` is legacy.** `viewer.py` still falls back to it when no usable
  manifest is present. It is not sync authority and can disagree with the
  manifest's length, which would desynchronise this device from the tablets.
  `ORDER_FILE` is defined in the recovered source, so the historical `NameError`
  that caused a restart loop is already fixed.

  It survives from the first proof of concept, which generated a manifest,
  converted it to `.order`, and ran `apply_manifest.py` to update service
  settings. That became a liability once remote sync existed — `.order` could
  hold a different number of entries than the manifest, so the Pi and the web
  client computed the same index against different lists, and `apply_manifest.py`
  brought `sudo`, file-ownership problems and needless service restarts. The
  syncer no longer calls it (the call is commented out at the bottom of
  `frame_sync.py`) and reads `manifest.photos[]` directly. Treat `.order` as
  state to be removed, not maintained.

## Approaches that were abandoned

- **Chromium kiosk** — needed X, Openbox, Chromium, JS and IndexedDB; too heavy
  and unreliable on a Pi Zero, especially with a large photo cache. Kept as
  `apps/pi-client/` for stronger hardware.
- **`fbi` / `fim`** — could paint the framebuffer but made control awkward:
  unreliable Ctrl+C and SSH behaviour, wrapper and viewer processes with
  different lifetimes, and giving up exact manifest-clock scheduling.
- **Pygame / SDL / KMSDRM** — the desired single-process Python control, but
  SDL could not initialise the Pi Zero graphics path. KMSDRM repeatedly failed
  with `EGL not initialized` even after installing Mesa/EGL/GBM; the fbcon
  backend was unavailable in that SDL build. Writing the framebuffer directly
  avoids SDL, EGL, GBM, GLES and X entirely.
- **`openvt` under systemd** — see the tty1 note above.

## Remote control

The old Chromium kiosk had a token-authenticated webhook that could point the
browser at another URL. This client has no browser, so arbitrary web pages are
out of scope. Native equivalents remain feasible but are **not implemented**:
triggering `frame-sync.service` on demand, switching the active local manifest,
temporarily showing a cached image or a camera snapshot, and returning to the
shared manifest.

## Remaining work

- No `install.sh`; the steps above have not been re-run end to end on fresh
  hardware.
- The device's boot config and tty/getty settings are described here from the
  running system but are not themselves captured in this repo.
- `tailscaled` on `frame-zero` has been dead since 2026-01-17, so remote access
  depends on a subnet router advertising the device's VLAN.
