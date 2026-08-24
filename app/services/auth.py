"""
认证服务
处理管理员登录、密码验证和 Session 管理
"""
import base64
import hashlib
import logging
import bcrypt
from typing import Optional, Dict, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Setting
from app.config import settings

logger = logging.getLogger(__name__)


class AuthService:
    """认证服务类"""

    def __init__(self):
        """初始化认证服务"""
        pass

    # bcrypt 会静默截断超过 72 字节的密码；为了行为可预测，
    # 在哈希/校验入口用 SHA256 预先压缩成固定长度字节串，再交给 bcrypt。
    # 这里选择 base64 编码而非原始 digest 以确保不含 NUL 字节（bcrypt 会在 NUL 处截断）。
    _BCRYPT_MAX_INPUT_BYTES = 72

    @staticmethod
    def _prepare_for_bcrypt(password: str) -> bytes:
        password_bytes = password.encode('utf-8')
        if len(password_bytes) <= AuthService._BCRYPT_MAX_INPUT_BYTES:
            return password_bytes
        digest = hashlib.sha256(password_bytes).digest()
        return base64.b64encode(digest)

    def hash_password(self, password: str) -> str:
        """
        哈希密码

        Args:
            password: 明文密码

        Returns:
            哈希后的密码
        """
        password_bytes = self._prepare_for_bcrypt(password)
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password_bytes, salt)
        return hashed.decode('utf-8')

    def _verify_password_detailed(self, password: str, hashed_password: str) -> str:
        """
        验证密码并返回具体走通的路径。

        返回值:
            "primary"  —— 使用当前逻辑（含长密码 SHA256 预处理）匹配成功
            "legacy"   —— 旧版本直接交给 bcrypt 由其截断至 72 字节的行为匹配成功
            ""         —— 密码错误或校验异常
        """
        try:
            hashed_bytes = hashed_password.encode('utf-8')
            # 1. 先尝试当前逻辑
            prepared = self._prepare_for_bcrypt(password)
            if bcrypt.checkpw(prepared, hashed_bytes):
                return "primary"

            # 2. 如果密码超过 72 字节，旧版本的 bcrypt(<4.1) 会静默截断到 72 字节，
            #    因此数据库里可能存在基于截断输入生成的哈希；为保证这些老哈希能被
            #    自动识别并升级，这里做一次兼容性兜底。
            #    现代 bcrypt(>=4.1) 会对超长输入直接 raise ValueError，我们手动截断
            #    到 72 字节来复现旧版本的匹配行为。
            password_bytes = password.encode('utf-8')
            if len(password_bytes) > self._BCRYPT_MAX_INPUT_BYTES:
                truncated = password_bytes[:self._BCRYPT_MAX_INPUT_BYTES]
                try:
                    if bcrypt.checkpw(truncated, hashed_bytes):
                        return "legacy"
                except ValueError:
                    pass
            return ""
        except Exception as e:
            logger.error(f"密码验证失败: {e}")
            return ""

    def verify_password(self, password: str, hashed_password: str) -> bool:
        """
        验证密码

        Args:
            password: 明文密码
            hashed_password: 哈希后的密码

        Returns:
            是否匹配
        """
        return bool(self._verify_password_detailed(password, hashed_password))

    async def get_admin_password_hash(self, db_session: AsyncSession) -> Optional[str]:
        """
        从数据库获取管理员密码哈希

        Args:
            db_session: 数据库会话

        Returns:
            密码哈希，如果不存在则返回 None
        """
        try:
            stmt = select(Setting).where(Setting.key == "admin_password_hash")
            result = await db_session.execute(stmt)
            setting = result.scalar_one_or_none()

            if setting:
                return setting.value
            return None

        except Exception as e:
            logger.error(f"获取管理员密码哈希失败: {e}")
            return None

    async def set_admin_password_hash(
        self,
        password_hash: str,
        db_session: AsyncSession
    ) -> bool:
        """
        设置管理员密码哈希到数据库

        Args:
            password_hash: 密码哈希
            db_session: 数据库会话

        Returns:
            是否成功
        """
        try:
            # 查询是否已存在
            stmt = select(Setting).where(Setting.key == "admin_password_hash")
            result = await db_session.execute(stmt)
            setting = result.scalar_one_or_none()

            if setting:
                # 更新
                setting.value = password_hash
            else:
                # 创建
                setting = Setting(
                    key="admin_password_hash",
                    value=password_hash,
                    description="管理员密码哈希"
                )
                db_session.add(setting)

            await db_session.commit()
            logger.info("管理员密码哈希已更新")
            return True

        except Exception as e:
            await db_session.rollback()
            logger.error(f"设置管理员密码哈希失败: {e}")
            return False

    async def initialize_admin_password(self, db_session: AsyncSession) -> bool:
        """
        初始化管理员密码
        如果数据库中没有密码哈希，则从配置文件读取并哈希后存储

        Args:
            db_session: 数据库会话

        Returns:
            是否成功
        """
        try:
            # 检查是否已存在
            existing_hash = await self.get_admin_password_hash(db_session)

            if existing_hash:
                logger.info("管理员密码已存在，跳过初始化")
                return True

            # 从配置读取密码
            admin_password = settings.admin_password

            if not admin_password or admin_password == "admin123":
                logger.warning("使用默认密码，建议修改！")

            # 哈希密码
            password_hash = self.hash_password(admin_password)

            # 存储到数据库
            success = await self.set_admin_password_hash(password_hash, db_session)

            if success:
                logger.info("管理员密码初始化成功")
            else:
                logger.error("管理员密码初始化失败")

            return success

        except Exception as e:
            logger.error(f"初始化管理员密码失败: {e}")
            return False

    async def verify_admin_login(
        self,
        password: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        验证管理员登录

        Args:
            password: 密码
            db_session: 数据库会话

        Returns:
            结果字典，包含 success, message, error
        """
        try:
            # 获取密码哈希
            password_hash = await self.get_admin_password_hash(db_session)

            if not password_hash:
                # 尝试初始化
                await self.initialize_admin_password(db_session)
                password_hash = await self.get_admin_password_hash(db_session)

                if not password_hash:
                    return {
                        "success": False,
                        "message": None,
                        "error": "系统错误：无法获取管理员密码"
                    }

            # 验证密码
            match_path = self._verify_password_detailed(password, password_hash)
            if match_path:
                if match_path == "legacy":
                    # 旧版本哈希被本次登录识别出来了，借这次成功登录顺手
                    # 升级到新的 SHA256 预处理哈希；失败也不影响本次登录。
                    try:
                        new_hash = self.hash_password(password)
                        await self.set_admin_password_hash(new_hash, db_session)
                        logger.info("检测到旧版 bcrypt 哈希，已在本次登录成功后升级为新格式")
                    except Exception as e:
                        logger.warning(f"升级管理员密码哈希失败（不影响本次登录）: {e}")
                logger.info("管理员登录成功")
                return {
                    "success": True,
                    "message": "登录成功",
                    "error": None
                }
            else:
                logger.warning("管理员登录失败：密码错误")
                return {
                    "success": False,
                    "message": None,
                    "error": "密码错误"
                }

        except Exception as e:
            logger.error(f"验证管理员登录失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"登录失败: {str(e)}"
            }

    async def change_admin_password(
        self,
        old_password: str,
        new_password: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        修改管理员密码

        Args:
            old_password: 旧密码
            new_password: 新密码
            db_session: 数据库会话

        Returns:
            结果字典，包含 success, message, error
        """
        try:
            # 验证旧密码
            verify_result = await self.verify_admin_login(old_password, db_session)

            if not verify_result["success"]:
                return {
                    "success": False,
                    "message": None,
                    "error": "旧密码错误"
                }

            # 哈希新密码
            new_password_hash = self.hash_password(new_password)

            # 更新密码
            success = await self.set_admin_password_hash(new_password_hash, db_session)

            if success:
                logger.info("管理员密码修改成功")
                return {
                    "success": True,
                    "message": "密码修改成功",
                    "error": None
                }
            else:
                return {
                    "success": False,
                    "message": None,
                    "error": "密码修改失败"
                }

        except Exception as e:
            logger.error(f"修改管理员密码失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"密码修改失败: {str(e)}"
            }


# 创建全局实例
auth_service = AuthService()
