from __future__ import annotations

import json
import subprocess
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from ..config import settings
from ..models.publish_run import PublishRun
from ..models.pin import Pin
from .settings_service import get_effective_settings
from .s3_service import get_manifest_head

def _now():
    return datetime.now()

def _csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    # allow comma-separated or newline-separated
    parts = []
    for raw in value.replace("\n", ",").split(","):
        p = raw.strip()
        if p:
            parts.append(p)
    return parts

def _csv_arg(values: list[str]) -> str:
    return ",".join(values)

def _active_pin_now(db: Session) -> Optional[str]:
    # latest non-expired pin_now
    q = db.query(Pin).filter(Pin.kind == "pin_now").order_by(Pin.created_at.desc())
    for p in q.all():
        if p.expires_at is None or p.expires_at > _now():
            return p.s3_key
    return None

def _priority_keys(db: Session, limit: int = 10) -> list[str]:
    q = db.query(Pin).filter(Pin.kind == "priority").order_by(Pin.weight.desc(), Pin.created_at.desc())
    keys = []
    for p in q.all():
        if p.expires_at is not None and p.expires_at <= _now():
            continue
        keys.append(p.s3_key)
        if len(keys) >= limit:
            break
    return keys

def _build_args(db: Session) -> list[str]:
    eff = get_effective_settings(db)
    args = ["python3", settings.PUBLISHER_PATH,
            "--bucket", settings.S3_BUCKET,
            "--region", settings.AWS_REGION,
            "--manifest-key", settings.MANIFEST_KEY,
            "--limit", str(int(eff.get("limit", settings.DEFAULT_LIMIT))),
    ]

    # Prefix selection (new hierarchical library support)
    include_prefixes = _csv_list(eff.get("include_prefixes"))
    exclude_prefixes = _csv_list(eff.get("exclude_prefixes"))

    if include_prefixes:
        args += ["--photos-prefixes", _csv_arg(include_prefixes)]

    if exclude_prefixes:
        args += ["--exclude-prefixes", _csv_arg(exclude_prefixes)]

    shuffle = eff.get("shuffle_mode", settings.DEFAULT_SHUFFLE_MODE)
    if shuffle == "daily":
        args += ["--shuffle", "daily"]
    elif shuffle == "random":
        args += ["--shuffle", "random"]
    # none => no flags

    mode = eff.get("mode", settings.DEFAULT_MODE)
    if mode == "sync":
        args += ["--mode", "sync"]
        # start epoch handling
        start_epoch = eff.get("start_epoch", settings.DEFAULT_START_EPOCH)
        if start_epoch == "now":
            args += ["--start-epoch", "now"]
    else:
        args += ["--mode", "inventory"]

    pin_now = _active_pin_now(db)
    if pin_now:
        args += ["--pin-first", pin_now]

    # If/when your script supports it, you can pass a priority list.
    # For now, we write a small json file and pass it if a flag exists later.
    # Keeping this hook in place makes it trivial to upgrade.
    priority = _priority_keys(db, limit=10)
    if priority:
        # portal generates a json file in ./data/priority.json
        import os
        os.makedirs("data", exist_ok=True)
        priority_path = "data/priority.json"
        with open(priority_path, "w", encoding="utf-8") as f:
            json.dump({"priority_keys": priority}, f)
        # NOTE: Only enable once your script supports it:
        # args += ["--priority-json", priority_path]
        pass

    return args

def publish(db: Session) -> PublishRun:
    run = PublishRun(success=False, manifest_key=settings.MANIFEST_KEY)
    db.add(run)
    db.commit()
    db.refresh(run)

    eff = get_effective_settings(db)
    run.settings_snapshot_json = json.dumps(eff, sort_keys=True)

    args = _build_args(db)

    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=True)
        run.success = True
        run.error_text = None
    except subprocess.CalledProcessError as e:
        run.success = False
        run.error_text = (e.stdout or "") + "\n" + (e.stderr or "")
    except Exception as e:
        run.success = False
        run.error_text = str(e)

    run.finished_at = _now()
    # best-effort manifest metadata
    mh = get_manifest_head()
    if mh:
        run.manifest_etag = (mh.get("ETag") or "").strip('"')
    db.commit()
    db.refresh(run)
    return run
