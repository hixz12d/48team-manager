"""Official quota snapshots. Sub2API usage never drives automation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.database import Base


class QuotaSnapshot(Base):
    __tablename__ = "quota_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    five_hour_used_percent: Mapped[int | None] = mapped_column(Integer)
    five_hour_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seven_day_used_percent: Mapped[int | None] = mapped_column(Integer)
    seven_day_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(20), default="official", nullable=False)
    queried_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account: Mapped["Account"] = relationship("Account", back_populates="quota_snapshots")

    __table_args__ = (
        Index("idx_quota_snapshots_account_queried", "account_id", "queried_at"),
        Index("idx_quota_snapshots_success", "account_id", "success", "queried_at"),
    )
