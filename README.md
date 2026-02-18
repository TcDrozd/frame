# frame (monorepo)

This repo is the refactored monorepo for the Shared Photo Frame ecosystem.

It contains **deployable apps** (portal, publisher API, client) and **legacy v1 source** kept for reference.

---

## Layout

### Repo (source of truth)

frame/
  apps/
    portal/            # FastAPI portal (admin UI + API)
    publisher-api/     # Flask API that serves published content / endpoints
    client/            # display client (static files)
  tools/               # helper scripts (deploy, maintenance, utilities)
  legacy/
    v1/                # archived v1 code (do not deploy from here)


### Runtime (production on the host)
Runtime lives under **/opt** and is intentionally split into:
- **code** (safe to deploy with rsync --delete)
- **data** (never touched by deploy)
- **venvs** (never touched by deploy unless rebuilding deps)


/opt/frame/
  apps/                # deployed code only
  var/                 # writable runtime state (db, selections, uploads, etc.)
  venv/                # per-app python venvs
  VERSION              # optional deploy stamp log


#### Key rule
> Deploy scripts may replace `/opt/frame/apps/*` freely, but must not delete or overwrite `/opt/frame/var/*` or `/opt/frame/venv/*`.

---

## V1 vs V2

- **V1** runtime is currently preserved separately (e.g. `/opt/shared-photo-frame/...`).
- This monorepo is the **V2** source and deployment target (`/opt/frame/...`).

The intent is to allow iteration on V2 without breaking the currently running V1 client/service.

---

## Deployment approach

### Deploy philosophy
- **Source:** `~/frame` (this repo)
- **Runtime:** `/opt/frame`
- Deploy uses `rsync` to copy code into `/opt/frame/apps/<app>`
- Runtime state is stored in `/opt/frame/var/<app>`

### Common paths
- Portal DB (prod): `/opt/frame/var/portal/portal.db`
- Publisher selections (prod): `/opt/frame/var/publisher-api/selections/`
- Venvs:
  - `/opt/frame/venv/portal/`
  - `/opt/frame/venv/publisher-api/`

---

## Local development (suggested)

This repo is designed to be developed locally and synced to the server via git.

Suggested flow:
1. Develop on local machine
2. Push to remote (GitHub/Gitea/etc.)
3. Pull on server into `~/frame`
4. Run deploy scripts to sync into `/opt/frame`

---

## Notes / conventions

- Secrets should not be committed (`.env` files are ignored). Use:
  - `/etc/<app>/...` or
  - systemd EnvironmentFile, or
  - a secrets directory outside the repo.
- Avoid storing SQLite DBs in the repo. Use `/opt/frame/var/...` for production.
- `legacy/` is reference-only unless explicitly stated.

---

## Status
This repo is actively under refactor; service files and deploy scripts are being standardized to point at `/opt/frame/...`.

## S3 Photo Sync CLI (`tools/s3_rsync.py`)

`tools/s3_rsync.py` syncs a local directory tree to an S3 prefix with idempotent upload checks and optional content dedupe.

### Install

```bash
pip install boto3
```

### AWS credentials

Credentials are loaded using standard AWS SDK resolution (env vars, `~/.aws/credentials`, IAM role, etc.).  
Do not put credentials in code.

### Examples

```bash
# Non-interactive dry run
python tools/s3_rsync.py --source "/photos/2025" --dest "s3://trevor-photo-bucket/photos/2025/" --dry-run

# Interactive (prompts for source and destination)
python tools/s3_rsync.py

# More worker threads
python tools/s3_rsync.py --source ./export --dest s3://my-bucket/ingest/export/ --workers 8

# Enable content dedupe by SHA-256
python tools/s3_rsync.py --source ./photos --dest s3://my-bucket/photos/ --content-dedupe
```

### Idempotency behavior

For each local file, the script maps `relative/path.ext` to `s3://bucket/prefix/relative/path.ext` and runs `head_object`:

1. If object is missing, upload.
2. If object exists and has metadata `sha256`, compare against local SHA-256 and skip when equal.
3. If `sha256` metadata is not present, fall back to metadata `size` + `mtime` and skip when both match.
4. Otherwise upload and set metadata:
   - `x-amz-meta-sha256`
   - `x-amz-meta-size`
   - `x-amz-meta-mtime`

### Cache file

Default cache path is `.s3_rsync_cache.json` (override with `--cache`).  
It stores local file signature (`path + size + mtime`) to SHA-256 so unchanged files are not re-hashed each run.  
Writes are atomic (temp file + rename) to reduce corruption risk.
