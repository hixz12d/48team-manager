"""Identity tables. Official plan, workspace role, and local purpose stay split."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event, func, inspect
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

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
    proxy_source: Mapped[str | None] = mapped_column(String(20))
    sub2api_proxy_id: Mapped[int | None] = mapped_column(Integer)
    proxy_instance_key: Mapped[str | None] = mapped_column(String(64))
    proxy_profile_id: Mapped[int | None] = mapped_column(ForeignKey("proxy_profiles.id"))
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    session_token_encrypted: Mapped[str | None] = mapped_column(Text)
    id_token_encrypted: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(String(100))
    password_encrypted: Mapped[str | None] = mapped_column(Text)
    mail_raw: Mapped[str | None] = mapped_column(Text)
    mailbox_provider: Mapped[str | None] = mapped_column(String(20))
    hme_account_id: Mapped[str | None] = mapped_column(String(100))
    mailbox_binding_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mailbox_read_state: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    mailbox_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mailbox_method: Mapped[str | None] = mapped_column(String(20))
    auto_reauth_opt_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credential_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(64))
    sms_url: Mapped[str | None] = mapped_column(String(500))
    next_reauth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reauth_fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reauth_code: Mapped[str | None] = mapped_column(String(40))
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_slot_minute: Mapped[int | None] = mapped_column(Integer)
    next_quota_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_probe_fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_team_id: Mapped[int | None] = mapped_column(Integer)
    source_child_account_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __mapper_args__ = {"version_id_col": credential_revision, "version_id_generator": False}

    owned_workspaces: Mapped[list[Workspace]] = relationship("Workspace", back_populates="owner_account")
    memberships: Mapped[list[WorkspaceMembership]] = relationship("WorkspaceMembership", back_populates="account")
    external_bindings: Mapped[list[ExternalBinding]] = relationship("ExternalBinding", back_populates="account")
    quota_snapshots: Mapped[list["QuotaSnapshot"]] = relationship("QuotaSnapshot", back_populates="account")
    proxy_profile: Mapped["ProxyProfile | None"] = relationship("ProxyProfile")

    __table_args__ = (
        Index("idx_accounts_purpose_state", "local_purpose", "operational_state"),
        Index("idx_accounts_source_team", "source_team_id"),
        Index("idx_accounts_source_child", "source_child_account_id"),
        Index("idx_accounts_next_quota_probe", "next_quota_probe_at"),
        Index("idx_accounts_next_reauth", "next_reauth_at"),
        Index("idx_accounts_auth_state", "auth_state"),
    )


@event.listens_for(Session, "before_flush")
def advance_credential_revision(session, flush_context, instances):
    # Covers OAuth, refresh, and direct import/upsert credential replacements.
    for account in session.dirty:
        if not isinstance(account, Account):
            continue
        state = inspect(account)
        if any(state.attrs[name].history.has_changes() for name in (
            "access_token_encrypted", "refresh_token_encrypted", "session_token_encrypted", "id_token_encrypted",
        )):
            if not state.attrs.credential_revision.history.has_changes():
                account.credential_revision = int(account.credential_revision or 1) + 1
            from app.core.time import utcnow
            account.next_quota_probe_at = utcnow()


class Workspace(Base):
    """A real Team / Workspace. official_workspace_id is UUID only."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    official_workspace_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(255))
    official_name: Mapped[str | None] = mapped_column(String(255))
    custom_name: Mapped[str | None] = mapped_column(String(255))
    name_source: Mapped[str] = mapped_column(String(20), default="placeholder", nullable=False)
    official_name_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    official_name_last_error: Mapped[str | None] = mapped_column(String(500))
    official_name_payload_source: Mapped[str | None] = mapped_column(String(80))
    subscription_plan: Mapped[str | None] = mapped_column(String(100))
    manual_expires_on: Mapped[date | None] = mapped_column(Date)
    manual_expiry_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    seat_limit: Mapped[int | None] = mapped_column(Integer)
    occupied_seats: Mapped[int | None] = mapped_column(Integer)
    last_official_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_official_sync_state: Mapped[str | None] = mapped_column(String(20))
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


class WorkspaceOfficialMemberSnapshot(Base):
    """Official members/invites snapshot. Remote-only rows stay here, not in Account."""

    __tablename__ = "workspace_official_member_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(255), nullable=False)
    official_user_id: Mapped[str | None] = mapped_column(String(100))
    official_role: Mapped[str] = mapped_column(String(40), default="unknown", nullable=False)
    remote_state: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    seat_type: Mapped[str | None] = mapped_column(String(40))
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "normalized_email", name="uq_workspace_official_member_email"),
        Index("idx_workspace_official_member_ws", "workspace_id", "remote_state"),
    )


class ExternalBinding(Base):
    """Explicit remote binding. One remote id maps to one local account × workspace context."""

    __tablename__ = "external_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    local_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    remote_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id"))
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
        UniqueConstraint("provider", "local_account_id", "workspace_id", name="uq_external_binding_local_workspace"),
        Index("idx_external_binding_state", "provider", "binding_state"),
        Index("idx_external_binding_workspace", "provider", "workspace_id"),
    )
