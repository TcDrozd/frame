from __future__ import annotations

from sqlalchemy.orm import Session
from ..models.setting import Setting
from ..config import settings as app_settings

DEFAULTS = {
    "limit": str(app_settings.DEFAULT_LIMIT),
    "shuffle_mode": app_settings.DEFAULT_SHUFFLE_MODE,
    "mode": app_settings.DEFAULT_MODE,
    "start_epoch": app_settings.DEFAULT_START_EPOCH,
}

def get_setting(db: Session, key: str) -> str | None:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else None

def set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()

def get_effective_settings(db: Session) -> dict:
    out = dict(DEFAULTS)
    rows = db.query(Setting).all()
    for r in rows:
        out[r.key] = r.value
    return out
