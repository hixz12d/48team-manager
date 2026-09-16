"""Account presence/runtime observations, separate from credential-sync receipts."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Sub2ApiAccountStatus(Base):
    __tablename__ = "sub2api_account_status"

    binding_id: Mapped[int] = mapped_column(ForeignKey("external_bindings.id", ondelete="CASCADE"), primary_key=True)
    remote_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    exists: Mapped[bool | None] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    schedulable: Mapped[bool | None] = mapped_column(Boolean)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
