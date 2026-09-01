"""ORM models. Domain tables land in later phases; settings boot the console."""

from app.persistence.models.settings import SystemSetting

__all__ = ["SystemSetting"]
