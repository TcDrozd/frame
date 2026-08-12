# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

V2 monorepo for the Shared Photo Frame ecosystem. Everything exists to produce and consume one artifact: `manifest.json` in the photo S3 bucket, which the display clients poll. The manifest is the contract between publisher and clients — see `apps/client/README.md`.

**Manifest schema:** the live publisher emits **`schema: 2`** (verified 2026-08-11), where each photo is `{id, key, url, name, bytes, etag}` — `id` is the basename, `key` the full S3 key. Schema 1 (`photos: [{id, url}]`) is the older shape; consumers must keep reading it. **Cache/identify photos by `key || id`, never `id` alone** — basenames can collide across folders, which is exactly why `key` was added (`apps/client` `normalizeManifest()` does this; `apps/pi-zero-client` does not yet — see its README).

**The one publisher — `apps/frame-dash` (serverless):** AWS SAM stack (see `apps/frame-dash/README.md` for the full architecture). Vanilla-JS SPA on S3+CloudFront → Cognito Hosted UI → API Gateway (JWT authorizer) → API Lambda (`src/api`) with playlists in DynamoDB (ordered key list = playback order). Publishing renders a playlist to the manifest with presigned URLs; a scheduled Lambda (`src/scheduled`) refreshes the URLs while reusing the frozen `resolved_start_epoch` so frames keep lockstep playback — only an explicit publish restarts the show. With `AutoPublishMode=window` that Lambda also auto-publishes a daily-rotating window of photos (`src/shared/auto_select.py`) whenever no curated playlist is active. Shared logic lives in `src/shared` (both Lambdas import it). The photo bucket pre-exists and is not managed by the stack.

**Uploads/metadata — `apps/portal`** (FastAPI + Jinja2/HTMX, SQLite via SQLAlchemy/Alembic): issues pre-signed S3 POSTs so browsers upload directly to S3, records photo metadata in SQLite, manages pins/bumps/hides. Its publish path was removed (publishing is frame-dash's job); the portal is slated for retirement once frame-dash gains an upload view.

**Consumer:** **`apps/client`** (static `index.html` + `app.js`, no build/deps) — **frozen/production** kiosk slideshow. Fetch manifest → cache locally → play offline-first. Intentionally dumb; changes here are rare.

**Second consumer — `apps/pi-zero-client`:** the native client actually deployed on the Pi Zero W `frame-zero`. Python + Pillow writing straight to `/dev/fb0`, no browser or GUI stack: `viewer.py` (always-running slideshow, reads only the local manifest) plus `frame_sync.py` (oneshot syncer on a systemd timer). Obeys the same ordered `photos[]` and sync clock as `apps/client`, and keeps playing from its local cache when offline. Recovered from the live device on 2026-08-11 — the source is now in the repo but there is still no `install.sh`. See its README, especially the sync-timer failure mode that silently froze photos for six months.

**Consumer host — `apps/pi-client`:** Raspberry Pi appliance that displays `apps/client` in a browser (X + Openbox + fullscreen Chromium on boot, systemd units, token-auth Flask webhook writing `/var/lib/kiosk/url.txt`, watched by a `.path` unit that restarts the browser). It runs the client unmodified and knows nothing about manifests — a *host*, not a client. **Not what runs on `frame-zero`** (that is `apps/pi-zero-client`); the browser stack was too heavy for a Pi Zero, so this is kept for provenance and stronger hardware — never install it over the live Pi Zero. Vendored from the separate private repo `TcDrozd/pi-dash` @ `00c69ad`; four upstream bugs were fixed on the way in and `install.sh` has not been re-run on hardware (see its README). Vendored as plain files, not a submodule.

`legacy/` is archived v1 source — reference only, never deploy from it. Vendored as plain files (`legacy/v1/shared-photo-frame` was a broken gitlink until 2026-08-10; see `legacy/README.md` — never `git add` a directory containing a `.git/`). Note the naming trap: the commit under `legacy/v1/` is tagged `client-v2.0.0` upstream. `tools/publish_manifest.py` is the retired v1 publisher CLI, kept only as a break-glass copy (its `--inject-placement random` path has a known `NameError`). The v1 `apps/publisher-api` was deleted (it exposed an unauthenticated publish endpoint); see git history.

## Commands

### Portal (run from `apps/portal/`)
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # edit bucket/region/secrets

python scripts/portalctl.py db init                   # ensure alembic versions dir
python scripts/portalctl.py db upgrade                # alembic upgrade head
python scripts/portalctl.py db revision -m "msg"      # autogenerate migration
python scripts/portalctl.py seed-admin --username admin --password '...'

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Docker alternative: `docker compose up` (maps host 8002 → container 8000). Dev DB is `./data/portal.db` (gitignored).

### frame-dash (run from `apps/frame-dash/`)
```bash
sam build && sam deploy      # dev: publishes to manifest.dev.json
./scripts/deploy_web.sh      # sync SPA to S3 + CloudFront invalidation
```
After a stack change that alters outputs, refresh `web/js/config.js` from `aws cloudformation describe-stacks --stack-name frame-dash`. `sam deploy --config-env prod` overrides `ManifestKey=manifest.json` (production cutover — see the runbook in `apps/frame-dash/README.md`).

### Pi Zero client (`frame-zero`, reached with `ssh pi`)
```bash
systemctl list-timers frame-sync.timer   # NEXT must not be "-" (see app README)
cat /opt/frame/.sync_state.json          # last_sync should be recent
journalctl -u frame-sync.service -n 20   # "Sync done" / "Manifest unchanged (304)"
sudo systemctl start frame-sync.service  # force a sync now
```
Deployed by hand to `/opt/frame` (owned by the service user), not by `tools/deploy/`. Remote access needs a subnet router advertising the device's VLAN — its own `tailscaled` has been dead since 2026-01-17.

### Tests (run from repo root; stdlib `unittest` with `botocore.stub.Stubber`, no live AWS)
```bash
# root tests (import tools.s3_rsync, hence repo-root cwd)
python3 -m unittest discover -s tests -v
python3 -m unittest tests.test_s3_rsync.TestS3Rsync.test_nested_path_key_mapping   # single test

# frame-dash tests (its .venv has boto3)
apps/frame-dash/.venv/bin/python -m unittest discover -s apps/frame-dash/tests -v
```

## Deployment model (matters when editing paths/scripts)

### Server apps (portal, client)
- Source of truth is this repo (checked out as `~/frame` on the server); runtime is `/opt/frame`.
- `tools/deploy/deploy_*.sh` rsync `apps/<app>` → `/opt/frame/apps/<app>` with `--delete`, excluding `.env`, `data/`, venvs.
- **Key rule:** deploy may freely replace `/opt/frame/apps/*` but must never touch `/opt/frame/var/*` (prod DB, selections, uploads) or `/opt/frame/venv/*`.
- Prod paths: portal DB at `/opt/frame/var/portal/portal.db`.

### frame-dash (AWS, account 084683516815)
- `samconfig.toml` pins `profile = "frame-dash-deploy"`, a least-privilege IAM user (policy versioned at `deploy-policy.json`) that can only manage `frame-dash-*` resources. Admin operations (policy updates, Cognito user creation) use the `sunflower-dev` profile. Region is `us-east-1`.
- **Key rule:** the stack parameter `ManifestKey` defaults to `manifest.dev.json` so a dev deploy can never clobber the live manifest; only the prod config env writes `manifest.json`.
- Bucket policy allows public `s3:GetObject` on the manifest keys only; photos stay private behind presigned URLs.

## Conventions

- No secrets in the repo: `.env` files are gitignored and excluded from deploy; AWS credentials come from standard SDK resolution (env/`~/.aws`/IAM role), never code.
- No SQLite DBs in the repo — prod state lives under `/opt/frame/var/`.
- Portal UI/API links use **named routes** (`request.url_for(...)` / `name=` on route decorators), not hardcoded paths.
- Portal auth uses argon2 via passlib (`app/auth.py`); sessions are signed with `itsdangerous`.
- frame-dash SPA is deliberately buildless vanilla JS (like the client) — no frameworks or bundlers.
