"""
用户路由
处理用户兑换页面
"""
import logging
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    tags=["user"]
)


@router.get("/", include_in_schema=False)
async def root_redirect():
    """自用控制台不开放兑换前台，根路径直接进后台。"""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/admin", status_code=302)
