"""
认证路由
处理管理员登录和登出
"""
import logging
import time
from threading import Lock
from typing import Dict, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth import auth_service
from app.dependencies.auth import get_current_user

logger = logging.getLogger(__name__)


# 登录失败节流：同一来源 IP 在 15 分钟内最多允许 10 次失败登录尝试。
# 仅为反暴力破解兜底，不替代密码强度/审计策略。
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 10
_login_failures: Dict[str, Tuple[int, float]] = {}  # ip -> (count, window_start_epoch)
_login_failures_lock = Lock()


def _client_key(request: Request) -> str:
    """获取客户端标识用于速率限制 (优先 X-Forwarded-For)。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _check_login_rate_limit(ip: str) -> Optional[int]:
    """返回 None 表示允许；返回整数表示需等待的秒数。"""
    now = time.time()
    with _login_failures_lock:
        count, window_start = _login_failures.get(ip, (0, now))
        if now - window_start >= _LOGIN_WINDOW_SECONDS:
            # 窗口已过，重置
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

# 创建路由器
router = APIRouter(
    prefix="/auth",
    tags=["auth"]
)


# 请求模型
class LoginRequest(BaseModel):
    """登录请求"""
    password: str = Field(..., description="管理员密码", min_length=1)


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., description="旧密码", min_length=1)
    new_password: str = Field(..., description="新密码", min_length=6)


# 响应模型
class LoginResponse(BaseModel):
    """登录响应"""
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None


class LogoutResponse(BaseModel):
    """登出响应"""
    success: bool
    message: str


class ChangePasswordResponse(BaseModel):
    """修改密码响应"""
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    login_data: LoginRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    管理员登录

    Args:
        request: FastAPI Request 对象
        login_data: 登录数据
        db: 数据库会话

    Returns:
        登录结果
    """
    try:
        ip = _client_key(request)
        logger.info(f"管理员登录请求 (ip={ip})")

        retry_after = _check_login_rate_limit(ip)
        if retry_after is not None:
            logger.warning(f"登录速率限制触发: ip={ip}, 需等待 {retry_after}s")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"登录失败次数过多，请 {max(retry_after, 1)} 秒后再试",
                headers={"Retry-After": str(max(retry_after, 1))}
            )

        # 验证密码
        result = await auth_service.verify_admin_login(
            login_data.password,
            db
        )

        if not result["success"]:
            _record_login_failure(ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=result["error"]
            )

        # 登录成功，清理失败计数
        _clear_login_failures(ip)

        # 设置 Session
        request.session["user"] = {
            "username": "admin",
            "is_admin": True
        }

        logger.info("管理员登录成功，Session 已创建")

        return LoginResponse(
            success=True,
            message="登录成功",
            error=None
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception("登录失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="登录失败，请稍后重试"
        )


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request):
    """
    管理员登出

    Args:
        request: FastAPI Request 对象

    Returns:
        登出结果
    """
    try:
        # 清除 Session
        request.session.clear()

        logger.info("管理员登出成功")

        return LogoutResponse(
            success=True,
            message="登出成功"
        )

    except Exception:
        logger.exception("登出失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="登出失败，请稍后重试"
        )


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_password(
    request: Request,
    password_data: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    修改管理员密码

    Args:
        request: FastAPI Request 对象
        password_data: 密码数据
        db: 数据库会话
        current_user: 当前用户（需要登录）

    Returns:
        修改结果
    """
    try:
        logger.info("管理员修改密码请求")

        # 修改密码
        result = await auth_service.change_admin_password(
            password_data.old_password,
            password_data.new_password,
            db
        )

        if not result["success"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result["error"]
            )

        # 清除 Session，要求重新登录
        request.session.clear()

        logger.info("管理员密码修改成功")

        return ChangePasswordResponse(
            success=True,
            message="密码修改成功，请重新登录",
            error=None
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception("修改密码失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="修改密码失败，请稍后重试"
        )


@router.get("/status")
async def get_auth_status(request: Request):
    """
    获取认证状态

    Args:
        request: FastAPI Request 对象

    Returns:
        认证状态
    """
    user = request.session.get("user")

    return {
        "authenticated": user is not None,
        "user": user
    }
