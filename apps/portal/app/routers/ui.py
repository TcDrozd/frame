from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import get_current_user, verify_password, make_session_token, COOKIE_NAME
from ..models.user import User
from ..models.photo import Photo
from ..models.publish_run import PublishRun
from ..services.settings_service import get_effective_settings, set_setting
from ..config import settings

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

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
def gallery(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if not user:
        return _redirect_to_login(request)

    photos = db.query(Photo).order_by(Photo.uploaded_at.desc()).limit(200).all()
    return templates.TemplateResponse("gallery.html", {"request": request, "user": user, "photos": photos})

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
