"""SQLite async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings
from app.persistence import models as _models  # noqa: F401


class Base(DeclarativeBase):
    pass


def sqlite_path_from_url(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    return Path(url.database)


def create_engine(settings: Settings):
    is_sqlite = settings.database_url.startswith("sqlite")
    if is_sqlite:
        engine_kwargs = dict(
            connect_args={"timeout": 60},
            pool_size=5,
            max_overflow=10,
            pool_recycle=3600,
            pool_pre_ping=True,
        )
    else:
        engine_kwargs = dict(
            pool_size=20,
            max_overflow=40,
            pool_recycle=3600,
            pool_pre_ping=True,
        )
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        future=True,
        **engine_kwargs,
    )


def create_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


async def get_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def init_db(engine) -> None:
    db_path = sqlite_path_from_url(str(engine.url))
    if db_path:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        if str(engine.url).startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
