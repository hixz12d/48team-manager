"""Local Sub2API usage cache and proxy mappings."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Sub2ApiUsageSnapshot(Base):
    """One locally cached billing window for a verified binding."""

    __tablename__ = "sub2api_usage_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(
        ForeignKey("external_bindings.id", ondelete="CASCADE"), nullable=False
    )
    local_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id"))
    remote_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    window_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    window_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    standard_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    user_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    account_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    sync_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("binding_id", "window_kind", name="uq_sub2api_usage_binding_window"),
        Index("idx_sub2api_usage_account_workspace", "local_account_id", "workspace_id"),
        Index("idx_sub2api_usage_status_attempt", "sync_status", "last_attempt_at"),
    )


class Sub2ApiProxyBinding(Base):
    """Stable local ProxyProfile to remote Sub2API proxy mapping."""

    __tablename__ = "sub2api_proxy_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_proxy_profile_id: Mapped[int] = mapped_column(
        ForeignKey("proxy_profiles.id", ondelete="CASCADE"), nullable=False
    )
    remote_proxy_id: Mapped[str | None] = mapped_column(String(100))
    sync_state: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    last_pushed_hash: Mapped[str | None] = mapped_column(String(64))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("local_proxy_profile_id", name="uq_sub2api_proxy_local"),
        UniqueConstraint("remote_proxy_id", name="uq_sub2api_proxy_remote"),
        Index("idx_sub2api_proxy_sync_state", "sync_state"),
    )
