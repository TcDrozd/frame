from __future__ import annotations
from datetime import datetime

from sqlalchemy import String, DateTime, func, Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

class PublishRun(Base):
    __tablename__ = "publish_runs"

    id: Mapped[int] = mapped_column(primary_key=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    manifest_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    manifest_etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manifest_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    settings_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
