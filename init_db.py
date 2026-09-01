"""
数据库初始化脚本
创建所有表并插入默认数据
"""
import asyncio
import bcrypt
from sqlalchemy import select
from app.database import init_db, AsyncSessionLocal
from app.models import Setting
from app.config import settings


async def create_default_settings():
    """创建默认系统设置"""
    async with AsyncSessionLocal() as session:
        # 检查是否已经初始化
        result = await session.execute(select(Setting).where(Setting.key == "initialized"))
        existing = result.scalar_one_or_none()

        if existing:
            print("数据库已经初始化,跳过默认数据插入")
            return

        # 生成管理员密码哈希
        password_hash = bcrypt.hashpw(
            settings.admin_password.encode('utf-8'),
            bcrypt.gensalt()
        ).decode('utf-8')

        # 默认设置
        default_settings = [
            Setting(
                key="initialized",
                value="true",
                description="数据库初始化标记"
            ),
            Setting(
                key="admin_password_hash",
                value=password_hash,
                description="管理员密码哈希"
            ),
            Setting(
                key="proxy",
                value=settings.proxy,
                description="代理地址 (支持 http://、https://、socks5:// 和 socks5h://)"
            ),
            Setting(
                key="proxy_enabled",
                value=str(settings.proxy_enabled).lower(),
                description="是否启用代理"
            ),
            Setting(
                key="log_level",
                value=settings.log_level,
                description="日志级别"
            ),
            Setting(
                key="default_team_max_members",
                value="6",
                description="新导入 Team 的默认总席位"
            ),
            Setting(
                key="sub2api_base_url",
                value="http://sub2api-canary:8080",
                description="Sub2API 地址。同机容器直连 sub2api-canary:8080",
            ),
            Setting(
                key="sub2api_api_key",
                value="",
                description="Sub2API Admin API Key"
            ),
            Setting(
                key="sub2api_admin_email",
                value="",
                description="Sub2API 后台邮箱，只读状态可选用"
            ),
            Setting(
                key="sub2api_admin_password",
                value="",
                description="Sub2API 后台密码，状态读取和推送可选用"
            ),
            Setting(
                key="sub2api_group_ids",
                value="",
                description="Sub2API 分组 ID，逗号分隔；模板没带分组时才用"
            ),
            Setting(
                key="sub2api_template_name",
                value="Team轮转",
                description="推子号时套用的 Sub2API 账号创建模板名"
            ),
            Setting(
                key="sub2api_free_template_name",
                value="Free模板",
                description="推免费号时套用的 Sub2API 账号创建模板名"
            ),
            Setting(
                key="free_account_proxy",
                value="",
                description="免费号默认静态 ISP"
            ),
        ]

        session.add_all(default_settings)
        await session.commit()
        print("默认设置已创建")


async def main():
    """主函数"""
    print("开始初始化数据库...")

    # 创建所有表
    await init_db()
    print("数据库表创建完成")

    # 插入默认数据
    await create_default_settings()

    print("数据库初始化完成!")


if __name__ == "__main__":
    asyncio.run(main())
