"""Internal API for codex-rs mailbox-OTP batch onboarding.

Authenticated exclusively by ``CODEX_RS_ONBOARD_TOKEN`` (Bearer, >= 24 chars). When unconfigured,
every endpoint returns 404. Error responses always use ``{"code", "message"}`` (never FastAPI's
default ``detail``).
"""
from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import codex_rs_onboard
from app.application.codex_rs_onboard import OnboardRequestError
from app.core.config import Settings

MIN_TOKEN_LENGTH = 24
MAX_BODY_BYTES = 64 * 1024
NO_STORE = {"Cache-Control": "no-store"}


def _error_response(status_code: int, code: str, message: str, *, extra: dict | None = None) -> JSONResponse:
    body = {"code": code, "message": message}
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status_code, content=body, headers=NO_STORE)


def _check_auth(request: Request, settings: Settings) -> JSONResponse | None:
    expected = str(settings.codex_rs_onboard_token or "")
    if len(expected) < MIN_TOKEN_LENGTH:
        return _error_response(404, "not_found", "未启用内部授权接口")
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), expected.encode()):
        return _error_response(401, "unauthorized", "内部接口密钥无效")
    return None


async def _read_json_object(request: Request, *, allow_empty: bool = False) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise OnboardRequestError(400, "invalid_request", "请求体过大", extra={"invalidEmails": []})
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise OnboardRequestError(400, "invalid_request", "请求体过大", extra={"invalidEmails": []})
    if not raw and allow_empty:
        return {}
    try:
        parsed = json.loads(bytes(raw))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OnboardRequestError(400, "invalid_request", "请求体不是合法的 JSON",
                                  extra={"invalidEmails": []}) from None
    if not isinstance(parsed, dict):
        raise OnboardRequestError(400, "invalid_request", "请求体必须是 JSON 对象", extra={"invalidEmails": []})
    return parsed


def build_codex_rs_onboard_router(get_db, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/internal/codex-rs/onboard", tags=["codex-rs-internal"])

    @router.get("")
    async def get_onboard_batch(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
        denied = _check_auth(request, settings)
        if denied is not None:
            return denied
        batch = await codex_rs_onboard.get_batch(db)
        return JSONResponse({"batch": batch}, headers=NO_STORE)

    @router.post("")
    async def start_onboard_batch(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
        denied = _check_auth(request, settings)
        if denied is not None:
            return denied
        try:
            payload = await _read_json_object(request)
            batch = await codex_rs_onboard.submit_batch(db, request.app.state.session_factory, payload)
        except OnboardRequestError as exc:
            return _error_response(exc.status_code, exc.code, exc.message, extra=exc.extra)
        return JSONResponse({"batch": batch}, headers=NO_STORE)

    @router.post("/stop")
    async def stop_onboard_batch(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
        denied = _check_auth(request, settings)
        if denied is not None:
            return denied
        try:
            await _read_json_object(request, allow_empty=True)
            batch = await codex_rs_onboard.stop_batch(db)
        except OnboardRequestError as exc:
            return _error_response(exc.status_code, exc.code, exc.message)
        return JSONResponse({"batch": batch}, headers=NO_STORE)

    @router.post("/retry")
    async def retry_onboard_item(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
        denied = _check_auth(request, settings)
        if denied is not None:
            return denied
        try:
            payload = await _read_json_object(request)
            batch = await codex_rs_onboard.retry_item(db, request.app.state.session_factory, payload)
        except OnboardRequestError as exc:
            return _error_response(exc.status_code, exc.code, exc.message)
        return JSONResponse({"batch": batch}, headers=NO_STORE)

    return router
