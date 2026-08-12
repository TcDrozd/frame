# Shared Photo Frame — Client

**Status:** Frozen / Production  
**Current Tag:** `client-v1`  
**Role:** Appliance-grade slideshow client (offline-first)

---

## Purpose

This directory contains the **frame client** — a minimal, static web app designed to run fullscreen on an Android tablet (via Fully Kiosk Browser) as a dedicated digital photo frame.

The client is intentionally **dumb and stable**:
- It does not upload photos
- It does not manage users
- It does not generate manifests
- It does not expose configuration or UI

Its only responsibility is to:
> Fetch a manifest → cache listed photos locally → play them reliably forever.

---

## Files

- `index.html`  
  Minimal container and status overlay.

- `app.js`  
  All runtime logic: sync, cache, playback, error handling.

There are no build steps, frameworks, or dependencies.

---

## Runtime Model

### Startup
1. Load immediately from local cache (if available)
2. Begin background sync against the manifest URL
3. Never block playback on network availability

### Offline Behavior
- Fully offline-capable once photos are cached
- Network failures do **not** interrupt playback
- Sync resumes opportunistically when network returns

---

## Manifest Contract (Input)

The client consumes **one input**: a `manifest.json` served over HTTP(S).

Minimal required structure:

```json
{
  "schema": 1,
  "version": "v2025-12-26",
  "generated_at": "2025-12-26T15:00:00Z",
  "mode": "inventory",
  "slide_seconds": 3600,
  "photos": [
    { "id": "IMG_1234.jpg", "url": "https://..." }
  ]
}