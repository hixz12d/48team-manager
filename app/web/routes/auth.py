"""Login, logout, and auth status."""

from __future__ import annotations

import time
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import verify_admin_login
from app.core.config import Settings
from app.web.schemas.auth import LoginRequest, LoginResponse

_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 10
_login_failures: dict[str, tuple[int, float]] = {}
_login_failures_lock = Lock()


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _check_login_rate_limit(ip: str) -> int | None:
    now = time.time()
    with _login_failures_lock:
        count, window_start = _login_failures.get(ip, (0, now))
        if now - window_start >= _LOGIN_WINDOW_SECONDS:
            _login_failures.pop(ip, None)
            return None
        if count >= _LOGIN_MAX_FAILURES:
            return int(_LOGIN_WINDOW_SECONDS - (now - window_start))
        return None


def _record_login_failure(ip: str) -> None:
    now = time.time()
    with _login_failures_lock:
        count, window_start = _login_failures.get(ip, (0, now))
        if now - window_start >= _LOGIN_WINDOW_SECONDS:
            count = 0
            window_start = now
        _login_failures[ip] = (count + 1, window_start)


def _clear_login_failures(ip: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(ip, None)


def build_auth_router(get_db, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/login", response_model=LoginResponse)
    async def login(
        request: Request,
        payload: LoginRequest,
        db: AsyncSession = Depends(get_db),
    ) -> LoginResponse:
        ip = _client_key(request)
        retry_after = _check_login_rate_limit(ip)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Try again later.",
                headers={"Retry-After": str(max(retry_after, 1))},
            )
        ok = await verify_admin_login(db, settings, payload.username, payload.password)
        if not ok:
            _record_login_failure(ip)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
        _clear_login_failures(ip)
        request.session["user"] = {"username": settings.admin_username, "is_admin": True}
        return LoginResponse(success=True, message="signed in")

    @router.post("/logout")
    async def logout(request: Request) -> dict:
        request.session.clear()
        return {"success": True}

    @router.get("/status")
    async def status_view(request: Request) -> dict:
        user = request.session.get("user")
        return {"authenticated": user is not None, "user": user}

    return router
