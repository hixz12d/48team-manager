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


async def remote_accepts_access_token_only(db, remote_id):
    from sqlalchemy import select
    from app.persistence.models.identity import ExternalBinding
    found = await db.scalar(select(Sub2ApiRefreshHandoff.account_id).join(
        Sub2ApiRefreshAuthority, Sub2ApiRefreshAuthority.account_id == Sub2ApiRefreshHandoff.account_id).join(
        ExternalBinding, ExternalBinding.local_account_id == Sub2ApiRefreshHandoff.account_id).where(
        ExternalBinding.provider == "sub2api", ExternalBinding.remote_account_id == str(remote_id),
        Sub2ApiRefreshHandoff.state == "completed", Sub2ApiRefreshHandoff.authority_epoch == Sub2ApiRefreshAuthority.epoch).limit(1))
    return type(found) is int
