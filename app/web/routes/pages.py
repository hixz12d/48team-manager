"""Console pages. One product, no v2/v3 aliases."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.web.deps import require_admin

PAGES = (
    ("overview", "Overview", "/"),
    ("workspaces", "Workspaces", "/workspaces"),
    ("accounts", "Accounts", "/accounts"),
    ("operations", "Operations", "/operations"),
    ("phones", "Phones", "/resources/phones"),
    ("hme", "HME", "/resources/hme"),
    ("proxies", "Proxies", "/resources/proxies"),
    ("settings", "Settings", "/settings"),
)


def build_pages_router(templates: Jinja2Templates, version: str) -> APIRouter:
    router = APIRouter()

    def render(request: Request, page: str, title: str, user: dict):
        return templates.TemplateResponse(
            request,
            "console.html",
            {
                "request": request,
                "page": page,
                "title": title,
                "user": user,
                "app_version": version,
                "pages": PAGES,
            },
        )

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if request.session.get("user"):
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "app_version": version},
        )

    @router.get("/", response_class=HTMLResponse)
    async def overview(request: Request, user: dict = Depends(require_admin)):
        return render(request, "overview", "Overview", user)

    @router.get("/workspaces", response_class=HTMLResponse)
    async def workspaces(request: Request, user: dict = Depends(require_admin)):
        return render(request, "workspaces", "Workspaces", user)

    @router.get("/accounts", response_class=HTMLResponse)
    async def accounts(request: Request, user: dict = Depends(require_admin)):
        return render(request, "accounts", "Accounts", user)

    @router.get("/operations", response_class=HTMLResponse)
    async def operations(request: Request, user: dict = Depends(require_admin)):
        return render(request, "operations", "Operations", user)

    @router.get("/resources/phones", response_class=HTMLResponse)
    async def phones(request: Request, user: dict = Depends(require_admin)):
        return render(request, "phones", "Phones", user)

    @router.get("/resources/hme", response_class=HTMLResponse)
    async def hme(request: Request, user: dict = Depends(require_admin)):
        return render(request, "hme", "HME", user)

    @router.get("/resources/proxies", response_class=HTMLResponse)
    async def proxies(request: Request, user: dict = Depends(require_admin)):
        return render(request, "proxies", "Proxies", user)

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, user: dict = Depends(require_admin)):
        return render(request, "settings", "Settings", user)

    return router
