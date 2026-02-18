from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from ..db import get_db
from ..auth import get_current_user, verify_password, make_session_token, COOKIE_NAME
from ..models.user import User
from ..models.photo import Photo
from ..models.publish_run import PublishRun
from ..services.settings_service import get_effective_settings, set_setting
from ..services import s3_service
from ..config import settings

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()
GALLERY_PAGE_SIZE = 40

# -------- helpers --------

def _redirect(to: str, *, request: Request, status_code: int = 302) -> RedirectResponse:
    """
    Redirect to a named endpoint (preferred) using url_for so it respects root_path (/portal).
    """
    url = request.url_for(to)
    return RedirectResponse(url=str(url), status_code=status_code)

def _redirect_with_query(to: str, *, request: Request, status_code: int = 302, **params) -> RedirectResponse:
    """
    Redirect to a named endpoint + query params, still root_path-safe.
    """
    base = str(request.url_for(to))
    if not params:
        return RedirectResponse(url=base, status_code=status_code)

    # simple query builder (avoid pulling in urllib unless you want it)
    from urllib.parse import urlencode
    return RedirectResponse(url=f"{base}?{urlencode(params)}", status_code=status_code)

def _require_user(request: Request, db: Session) -> Optional[User]:
    """
    Returns User if authenticated, else None.
    (Your get_current_user already does this, but keeping a wrapper makes callsites clearer.)
    """
    return get_current_user(request, db)

def _redirect_to_login(request: Request) -> RedirectResponse:
    return _redirect("login_page", request=request)

def _redirect_to_status(request: Request) -> RedirectResponse:
    return _redirect("status", request=request)


def _child_prefixes(keys: list[str], current_prefix: str) -> list[str]:
    out: set[str] = set()
    for key in keys:
        if not key.startswith(current_prefix):
            continue
        remainder = key[len(current_prefix) :]
        parts = remainder.split("/", 1)
        if len(parts) > 1 and parts[0]:
            out.add(f"{current_prefix}{parts[0]}/")
    return sorted(out)


def _parent_prefix(prefix: str) -> str:
    cleaned = prefix.strip("/")
    if not cleaned:
        return ""
    parts = cleaned.split("/")
    if len(parts) == 1:
        return ""
    return "/".join(parts[:-1]) + "/"


def _preview_kind(photo: Photo) -> str | None:
    ctype = (photo.content_type or "").lower()
    key = (photo.s3_key or "").lower()
    if ctype.startswith("image/") or key.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff")):
        return "image"
    if ctype.startswith("video/") or key.endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")):
        return "video"
    return None

# -------- routes --------

@router.get("/", response_class=HTMLResponse, name="home")
def home(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if not user:
        return _redirect_to_login(request)
    return _redirect_to_status(request)

@router.get("/login", response_class=HTMLResponse, name="login_page")
def login_page(request: Request, db: Session = Depends(get_db), error: str | None = None):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "app_name": settings.APP_NAME, "error": error},
    )

@router.post("/login", name="login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        # root_path-safe redirect with query
        return _redirect_with_query("login_page", request=request, error=1)

    token = make_session_token(user.username)
    resp = _redirect_to_status(request)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.AUTH_SESSION_TTL_SECONDS,
    )
    return resp

@router.post("/logout", name="logout")
def logout(request: Request):
    resp = _redirect_to_login(request)
    resp.delete_cookie(COOKIE_NAME)
    return resp

@router.get("/dashboard/status", response_class=HTMLResponse, name="status")
def status(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if not user:
        return _redirect_to_login(request)

    last_run = db.query(PublishRun).order_by(PublishRun.id.desc()).first()
    eff = get_effective_settings(db)
    count_active = db.query(Photo).filter(Photo.active == True).count()  # noqa

    return templates.TemplateResponse(
        "status.html",
        {
            "request": request,
            "user": user,
            "settings": eff,
            "last_run": last_run,
            "count_active": count_active,
            "manifest_key": settings.MANIFEST_KEY,
            "bucket": settings.S3_BUCKET,
        },
    )

@router.get("/upload", response_class=HTMLResponse, name="upload_page")
def upload_page(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if not user:
        return _redirect_to_login(request)
    return templates.TemplateResponse("upload.html", {"request": request, "user": user})

@router.get("/gallery", response_class=HTMLResponse, name="gallery")
def gallery(
    request: Request,
    db: Session = Depends(get_db),
    synced: int = 0,
    scanned: int = 0,
    sync_error: str | None = None,
    page: int = 1,
    prefix: str | None = None,
):
    user = _require_user(request, db)
    if not user:
        return _redirect_to_login(request)

    safe_page = max(1, page)
    default_prefix = settings.PHOTOS_PREFIX if settings.PHOTOS_PREFIX.endswith("/") else f"{settings.PHOTOS_PREFIX}/"
    current_prefix = (prefix or default_prefix).strip()
    if current_prefix and not current_prefix.endswith("/"):
        current_prefix += "/"

    base = db.query(Photo)
    if current_prefix:
        base = base.filter(Photo.s3_key.like(f"{current_prefix}%"))

    total = base.count()
    offset = (safe_page - 1) * GALLERY_PAGE_SIZE
    photos = (
        base.order_by(Photo.uploaded_at.desc())
        .offset(offset)
        .limit(GALLERY_PAGE_SIZE)
        .all()
    )
    photo_cards = []
    for p in photos:
        preview = None
        kind = _preview_kind(p)
        if kind:
            try:
                preview = s3_service.presign_get(p.s3_key, expires_seconds=1800)
            except Exception:
                preview = None
        photo_cards.append({"photo": p, "preview_url": preview, "preview_kind": kind})

    keys_for_prefixes = [k for (k,) in base.with_entities(Photo.s3_key).all()]
    folders = _child_prefixes(keys_for_prefixes, current_prefix=current_prefix)
    has_prev = safe_page > 1
    has_next = offset + len(photos) < total

    return templates.TemplateResponse(
        "gallery.html",
        {
            "request": request,
            "user": user,
            "photo_cards": photo_cards,
            "photos": photos,
            "synced": synced,
            "scanned": scanned,
            "sync_error": sync_error,
            "page": safe_page,
            "page_size": GALLERY_PAGE_SIZE,
            "total": total,
            "has_prev": has_prev,
            "has_next": has_next,
            "current_prefix": current_prefix,
            "parent_prefix": _parent_prefix(current_prefix),
            "folders": folders,
        },
    )


@router.post("/gallery/sync-s3", name="gallery_sync_s3")
def gallery_sync_s3(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if not user:
        return _redirect_to_login(request)

    eff = get_effective_settings(db)
    include, exclude = s3_service.get_gallery_prefixes(
        include_raw=eff.get("include_prefixes"),
        exclude_raw=eff.get("exclude_prefixes"),
    )
    existing_keys = {k for (k,) in db.query(Photo.s3_key).all()}
    scanned = 0
    synced = 0

    try:
        for obj in s3_service.list_media_objects(include, exclude):
            scanned += 1
            if obj.key in existing_keys:
                continue
            photo = Photo(
                s3_key=obj.key,
                original_filename=obj.key.rsplit("/", 1)[-1],
                etag=obj.etag,
                size_bytes=obj.size,
                content_type=None,
                uploaded_by="s3-sync",
                active=True,
            )
            db.add(photo)
            try:
                db.commit()
                existing_keys.add(obj.key)
                synced += 1
            except IntegrityError:
                db.rollback()
    except Exception as exc:  # noqa: BLE001
        return _redirect_with_query(
            "gallery",
            request=request,
            synced=synced,
            scanned=scanned,
            sync_error=f"s3_sync_failed:{exc}",
        )

    return _redirect_with_query("gallery", request=request, synced=synced, scanned=scanned)

@router.get("/dashboard/settings", response_class=HTMLResponse, name="settings_page")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if not user:
        return _redirect_to_login(request)

    eff = get_effective_settings(db)
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "settings": eff})

@router.post("/dashboard/settings", name="settings_save")
def settings_save(
    request: Request,
    limit: int = Form(...),
    shuffle_mode: str = Form(...),
    mode: str = Form(...),
    start_epoch: str = Form(...),
    include_prefixes: str = Form(""),
    exclude_prefixes: str = Form(""),
    default_upload_prefix: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    if not user:
        return _redirect_to_login(request)

    set_setting(db, "limit", str(limit))
    set_setting(db, "shuffle_mode", shuffle_mode)
    set_setting(db, "mode", mode)
    set_setting(db, "start_epoch", start_epoch)
    set_setting(db, "include_prefixes", include_prefixes.strip())
    set_setting(db, "exclude_prefixes", exclude_prefixes.strip())
    set_setting(db, "default_upload_prefix", default_upload_prefix.strip())

    # Redirect back to settings page root_path-safe
    return _redirect("settings_page", request=request)
