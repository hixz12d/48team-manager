"""Application configuration."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Process settings. Secrets never belong in templates or query payloads."""

    app_name: str = "48 Team Manager"
    app_version: str = __version__
    app_host: str = "0.0.0.0"
    app_port: int = 8008
    debug: bool = False

    # New schema lives in team48.db. Legacy production files stay at team_manage.db.
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR}/data/team48.db"

    secret_key: str = "your-secret-key-here-change-in-production"
    session_secret_key: str = ""
    encryption_key: str = ""
    admin_username: str = "hixz12"
    admin_password: str = "admin123"
    session_cookie_secure: bool = False

    log_level: str = "INFO"
    database_echo: bool = False
    timezone: str = "Asia/Shanghai"

    browser_headless: bool = False
    browser_channel: str = ""

    identity_gmail_policy: str = "owner_only"
    official_quota_probe_enabled: bool = True
    auto_reauth_enabled: bool = False
    auto_rotate_enabled: bool = False
    force_refill: bool = False

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def effective_session_secret_key(self) -> str:
        return self.session_secret_key or self.secret_key

    @property
    def effective_encryption_key(self) -> str:
        return self.encryption_key or self.secret_key


def load_settings() -> Settings:
    return Settings()
