"""Standby pool: locally registered accounts waiting to be pulled into a team."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class StandbyPoolEntry(Base):
    __tablename__ = "standby_pool_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hme_account_id: Mapped[str | None] = mapped_column(String(100))
    anonymous_id: Mapped[str | None] = mapped_column(String(100))
    original_label: Mapped[str | None] = mapped_column(String(200))
    # none / pool (tagged GPT号池) / team (team label applied) / kept (pre-existing label untouched) / failed
    label_state: Mapped[str] = mapped_column(String(20), default="none", server_default="none", nullable=False)
    # pending / joining / joined / failed / manual_required
    state: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    workspace_id: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[str | None] = mapped_column(String(20))
    seat_intent: Mapped[str | None] = mapped_column(String(30))
    operation_public_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(60))
    error: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("idx_standby_pool_state", "state"),
        Index("idx_standby_pool_workspace", "workspace_id"),
    )
