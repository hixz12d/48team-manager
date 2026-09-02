"""Phone pool, HME leases, and proxy profiles."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class HmeAliasLease(Base):
    __tablename__ = "hme_alias_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    anonymous_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(32))
    operation_id: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str | None] = mapped_column(String(40))
    workspace_id: Mapped[int | None] = mapped_column(Integer)
    local_state: Mapped[str] = mapped_column(String(20), default="reserved", nullable=False)
    label_desired: Mapped[str | None] = mapped_column(String(255))
    label_sync_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("idx_hme_lease_expires", "expires_at"),
        Index("idx_hme_lease_job", "job_id"),
        Index("idx_hme_lease_state", "local_state"),
        Index("idx_hme_lease_operation", "operation_id"),
    )


class PhonePool(Base):
    __tablename__ = "phone_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    sms_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_type: Mapped[str | None] = mapped_column(String(40))
    reserved_by: Mapped[str | None] = mapped_column(String(64))
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_id: Mapped[int | None] = mapped_column(Integer)
    risk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    no_sms_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("idx_phone_pool_status", "status"),
        Index("idx_phone_pool_reserved", "reserved_by"),
        Index("idx_phone_pool_heartbeat", "lease_heartbeat_at"),
    )


class PhoneAttempt(Base):
    __tablename__ = "phone_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phone_pool.id"), nullable=False)
    operation_id: Mapped[int | None] = mapped_column(Integer)
    operation_public_id: Mapped[str | None] = mapped_column(String(32))
    account_id: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(20), default="signup", nullable=False)
    result: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_phone_attempts_phone", "phone_id", "finished_at"),
        Index("idx_phone_attempts_operation", "operation_public_id"),
    )


class ProxyProfile(Base):
    __tablename__ = "proxy_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(120))
    name_source: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)
    scheme: Mapped[str] = mapped_column(String(20), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username_encrypted: Mapped[str | None] = mapped_column(Text)
    password_encrypted: Mapped[str | None] = mapped_column(Text)
    url_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    region: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    health_state: Mapped[str] = mapped_column(String(20), default="unchecked", nullable=False)
    last_exit_ip: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("idx_proxy_profiles_status", "status"),
        Index("idx_proxy_profiles_host", "host", "port"),
        Index("idx_proxy_profiles_health", "health_state"),
    )
