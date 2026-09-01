"""
管理员路由
处理管理员面板的所有页面和操作
"""
import asyncio
import json
import logging
import re
import zipfile
from io import BytesIO
from typing import Any, Optional, List, Dict, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.database import AsyncSessionLocal, get_db
from app.dependencies.auth import require_admin
from app.services.team import TeamService
from app.services.onboard import onboard_service
from app.services.chatgpt import chatgpt_service
from app.services.settings import (
    settings_service,
    DEFAULT_UI_THEME,
    DEFAULT_UI_STYLE,
)
from app.services.cliproxyapi import cliproxyapi_service
from app.services.sub2api import sub2api_service
from app.models import Team
from app.utils.time_utils import get_now
from app.utils.proxy import mask_proxy_url, normalize_proxy_url

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    prefix="/admin",
    tags=["admin"]
)

# 服务实例
team_service = TeamService()


async def resolve_ui_theme(db: AsyncSession) -> str:
    """获取当前系统 UI 主题。"""
    return settings_service.normalize_ui_theme(
        await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)
    )


async def resolve_ui_style(db: AsyncSession) -> str:
    """获取当前界面风格（cartoon / classic）。"""
    return settings_service.normalize_ui_style(
        await settings_service.get_setting(db, "ui_style", DEFAULT_UI_STYLE)
    )


async def resolve_admin_profile(db: AsyncSession) -> Dict[str, str]:
    """读取管理员个人资料（昵称 + 头像 data URL）。"""
    nickname = (await settings_service.get_setting(db, "admin_nickname", "") or "").strip()
    avatar = await settings_service.get_setting(db, "admin_avatar", "") or ""
    return {
        "nickname": nickname,
        "avatar": avatar,
    }


async def build_admin_base_context(
    request: Request,
    db: AsyncSession,
    current_user: dict,
    active_page: str,
) -> Dict[str, Any]:
    """构建后台页面通用模板上下文。"""
    return {
        "request": request,
        "user": current_user,
        "active_page": active_page,
        "ui_theme": await resolve_ui_theme(db),
        "ui_style": await resolve_ui_style(db),
        "admin_profile": await resolve_admin_profile(db),
    }


# 请求模型
class TeamImportRequest(BaseModel):
    """Team 导入请求"""
    import_type: str = Field(..., description="导入类型: single 或 batch")
    access_token: Optional[str] = Field(None, description="AT Token (单个导入)")
    id_token: Optional[str] = Field(None, description="ID Token (单个导入)")
    refresh_token: Optional[str] = Field(None, description="Refresh Token (单个导入)")
    session_token: Optional[str] = Field(None, description="Session Token (单个导入)")
    client_id: Optional[str] = Field(None, description="Client ID (单个导入)")
    email: Optional[str] = Field(None, description="邮箱 (单个导入)")
    account_id: Optional[str] = Field(None, description="Account ID (单个导入)")
    content: Optional[str] = Field(None, description="批量导入内容")
    pool_type: str = Field("normal", description="导入池类型")




class OAuthAuthorizeRequest(BaseModel):
    """生成 OAuth 授权链接请求"""
    client_id: str = Field("app_EMoamEEZ73f0CkXaXp7hrann", description="OAuth Client ID")
    redirect_uri: str = Field("http://localhost:1455/auth/callback", description="回调地址")
    scope: str = Field("openid email profile offline_access", description="OAuth scope")
    audience: Optional[str] = Field(None, description="audience（可选）")
    codex_cli_simplified_flow: bool = Field(True, description="是否启用 codex 简化流程")
    id_token_add_organizations: bool = Field(True, description="是否在 id_token 中附带组织信息")


class OAuthCallbackParseRequest(BaseModel):
    """OAuth 回调解析请求"""
    callback_text: str = Field(..., description="完整回调 URL 或回调文本")
    code_verifier: Optional[str] = Field(None, description="PKCE code_verifier")
    expected_state: Optional[str] = Field(None, description="期望的 state 值")
    client_id: Optional[str] = Field("app_EMoamEEZ73f0CkXaXp7hrann", description="兜底 client_id")
    redirect_uri: str = Field("http://localhost:1455/auth/callback", description="回调地址")

class AddMemberRequest(BaseModel):
    """单邮箱成员请求"""
    email: str = Field(..., description="成员邮箱")


class DeleteMemberRequest(BaseModel):
    """删除成员请求"""
    email: Optional[str] = Field(None, description="成员邮箱")


class AddMembersRequest(BaseModel):
    """批量添加成员请求"""
    emails: List[str] = Field(..., description="成员邮箱列表")


class TeamUpdateRequest(BaseModel):
    """Team 更新请求"""
    email: Optional[str] = Field(None, description="新邮箱")
    account_id: Optional[str] = Field(None, description="新 Account ID")
    access_token: Optional[str] = Field(None, description="新 Access Token")
    id_token: Optional[str] = Field(None, description="新 ID Token")
    refresh_token: Optional[str] = Field(None, description="新 Refresh Token")
    session_token: Optional[str] = Field(None, description="新 Session Token")
    client_id: Optional[str] = Field(None, description="新 Client ID")
    max_members: Optional[int] = Field(None, description="最大成员数")
    team_name: Optional[str] = Field(None, description="Team 名称")
    status: Optional[str] = Field(None, description="状态: active/full/expired/error/banned")
    proxy: Optional[str] = Field(None, description="母号专属静态 ISP")
    seat_cycle_days: Optional[int] = Field(None, description="子号轮转天数")


class BulkActionRequest(BaseModel):
    """批量操作请求"""
    ids: List[int] = Field(..., description="Team ID 列表")


class BatchRefreshRequest(BaseModel):
    """批量刷新请求"""
    ids: List[int] = Field(default_factory=list, description="Team ID 列表")
    all_in_pool: bool = Field(False, description="是否刷新当前池全部 Team")
    pool_type: Optional[Literal["normal"]] = Field(None, description="池类型")


async def _team_list_payload(
    db: AsyncSession,
    *,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    pool_type: str = "normal",
) -> Dict[str, Any]:
    teams_result = await team_service.get_all_teams(
        db,
        page=page,
        per_page=per_page,
        search=search,
        status=status_filter,
        pool_type=pool_type,
    )
    if not teams_result.get("success"):
        return {
            "success": False,
            "error": teams_result.get("error") or "获取 Team 列表失败",
            "teams": [],
            "stats": {},
            "pagination": {"current_page": page, "total_pages": 1, "total": 0, "per_page": per_page},
        }
    team_stats = await team_service.get_stats(db, pool_type=pool_type)
    stats: Dict[str, Any] = {
        "total_teams": team_stats["total"],
        "available_teams": team_stats["available"],
        "live_teams": team_stats["live"],
        "banned_teams": team_stats["banned"],
        "expired_teams": team_stats["expired"],
    }
    return {
        "success": True,
        "teams": teams_result.get("teams", []),
        "stats": stats,
        "search": search or "",
        "status_filter": status_filter or "",
        "pool_type": pool_type,
        "pagination": {
            "current_page": teams_result.get("current_page", page),
            "total_pages": teams_result.get("total_pages", 1),
            "total": teams_result.get("total", 0),
            "per_page": per_page,
        },
    }

@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    legacy_status: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    管理员面板首页
    """
    try:
        from app.main import templates
        if status_filter is None and legacy_status is not None:
            status_filter = legacy_status

        logger.info(f"管理员访问控制台, search={search}, page={page}, per_page={per_page}, status_filter={status_filter}")

        # 设置每页数量
        # per_page = 20 (Removed hardcoded value)
        
        # 获取 Team 列表 (分页)
        teams_result = await team_service.get_all_teams(db, page=page, per_page=per_page, search=search, status=status_filter, pool_type="normal")
        
        # 获取统计信息 (使用专用统计方法优化)
        team_stats = await team_service.get_stats(db, pool_type="normal")

        # 计算统计数据
        stats = {
            "total_teams": team_stats["total"],
            "available_teams": team_stats["available"],
            "live_teams": team_stats["live"],
            "banned_teams": team_stats["banned"],
            "expired_teams": team_stats["expired"],
        }

        context = await build_admin_base_context(request, db, current_user, "dashboard")
        context.update({
            "teams": teams_result.get("teams", []),
            "stats": stats,
            "search": search,
            "status_filter": status_filter,
            "pagination": {
                "current_page": teams_result.get("current_page", page),
                "total_pages": teams_result.get("total_pages", 1),
                "total": teams_result.get("total", 0),
                "per_page": per_page
            }
        })
        return templates.TemplateResponse(
            request,
            "admin/index.html",
            context,
        )
    except Exception as e:
        logger.exception("加载管理员面板失败")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="加载管理员面板失败，请稍后重试"
        )




@router.get("/teams/list")
async def teams_list(
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    legacy_status: Optional[str] = Query(None, alias="status"),
    pool_type: str = "normal",
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    if status_filter is None and legacy_status is not None:
        status_filter = legacy_status
    normalized_pool = "normal"
    payload = await _team_list_payload(
        db,
        page=page,
        per_page=per_page,
        search=search,
        status_filter=status_filter,
        pool_type=normalized_pool,
    )
    status_code = status.HTTP_200_OK if payload.get("success") else status.HTTP_400_BAD_REQUEST
    return JSONResponse(status_code=status_code, content=payload)


@router.post("/teams/{team_id}/delete")
async def delete_team(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    删除 Team

    Args:
        team_id: Team ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        删除结果
    """
    try:
        logger.info(f"管理员删除 Team: {team_id}")

        result = await team_service.delete_team(team_id, db)

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("删除 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "删除 Team 失败，请稍后重试"
            }
        )


@router.get("/teams/{team_id}/info")
async def get_team_info(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """获取 Team 详情 (包含解密后的 Token)"""
    try:
        result = await team_service.get_team_by_id(team_id, db)
        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=result
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/{team_id}/update")
async def update_team(
    team_id: int,
    update_data: TeamUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 Team 信息"""
    try:
        result = await team_service.update_team(
            team_id=team_id,
            db_session=db,
            email=update_data.email,
            account_id=update_data.account_id,
            access_token=update_data.access_token,
            id_token=update_data.id_token,
            refresh_token=update_data.refresh_token,
            session_token=update_data.session_token,
            client_id=update_data.client_id,
            max_members=update_data.max_members,
            team_name=update_data.team_name,
            status=update_data.status,
            proxy=update_data.proxy,
            seat_cycle_days=update_data.seat_cycle_days,
        )
        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/import")
async def team_import(
    import_data: TeamImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    处理 Team 导入

    Args:
        import_data: 导入数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        导入结果
    """
    try:
        pool_type = "normal"
        logger.info(f"管理员导入 Team: {import_data.import_type}, pool={pool_type}")

        if import_data.import_type == "single":
            # 单个导入 - 允许通过 AT, RT 或 ST 导入
            if not any([import_data.access_token, import_data.refresh_token, import_data.session_token]):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "error": "必须提供 Access Token、Refresh Token 或 Session Token 其中之一"
                    }
                )

            result = await team_service.import_team_single(
                access_token=import_data.access_token,
                db_session=db,
                email=import_data.email,
                account_id=import_data.account_id,
                id_token=import_data.id_token,
                refresh_token=import_data.refresh_token,
                session_token=import_data.session_token,
                client_id=import_data.client_id,
                pool_type=pool_type
            )

            if not result["success"]:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=result
                )

            return JSONResponse(content=result)

        elif import_data.import_type == "batch":
            # 批量导入使用 StreamingResponse
            async def progress_generator():
                async for status_item in team_service.import_team_batch(
                    text=import_data.content,
                    db_session=db,
                    pool_type=pool_type
                ):
                    yield json.dumps(status_item, ensure_ascii=False) + "\n"

            return StreamingResponse(
                progress_generator(),
                media_type="application/x-ndjson"
            )

        elif import_data.import_type == "json":
            async def progress_generator():
                async for status_item in team_service.import_team_json(
                    json_text=import_data.content,
                    db_session=db,
                    pool_type=pool_type
                ):
                    yield json.dumps(status_item, ensure_ascii=False) + "\n"

            return StreamingResponse(
                progress_generator(),
                media_type="application/x-ndjson"
            )

        else:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "error": "无效的导入类型"
                }
            )

    except Exception as e:
        logger.exception("导入 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "导入失败，请稍后重试"
            }
        )



@router.post("/oauth/openai/authorize")
async def create_openai_oauth_authorize_url(
    payload: OAuthAuthorizeRequest,
    current_user: dict = Depends(require_admin)
):
    """生成 OpenAI OAuth 授权链接。"""
    try:
        client_id = (payload.client_id or "").strip()
        if not client_id:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"success": False, "error": "client_id 不能为空"})

        auth_data = chatgpt_service.create_oauth_authorize_url(
            client_id=client_id,
            redirect_uri=payload.redirect_uri.strip(),
            scope=payload.scope.strip() or "openid email profile offline_access",
            audience=(payload.audience.strip() if payload.audience else None),
            codex_cli_simplified_flow=payload.codex_cli_simplified_flow,
            id_token_add_organizations=payload.id_token_add_organizations,
        )

        return JSONResponse(content={"success": True, "data": {
            "authorize_url": auth_data["authorize_url"],
            "code_verifier": auth_data["code_verifier"],
            "state": auth_data["state"],
            "client_id": client_id
        }})
    except Exception as e:
        logger.exception("生成 OAuth 授权链接失败")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"success": False, "error": "操作失败，请稍后重试"})


@router.post("/oauth/openai/parse-callback")
async def parse_openai_oauth_callback(
    payload: OAuthCallbackParseRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """解析 OAuth 回调内容并提取 token。"""
    from urllib.parse import parse_qs, urlparse

    try:
        text = (payload.callback_text or "").strip()
        if not text:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"success": False, "error": "回调内容不能为空"})

        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        fragment = parse_qs(parsed.fragment)

        merged: Dict[str, str] = {}
        for source in (query, fragment):
            for k, v in source.items():
                if v:
                    merged[k] = v[0]

        # 兼容非标准粘贴内容（如日志文本/JSON片段）
        if not merged:
            pairs = re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)=([^\s&]+)', text)
            for k, v in pairs:
                if k not in merged:
                    merged[k] = v

        # 兼容直接粘贴 JSON 的场景
        if "{" in text and "}" in text:
            try:
                json_candidate = json.loads(text)
                if isinstance(json_candidate, dict):
                    for key in ("access_token", "refresh_token", "id_token", "client_id", "account_id", "email", "expired", "last_refresh", "type"):
                        value = json_candidate.get(key)
                        if value and key not in merged:
                            merged[key] = str(value)
            except Exception:
                pass

        # 兜底直接提取 token/client_id
        if not merged.get("access_token"):
            m = re.search(r'(eyJ[a-zA-Z0-9_\-.]+\.[a-zA-Z0-9_\-.]+\.[a-zA-Z0-9_\-.]+)', text)
            if m:
                merged["access_token"] = m.group(1)
        if not merged.get("id_token"):
            token_matches = re.findall(r'(eyJ[a-zA-Z0-9_\-.]+\.[a-zA-Z0-9_\-.]+\.[a-zA-Z0-9_\-.]+)', text)
            if len(token_matches) >= 2:
                merged["id_token"] = token_matches[1]
        if not merged.get("refresh_token"):
            m = re.search(r'(rt[_-][A-Za-z0-9._-]+)', text)
            if m:
                merged["refresh_token"] = m.group(1)
        if not merged.get("client_id"):
            m = re.search(r'(app_[A-Za-z0-9]+)', text)
            if m:
                merged["client_id"] = m.group(1)

        if payload.expected_state and merged.get("state") and merged.get("state") != payload.expected_state:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"success": False, "error": "state 不匹配，请重新生成授权链接"})

        access_token = merged.get("access_token")
        refresh_token = merged.get("refresh_token")
        id_token = merged.get("id_token")
        client_id = merged.get("client_id") or payload.client_id

        # 如果回调中只有 code，尝试自动换取 AT/RT
        code = merged.get("code")
        if code and not access_token:
            if not payload.code_verifier:
                return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={
                    "success": False,
                    "error": "回调中是 code 流程，需要 code_verifier 才能兑换 token"
                })
            if not client_id:
                return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={
                    "success": False,
                    "error": "缺少 client_id，无法兑换 token"
                })

            exchange = await chatgpt_service.exchange_oauth_code(
                code=code,
                client_id=client_id,
                redirect_uri=payload.redirect_uri.strip(),
                code_verifier=payload.code_verifier.strip(),
                db_session=db,
                identifier=f"oauth_{current_user.get('username', 'admin')}"
            )
            if not exchange.get("success"):
                return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=exchange)

            access_token = exchange.get("access_token")
            refresh_token = exchange.get("refresh_token")
            id_token = exchange.get("id_token")
            if id_token:
                merged["id_token"] = id_token

        if not access_token and not refresh_token:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={
                "success": False,
                "error": "未在回调内容中解析到 access_token/refresh_token 或可兑换的 code"
            })

        return JSONResponse(content={
            "success": True,
            "data": {
                "access_token": access_token or "",
                "refresh_token": refresh_token or "",
                "id_token": id_token or "",
                "client_id": client_id or "",
                "raw": merged
            }
        })
    except Exception as e:
        logger.exception("解析 OAuth 回调失败")
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"success": False, "error": "操作失败，请稍后重试"})


@router.get("/teams/{team_id}/members/list")
async def team_members_list(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    获取 Team 成员列表 (JSON)

    Args:
        team_id: Team ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        成员列表 JSON
    """
    try:
        # 获取成员列表
        result = await team_service.get_team_members(team_id, db)
        return JSONResponse(content=result)
    except Exception as e:
        logger.exception("获取成员列表失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "获取成员列表失败，请稍后重试"
            }
        )


@router.post("/teams/{team_id}/members/add")
async def add_team_member(
    team_id: int,
    member_data: AddMembersRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    批量添加 Team 成员

    Args:
        team_id: Team ID
        member_data: 成员数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        添加结果
    """
    try:
        logger.info(f"管理员批量添加成员到 Team {team_id}: {member_data.emails}")

        result = await team_service.add_team_members(
            team_id=team_id,
            emails=member_data.emails,
            db_session=db
        )

        if not result.get("processed") and not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception:
        logger.exception("添加成员失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "添加成员失败，请稍后重试"
            }
        )


@router.post("/teams/{team_id}/members/{user_id}/delete")
async def delete_team_member(
    team_id: int,
    user_id: str,
    payload: DeleteMemberRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    删除 Team 成员

    Args:
        team_id: Team ID
        user_id: 用户 ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        删除结果
    """
    try:
        logger.info(f"管理员从 Team {team_id} 删除成员: {user_id}")

        if (payload.email or "").strip():
            result = await onboard_service.kick_to_standby(
                db,
                team_id=team_id,
                email=payload.email,
                user_id=user_id,
            )
        else:
            result = await team_service.delete_team_member(
                team_id=team_id,
                user_id=user_id,
                db_session=db,
                email=payload.email,
            )

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("删除成员失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "删除成员失败，请稍后重试"
            }
        )


@router.post("/teams/{team_id}/invites/revoke")
async def revoke_team_invite(
    team_id: int,
    member_data: AddMemberRequest, # 使用相同的包含 email 的模型
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    撤回 Team 邀请

    Args:
        team_id: Team ID
        member_data: 成员数据 (包含 email)
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        撤回结果
    """
    try:
        logger.info(f"管理员从 Team {team_id} 撤回邀请: {member_data.email}")

        result = await team_service.revoke_team_invite(
            team_id=team_id,
            email=member_data.email,
            db_session=db
        )

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("撤回邀请失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "撤回邀请失败，请稍后重试"
            }
        )


@router.post("/teams/{team_id}/enable-device-auth")
async def enable_team_device_auth(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    开启 Team 的设备代码身份验证

    Args:
        team_id: Team ID
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        结果
    """
    try:
        logger.info(f"管理员开启 Team {team_id} 的设备身份验证")

        result = await team_service.enable_device_code_auth(
            team_id=team_id,
            db_session=db
        )

        if not result["success"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        return JSONResponse(content=result)

    except Exception as e:
        logger.exception("开启设备身份验证失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": "操作失败，请稍后重试"
            }
        )


@router.get("/teams/{team_id}/export-json")
async def export_team_json(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """导出单个 Team 的 JSON 认证文件。"""
    try:
        logger.info("管理员导出 Team %s 的 JSON 认证文件", team_id)
        result = await cliproxyapi_service.get_team_auth_file_data(team_id, db)
        if not result.get("success"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=result
            )

        payload_bytes = json.dumps(
            result.get("payload") or {},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        filename = str(result.get("filename") or f"team-{team_id}.json")
        quoted_filename = json.dumps(filename, ensure_ascii=False)
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}; filename={quoted_filename}"
        }
        return Response(content=payload_bytes, media_type="application/json; charset=utf-8", headers=headers)
    except Exception as e:
        logger.error("导出 Team %s 的 JSON 认证文件失败: %s", team_id, e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
        )


@router.post("/teams/batch-export-json")
async def batch_export_team_json(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """批量导出 Team 的 JSON 认证文件并打包为 zip。"""
    try:
        team_ids = [team_id for team_id in action_data.ids if isinstance(team_id, int)]
        if not team_ids:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "请选择要导出的 Team"}
            )

        logger.info("管理员批量导出 %s 个 Team 的 JSON 认证文件", len(team_ids))

        zip_buffer = BytesIO()
        exported_count = 0
        failed_count = 0
        warning_count = 0
        results = []

        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for team_id in team_ids:
                result = await cliproxyapi_service.get_team_auth_file_data(team_id, db)
                if not result.get("success"):
                    failed_count += 1
                    results.append({
                        "team_id": team_id,
                        "email": result.get("email"),
                        "filename": None,
                        "warning": None,
                        "warnings": [],
                        "error": result.get("error"),
                    })
                    continue

                filename = str(result.get("filename") or f"team-{team_id}.json")
                payload_text = json.dumps(result.get("payload") or {}, ensure_ascii=False, indent=2)
                zip_file.writestr(filename, payload_text)

                exported_count += 1
                if result.get("warning"):
                    warning_count += 1
                results.append({
                    "team_id": team_id,
                    "email": result.get("email"),
                    "filename": filename,
                    "warning": result.get("warning"),
                    "warnings": result.get("warnings") or [],
                    "error": None,
                })

            if failed_count > 0:
                summary_payload = {
                    "success": failed_count == 0,
                    "message": f"批量导出完成：成功 {exported_count}，失败 {failed_count}",
                    "exported_count": exported_count,
                    "failed_count": failed_count,
                    "warning_count": warning_count,
                    "results": results,
                }
                zip_file.writestr("export-summary.json", json.dumps(summary_payload, ensure_ascii=False, indent=2))

        if exported_count == 0:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "error": "选中的 Team 都无法导出 JSON",
                    "failed_count": failed_count,
                    "results": results,
                }
            )

        zip_buffer.seek(0)
        archive_name = f"teams-json-export-{get_now().strftime('%Y%m%d%H%M%S')}.zip"
        quoted_archive_name = json.dumps(archive_name, ensure_ascii=False)
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{archive_name}; filename={quoted_archive_name}",
            "X-Exported-Count": str(exported_count),
            "X-Failed-Count": str(failed_count),
            "X-Warning-Count": str(warning_count),
        }
        return Response(content=zip_buffer.getvalue(), media_type="application/zip", headers=headers)
    except Exception as e:
        logger.error("批量导出 Team JSON 失败: %s", e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
        )


async def _push_team_to_sub2api(team_id: int, db: AsyncSession) -> Dict[str, Any]:
    team = await db.get(Team, team_id)
    if not team:
        return {"success": False, "error": "Team 不存在", "team_id": team_id}
    try:
        result = await sub2api_service.push_team(db, team)
        result["team_id"] = team_id
        return result
    except Exception as exc:
        logger.exception("推送 Team %s 到 Sub2API 失败", team_id)
        return {"success": False, "error": str(exc), "email": team.email, "team_id": team_id}


@router.post("/teams/{team_id}/push-sub2api")
@router.post("/teams/{team_id}/push-cliproxyapi")
async def push_team_to_sub2api(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """将单个 Team 的会话推送到 Sub2API。"""
    logger.info("管理员推送 Team %s 到 Sub2API", team_id)
    result = await _push_team_to_sub2api(team_id, db)
    if not result.get("success"):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=result
        )
    return JSONResponse(content=result)


# ==================== 批量操作路由 ====================

@router.post("/teams/batch-push-sub2api")
@router.post("/teams/batch-push-cliproxyapi")
async def batch_push_teams_to_sub2api(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """批量推送 Team 会话到 Sub2API。"""
    try:
        logger.info("管理员批量推送 %s 个 Team 到 Sub2API", len(action_data.ids))

        uploaded_count = 0
        updated_count = 0
        skipped_count = 0
        warning_count = 0
        failed_count = 0
        results = []

        for team_id in action_data.ids:
            result = await _push_team_to_sub2api(team_id, db)
            action = result.get("action")
            warning = result.get("warning")

            if result.get("success"):
                if action == "uploaded":
                    uploaded_count += 1
                elif action == "updated":
                    updated_count += 1
                elif action == "skipped":
                    skipped_count += 1
                if warning:
                    warning_count += 1

                results.append(
                    {
                        "team_id": team_id,
                        "email": result.get("email"),
                        "filename": result.get("filename"),
                        "action": action,
                        "warning": warning,
                        "warnings": result.get("warnings") or [],
                        "error": None,
                    }
                )
                continue

            failed_count += 1
            results.append(
                {
                    "team_id": team_id,
                    "email": result.get("email"),
                    "filename": result.get("filename"),
                    "action": None,
                    "warning": None,
                    "warnings": [],
                    "error": result.get("error"),
                }
            )

        message = (
            "批量推送完成: "
            f"新增 {uploaded_count}, 更新 {updated_count}, 跳过 {skipped_count}, 失败 {failed_count}"
        )
        if warning_count:
            message += f"，其中 {warning_count} 个 Team 缺少 id_token 或 refresh_token"

        return JSONResponse(content={
            "success": True,
            "message": message,
            "uploaded_count": uploaded_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "warning_count": warning_count,
            "failed_count": failed_count,
            "results": results,
        })
    except Exception as e:
        logger.error("批量推送 Team 到 Sub2API 失败: %s", e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": str(e)}
        )


@router.post("/teams/batch-refresh")
async def batch_refresh_teams(
    action_data: BatchRefreshRequest,
    current_user: dict = Depends(require_admin)
):
    """批量刷新 Team 信息，并以流式方式返回进度。"""
    try:
        team_ids = [team_id for team_id in action_data.ids if isinstance(team_id, int)]

        if action_data.all_in_pool and team_ids:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "请勿同时提交 Team 列表和整池检测参数"}
            )

        if not action_data.all_in_pool and action_data.pool_type:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "仅整池检测时允许指定 Team 池"}
            )

        if action_data.all_in_pool:
            if not action_data.pool_type:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"success": False, "error": "请选择要检测的 Team 池"}
                )

            stmt = select(Team.id).where(Team.pool_type == action_data.pool_type).order_by(Team.created_at.desc())
            async with AsyncSessionLocal() as db_session:
                result = await db_session.execute(stmt)
                team_ids = [team_id for team_id in result.scalars().all() if isinstance(team_id, int)]

        if not team_ids:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "error": "当前池没有可检测的 Team" if action_data.all_in_pool else "请选择要刷新的 Team"
                }
            )

        logger.info(
            "管理员批量刷新 %s 个 Team%s",
            len(team_ids),
            f" (pool_type={action_data.pool_type})" if action_data.all_in_pool and action_data.pool_type else "",
        )

        async def progress_generator():
            success_count = 0
            failed_count = 0
            completed_count = 0
            total = len(team_ids)
            concurrency = min(3, total) if total > 0 else 1

            yield json.dumps({
                "type": "start",
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "completed_count": completed_count,
                "concurrency": concurrency,
            }, ensure_ascii=False) + "\n"

            async def refresh_single_team(team_id: int) -> Dict[str, object]:
                item_success = False
                item_error = None
                item_message = None

                try:
                    async with AsyncSessionLocal() as db_session:
                        result = await team_service.sync_team_info(team_id, db_session, force_refresh=False)
                    item_success = bool(result.get("success"))
                    item_message = result.get("message")
                    item_error = result.get("error")
                except Exception as ex:
                    logger.error(f"批量刷新 Team {team_id} 时出错: {ex}")
                    item_error = str(ex)

                return {
                    "team_id": team_id,
                    "success": item_success,
                    "message": item_message,
                    "error": item_error,
                }

            for start_index in range(0, total, concurrency):
                team_batch = team_ids[start_index:start_index + concurrency]
                pending_tasks = {
                    asyncio.create_task(refresh_single_team(team_id))
                    for team_id in team_batch
                }

                while pending_tasks:
                    done_tasks, pending_tasks = await asyncio.wait(
                        pending_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for done_task in done_tasks:
                        item = await done_task
                        completed_count += 1
                        item_success = bool(item["success"])
                        if item_success:
                            success_count += 1
                        else:
                            failed_count += 1

                        yield json.dumps({
                            "type": "progress",
                            "current": completed_count,
                            "completed_count": completed_count,
                            "total": total,
                            "success_count": success_count,
                            "failed_count": failed_count,
                            "team_id": item["team_id"],
                            "concurrency": concurrency,
                            "last_result": {
                                "success": item_success,
                                "message": item["message"],
                                "error": item["error"],
                            }
                        }, ensure_ascii=False) + "\n"

            yield json.dumps({
                "type": "finish",
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "completed_count": completed_count,
                "concurrency": concurrency,
                "message": f"批量刷新完成: 成功 {success_count}, 失败 {failed_count}"
            }, ensure_ascii=False) + "\n"

        return StreamingResponse(
            progress_generator(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            }
        )
    except Exception:
        logger.exception("批量刷新 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/batch-delete")
async def batch_delete_teams(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    批量删除 Team
    """
    try:
        logger.info(f"管理员批量删除 {len(action_data.ids)} 个 Team")
        
        success_count = 0
        failed_count = 0
        
        for team_id in action_data.ids:
            try:
                result = await team_service.delete_team(team_id, db)
                if result.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as ex:
                logger.error(f"批量删除 Team {team_id} 时出错: {ex}")
                failed_count += 1
        
        return JSONResponse(content={
            "success": True,
            "message": f"批量删除完成: 成功 {success_count}, 失败 {failed_count}",
            "success_count": success_count,
            "failed_count": failed_count
        })
    except Exception as e:
        logger.exception("批量删除 Team 失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.post("/teams/batch-enable-device-auth")
async def batch_enable_device_auth(
    action_data: BulkActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    批量开启设备代码身份验证
    """
    try:
        logger.info(f"管理员批量开启 {len(action_data.ids)} 个 Team 的设备验证")

        success_count = 0
        failed_count = 0

        for team_id in action_data.ids:
            try:
                result = await team_service.enable_device_code_auth(team_id, db)
                if result.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as ex:
                logger.error(f"批量开启 Team {team_id} 设备验证时出错: {ex}")
                failed_count += 1

        return JSONResponse(content={
            "success": True,
            "message": f"批量处理完成: 成功 {success_count}, 失败 {failed_count}",
            "success_count": success_count,
            "failed_count": failed_count
        })
    except Exception as e:
        logger.exception("批量处理失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "操作失败，请稍后重试"}
        )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    系统设置页面

    Args:
        request: FastAPI Request 对象
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        系统设置页面 HTML
    """
    try:
        from app.main import templates
        from app.services.settings import settings_service

        logger.info("管理员访问系统设置页面")

        # 获取当前配置
        proxy_config = await settings_service.get_proxy_config(db)
        log_level = await settings_service.get_log_level(db)

        context = await build_admin_base_context(request, db, current_user, "settings")
        context.update({
            "proxy_enabled": proxy_config["enabled"],
            "proxy": proxy_config["proxy"],
            "log_level": log_level,
            "webhook_url": await settings_service.get_setting(db, "webhook_url", ""),
            "low_stock_threshold": await settings_service.get_setting(db, "low_stock_threshold", "10"),
            "api_key": await settings_service.get_setting(db, "api_key", ""),
            "token_refresh_interval_minutes": await settings_service.get_setting(db, "token_refresh_interval_minutes", "30"),
            "token_refresh_window_hours": await settings_service.get_setting(db, "token_refresh_window_hours", "2"),
            "token_refresh_client_id": await settings_service.get_setting(db, "token_refresh_client_id", ""),
            "periodic_team_sync_enabled": await settings_service.get_setting(db, "periodic_team_sync_enabled", "true"),
            "periodic_team_sync_interval_hours": await settings_service.get_setting(db, "periodic_team_sync_interval_hours", "12"),
            "periodic_team_sync_days": await settings_service.get_setting(db, "periodic_team_sync_days", "7"),
            "default_team_max_members": await settings_service.get_setting(db, "default_team_max_members", "6"),
            "cliproxyapi_base_url": await settings_service.get_setting(db, "cliproxyapi_base_url", ""),
            "cliproxyapi_api_key": await settings_service.get_setting(db, "cliproxyapi_api_key", ""),
            "sub2api_base_url": await settings_service.get_setting(db, "sub2api_base_url", ""),
            "sub2api_api_key": await settings_service.get_setting(db, "sub2api_api_key", ""),
            "sub2api_admin_email": await settings_service.get_setting(db, "sub2api_admin_email", ""),
            "sub2api_admin_password": await settings_service.get_setting(db, "sub2api_admin_password", ""),
            "sub2api_group_ids": await settings_service.get_setting(db, "sub2api_group_ids", ""),
            "sub2api_template_name": await settings_service.get_setting(db, "sub2api_template_name", "Team轮转"),
            "sub2api_free_template_name": await settings_service.get_setting(db, "sub2api_free_template_name", "Free模板"),
            "free_account_proxy": await settings_service.get_setting(db, "free_account_proxy", ""),
            "cf_mail_base_url": await settings_service.get_setting(db, "cf_mail_base_url", "https://apimail.xiaozhudf2026.foo"),
            "cf_mail_address": await settings_service.get_setting(db, "cf_mail_address", "icloud@xiaozhudf2026.foo"),
            "cf_mail_admin_password": await settings_service.get_setting(db, "cf_mail_admin_password", ""),
            "hme_base_url": await settings_service.get_setting(db, "hme_base_url", "http://icloud-hme:8081"),
            "hme_service_token": await settings_service.get_setting(db, "hme_service_token", ""),
            "hme_account_id": await settings_service.get_setting(db, "hme_account_id", ""),
            "hme_team_tag_map": await settings_service.get_setting(db, "hme_team_tag_map", ""),
            "sms_max_uses_per_phone": await settings_service.get_setting(db, "sms_max_uses_per_phone", "3"),
            "sms_cooldown_sec": await settings_service.get_setting(db, "sms_cooldown_sec", "1200"),
            "sms_reserve_sec": await settings_service.get_setting(db, "sms_reserve_sec", "180"),
            "sms_max_phone_retries": await settings_service.get_setting(db, "sms_max_phone_retries", "3"),
            "ui_theme": settings_service.normalize_ui_theme(await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)),
            "ui_style": settings_service.normalize_ui_style(await settings_service.get_setting(db, "ui_style", DEFAULT_UI_STYLE)),
            "usage_probe_enabled": await settings_service.get_setting(db, "usage_probe_enabled", "true"),
            "usage_probe_interval_minutes": await settings_service.get_setting(db, "usage_probe_interval_minutes", "60"),
            "usage_probe_stagger_minutes": await settings_service.get_setting(db, "usage_probe_stagger_minutes", "60"),
            "usage_probe_batch_size": await settings_service.get_setting(db, "usage_probe_batch_size", "1"),
            "usage_probe_force": await settings_service.get_setting(db, "usage_probe_force", "true"),
            "usage_probe_scan_minutes": await settings_service.get_setting(db, "usage_probe_scan_minutes", "2"),
            "auto_reauth_enabled": await settings_service.get_setting(db, "auto_reauth_enabled", "false"),
            "auto_rotate_enabled": await settings_service.get_setting(db, "auto_rotate_enabled", "false"),
        })
        return templates.TemplateResponse(
            request,
            "admin/settings/index.html",
            context,
        )

    except Exception as e:
        logger.exception("获取系统设置失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取系统设置失败，请稍后重试"
        )


class ProxyConfigRequest(BaseModel):
    """代理配置请求"""
    enabled: bool = Field(..., description="是否启用代理")
    proxy: str = Field("", description="代理地址")


class LogLevelRequest(BaseModel):
    """日志级别请求"""
    level: str = Field(..., description="日志级别")


class WebhookSettingsRequest(BaseModel):
    """Webhook 设置请求"""
    webhook_url: str = Field("", description="Webhook URL")
    low_stock_threshold: int = Field(10, description="库存阈值")
    api_key: str = Field("", description="API Key")


class TokenRefreshSettingsRequest(BaseModel):
    """Token 自动刷新设置请求"""
    interval_minutes: int = Field(30, ge=5, le=1440, description="定时刷新间隔（分钟）")
    window_hours: int = Field(2, ge=1, le=24, description="过期前提前刷新窗口（小时）")
    client_id: str = Field("", description="OAuth Client ID（用于 RT 刷新）")


class TeamImportSettingsRequest(BaseModel):
    """Team 导入设置请求"""
    default_team_max_members: int = Field(6, ge=1, le=100, description="新导入 Team 的默认总席位")


class CliproxyapiSettingsRequest(BaseModel):
    """CliproxyAPI 推送配置请求"""
    base_url: str = Field("", description="CliproxyAPI 站点地址")
    api_key: str = Field("", description="CliproxyAPI 管理密钥")


class Sub2ApiSettingsRequest(BaseModel):
    base_url: str = Field("", description="Sub2API 地址")
    api_key: str = Field("", description="Sub2API Admin API Key")
    admin_email: str = Field("", description="Sub2API 后台邮箱，只读状态可选用")
    admin_password: str = Field("", description="Sub2API 后台密码，只读状态可选用")
    group_ids: str = Field("", description="分组 ID，逗号分隔")
    template_name: str = Field("Team轮转", description="推子号时套用的账号创建模板名")
    free_template_name: str = Field("Free模板", description="推免费号时套用的账号创建模板名")
    free_account_proxy: str = Field("", description="免费号默认静态 ISP")


class CloudflareMailSettingsRequest(BaseModel):
    base_url: str = Field("", description="Cloudflare Temp Email API 地址")
    address: str = Field("", description="Forward To 收件地址")
    admin_password: str = Field("", description="x-admin-auth 密钥")


class HmeSettingsRequest(BaseModel):
    base_url: str = Field("", description="HME 服务地址")
    service_token: str = Field("", description="X-HME-Service-Token，留空沿用已保存")
    account_id: str = Field("", description="HME 账号 ID，只有一个时可空")
    team_tag_map: str = Field("", description="可选 JSON：team_id -> 标签")


class TeamAutoRefreshSettingsRequest(BaseModel):
    """Team 自动刷新设置请求"""
    enabled: bool = Field(True, description="是否启用 Team 周期状态自动刷新")
    interval_hours: int = Field(12, ge=1, le=168, description="检查间隔（小时）")
    refresh_interval_days: int = Field(7, ge=1, le=30, description="同步周期（天）")


class UsageProbeSettingsRequest(BaseModel):
    """Sub2API 额度错峰探测设置。第 2/3 层默认关，保存后才注册任务。强制补位始终关。"""
    enabled: bool = Field(True, description="是否启用额度错峰探测")
    interval_minutes: int = Field(60, ge=5, le=1440, description="每号探测间隔（分钟）")
    stagger_minutes: int = Field(60, ge=5, le=1440, description="把本轮账号摊进这么多分钟")
    batch_size: int = Field(1, ge=1, le=3, description="每轮 force 的账号数")
    force: bool = Field(True, description="usage/batch 是否 force 刷新缓存")
    scan_minutes: int = Field(2, ge=1, le=10, description="扫描器检查间隔（分钟）")
    auto_reauth_enabled: bool = Field(False, description="第 2 层：401 自动重授权，必须默认关")
    auto_rotate_enabled: bool = Field(False, description="第 3 层：封禁/周限满踢拉，必须默认关")


class UiThemeSettingsRequest(BaseModel):
    """系统配色设置请求"""
    theme: Literal["ocean", "warm"] = Field(DEFAULT_UI_THEME, description="系统配色主题")


class UiStyleSettingsRequest(BaseModel):
    """界面风格设置请求"""
    style: Literal["cartoon", "classic"] = Field(DEFAULT_UI_STYLE, description="界面风格")


class AdminProfileRequest(BaseModel):
    """管理员个人资料更新请求"""
    nickname: str = Field("", max_length=32, description="昵称")
    avatar: str = Field("", description="头像 data URL（image/* base64）；空字符串表示清除")


@router.get("/settings/ui-theme")
async def get_ui_theme_settings(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """获取系统配色设置。"""
    theme = settings_service.normalize_ui_theme(
        await settings_service.get_setting(db, "ui_theme", DEFAULT_UI_THEME)
    )
    return JSONResponse(content={"success": True, "theme": theme})


@router.post("/settings/ui-theme")
async def update_ui_theme_settings(
    theme_data: UiThemeSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新系统配色设置。"""
    try:
        theme = settings_service.normalize_ui_theme(theme_data.theme)
        logger.info("管理员更新系统配色: %s", theme)

        success = await settings_service.update_setting(db, "ui_theme", theme)
        if success:
            return JSONResponse(content={"success": True, "message": "系统配色已保存", "theme": theme})

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败"}
        )
    except Exception as e:
        logger.exception("更新系统配色失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.get("/settings/ui-style")
async def get_ui_style_settings(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """获取界面风格设置。"""
    style = settings_service.normalize_ui_style(
        await settings_service.get_setting(db, "ui_style", DEFAULT_UI_STYLE)
    )
    return JSONResponse(content={"success": True, "style": style})


@router.post("/settings/ui-style")
async def update_ui_style_settings(
    style_data: UiStyleSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新界面风格设置。"""
    try:
        style = settings_service.normalize_ui_style(style_data.style)
        logger.info("管理员更新界面风格: %s", style)

        success = await settings_service.update_setting(db, "ui_style", style)
        if success:
            return JSONResponse(content={"success": True, "message": "界面风格已保存", "style": style})

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败"}
        )
    except Exception as e:
        logger.exception("更新界面风格失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


# 头像 data URL 上限（约等于 1MB 二进制 + base64 30% 膨胀）
_ADMIN_AVATAR_MAX_LEN = 1_400_000


@router.get("/settings/profile")
async def get_admin_profile(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """获取管理员个人资料（昵称 + 头像）。"""
    profile = await resolve_admin_profile(db)
    return JSONResponse(content={"success": True, **profile})


@router.post("/settings/profile")
async def update_admin_profile(
    profile_data: AdminProfileRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新管理员个人资料。"""
    try:
        nickname = (profile_data.nickname or "").strip()
        avatar = (profile_data.avatar or "").strip()

        if avatar and not avatar.startswith("data:image/"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "头像格式无效，请上传图片"}
            )
        if avatar and len(avatar) > _ADMIN_AVATAR_MAX_LEN:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "头像太大，请压缩后再上传"}
            )

        # 用 update_settings（复数）单事务写入，避免半成功 + 检查返回值
        success = await settings_service.update_settings(db, {
            "admin_nickname": nickname,
            "admin_avatar": avatar,
        })
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败，请稍后重试"}
            )

        logger.info(
            "管理员更新个人资料: nickname_len=%s, has_avatar=%s",
            len(nickname),
            bool(avatar),
        )

        return JSONResponse(content={
            "success": True,
            "message": "已保存",
            "nickname": nickname,
            "avatar": avatar,
        })
    except Exception:
        logger.exception("更新管理员个人资料失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败，请稍后重试"}
        )

@router.post("/settings/proxy")
async def update_proxy_config(
    proxy_data: ProxyConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    更新代理配置

    Args:
        proxy_data: 代理配置数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        更新结果
    """
    try:
        from app.services.settings import settings_service

        masked_proxy = ""
        if proxy_data.proxy:
            try:
                masked_proxy = mask_proxy_url(proxy_data.proxy)
            except ValueError:
                masked_proxy = "<invalid-proxy>"
        logger.info(f"管理员更新代理配置: enabled={proxy_data.enabled}, proxy={masked_proxy}")

        # 验证代理地址格式
        if proxy_data.enabled:
            if not str(proxy_data.proxy or "").strip():
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "error": "启用代理时必须填写代理地址"
                    }
                )
            try:
                normalize_proxy_url(proxy_data.proxy)
            except ValueError as exc:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "error": str(exc)
                    }
                )

        # 更新配置
        success = await settings_service.update_proxy_config(
            db,
            proxy_data.enabled,
            proxy_data.proxy.strip() if proxy_data.proxy else ""
        )

        if success:
            # 清理 ChatGPT 服务的会话,确保下次请求使用新代理
            from app.services.chatgpt import chatgpt_service
            await chatgpt_service.clear_session()
            
            return JSONResponse(content={"success": True, "message": "代理配置已保存"})
        else:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

    except Exception as e:
        logger.exception("更新代理配置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/log-level")
async def update_log_level(
    log_data: LogLevelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    更新日志级别

    Args:
        log_data: 日志级别数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        更新结果
    """
    try:
        from app.services.settings import settings_service

        logger.info(f"管理员更新日志级别: {log_data.level}")

        # 更新日志级别
        success = await settings_service.update_log_level(db, log_data.level)

        if success:
            return JSONResponse(content={"success": True, "message": "日志级别已保存"})
        else:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "无效的日志级别"}
            )

    except Exception as e:
        logger.exception("更新日志级别失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/webhook")
async def update_webhook_settings(
    webhook_data: WebhookSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """
    更新 Webhook 和 API Key 设置
    """
    try:
        from app.services.settings import settings_service

        logger.info(f"管理员更新 Webhook/API 配置: url={webhook_data.webhook_url}, threshold={webhook_data.low_stock_threshold}")

        settings = {
            "webhook_url": webhook_data.webhook_url.strip(),
            "low_stock_threshold": str(webhook_data.low_stock_threshold),
            "api_key": webhook_data.api_key.strip()
        }

        success = await settings_service.update_settings(db, settings)

        if success:
            return JSONResponse(content={"success": True, "message": "配置已保存"})
        else:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

    except Exception as e:
        logger.exception("更新配置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/token-refresh")
async def update_token_refresh_settings(
    token_data: TokenRefreshSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 Token 自动刷新设置。"""
    try:
        from app.main import configure_proactive_refresh_job
        from app.services.settings import settings_service

        logger.info(
            "管理员更新 Token 自动刷新配置: interval=%s, window=%s",
            token_data.interval_minutes,
            token_data.window_hours,
        )

        settings = {
            "token_refresh_interval_minutes": str(token_data.interval_minutes),
            "token_refresh_window_hours": str(token_data.window_hours),
            "token_refresh_client_id": token_data.client_id.strip(),
        }

        success = await settings_service.update_settings(db, settings)
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

        interval = configure_proactive_refresh_job(token_data.interval_minutes)
        return JSONResponse(
            content={
                "success": True,
                "message": f"Token 自动刷新配置已保存（当前间隔: {interval} 分钟）",
                "interval": interval
            }
        )

    except Exception as e:
        logger.exception("更新 Token 自动刷新设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/team-auto-refresh")
async def update_team_auto_refresh_settings(
    team_refresh_data: TeamAutoRefreshSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 Team 周期状态自动刷新设置。"""
    try:
        from app.main import configure_periodic_team_sync_job
        from app.services.settings import settings_service

        logger.info(
            "管理员更新 Team 自动刷新配置: enabled=%s, interval_hours=%s, days=%s",
            team_refresh_data.enabled,
            team_refresh_data.interval_hours,
            team_refresh_data.refresh_interval_days,
        )

        settings_payload = {
            "periodic_team_sync_enabled": str(team_refresh_data.enabled).lower(),
            "periodic_team_sync_interval_hours": str(team_refresh_data.interval_hours),
            "periodic_team_sync_days": str(team_refresh_data.refresh_interval_days),
        }

        success = await settings_service.update_settings(db, settings_payload)
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"}
            )

        applied_interval = configure_periodic_team_sync_job(
            team_refresh_data.enabled,
            team_refresh_data.interval_hours,
        )

        if team_refresh_data.enabled:
            message = (
                "Team 自动刷新配置已保存（每 "
                f"{applied_interval} 小时检查一次，超过 {team_refresh_data.refresh_interval_days} 天未同步则执行刷新）"
            )
        else:
            message = "Team 自动刷新已关闭"

        return JSONResponse(
            content={
                "success": True,
                "message": message,
                "enabled": team_refresh_data.enabled,
                "interval_hours": applied_interval,
                "refresh_interval_days": team_refresh_data.refresh_interval_days,
            }
        )
    except Exception as e:
        logger.exception("更新 Team 自动刷新设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/usage-probe")
async def update_usage_probe_settings(
    probe_data: UsageProbeSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新额度错峰探测和第 2/3 层开关。强制补位始终关。"""
    try:
        from app.main import (
            configure_auto_reauth_job,
            configure_auto_rotate_job,
            configure_usage_probe_job,
            DEFAULT_AUTO_REAUTH_SCAN_MINUTES,
            DEFAULT_AUTO_ROTATE_SCAN_MINUTES,
        )
        from app.services.auto_rotate import (
            clamp_auto_reauth_interval_minutes,
            clamp_usage_probe_batch_size,
            clamp_usage_probe_interval_minutes,
            clamp_usage_probe_scan_minutes,
            clamp_usage_probe_stagger_minutes,
        )

        interval_minutes = clamp_usage_probe_interval_minutes(probe_data.interval_minutes)
        stagger_minutes = clamp_usage_probe_stagger_minutes(probe_data.stagger_minutes)
        batch_size = clamp_usage_probe_batch_size(probe_data.batch_size)
        scan_minutes = clamp_usage_probe_scan_minutes(probe_data.scan_minutes)
        logger.info(
            "管理员更新额度探测配置: enabled=%s interval=%s stagger=%s batch=%s force=%s scan=%s reauth=%s rotate=%s",
            probe_data.enabled,
            interval_minutes,
            stagger_minutes,
            batch_size,
            probe_data.force,
            scan_minutes,
            probe_data.auto_reauth_enabled,
            probe_data.auto_rotate_enabled,
        )
        settings_payload = {
            "usage_probe_enabled": str(probe_data.enabled).lower(),
            "usage_probe_interval_minutes": str(interval_minutes),
            "usage_probe_stagger_minutes": str(stagger_minutes),
            "usage_probe_batch_size": str(batch_size),
            "usage_probe_force": str(probe_data.force).lower(),
            "usage_probe_scan_minutes": str(scan_minutes),
            "auto_reauth_enabled": str(bool(probe_data.auto_reauth_enabled)).lower(),
            "auto_rotate_enabled": str(bool(probe_data.auto_rotate_enabled)).lower(),
            "auto_rotate_force_refill": "false",
        }

        success = await settings_service.update_settings(db, settings_payload)
        if not success:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "保存失败"},
            )

        applied_scan = configure_usage_probe_job(probe_data.enabled, scan_minutes)
        reauth_interval = clamp_auto_reauth_interval_minutes(DEFAULT_AUTO_REAUTH_SCAN_MINUTES)
        configure_auto_reauth_job(bool(probe_data.auto_reauth_enabled), reauth_interval)
        configure_auto_rotate_job(bool(probe_data.auto_rotate_enabled), DEFAULT_AUTO_ROTATE_SCAN_MINUTES)
        parts = []
        if probe_data.enabled:
            parts.append(
                f"额度错峰探测已保存（每 {applied_scan} 分钟扫一轮，每号约 {interval_minutes} 分钟，窗口 {stagger_minutes} 分钟，每次 force {batch_size} 个）"
            )
        else:
            parts.append("额度错峰探测已关闭")
        parts.append("第 2 层 401 自动重授权已打开" if probe_data.auto_reauth_enabled else "第 2 层保持关闭")
        parts.append("第 3 层封禁/周限满踢拉已打开（每 Team 每天最多 2 次）" if probe_data.auto_rotate_enabled else "第 3 层保持关闭")
        message = "；".join(parts)
        return JSONResponse(
            content={
                "success": True,
                "message": message,
                "enabled": probe_data.enabled,
                "interval_minutes": interval_minutes,
                "stagger_minutes": stagger_minutes,
                "batch_size": batch_size,
                "force": probe_data.force,
                "scan_minutes": applied_scan,
                "auto_reauth_enabled": bool(probe_data.auto_reauth_enabled),
                "auto_rotate_enabled": bool(probe_data.auto_rotate_enabled),
            }
        )
    except Exception:
        logger.exception("更新额度错峰探测设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"},
        )

@router.post("/settings/team-import")
async def update_team_import_settings(
    team_import_data: TeamImportSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 Team 导入默认配置。"""
    try:
        logger.info(
            "管理员更新 Team 导入配置: default_team_max_members=%s",
            team_import_data.default_team_max_members,
        )

        success = await settings_service.update_setting(
            db,
            "default_team_max_members",
            str(team_import_data.default_team_max_members),
        )

        if success:
            return JSONResponse(content={"success": True, "message": "Team 导入配置已保存"})

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败"}
        )

    except Exception as e:
        logger.exception("更新 Team 导入设置失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "更新失败，请稍后重试"}
        )


@router.post("/settings/cliproxyapi")
async def update_cliproxyapi_settings(
    cliproxyapi_data: CliproxyapiSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin)
):
    """更新 CliproxyAPI 推送配置。"""
    try:
        base_url = cliproxyapi_service.normalize_base_url(cliproxyapi_data.base_url)
        api_key = cliproxyapi_data.api_key.strip()

        if not base_url:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "CliproxyAPI 地址不能为空"}
            )

        if not api_key:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "CliproxyAPI 管理密钥不能为空"}
            )

        if not cliproxyapi_service.is_valid_base_url(base_url):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "error": "CliproxyAPI 地址格式错误，仅支持 http/https"}
            )

        success = await settings_service.update_settings(
            db,
            {
                "cliproxyapi_base_url": base_url,
                "cliproxyapi_api_key": api_key,
            }
        )

        if success:
            return JSONResponse(
                content={
                    "success": True,
                    "message": "CliproxyAPI 配置已保存",
                    "base_url": base_url,
                }
            )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "保存失败"}
        )

    except Exception as e:
        logger.error("更新 CliproxyAPI 配置失败: %s", e)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新失败: {str(e)}"}
        )


@router.post("/settings/sub2api")
async def update_sub2api_settings(
    payload: Sub2ApiSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    success = await settings_service.update_settings(db, {
        "sub2api_base_url": payload.base_url.strip().rstrip("/"),
        "sub2api_api_key": payload.api_key.strip(),
        "sub2api_admin_email": payload.admin_email.strip(),
        "sub2api_admin_password": payload.admin_password.strip(),
        "sub2api_group_ids": payload.group_ids.strip(),
        "sub2api_template_name": payload.template_name.strip() or "Team轮转",
        "sub2api_free_template_name": payload.free_template_name.strip() or "Free模板",
        "free_account_proxy": payload.free_account_proxy.strip(),
    })
    if success:
        return JSONResponse(content={"success": True, "message": "Sub2API 配置已保存"})
    return JSONResponse(status_code=500, content={"success": False, "error": "保存失败"})


@router.post("/settings/cloudflare-mail")
async def update_cloudflare_mail_settings(
    payload: CloudflareMailSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.cloudflare_mail import (
        CF_SETTING_ADDRESS,
        CF_SETTING_ADMIN_PASSWORD,
        CF_SETTING_BASE_URL,
        cloudflare_mail_client,
        normalize_cloudflare_base_url,
        normalize_mailbox_address,
    )

    base_url = normalize_cloudflare_base_url(payload.base_url)
    address = normalize_mailbox_address(payload.address)
    admin_password = payload.admin_password.strip()
    if not admin_password:
        admin_password = (await settings_service.get_setting(db, CF_SETTING_ADMIN_PASSWORD, "") or "").strip()
    if not admin_password:
        return JSONResponse(status_code=400, content={"success": False, "error": "请输入 Cloudflare 管理员密钥"})
    try:
        cloudflare_mail_client.fetch_messages(
            base_url=base_url,
            address=address,
            admin_password=admin_password,
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"success": False, "error": f"连接失败: {exc}"})
    success = await settings_service.update_settings(db, {
        CF_SETTING_BASE_URL: base_url,
        CF_SETTING_ADDRESS: address,
        CF_SETTING_ADMIN_PASSWORD: admin_password,
    })
    if success:
        return JSONResponse(content={"success": True, "message": "Cloudflare 邮箱配置已保存", "base_url": base_url, "address": address})
    return JSONResponse(status_code=500, content={"success": False, "error": "保存失败"})


@router.post("/settings/hme")
async def update_hme_settings(
    payload: HmeSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.hme import (
        DEFAULT_HME_BASE_URL,
        HME_SETTING_ACCOUNT_ID,
        HME_SETTING_BASE_URL,
        HME_SETTING_SERVICE_TOKEN,
        HME_SETTING_TEAM_TAG_MAP,
        normalize_hme_base_url,
        parse_team_tag_map,
        probe_status,
    )

    base_url = normalize_hme_base_url(payload.base_url or DEFAULT_HME_BASE_URL)
    token = payload.service_token.strip()
    if not token:
        token = (await settings_service.get_setting(db, HME_SETTING_SERVICE_TOKEN, "") or "").strip()
    if not token:
        return JSONResponse(status_code=400, content={"success": False, "error": "请输入 HME 服务 token"})
    team_tag_map = payload.team_tag_map.strip()
    if team_tag_map:
        parse_team_tag_map(team_tag_map)
    success = await settings_service.update_settings(db, {
        HME_SETTING_BASE_URL: base_url,
        HME_SETTING_SERVICE_TOKEN: token,
        HME_SETTING_ACCOUNT_ID: payload.account_id.strip(),
        HME_SETTING_TEAM_TAG_MAP: team_tag_map,
    })
    if not success:
        return JSONResponse(status_code=500, content={"success": False, "error": "保存失败"})
    probe = await probe_status(db)
    if not probe.get("ok"):
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": probe.get("error") or "探测失败", "base_url": base_url, **probe},
        )
    return JSONResponse(content={
        "success": True,
        "message": f"HME 已保存，未占用 {probe.get('unused', 0)}",
        "base_url": base_url,
        "account_id": probe.get("account_id") or payload.account_id.strip(),
        **probe,
    })


@router.post("/settings/hme/probe")
async def probe_hme_settings(
    payload: HmeSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.hme import (
        DEFAULT_HME_BASE_URL,
        HME_SETTING_ACCOUNT_ID,
        HME_SETTING_SERVICE_TOKEN,
        normalize_hme_base_url,
    )

    base_url = normalize_hme_base_url(payload.base_url or DEFAULT_HME_BASE_URL)
    token = payload.service_token.strip() or (await settings_service.get_setting(db, HME_SETTING_SERVICE_TOKEN, "") or "").strip()
    account_id = payload.account_id.strip()
    # 探测用表单值，不落盘；临时写入缓存会造成误会，所以直接调客户端。
    from app.services import hme as hme_service

    cfg = hme_service.HmeConfig(
        base_url=base_url,
        service_token=token,
        account_id=account_id or (await settings_service.get_setting(db, HME_SETTING_ACCOUNT_ID, "") or "").strip(),
    )
    if not cfg.configured:
        return JSONResponse(status_code=400, content={"success": False, "error": "请填写 HME 地址和 token"})
    try:
        accounts = await asyncio.to_thread(hme_service.hme_client.list_accounts, cfg)
        account = hme_service.resolve_account(accounts, cfg.account_id)
        aliases = await asyncio.to_thread(
            hme_service.hme_client.list_aliases, cfg, str(account.get("id") or "")
        )
        await hme_service.purge_expired_leases(db)
        leased = await hme_service.active_leased_emails(db)
        unused = hme_service.pick_all_unoccupied(aliases, leased)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    return JSONResponse(content={
        "success": True,
        "ok": True,
        "base_url": base_url,
        "account_id": account.get("id"),
        "account_name": account.get("name") or "",
        "alias_total": len(aliases),
        "unused": len(unused),
        "message": f"连通，未占用 {len(unused)} / 共 {len(aliases)}",
    })


class SmsPoolSettingsRequest(BaseModel):
    sms_max_uses_per_phone: int = Field(3, ge=1, le=20)
    sms_cooldown_sec: int = Field(1200, ge=60, le=86400)
    sms_reserve_sec: int = Field(180, ge=60, le=7200)
    sms_max_phone_retries: int = Field(3, ge=1, le=10)


class PhonePoolImportRequest(BaseModel):
    text: str = Field("", description="批量 号码----sms_url")


class PhonePoolStatusRequest(BaseModel):
    enabled: bool = True


class PhonePoolClearRequest(BaseModel):
    status: str = Field(..., description="maxed 或 disabled")


def _phone_pool_payload(stats, items, cfg=None):
    from app.services.phone_pool import phone_pool_service

    return {
        "success": True,
        "stats": stats,
        "items": items,
        "config": (cfg or stats.get("config") or {}),
    }


@router.get("/phone-pool")
async def phone_pool_list(
    status: str = "",
    q: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.phone_pool import phone_pool_service

    cfg = await phone_pool_service.get_config(db)
    stats = await phone_pool_service.stats(db)
    rows = await phone_pool_service.list_phones(db, status=status, q=q)
    await db.commit()
    return _phone_pool_payload(stats, [phone_pool_service.serialize(row, cfg) for row in rows], stats.get("config"))


@router.post("/phone-pool/import")
async def phone_pool_import(
    payload: PhonePoolImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.phone_pool import phone_pool_service

    result = await phone_pool_service.import_lines(db, payload.text)
    cfg = await phone_pool_service.get_config(db)
    stats = await phone_pool_service.stats(db)
    rows = await phone_pool_service.list_phones(db)
    await db.commit()
    return {
        "success": True,
        "imported": result["imported"],
        "skipped": result["skipped"],
        "errors": result["errors"],
        "message": f"导入 {result['imported']} 条，跳过 {result['skipped']} 条",
        "stats": stats,
        "items": [phone_pool_service.serialize(row, cfg) for row in rows],
        "config": stats.get("config") or {},
    }


@router.post("/phone-pool/{phone_id}/status")
async def phone_pool_set_status(
    phone_id: int,
    payload: PhonePoolStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.phone_pool import phone_pool_service

    try:
        await phone_pool_service.set_enabled(db, phone_id, payload.enabled)
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"success": False, "error": str(exc)})
    cfg = await phone_pool_service.get_config(db)
    stats = await phone_pool_service.stats(db)
    rows = await phone_pool_service.list_phones(db)
    await db.commit()
    return _phone_pool_payload(stats, [phone_pool_service.serialize(row, cfg) for row in rows], stats.get("config"))


@router.post("/phone-pool/clear")
async def phone_pool_clear(
    payload: PhonePoolClearRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.phone_pool import phone_pool_service

    try:
        deleted = await phone_pool_service.clear_status(db, payload.status)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    cfg = await phone_pool_service.get_config(db)
    stats = await phone_pool_service.stats(db)
    rows = await phone_pool_service.list_phones(db)
    await db.commit()
    return {
        "success": True,
        "deleted": deleted,
        "message": f"已删除 {deleted} 条",
        "stats": stats,
        "items": [phone_pool_service.serialize(row, cfg) for row in rows],
        "config": stats.get("config") or {},
    }


@router.post("/settings/sms-pool")
async def update_sms_pool_settings(
    payload: SmsPoolSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.phone_pool import (
        SETTING_COOLDOWN_SEC,
        SETTING_MAX_RETRIES,
        SETTING_MAX_USES,
        SETTING_RESERVE_SEC,
    )

    success = await settings_service.update_settings(db, {
        SETTING_MAX_USES: str(payload.sms_max_uses_per_phone),
        SETTING_COOLDOWN_SEC: str(payload.sms_cooldown_sec),
        SETTING_RESERVE_SEC: str(payload.sms_reserve_sec),
        SETTING_MAX_RETRIES: str(payload.sms_max_phone_retries),
    })
    if not success:
        return JSONResponse(status_code=500, content={"success": False, "error": "保存失败"})
    return JSONResponse(content={
        "success": True,
        "message": "号码池设置已保存",
        "sms_max_uses_per_phone": payload.sms_max_uses_per_phone,
        "sms_cooldown_sec": payload.sms_cooldown_sec,
        "sms_reserve_sec": payload.sms_reserve_sec,
        "sms_max_phone_retries": payload.sms_max_phone_retries,
    })

