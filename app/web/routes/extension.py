"""Narrow API for the personal signup extension.

Authenticated only by the EXTENSION_API_TOKEN bearer token; never by the admin session.
The token can list teams, find which team an email is in, run the sync -> link -> OAuth handoff for one joined member,
complete that OAuth with the standard state/PKCE/email checks, and relay SMS codes from the phone pool
(docs/contracts/phone-relay.md). Nothing else.
"""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.web.schemas.workspaces import (
    ExtensionHandoffCompleteRequest,
    ExtensionHandoffRequest,
    ExtensionPhoneRequest,
    ExtensionResolveRequest,
)

MIN_TOKEN_LENGTH = 24
PHONE_MAX_BYTES = 4 * 1024
NO_STORE = {"Cache-Control": "no-store"}


async def _phone_payload(request: Request) -> ExtensionPhoneRequest:
    """Bounded body read for ``/phone``: 413 over 4KB, 422 when invalid."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > PHONE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="payload too large", headers=NO_STORE)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > PHONE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="payload too large", headers=NO_STORE)
    try:
        return ExtensionPhoneRequest.model_validate(json.loads(bytes(body) or b"null"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise HTTPException(status_code=422, detail="invalid payload", headers=NO_STORE) from None


def build_extension_router(get_db, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/ext", tags=["extension"])

    def require_extension(request: Request) -> None:
        expected = str(settings.extension_api_token or "")
        if len(expected) < MIN_TOKEN_LENGTH:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="extension api disabled")
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), expected.encode()):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid extension token")

    @router.get("/ping")
    async def ping(_: None = Depends(require_extension)) -> dict:
        return {"ok": True}

    @router.get("/workspaces")
    async def workspaces(_: None = Depends(require_extension), db: AsyncSession = Depends(get_db)) -> dict:
        from app.application.member_handoff import list_extension_workspaces
        return {"ok": True, "items": await list_extension_workspaces(db)}

    @router.post("/resolve")
    async def resolve(
        payload: ExtensionResolveRequest,
        _: None = Depends(require_extension),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        from app.application.member_handoff import resolve_extension_workspace
        return await resolve_extension_workspace(db, email=payload.email)

    @router.post("/handoff")
    async def handoff(
        payload: ExtensionHandoffRequest,
        _: None = Depends(require_extension),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        from app.application.member_handoff import extension_handoff
        return await extension_handoff(
            db, email=payload.email, workspace_id=payload.workspace_id, sync_operation_id=payload.sync_operation_id,
            phone_session=payload.phone_session,
        )

    @router.post("/phone")
    async def phone(
        request: Request,
        _: None = Depends(require_extension),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        from app.application.resources import phone_relay

        payload = await _phone_payload(request)
        ctx = await phone_relay.personal_context(
            db, session=payload.session, email=payload.email,
            workspace_id=payload.workspaceId, phase=payload.phase,
        )
        if isinstance(ctx, dict):
            return JSONResponse(ctx, headers=NO_STORE)
        result = await phone_relay.handle(
            db, ctx, action=payload.action, phone_id=payload.phoneId,
            bound=payload.bound, outcome=payload.outcome,
        )
        return JSONResponse(result, headers=NO_STORE)

    @router.post("/handoff/complete")
    async def handoff_complete(
        payload: ExtensionHandoffCompleteRequest,
        _: None = Depends(require_extension),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        from app.application import console_actions
        from app.application.settings import load_auth_push_target
        result = await console_actions.account_reauth_complete(
            db,
            payload.account_id,
            ticket=payload.ticket,
            callback_url=payload.callback_url,
            workspace_id=payload.workspace_id,
            push_sub2api=payload.push_sub2api,
            count_switch=payload.count_switch,
            push_target=await load_auth_push_target(db),
        )
        if not result.get("ok"):
            return {"ok": False, "error_code": str(result.get("error_code") or "reauth_failed"),
                    "message": str(result.get("error") or result.get("message") or "授权失败")}
        return {"ok": True, "email": result.get("email"), "message": result.get("message"),
                "followups": result.get("followups") or {}}

    return router
