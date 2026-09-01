"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def session_dependency(factory: async_sessionmaker[AsyncSession]) -> Callable:
    async def _get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    return _get_db


def require_admin(request: Request) -> dict:
    user = request.session.get("user")
    if user and user.get("is_admin"):
        return user
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept.split(",")[0]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthenticated")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthenticated")


def html_login_redirect(request: Request, exc: HTTPException):
    accept = request.headers.get("accept", "")
    if exc.status_code in {401, 403} and "text/html" in accept:
        return RedirectResponse(url="/login", status_code=303)
    return None
