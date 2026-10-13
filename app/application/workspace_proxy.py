"""插件接入时冻结所选团队母号代理；不切换已托管账号的出口。"""
from sqlalchemy import select, update

from app.core.proxy import normalize_proxy_url
from app.core.time import utcnow
from app.integrations.openai.chatgpt import ChatGPTClient
from app.persistence.models.identity import Account, ExternalBinding
from app.persistence.models.codex import CodexBinding
from app.persistence.models.oauth import OAuthSession
from app.persistence.models.quota import CredentialLease

POLICY = "workspace_owner"


class WorkspaceProxyError(ValueError):
    pass


async def bind_workspace_proxy(db, account, workspace):
    # 与导入、刷新和创建授权会话共用 SQLite 写锁，避免进行中换出口。
    await db.execute(update(Account).where(Account.id == account.id).values(version=Account.version))
    await db.refresh(account)
    binding = await db.get(CodexBinding, account.id, populate_existing=True)
    external = await db.scalar(select(ExternalBinding.id).where(
        ExternalBinding.local_account_id == account.id,
        ExternalBinding.binding_state.not_in(("missing", "orphaned"))).limit(1))
    from app.application.refresh_ownership import remote_refresh_owner
    if binding or external or await remote_refresh_owner(db, account.id):
        raise WorkspaceProxyError("该号已有上游绑定，请在账号管理中核对代理并重新授权；插件不会切换已有账号的出口")
    lease = await db.get(CredentialLease, account.id)
    if lease and lease.token:
        raise WorkspaceProxyError("该号正在更新凭据，请完成后再接入")
    if await db.scalar(select(OAuthSession.id).where(
            OAuthSession.account_id == account.id, OAuthSession.status.in_(("waiting", "exchanging")),
            OAuthSession.expires_at > utcnow()).limit(1)):
        raise WorkspaceProxyError("该号已有未完成的授权，请完成或等待过期后再接入")
    owner = await db.get(Account, workspace.owner_account_id, populate_existing=True) if workspace.owner_account_id else None
    try:
        proxy = normalize_proxy_url(owner.proxy if owner else None)
    except ValueError:
        raise WorkspaceProxyError("团队母号代理配置无效，请先在管理台修正") from None
    if not proxy:
        raise WorkspaceProxyError("团队母号没有配置代理，请先配置后再接入；未使用服务器直连")
    current = normalize_proxy_url(account.proxy)
    if current and current != proxy:
        raise WorkspaceProxyError("子号已有不同代理，请先在管理台核对；插件不会覆盖现有代理")
    account.proxy = proxy
    account.proxy_source = POLICY
    account.proxy_profile_id = owner.proxy_profile_id
    account.sub2api_proxy_id = owner.sub2api_proxy_id
    account.proxy_instance_key = owner.proxy_instance_key
    await db.flush()


class WorkspaceProxyClient(ChatGPTClient):
    """换票使用授权开始时的代理快照，禁止失败后回退直连。"""
    def __init__(self, proxy):
        super().__init__()
        self.proxy = normalize_proxy_url(proxy)
        if not self.proxy:
            raise WorkspaceProxyError("授权会话缺少团队母号代理，请重新接入")

    async def _proxy_for(self, db_session, identifier):
        return self.proxy
