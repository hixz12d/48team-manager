"""Loopback API for the extension runner (protocol: ``docs/contracts/extension-runner.md``).

Authenticated only by the per-run token that Team48 wrote into the run's
``private-config.mjs``; never by the admin session or ``EXTENSION_API_TOKEN``.
Unknown run, wrong token and finished run all answer 404 without saying which.
Mounting is done by ``app/main.py`` (part-4).
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import extension_runner as runner

NO_STORE = {"Cache-Control": "no-store"}


class RunnerEventRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    seq: int = Field(ge=1)
    status: Literal["running", "paused", "stopped", "done"]
    phase: Literal["signup", "oauth", "selfcheck"]
    stage: str = Field(default="unknown", max_length=40)
    pauseReason: str | None = Field(default=None, max_length=40)
    pauseFinal: bool = False
    message: str = Field(default="", max_length=400)
    codexResult: str = Field(default="unknown", max_length=40)
    diagnostics: dict[str, Any] | None = None


class RunnerCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    callbackUrl: str = Field(min_length=1, max_length=4096)


class RunnerProbeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(pattern=runner.PROBE_NAME_PATTERN)
    kind: Literal["signals", "exit", "screenshot"]
    data: Any = None


class RunnerPhoneRequest(BaseModel):
    """``docs/contracts/phone-relay.md``; ``session`` / ``email`` / ``workspaceId`` are ignored here."""
    model_config = ConfigDict(extra="ignore")

    action: Literal["acquire", "code", "report", "release"]
    phase: Literal["signup", "oauth"] = "signup"
    phoneId: int | None = Field(default=None, ge=0)
    bound: bool = False
    outcome: Literal["success", "invalid", "recently_used", "risk", "no_sms", "wrong_code", "cancelled"] | None = None

    @model_validator(mode="after")
    def _required_fields(self):
        if self.action in {"code", "report"} and self.phoneId is None:
            raise ValueError("phoneId is required")
        if self.action == "report" and self.outcome is None:
            raise ValueError("outcome is required")
        return self


PHONE_MAX_BYTES = 4 * 1024


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="not found", headers=NO_STORE)


def _token(request: Request) -> str:
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not supplied.strip():
        raise _not_found()
    return supplied.strip()


def _reply(body: dict[str, Any] | None) -> JSONResponse:
    if body is None:
        raise _not_found()
    return JSONResponse(body, headers=NO_STORE)


async def _read_json(request: Request, limit: int) -> Any:
    """Bounded body read; 413 over the limit, 422 when it is not JSON."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="payload too large", headers=NO_STORE)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="payload too large", headers=NO_STORE)
    try:
        return json.loads(bytes(body) or b"null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="invalid json", headers=NO_STORE) from None


def _parse(model: type[BaseModel], payload: Any) -> BaseModel:
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid payload", headers=NO_STORE) from None


def build_runner_router(get_db=None) -> APIRouter:
    """The run registry is in-process; ``get_db`` is only used by ``/phone`` (a fresh session per
    request, never the session ``signup_and_authorize`` is using)."""
    router = APIRouter(prefix="/api/ext/runner", tags=["extension-runner"])

    def _authorized(run_id: str, request: Request) -> str:
        token = _token(request)
        if not re.fullmatch(runner.RUN_ID_PATTERN, run_id or "") or not runner.runner_authorized(run_id, token):
            raise _not_found()
        return token

    @router.get("/{run_id}/job")
    async def job(run_id: str, request: Request) -> JSONResponse:
        token = _authorized(run_id, request)
        return _reply(runner.runner_job(run_id, token))

    @router.post("/{run_id}/event")
    async def event(run_id: str, request: Request) -> JSONResponse:
        token = _authorized(run_id, request)
        payload = _parse(RunnerEventRequest, await _read_json(request, runner.EVENT_MAX_BYTES))
        return _reply(runner.runner_event(run_id, token, payload.model_dump()))

    @router.post("/{run_id}/callback")
    async def callback(run_id: str, request: Request) -> JSONResponse:
        token = _authorized(run_id, request)
        payload = _parse(RunnerCallbackRequest, await _read_json(request, 8 * 1024))
        return _reply(runner.runner_callback(run_id, token, payload.callbackUrl))

    @router.post("/{run_id}/probe")
    async def probe(run_id: str, request: Request) -> JSONResponse:
        token = _authorized(run_id, request)
        # Screenshots are a data URL up to 4MB; allow the JSON envelope around it.
        payload = _parse(RunnerProbeRequest, await _read_json(request, runner.PROBE_SCREENSHOT_MAX_BYTES + 4096))
        if payload.kind == "screenshot":
            if not isinstance(payload.data, str):
                raise HTTPException(status_code=422, detail="invalid payload", headers=NO_STORE)
            if len(payload.data) > runner.PROBE_SCREENSHOT_MAX_BYTES:
                raise HTTPException(status_code=413, detail="payload too large", headers=NO_STORE)
        else:
            if not isinstance(payload.data, dict):
                raise HTTPException(status_code=422, detail="invalid payload", headers=NO_STORE)
            size = len(json.dumps(payload.data, ensure_ascii=False).encode("utf-8"))
            if size > runner.PROBE_JSON_MAX_BYTES:
                raise HTTPException(status_code=413, detail="payload too large", headers=NO_STORE)
        return _reply(runner.runner_probe(run_id, token, name=payload.name, kind=payload.kind, data=payload.data))

    if get_db is not None:
        @router.post("/{run_id}/phone")
        async def phone(run_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
            from app.application.resources import phone_relay

            token = _authorized(run_id, request)
            payload = _parse(RunnerPhoneRequest, await _read_json(request, PHONE_MAX_BYTES))
            context = runner.runner_phone_context(run_id, token)
            if context is None:
                raise _not_found()
            if not context.get("enabled"):
                return _reply({"ok": False, "error_code": "phone_relay_disabled",
                               "message": "本次运行未开放自动接码"})
            # The plugin's own phase is the freshest; the registry's phase only fills in when it is omitted.
            phase = payload.phase if "phase" in payload.model_fields_set else str(context.get("phase") or "signup")
            ctx = phone_relay.RelayContext(
                lease_key=str(context["lease_key"]),
                account_id=context.get("account_id"),
                proxy_url=str(context.get("proxy_url") or ""),
                phase=phase,
            )
            result = await phone_relay.handle(
                db, ctx, action=payload.action, phone_id=payload.phoneId,
                bound=payload.bound, outcome=payload.outcome,
            )
            return _reply(result)

    return router
