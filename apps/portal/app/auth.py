from __future__ import annotations

import logging
import time
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.context import CryptContext
from passlib.exc import MissingBackendError, UnknownHashError
from passlib.hash import argon2
from fastapi import Request
from sqlalchemy.orm import Session

from .config import settings
from .models.user import User

logger = logging.getLogger(__name__)


def _build_pwd_context() -> CryptContext:
    """
    Prefer Argon2, but gracefully fall back to pbkdf2_sha256 if the argon2 backend
    is missing in the current runtime environment.
    """
    try:
        argon2.get_backend()
        return CryptContext(schemes=["argon2", "pbkdf2_sha256"], default="argon2", deprecated="auto")
    except MissingBackendError:
        logger.warning("Argon2 backend unavailable; falling back to pbkdf2_sha256 for password hashing.")
        return CryptContext(schemes=["pbkdf2_sha256"], default="pbkdf2_sha256", deprecated="auto")


pwd_context = _build_pwd_context()
serializer = URLSafeTimedSerializer(settings.APP_SECRET_KEY)

COOKIE_NAME = "portal_session"

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except (UnknownHashError, MissingBackendError):
        if password_hash.startswith("$argon2"):
            logger.error("Argon2 password hash detected but argon2 backend is unavailable in this runtime.")
        return False

def make_session_token(username: str) -> str:
    return serializer.dumps({"u": username})

def read_session_token(token: str, max_age_seconds: int) -> Optional[str]:
    try:
        data = serializer.loads(token, max_age=max_age_seconds)
        return data.get("u")
    except (BadSignature, SignatureExpired):
        return None

def get_current_user(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    username = read_session_token(token, settings.AUTH_SESSION_TTL_SECONDS)
    if not username:
        return None
    return db.query(User).filter(User.username == username).first()
