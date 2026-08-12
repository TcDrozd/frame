# frame

A shared digital photo frame: family members' photos land in one S3 bucket, a
curator picks what plays, and tablets and a Pi Zero mounted in different houses
show the same photo at the same time — and keep showing photos when the network
is gone.

Everything in this repo exists to produce or consume **one artifact**: a
`manifest.json` in the photo bucket. Producers write it; frames poll it. The
manifest is the entire integration surface between the two halves of the
system, and its schema is a frozen contract.

## How it works

```
       photos                          curation                    playback
 ┌───────────────────┐        ┌────────────────────────┐     ┌──────────────────┐
 │ portal (uploads)  │        │ frame-dash             │     │ browser client   │
 │ tools/s3_rsync.py │──────► │  playlists in DynamoDB │     │ Android tablets  │
 │  (bulk ingest)    │        │  renders + presigns    │     ├──────────────────┤
 └───────────────────┘        └───────────┬────────────┘     │ native client    │
           │                              │ writes           │ Pi Zero -> fb0   │
           ▼                              ▼                  └────────▲─────────┘
      s3://trevor-shared-photo-stream/photos/…     …/manifest.json ───┘
           (private, presigned URLs only)           (public read)
```

1. **Photos go into S3.** Either through the portal's browser uploads
   (pre-signed POST, so bytes never pass through a server) or in bulk from a
   laptop with `tools/s3_rsync.py`.
2. **A playlist is published.** frame-dash renders an ordered list of S3 keys
   into a manifest of pre-signed GET URLs and writes it to the manifest key.
3. **Frames poll and cache.** Browser clients fetch the manifest and store
   photos in IndexedDB. The Pi Zero has a separate Python sync job that stores
   the manifest and photos on disk, while an always-running Pillow viewer
   writes directly to `/dev/fb0`. Both continue from their local cache when the
   network is unavailable.

Presigned URLs expire, so a scheduled Lambda re-publishes every couple of hours
to refresh them. It reuses the `resolved_start_epoch` frozen at the original
publish, which is what keeps geographically separate frames on the same slide:
playback position is wall-clock math from that epoch, not from boot time. Only
an explicit publish restarts the show.

## Components

| Path | What it is | Runs on |
| --- | --- | --- |
| `apps/frame-dash` | **The publisher.** Vanilla-JS SPA + Cognito + API Gateway + Lambda + DynamoDB, deployed with AWS SAM. Browse the library, build playlists, publish one. Also auto-publishes a rotating window when nothing is curated. | AWS (account 084683516815, us-east-1) |
| `apps/client` | **The browser display.** Static `index.html` + `app.js`, no build step or dependencies. Fetch -> IndexedDB cache -> play. Frozen/production; changes here are rare and deliberate. | Android tablets under Fully Kiosk |
| `apps/pi-zero-client` | **The native Pi Zero display.** Python + Pillow, local manifest/image cache, direct `/dev/fb0` rendering, and separate viewer/sync systemd units. Source recovered from the live device 2026-08-11; no `install.sh` yet. | Original Pi Zero on an HDMI display |
| `apps/pi-client` | **Legacy browser host.** Vendored X + Openbox + Chromium kiosk with a URL-switching webhook. It predates the native client, has not been re-verified, and is not what the live Pi Zero runs. | Retained for provenance or stronger Pi hardware |
| `apps/portal` | Uploads + photo metadata (FastAPI, HTMX, SQLite). Pins/bumps/hides. Its publish path was removed. Slated for retirement once frame-dash grows an upload view. | Home server, `/opt/frame`, Tailscale-only |
| `tools/` | `s3_rsync.py` (bulk photo ingest), `deploy/` (rsync deploy scripts), `manual-selector/` and `generate_manifest-v1.py` (v1-era helpers), `publish_manifest.py` (retired v1 publisher, break-glass only) | Laptop / server |
| `legacy/` | Archived v1 source, vendored as plain files. Reference only — never deploy from here. See `legacy/README.md` for provenance. | — |

Each app has its own README with the detail: architecture and runbooks in
`apps/frame-dash/README.md`, the manifest contract and recovery model in
`apps/client/README.md`, setup in `apps/portal/README.md`, and the native Pi
architecture, install steps and failure modes in
[`apps/pi-zero-client/README.md`](apps/pi-zero-client/README.md).

## The manifest contract

The client is intentionally dumb, which only works if the manifest never
surprises it. Schema 2 (current) is a strict superset of schema 1:

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
    { "id": "IMG_1234.jpg", "url": "https://…", "key": "photos/2026/trip/IMG_1234.jpg", "etag": "abc123" }
  ]
}
```

Schema-1 fields never change name or type. The canonical example is the golden
fixture at `apps/frame-dash/tests/fixtures/golden_manifest.json`, pinned by a
test — if a change breaks that test, it breaks frames in other people's houses.
Field-by-field notes live in `apps/client/README.md`.

## Where things run

**AWS.** frame-dash owns the manifest. The photo bucket pre-exists and is *not*
managed by the SAM stack; the stack only gets least-privilege IAM against it.
The bucket policy allows public `s3:GetObject` on the manifest keys only —
photos stay private and are reachable exclusively through presigned URLs.

The stack parameter `ManifestKey` defaults to **`manifest.dev.json`**, so a dev
deploy can never clobber the live show. Only `sam deploy --config-env prod`
writes `manifest.json`.

**The home server.** Source of truth is this repo, checked out as `~/frame`;
runtime lives at `/opt/frame`. `tools/deploy/deploy_*.sh` rsync
`apps/<app>` → `/opt/frame/apps/<app>` with `--delete`.

```
/opt/frame/
  apps/     deployed code only — safe to replace wholesale
  var/      writable state (portal DB at var/portal/portal.db, uploads) — never touched by deploy
  venv/     per-app virtualenvs — never touched by deploy
  VERSION   deploy stamp log
```

> **Key rule:** deploy may replace `/opt/frame/apps/*` freely, but must never
> delete or overwrite `/opt/frame/var/*` or `/opt/frame/venv/*`.

The v1 runtime still exists separately at `/opt/shared-photo-frame/…` so v2 can
be iterated on without taking a live frame down.

**The Pi Zero.** Its native runtime lives separately at `/opt/frame` on the Pi:
`viewer.py` renders the local manifest to `/dev/fb0`, while `frame_sync.py` is
run by a systemd timer to refresh the manifest and image cache. Both, plus the
systemd units, now live in `apps/pi-zero-client/`, recovered from the device on
2026-08-11. Do not use `apps/pi-client` as a replacement — it installs the
obsolete Chromium kiosk. See
[`apps/pi-zero-client/README.md`](apps/pi-zero-client/README.md), particularly
the sync-timer failure mode that silently froze the frame's photos for six
months.

## Working in this repo

```bash
# frame-dash (from apps/frame-dash/)
sam build && sam deploy          # dev → manifest.dev.json
./scripts/deploy_web.sh          # sync SPA to S3 + CloudFront invalidation

# portal (from apps/portal/)
python -m venv venv && source venv/bin/activate && pip install -r requirements.txt
python scripts/portalctl.py db upgrade
uvicorn app.main:app --port 8000 --reload

# tests (from repo root; stdlib unittest, botocore Stubber, no live AWS)
python3 -m unittest discover -s tests -v
apps/frame-dash/.venv/bin/python -m unittest discover -s apps/frame-dash/tests -v
```

Conventions:

- No secrets in the repo. `.env` files are gitignored and excluded from deploy;
  AWS credentials come from standard SDK resolution (env, `~/.aws`, IAM role),
  never from code.
- No SQLite databases in the repo — production state lives under `/opt/frame/var/`.
- The browser client and the frame-dash SPA are deliberately buildless vanilla
  JS. The separate Pi Zero client is deliberately minimal Python + Pillow with
  no browser or GUI stack.
- Portal UI/API links use named routes (`request.url_for(...)`), not hardcoded
  paths.

## Bulk photo ingest (`tools/s3_rsync.py`)

Syncs a local directory tree to an S3 prefix, idempotently — for seeding the
library from an existing photo archive rather than uploading one at a time.

```bash
pip install boto3
python tools/s3_rsync.py --source ./photos --dest s3://my-bucket/photos/ --dry-run
python tools/s3_rsync.py                      # interactive prompts
python tools/s3_rsync.py --source ./export --dest s3://my-bucket/ingest/ --workers 8
python tools/s3_rsync.py --source ./photos --dest s3://my-bucket/photos/ --content-dedupe
```

Each local file maps `relative/path.ext` → `s3://bucket/prefix/relative/path.ext`
and is checked with `head_object` before upload: skip if the stored `sha256`
metadata matches, else fall back to `size` + `mtime`, else upload and record
`x-amz-meta-sha256` / `-size` / `-mtime`. A local cache
(`.s3_rsync_cache.json`, override with `--cache`) maps path+size+mtime to
SHA-256 so unchanged files aren't re-hashed; it's written atomically.
