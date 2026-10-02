"""Team revenue ledger: lifetime Sub2API user cost booked when a member leaves."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Sub2ApiRevenueEntry(Base):
    """One booked departure. No foreign keys: entries outlive teams and accounts."""

    __tablename__ = "sub2api_revenue_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_name: Mapped[str | None] = mapped_column(String(255))
    account_id: Mapped[int | None] = mapped_column(Integer)
    email: Mapped[str | None] = mapped_column(String(255))
    remote_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_cost: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False, default=Decimal("0"))
    # lifetime / lifetime_capped_90d / cache_lifetime / cache_seven_day / missing
    amount_source: Mapped[str] = mapped_column(String(30), nullable=False)
    window_days: Mapped[int | None] = mapped_column(Integer)
    # manual_rotation / auto_rotation / kick / purge / binding_cleanup
    departure_source: Mapped[str] = mapped_column(String(30), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64))
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # First booking time; never changed afterwards. Monthly totals use it.
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "remote_account_id", name="uq_sub2api_revenue_workspace_remote"),
        Index("idx_sub2api_revenue_settled", "settled_at"),
        Index("idx_sub2api_revenue_workspace", "workspace_id"),
    )


class Sub2ApiRevenueDaily(Base):
    """One remote account's cumulative user cost for one local day. No foreign keys; never deleted."""

    __tablename__ = "sub2api_revenue_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    remote_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # Local calendar day in settings.timezone (same boundary as Sub2API "today").
    day: Mapped[date] = mapped_column(Date, nullable=False)
    # Team / account at write time; kept as a snapshot.
    workspace_id: Mapped[int | None] = mapped_column(Integer)
    account_id: Mapped[int | None] = mapped_column(Integer)
    email: Mapped[str | None] = mapped_column(String(255))
    # Only grows within a day: writes keep the larger of old and new.
    user_cost: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False, default=Decimal("0"))
    # sync / settle / backfill
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("remote_account_id", "day", name="uq_sub2api_revenue_daily_remote_day"),
        Index("idx_sub2api_revenue_daily_day", "day"),
    )
