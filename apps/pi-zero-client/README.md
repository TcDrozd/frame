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

**Pruning is skipped whenever a download failed.** Offline playback is the
property that must never break: a run that loses the network half way through
keeps the images the viewer is still playing from and retries on the next tick.
A failed run also persists *neither* the ETag *nor* the content signature —
both are early returns, so keeping them would make every run for the rest of
the publish window skip straight past the photos that just failed. It records
`last_failed` in `.sync_state.json` instead.

Because a publish re-signs every URL, the content signature changes once per
publish (~2h) rather than per sync tick (10 min); runs in between are 304s and
touch nothing.

Covered by `tests/test_frame_cache.py` and `tests/test_frame_sync.py`, which
runs the syncer end to end against a local HTTP server.

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
- **The cache is never pruned.** 323 files locally against 295 in the bucket —
  images dropped from the manifest stay forever. Harmless today (618 MB used,
  11 GB free) but unbounded.
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
