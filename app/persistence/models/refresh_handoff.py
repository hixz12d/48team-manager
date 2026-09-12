"""Durable reverse handoff metadata. No plaintext or encrypted token payloads."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.persistence.database import Base


class Sub2ApiRefreshHandoff(Base):
    __tablename__ = "sub2api_refresh_handoffs"
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    authority_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    local_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    local_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
