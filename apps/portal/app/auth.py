from __future__ import annotations

import time
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.context import CryptContext
from fastapi import Request
from sqlalchemy.orm import Session

from .config import settings
from .models.user import User

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
serializer = URLSafeTimedSerializer(settings.APP_SECRET_KEY)

COOKIE_NAME = "portal_session"

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)

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
