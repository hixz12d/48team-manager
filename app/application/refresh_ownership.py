"""One durable ownership decision shared by refresh and automatic OAuth paths."""
from app.persistence.models.sub2api import Sub2ApiRefreshAuthority
from app.persistence.models.refresh_handoff import Sub2ApiRefreshHandoff


async def returned_to_team(db, account_id):
    owner = await db.get(Sub2ApiRefreshAuthority, account_id, populate_existing=True)
    handoff = await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True)
    return bool(owner and handoff and handoff.state == "completed" and handoff.authority_epoch == owner.epoch)


async def remote_refresh_owner(db, account_id):
    owner = await db.get(Sub2ApiRefreshAuthority, account_id, populate_existing=True)
    if owner is None:
        return None
    handoff = await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True)
    if handoff and handoff.state == "completed" and handoff.authority_epoch == owner.epoch:
        return None
    return owner


async def codex_refresh_owner(db, account_id):
    """codex-rs owns refresh once an import was attempted: the RT may already be there."""
    from app.persistence.models.codex import CodexBinding
    binding = await db.get(CodexBinding, account_id, populate_existing=True)
    if binding is None or not binding.import_attempted:
        return None
    return binding


async def refresh_owner_kind(db, account_id):
    """Return "sub2api", "codex_rs", or None when Team48 refreshes locally."""
    if await remote_refresh_owner(db, account_id) is not None:
        return "sub2api"
    if await codex_refresh_owner(db, account_id) is not None:
        return "codex_rs"
    return None


async def remote_accepts_access_token_only(db, remote_id):
    from sqlalchemy import select
    from app.persistence.models.identity import ExternalBinding
    found = await db.scalar(select(Sub2ApiRefreshHandoff.account_id).join(
        Sub2ApiRefreshAuthority, Sub2ApiRefreshAuthority.account_id == Sub2ApiRefreshHandoff.account_id).join(
        ExternalBinding, ExternalBinding.local_account_id == Sub2ApiRefreshHandoff.account_id).where(
        ExternalBinding.provider == "sub2api", ExternalBinding.remote_account_id == str(remote_id),
        Sub2ApiRefreshHandoff.state == "completed", Sub2ApiRefreshHandoff.authority_epoch == Sub2ApiRefreshAuthority.epoch).limit(1))
    return type(found) is int
