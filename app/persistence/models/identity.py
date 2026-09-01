"""Identity tables. Official plan, workspace role, and local purpose stay split."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.database import Base


class Account(Base):
    """One login identity. Plan never implies purpose."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    official_plan: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    official_user_id: Mapped[str | None] = mapped_column(String(100))
    official_account_id: Mapped[str | None] = mapped_column(String(100))
    auth_state: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    operational_state: Mapped[str] = mapped_column(String(20), default="available", nullable=False)
    local_purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    proxy: Mapped[str | None] = mapped_column(String(500))
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    session_token_encrypted: Mapped[str | None] = mapped_column(Text)
    id_token_encrypted: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(String(100))
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_team_id: Mapped[int | None] = mapped_column(Integer)
    source_child_account_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    owned_workspaces: Mapped[list[Workspace]] = relationship("Workspace", back_populates="owner_account")
    memberships: Mapped[list[WorkspaceMembership]] = relationship("WorkspaceMembership", back_populates="account")
    external_bindings: Mapped[list[ExternalBinding]] = relationship("ExternalBinding", back_populates="account")

    __table_args__ = (
        Index("idx_accounts_purpose_state", "local_purpose", "operational_state"),
        Index("idx_accounts_source_team", "source_team_id"),
        Index("idx_accounts_source_child", "source_child_account_id"),
    )


class Workspace(Base):
    """A real Team / Workspace. official_workspace_id is UUID only."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    official_workspace_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(255))
    subscription_plan: Mapped[str | None] = mapped_column(String(100))
    owner_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    seat_limit: Mapped[int | None] = mapped_column(Integer)
    last_official_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_team_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    owner_account: Mapped[Account | None] = relationship("Account", back_populates="owned_workspaces")
    memberships: Mapped[list[WorkspaceMembership]] = relationship("WorkspaceMembership", back_populates="workspace")

    __table_args__ = (
        Index("idx_workspaces_official_id", "official_workspace_id"),
        Index("idx_workspaces_owner", "owner_account_id"),
    )


class WorkspaceMembership(Base):
    """Account ↔ Workspace. Owner rows come from Workspace.owner, never from a mapping guess."""

    __tablename__ = "workspace_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    official_user_id: Mapped[str | None] = mapped_column(String(100))
    official_role: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    membership_state: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    local_purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_mapping_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    workspace: Mapped[Workspace] = relationship("Workspace", back_populates="memberships")
    account: Mapped[Account] = relationship("Account", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", name="uq_workspace_membership_account"),
        Index("idx_membership_workspace_state", "workspace_id", "membership_state"),
        Index("idx_membership_account", "account_id"),
    )


class ExternalBinding(Base):
    """Explicit remote binding. One remote id maps to one local account."""

    __tablename__ = "external_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    local_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    remote_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    binding_state: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    verified_email: Mapped[str | None] = mapped_column(String(255))
    verified_official_account_id: Mapped[str | None] = mapped_column(String(100))
    verified_workspace_id: Mapped[str | None] = mapped_column(String(100))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    account: Mapped[Account] = relationship("Account", back_populates="external_bindings")

    __table_args__ = (
        UniqueConstraint("provider", "remote_account_id", name="uq_external_binding_remote"),
        UniqueConstraint("provider", "local_account_id", name="uq_external_binding_local"),
        Index("idx_external_binding_state", "provider", "binding_state"),
    )
