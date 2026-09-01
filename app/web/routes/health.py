"""Health and empty query stubs for the skeleton."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.web.deps import require_admin


def build_api_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/overview")
    async def overview(_: dict = Depends(require_admin)) -> dict:
        return {
            "attention": [],
            "running_operations": [],
            "recent_events": [],
            "healthy": True,
        }

    @router.get("/workspaces")
    async def workspaces(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/accounts")
    async def accounts(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/operations")
    async def operations(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/resources/phones")
    async def phones(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/resources/hme")
    async def hme(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/resources/proxies")
    async def proxies(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/settings")
    async def settings_view(_: dict = Depends(require_admin)) -> dict:
        return {
            "connections": {"sub2api": {"configured": False}, "hme": {"configured": False}},
            "automation": {
                "official_quota_probe": True,
                "auto_reauth": False,
                "auto_rotate": False,
                "force_refill": False,
            },
            "secrets": {"sub2api_api_key": "••••••", "hme_token": "••••••"},
        }

    return router
