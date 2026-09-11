"""Independent Codex push bindings and durable import intents."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class CodexBinding(Base):
    __tablename__ = "codex_bindings"

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    target_url: Mapped[str] = mapped_column(String(500), nullable=False)
    remote_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    remote_account_id: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    import_attempted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_revision: Mapped[int | None] = mapped_column(Integer)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
