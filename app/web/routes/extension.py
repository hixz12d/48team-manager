"""Narrow API for the personal signup extension.

Authenticated only by the EXTENSION_API_TOKEN bearer token; never by the admin session.
The token can list teams, run the sync -> link -> OAuth handoff for one joined member,
and complete that OAuth with the standard state/PKCE/email checks. Nothing else.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.web.schemas.workspaces import ExtensionHandoffCompleteRequest, ExtensionHandoffRequest

MIN_TOKEN_LENGTH = 24


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

    @router.post("/handoff")
    async def handoff(
        payload: ExtensionHandoffRequest,
        _: None = Depends(require_extension),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        from app.application.member_handoff import extension_handoff
        return await extension_handoff(
            db, email=payload.email, workspace_id=payload.workspace_id, sync_operation_id=payload.sync_operation_id,
        )

    @router.post("/handoff/complete")
    async def handoff_complete(
        payload: ExtensionHandoffCompleteRequest,
        _: None = Depends(require_extension),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        from app.application import console_actions
        result = await console_actions.account_reauth_complete(
            db,
            payload.account_id,
            ticket=payload.ticket,
            callback_url=payload.callback_url,
            workspace_id=payload.workspace_id,
            push_sub2api=payload.push_sub2api,
            count_switch=payload.count_switch,
        )
        if not result.get("ok"):
            return {"ok": False, "error_code": str(result.get("error_code") or "reauth_failed"),
                    "message": str(result.get("error") or result.get("message") or "授权失败")}
        return {"ok": True, "email": result.get("email"), "message": result.get("message"),
                "followups": result.get("followups") or {}}

    return router
