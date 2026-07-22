# Shared Photo Frame — Client

**Status:** Production (v3, `app.js?v=4`)  
**Role:** Appliance-grade slideshow client (offline-first)

---

## Purpose

This directory contains the **frame client** — a minimal, static web app designed to run fullscreen on an Android tablet (via Fully Kiosk Browser) as a dedicated digital photo frame.

The client is intentionally **dumb and stable**:
- It does not upload photos
- It does not manage users or hold any credentials — photo URLs in the
  manifest arrive pre-signed, so the client never authenticates
- It does not generate manifests
- It does not expose configuration or UI

Its only responsibility is to:
> Fetch a manifest → cache listed photos locally → play them reliably forever.

---

## Files

- `index.html`  
  Minimal container and status overlay. The overlay shows boot/sync progress
  and auto-hides shortly after the first photo renders (`STATUS_HIDE_MS` in
  `app.js`); it stays visible if playback never starts, so failures remain
  readable on-screen. Bump the `app.js?v=N` query param whenever `app.js`
  changes — it is the cache-buster kiosk browsers see.

- `app.js`  
  All runtime logic: sync, cache, playback, error handling.

There are no build steps, frameworks, or dependencies — plain browser APIs
only (fetch, IndexedDB, DOM).

---

## Runtime Model

### Startup
1. Load immediately from local cache (if available)
2. Begin background sync against the manifest URL
3. Never block playback on network availability

### Sync & recovery (v3)
- Manifest polled hourly; fetches carry timeouts (15s manifest / 60s photo)
  so a hung socket can never stall sync
- Photo downloads that 403/404 (dead URLs) trigger an early manifest
  refetch with 1m → 5m → 30m backoff instead of waiting out the hour
- `url_expires_at` is honored: the manifest is refetched shortly before
  photo URLs expire
- `online` / `visibilitychange` events nudge a debounced resync
- Cache identity is the schema-2 full `key` (falls back to `id`); an
  `etag` change re-downloads the photo in place
- Cache deletions happen only **after** replacement downloads land, so a
  mid-sync network drop can never shrink the cache

### Offline Behavior
- Fully offline-capable once photos are cached
- Network failures do **not** interrupt playback — HTTP errors (403, 404)
  and unreachable networks take the same path: keep playing from cache
- Sync resumes opportunistically when network returns

### Synchronized playback
In `sync` mode the slide index AND the slide boundaries are both computed
from `start_epoch` wall-clock math, so NTP-synced frames flip together
regardless of when each booted.

### Remote reconfiguration
Load `index.html?manifest=<url>` once and the URL persists to
localStorage — this is how frames are repointed without touching code
(e.g. via Fully Kiosk remote admin's Load URL).

---

## Manifest Contract (Input)

The client consumes **one input**: a `manifest.json` served over HTTP(S).

Schema 2 (current, produced by `apps/frame-dash`) — a strict superset of
schema 1, so schema-1 clients keep working unchanged:

```json
{
  "schema": 2,
  "version": "v20260101-000000Z",
  "generated_at": "2026-01-01T00:00:00Z",
  "mode": "sync",
  "slide_seconds": 1380,
  "start_epoch": 1750000000,
  "url_expires_at": 1757776000,
  "photos": [
    {
      "id": "IMG_1234.jpg",
      "url": "https://...",
      "name": "IMG 1234",
      "bytes": 123456,
      "key": "photos/2026/trip/IMG_1234.jpg",
      "etag": "abc123"
    }
  ]
}
```

Schema-1 fields (`schema`, `version`, `generated_at`, `mode`,
`slide_seconds`, `photos[].id`, `photos[].url`, plus `start_epoch` when
`mode == "sync"`) must never change name or type. Schema-2 additions:

- `photos[].key` — full S3 key; preferred cache identity (`id` is the
  basename and collides across folders)
- `photos[].etag` — content-change token; re-download when it changes
- `url_expires_at` — epoch when photo URLs die; refetch the manifest
  before then

The canonical example is the golden fixture at
`apps/frame-dash/tests/fixtures/golden_manifest.json`, pinned by
`test_manifest_renderer.py::TestGoldenManifest`. To manually contract-check
a client build, serve that fixture and point the client at it:
`index.html?manifest=<url-to-fixture>` (the URLs inside won't resolve, but
parse/sync/status behavior must not error).