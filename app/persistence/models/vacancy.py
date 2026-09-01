"""Kick receipts. Evidence only; never a refill permit by themselves."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class SeatVacancyEvent(Base):
    __tablename__ = "seat_vacancy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(100))
    user_id: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    policy_notice_null: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    vacancy_ordinal: Mapped[int | None] = mapped_column(Integer)
    free_vacancy_threshold: Mapped[int | None] = mapped_column(Integer)
    billing_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_free: Mapped[bool | None] = mapped_column(Boolean)
    has_billing_notice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    policy_kind: Mapped[str | None] = mapped_column(String(80))
    billed_seat_delta: Mapped[int | None] = mapped_column(Integer)
    replacement_required: Mapped[bool | None] = mapped_column(Boolean)
    policy_notice_json: Mapped[str | None] = mapped_column(Text)
    billing_notice_json: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index("idx_vacancy_workspace_captured", "workspace_id", "captured_at"),)
