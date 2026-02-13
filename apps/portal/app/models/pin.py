from __future__ import annotations
from datetime import datetime

from sqlalchemy import String, DateTime, func, Integer
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

class Pin(Base):
    __tablename__ = "pins"

    id: Mapped[int] = mapped_column(primary_key=True)
    # store keys (simpler than FK for now)
    s3_key: Mapped[str] = mapped_column(String(512), index=True)

    # kind: pin_now | priority
    kind: Mapped[str] = mapped_column(String(32), index=True)

    weight: Mapped[int] = mapped_column(Integer, default=0)  # for priority ordering
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
