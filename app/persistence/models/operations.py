"""Persistent operations. Restart recovery uses DB leases, not process memory."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.database import Base


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    op_type: Mapped[str] = mapped_column("type", String(40), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    workspace_id: Mapped[int | None] = mapped_column(Integer)
    account_id: Mapped[int | None] = mapped_column(Integer)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(40))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    locked_by: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    input_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    log_json: Mapped[str | None] = mapped_column(Text)
    resolved_proxy: Mapped[str | None] = mapped_column(String(500))
    resolved_proxy_profile_id: Mapped[int | None] = mapped_column(Integer)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_reason: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    steps: Mapped[list[OperationStep]] = relationship(
        "OperationStep",
        back_populates="operation",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_operations_state_lease", "state", "lease_expires_at"),
        Index("idx_operations_email_created", "email", "created_at"),
        Index("idx_operations_type_state", "type", "state"),
        Index("idx_operations_archived_created", "archived_at", "created_at"),
        Index("idx_operations_workspace_created", "workspace_id", "created_at"),
        Index("idx_operations_archived_finished", "archived_at", "finished_at", "id"),
        Index(
            "uq_workspace_sync_active_key", "idempotency_key", unique=True,
            sqlite_where=(op_type == "workspace_sync") & state.in_(("queued", "running", "waiting")) & idempotency_key.is_not(None),
        ),
    )


class OperationStep(Base):
    __tablename__ = "operation_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("operations.id", ondelete="CASCADE"), nullable=False)
    step_name: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    input_snapshot: Mapped[str | None] = mapped_column(Text)
    result_snapshot: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)

    operation: Mapped[Operation] = relationship("Operation", back_populates="steps")

    __table_args__ = (
        UniqueConstraint("operation_id", "step_name", name="uq_operation_step_name"),
        Index("idx_operation_steps_op", "operation_id", "state"),
    )
