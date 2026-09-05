"""Persistent OAuth authorization sessions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class OAuthSession(Base):
    __tablename__ = "oauth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(32))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id"))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(String(100), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    authorize_url: Mapped[str] = mapped_column(Text, nullable=False)
    proxy_snapshot_encrypted: Mapped[str | None] = mapped_column(Text)
    proxy_source: Mapped[str | None] = mapped_column(String(20))
    sub2api_proxy_id: Mapped[int | None] = mapped_column(Integer)
    proxy_instance_key: Mapped[str | None] = mapped_column(String(64))
    credential_revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="waiting", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_oauth_sessions_status_expiry", "status", "expires_at"),
        Index("idx_oauth_sessions_account", "account_id", "created_at"),
    )
