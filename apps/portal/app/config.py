from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    APP_NAME: str = "Shared Photo Frame Portal"
    APP_SECRET_KEY: str = "change-this-secret"

    # Auth
    AUTH_SESSION_TTL_SECONDS: int = 86400

    # AWS / S3
    AWS_REGION: str = "us-east-1"
    S3_BUCKET: str = "trevor-shared-photo-stream"
    PHOTOS_PREFIX: str = "photos/"
    MANIFEST_KEY: str = "manifest.json"

    # Publisher script
    PUBLISHER_PATH: str = "tools/publish_manifest.py"

    # DB
    DATABASE_URL: str = "sqlite:///./data/portal.db"

    # Behavior
    AUTO_PUBLISH_ON_UPLOAD: bool = True
    AUTO_BUMP_ON_UPLOAD: bool = True
    DEFAULT_BUMP_EXPIRES_HOURS: int = 24
    DEFAULT_PIN_EXPIRES_HOURS: int = 6

    # Publisher defaults
    DEFAULT_LIMIT: int = 40
    DEFAULT_SHUFFLE_MODE: str = "daily"  # none|random|daily
    DEFAULT_MODE: str = "sync"           # inventory|sync
    DEFAULT_START_EPOCH: str = "now"     # now|fixed

settings = Settings()
