"""Console pages. One product, no v2/v3 aliases."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.web.deps import require_admin

PAGES = {
    "overview": {
        "title": "总览",
        "path": "/",
        "subtitle": "优先查看异常。工作区资产在左，待处理在右；没有异常时不占大卡片。",
    },
    "workspaces": {
        "title": "团队",
        "path": "/workspaces",
        "subtitle": "登记已有 ChatGPT Team 母号，查看席位、健康和最近同步。",
    },
    "accounts": {
        "title": "账号",
        "path": "/accounts",
        "subtitle": "默认按 Workspace 分组查看母号、当前子号和历史成员；可切回平铺排障。",
    },
    "operations": {
        "title": "任务",
        "path": "/operations",
        "subtitle": "查看持久化任务的当前步骤、结果和人工处理项。",
    },
    "phones": {
        "title": "手机号",
        "path": "/resources/phones",
        "subtitle": "导入号码池，查看余量、冷却、租约和尝试历史。",
    },
    "hme": {
        "title": "HME",
        "path": "/resources/hme",
        "subtitle": "查看别名占用、标签同步和任务租约。",
    },
    "proxies": {
        "title": "代理",
        "path": "/resources/proxies",
        "subtitle": "添加代理档案；登记母号时填的代理也会自动入库。",
    },
    "settings": {
        "title": "设置",
        "path": "/settings",
        "subtitle": "管理外部服务、自动化开关和资源策略。检测使用当前输入值，不会自动保存。",
    },
}
NAV = (
    ("overview", "总览"),
    ("workspaces", "团队"),
    ("accounts", "账号"),
    ("operations", "任务"),
    ("phones", "手机号"),
    ("hme", "HME"),
    ("proxies", "代理"),
    ("settings", "设置"),
)


def build_pages_router(templates: Jinja2Templates, version: str) -> APIRouter:
    router = APIRouter()

    def render(request: Request, page: str, user: dict):
        meta = PAGES[page]
        return templates.TemplateResponse(
            request,
            "console.html",
            {
                "request": request,
                "page": page,
                "title": meta["title"],
                "subtitle": meta["subtitle"],
                "user": user,
                "app_version": version,
                "pages": NAV,
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
        return render(request, "overview", user)

    @router.get("/workspaces", response_class=HTMLResponse)
    async def workspaces(request: Request, user: dict = Depends(require_admin)):
        return render(request, "workspaces", user)

    @router.get("/accounts", response_class=HTMLResponse)
    async def accounts(request: Request, user: dict = Depends(require_admin)):
        return render(request, "accounts", user)

    @router.get("/operations", response_class=HTMLResponse)
    async def operations(request: Request, user: dict = Depends(require_admin)):
        return render(request, "operations", user)

    @router.get("/resources/phones", response_class=HTMLResponse)
    async def phones(request: Request, user: dict = Depends(require_admin)):
        return render(request, "phones", user)

    @router.get("/resources/hme", response_class=HTMLResponse)
    async def hme(request: Request, user: dict = Depends(require_admin)):
        return render(request, "hme", user)

    @router.get("/resources/proxies", response_class=HTMLResponse)
    async def proxies(request: Request, user: dict = Depends(require_admin)):
        return render(request, "proxies", user)

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, user: dict = Depends(require_admin)):
        return render(request, "settings", user)

    return router
