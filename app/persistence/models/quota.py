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
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id"))
    five_hour_used_percent: Mapped[int | None] = mapped_column(Integer)
    five_hour_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seven_day_used_percent: Mapped[int | None] = mapped_column(Integer)
    seven_day_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(20), default="official", nullable=False)
    queried_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    request_count: Mapped[int | None] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_source: Mapped[str | None] = mapped_column(String(30))
    credential_revision: Mapped[int | None] = mapped_column(Integer)
    check_id: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account: Mapped["Account"] = relationship("Account", back_populates="quota_snapshots")

    __table_args__ = (
        Index("idx_quota_snapshots_account_queried", "account_id", "queried_at"),
        Index("idx_quota_snapshots_success", "account_id", "success", "queried_at"),
        Index("idx_quota_snapshots_account_workspace_queried", "account_id", "workspace_id", "queried_at"),
    )


class QuotaProbeState(Base):
    """One durable schedule per context, including exactly one NULL workspace context."""

    __tablename__ = "quota_probe_states"

    context_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    credential_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_id: Mapped[str | None] = mapped_column(String(32))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (Index("idx_quota_probe_due", "enabled", "next_check_at"),)


class ProbeDispatchLease(Base):
    __tablename__ = "quota_dispatch_lease"

    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    token: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CredentialLease(Base):
    """Account-level refresh exclusion and expiry-based crash recovery."""

    __tablename__ = "credential_leases"

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    token: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
