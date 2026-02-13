from __future__ import annotations
from datetime import datetime

from sqlalchemy import String, DateTime, func, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    s3_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)

    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(128), nullable=True)

    uploaded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    active: Mapped[bool] = mapped_column(Boolean, default=True)
