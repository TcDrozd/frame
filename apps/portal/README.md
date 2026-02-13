# Shared Photo Frame — Portal (server-side)

FastAPI-based dashboard/portal for managing the photo stream **without touching the frame client**.

## What it does (MVP)
- Issues **pre-signed S3 POST** uploads (browser uploads directly to S3)
- Records photo metadata in a local DB (SQLite by default)
- Provides a simple dashboard:
  - Status (last publish run, current settings)
  - Upload page
  - Gallery with actions: **Pin Now**, **Bump**, **Hide**
- Orchestrates your existing publisher script (`publish_manifest.py`) to generate `manifest.json` in S3.

## Requirements
- Python 3.11+
- AWS credentials on the portal host **for signing + publishing** (uploading clients do not get keys)
- Access is expected to be **Tailscale-only** for now.

## Quick start (dev)
```bash
python -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt

cp .env.example .env
# edit .env (bucket, region, publisher path)

# initialize DB
python scripts/portalctl.py db init
python scripts/portalctl.py db upgrade
python scripts/portalctl.py seed-admin --username admin --password 'change-me'

# run
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open:
- `http://<tailnet-hostname>:8000/` (redirects to login)

## Publisher integration
This portal treats the publisher as the "render engine" and shells out to it. Configure:
- `PUBLISHER_PATH=/path/to/tools/publish_manifest.py`

If you prefer vendoring the publisher script into this repo, drop it in:
- `tools/publish_manifest.py`
and set `PUBLISHER_PATH=tools/publish_manifest.py`

## Notes
- Default DB is SQLite at `./data/portal.db` (gitignored). You can switch to Postgres later.
- The UI uses minimal JS + HTMX for "appliance-grade" simplicity.
