from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from ..db import get_db
from ..auth import get_current_user
from ..models.photo import Photo
from ..models.pin import Pin
from ..services import s3_service
from ..services.publish_service import publish
from ..config import settings
from ..services.settings_service import get_effective_settings

router = APIRouter(prefix="/api")

def _require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        raise PermissionError("unauthorized")
    return user

@router.post("/uploads/presign", name="presign_upload")
def presign_upload(request: Request, payload: dict, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    original_filename = payload.get("filename")
    content_type = payload.get("content_type")
    eff = get_effective_settings(db)
    upload_prefix = eff.get("default_upload_prefix")
    key = s3_service.build_object_key(original_filename, content_type, upload_prefix=upload_prefix)

    presigned = s3_service.presign_post(key=key, content_type=content_type)
    return {"key": key, "presigned": presigned}

@router.post("/uploads/complete", name="upload_complete")
def upload_complete(request: Request, payload: dict, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    key = payload["key"]
    original_filename = payload.get("filename")

    head = s3_service.head_object(key)
    etag = (head.get("ETag") or "").strip('"')
    size = head.get("ContentLength")
    ctype = head.get("ContentType")

    photo = Photo(
        s3_key=key,
        original_filename=original_filename,
        etag=etag,
        size_bytes=size,
        content_type=ctype,
        uploaded_by=user.username,
    )
    db.add(photo)
    db.commit()

    # auto-bump new uploads
    if settings.AUTO_BUMP_ON_UPLOAD:
        expires_at = datetime.utcnow() + timedelta(hours=settings.DEFAULT_BUMP_EXPIRES_HOURS)
        db.add(Pin(s3_key=key, kind="priority", weight=100, created_by=user.username, expires_at=expires_at))
        db.commit()

    run = None
    if settings.AUTO_PUBLISH_ON_UPLOAD:
        run = publish(db)

    return {"ok": True, "photo_id": photo.id, "published": bool(run), "publish_run_id": getattr(run, "id", None)}

@router.post("/photos/{photo_id}/pin-now", name="pin_now")
def pin_now(photo_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        return {"ok": False, "error": "not_found"}

    expires_at = datetime.utcnow() + timedelta(hours=settings.DEFAULT_PIN_EXPIRES_HOURS)
    db.add(Pin(s3_key=photo.s3_key, kind="pin_now", weight=999, created_by=user.username, expires_at=expires_at))
    db.commit()

    run = publish(db)
    return {"ok": True, "publish_run_id": run.id}

@router.post("/photos/{photo_id}/bump", name="bump")
def bump(photo_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        return {"ok": False, "error": "not_found"}

    expires_at = datetime.utcnow() + timedelta(hours=settings.DEFAULT_BUMP_EXPIRES_HOURS)
    db.add(Pin(s3_key=photo.s3_key, kind="priority", weight=200, created_by=user.username, expires_at=expires_at))
    db.commit()

    run = publish(db)
    return {"ok": True, "publish_run_id": run.id}

@router.post("/photos/{photo_id}/hide", name="hide")
def hide(photo_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        return {"ok": False, "error": "not_found"}
    photo.active = False
    db.commit()

    run = publish(db)
    return {"ok": True, "publish_run_id": run.id}

@router.post("/publish", name="publish_now")
def publish_now(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    run = publish(db)
    return {"ok": run.success, "publish_run_id": run.id, "error": run.error_text}
