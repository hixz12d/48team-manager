"""Console APIs. List queries stay local; action endpoints may call integrations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import console_actions
from app.application.commands.workspaces import RegisterWorkspaceError, complete_workspace_oauth, start_workspace_oauth
from app.application.connection_probe import probe_hme, probe_mail, probe_sub2api
from app.application.queries import console as console_query
from app.application.queries.identity import identity_audit_query
from app.application.resources.phones import phone_pool_service
from app.application.resources.proxies import proxy_profile_service
from app.application.resources.proxy_probe import proxy_probe_service
from app.application.settings import save_console_settings
from app.application.workspace_sync import workspace_sync_service
from app.core.proxy import normalize_proxy_url
from app.core.time import utcnow
from app.persistence.models.resources import ProxyProfile
from app.web.deps import require_admin
from app.web.schemas.resources import (
    AccountProxyPatch,
    KickRequest,
    OnboardRequest,
    OperationArchiveRequest,
    OperationBulkArchiveRequest,
    PhoneImportRequest,
    PhoneStatusPatch,
    ProxyCreateRequest,
    ProxyPatchRequest,
    RevokeInviteRequest,
    RotateRequest,
    Sub2ApiPushRequest,
    WorkspaceAddChildRequest,
    WorkspaceLinkMemberRequest,
    WorkspaceNamePatch,
    WorkspacePurgeChildRequest,
    WorkspaceRemoveChildRequest,
)
from app.web.schemas.settings import ConnectionProbeRequest, SettingsPatch
from app.web.schemas.workspaces import CompleteAccountOAuthRequest, CompleteWorkspaceOAuthRequest, StartWorkspaceOAuthRequest


def _accepted(payload: dict) -> dict:
    return payload


def build_api_router(get_db) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/overview")
    async def overview(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.overview(db)

    @router.get("/workspaces")
    async def workspaces(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.workspaces(db)

    @router.post("/workspaces/oauth/start")
    async def start_workspace_oauth_route(
        payload: StartWorkspaceOAuthRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        try:
            return await start_workspace_oauth(db, email=payload.email, proxy=payload.proxy)
        except RegisterWorkspaceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.post("/workspaces/oauth/complete")
    async def complete_workspace_oauth_route(
        payload: CompleteWorkspaceOAuthRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        try:
            return await complete_workspace_oauth(db, ticket=payload.ticket, callback_url=payload.callback_url)
        except RegisterWorkspaceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.post("/workspaces/{workspace_id}/sync")
    async def sync_workspace(
        workspace_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await workspace_sync_service.sync_workspace(db, workspace_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.get("error") or "not found")
        return _accepted(result)


    @router.post("/workspaces/{workspace_id}/onboard", status_code=status.HTTP_202_ACCEPTED)
    async def onboard_workspace(
        workspace_id: int,
        payload: OnboardRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.start_workspace_onboard(
            db,
            workspace_id,
            email_line=payload.email_line,
            phone_line=payload.phone_line,
            proxy=payload.proxy,
            password=payload.password,
            force=payload.force,
            skip_invite=payload.skip_invite,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/workspaces/{workspace_id}/rotate", status_code=status.HTTP_202_ACCEPTED)
    async def rotate_workspace_member(
        workspace_id: int,
        payload: RotateRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.start_controlled_rotate(
            db,
            workspace_id,
            email=payload.email,
            email_line=payload.email_line,
            phone_line=payload.phone_line,
            proxy=payload.proxy,
            force_refill=payload.force_refill,
            reason=payload.reason,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/workspaces/{workspace_id}/kick", status_code=status.HTTP_202_ACCEPTED)
    async def kick_workspace_member(
        workspace_id: int,
        payload: KickRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.kick_member_to_standby(
            db,
            workspace_id,
            email=payload.email,
            user_id=payload.user_id,
            reason=payload.reason,
            unbind_sub2api=payload.unbind_sub2api,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/workspaces/{workspace_id}/revoke-invite", status_code=status.HTTP_202_ACCEPTED)
    async def revoke_workspace_invite_route(
        workspace_id: int,
        payload: RevokeInviteRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.revoke_workspace_invite(db, workspace_id, email=payload.email)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/workspaces/{workspace_id}/members/purge", status_code=status.HTTP_202_ACCEPTED)
    async def purge_workspace_child(
        workspace_id: int,
        payload: WorkspacePurgeChildRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.purge_workspace_child(
            db,
            workspace_id,
            email=payload.email,
            user_id=payload.user_id,
            reason=payload.reason,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if result.get("error_code") == "not_linkable":
            raise HTTPException(status_code=400, detail=result.get("error") or "not linkable")
        return _accepted(result)

    @router.patch("/workspaces/{workspace_id}/name")
    async def patch_workspace_name(
        workspace_id: int,
        payload: WorkspaceNamePatch,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.update_workspace_display_name(db, workspace_id, payload.custom_name)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "name update failed")
        return result

    @router.post("/workspaces/{workspace_id}/sync-name")
    async def sync_workspace_name(
        workspace_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.sync_workspace_official_name(db, workspace_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return result

    @router.post("/workspaces/{workspace_id}/members/link")
    async def link_workspace_member(
        workspace_id: int,
        payload: WorkspaceLinkMemberRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.link_remote_only_member(
            db,
            workspace_id,
            email=payload.email,
            account_id=payload.account_id,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "link failed")
        return result

    @router.post("/workspaces/{workspace_id}/members/add")
    async def add_workspace_child(
        workspace_id: int,
        payload: WorkspaceAddChildRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.add_local_child(db, workspace_id, email=payload.email)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "add failed")
        return result

    @router.post("/workspaces/{workspace_id}/members/remove")
    async def remove_workspace_child(
        workspace_id: int,
        payload: WorkspaceRemoveChildRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.remove_local_child(
            db,
            workspace_id,
            email=payload.email,
            account_id=payload.account_id,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "remove failed")
        return result

    @router.post("/workspaces/repair-names")
    async def repair_workspace_names_route(
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        return await console_actions.repair_workspace_names(db)

    @router.get("/accounts")
    async def accounts(
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        purpose: str = Query("all"),
        include_archived: bool = Query(False),
    ) -> dict:
        return await console_query.accounts(db, purpose=purpose, include_archived=include_archived)

    @router.get("/accounts/portfolio")
    async def accounts_portfolio(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.portfolio(db)

    @router.post("/accounts/{account_id}/refresh")
    async def refresh_account(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_refresh(db, account_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/accounts/{account_id}/auth/probe")
    async def probe_account_auth(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_auth_probe(db, account_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/accounts/{account_id}/quota/probe")
    async def probe_account_quota(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        workspace_id: int | None = Query(default=None),
    ) -> dict:
        result = await console_actions.account_quota_probe(db, account_id, workspace_id=workspace_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if result.get("error_code") == "ambiguous_workspace_context":
            raise HTTPException(status_code=400, detail=result.get("error") or "ambiguous workspace")
        return _accepted(result)

    @router.post("/workspaces/{workspace_id}/accounts/{account_id}/quota/probe")
    async def probe_workspace_account_quota(
        workspace_id: int,
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_quota_probe(db, account_id, workspace_id=workspace_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/accounts/{account_id}/reauth")
    async def reauth_account(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_reauth(db, account_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return result

    @router.post("/accounts/{account_id}/reauth/complete")
    async def complete_account_reauth(
        account_id: int,
        payload: CompleteAccountOAuthRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_reauth_complete(
            db,
            account_id,
            ticket=payload.ticket,
            callback_url=payload.callback_url,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "reauth failed")
        return result

    @router.post("/accounts/{account_id}/sub2api/sync")
    async def sync_account_sub2api(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        # Compatibility alias: old sync is reconcile-only.
        result = await console_actions.account_sub2api_reconcile(db, account_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/accounts/{account_id}/sub2api/reconcile")
    async def reconcile_account_sub2api(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_sub2api_reconcile(db, account_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/accounts/{account_id}/sub2api/push")
    async def push_account_sub2api(
        account_id: int,
        payload: Sub2ApiPushRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        workspace_id: int | None = Query(default=None),
    ) -> dict:
        result = await console_actions.account_sub2api_push(
            db,
            account_id,
            group_ids=payload.group_ids,
            name=payload.name,
            schedulable=payload.schedulable,
            confirm_mixed_channel_risk=payload.confirm_mixed_channel_risk,
            workspace_id=workspace_id,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if result.get("error_code") in {"not_eligible", "ambiguous_workspace_context"}:
            raise HTTPException(status_code=400, detail=result.get("error") or "not eligible")
        return _accepted(result)

    @router.post("/workspaces/{workspace_id}/accounts/{account_id}/sub2api/push")
    async def push_workspace_account_sub2api(
        workspace_id: int,
        account_id: int,
        payload: Sub2ApiPushRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_sub2api_push(
            db,
            account_id,
            group_ids=payload.group_ids,
            name=payload.name,
            schedulable=payload.schedulable,
            confirm_mixed_channel_risk=payload.confirm_mixed_channel_risk,
            workspace_id=workspace_id,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if result.get("error_code") in {"not_eligible", "ambiguous_workspace_context"}:
            raise HTTPException(status_code=400, detail=result.get("error") or "not eligible")
        return _accepted(result)


    @router.patch("/accounts/{account_id}/proxy")
    async def patch_account_proxy(
        account_id: int,
        payload: AccountProxyPatch,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.update_account_proxy(
            db,
            account_id,
            proxy=payload.proxy,
            proxy_profile_id=payload.proxy_profile_id,
            clear=payload.clear,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "proxy update failed")
        return result

    @router.get("/identity/audit")
    async def identity_audit(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await identity_audit_query(db)

    @router.get("/operations")
    async def operations(
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        q: str = Query(""),
        state: str = Query(""),
        type: str = Query(""),
        source: str = Query(""),
        workspace_id: int | None = Query(None),
        account_id: int | None = Query(None),
        date_from: str | None = Query(None),
        date_to: str | None = Query(None),
        include_archived: bool = Query(False),
        archived_only: bool = Query(False),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
    ) -> dict:
        return await console_query.operations(
            db,
            q=q,
            state=state,
            op_type=type,
            source=source,
            workspace_id=workspace_id,
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            include_archived=include_archived,
            archived_only=archived_only,
            page=page,
            page_size=page_size,
        )

    @router.get("/operations/{public_id}")
    async def operation_detail(
        public_id: str,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        payload = await console_actions.get_operation_detail(db, public_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="operation not found")
        return payload

    @router.post("/operations/{public_id}/cancel")
    async def operation_cancel(
        public_id: str,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.request_operation_cancel(db, public_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "cancel failed")
        return result

    @router.post("/operations/{public_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    async def operation_retry(
        public_id: str,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.retry_operation(db, public_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "retry forbidden")
        return _accepted(result)

    @router.patch("/operations/{public_id}/archive")
    async def operation_archive(
        public_id: str,
        payload: OperationArchiveRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.archive_operation(db, public_id, reason=payload.reason)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "archive failed")
        return result

    @router.patch("/operations/{public_id}/restore")
    async def operation_restore(
        public_id: str,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.restore_operation(db, public_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return result

    @router.post("/operations/bulk-archive")
    async def operation_bulk_archive(
        payload: OperationBulkArchiveRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        return await console_actions.bulk_archive_operations(
            db,
            payload.public_ids,
            reason=payload.reason,
            only_terminal=payload.only_terminal,
        )

    @router.get("/resources/phones")
    async def phones(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.phones(db)

    @router.post("/resources/phones/import")
    async def import_phones(
        payload: PhoneImportRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await phone_pool_service.import_lines(db, payload.text)
        return {"ok": True, **result}


    @router.patch("/resources/phones/{phone_id}")
    async def patch_phone(
        phone_id: int,
        payload: PhoneStatusPatch,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.set_phone_status(db, phone_id, payload.status)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "phone update failed")
        return result

    @router.post("/resources/phones/{phone_id}/reset-cooldown")
    async def reset_phone_cooldown(
        phone_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.reset_phone_cooldown(db, phone_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return result

    @router.get("/resources/hme")
    async def hme(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.hme(db)

    @router.post("/resources/hme/reconcile")
    async def hme_reconcile(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return _accepted(await console_actions.run_hme_reconcile(db))

    @router.post("/resources/hme/{lease_id}/retry-label")
    async def hme_retry_label(
        lease_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.retry_hme_label(db, lease_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/resources/hme/{lease_id}/release")
    async def hme_release(
        lease_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.release_hme_lease_safe(db, lease_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "release forbidden")
        return result

    @router.get("/resources/proxies")
    async def proxies(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.proxies(db)


    @router.get("/resources/proxies/{proxy_id}/bindings")
    async def proxy_bindings(
        proxy_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.proxy_bindings(db, proxy_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return result

    @router.post("/resources/proxies")
    async def create_proxy(
        payload: ProxyCreateRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        try:
            url = normalize_proxy_url(payload.url)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if not url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="代理地址不能为空")
        name = str(payload.name or "").strip()
        profile = await proxy_profile_service.upsert_from_url(
            db,
            url,
            name=name,
            name_source="user" if name else "auto",
        )
        await db.commit()
        return {"ok": True, "item": proxy_profile_service.serialize(profile)}

    @router.patch("/resources/proxies/{proxy_id}")
    async def patch_proxy(
        proxy_id: int,
        payload: ProxyPatchRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        profile = await db.get(ProxyProfile, int(proxy_id))
        if profile is None:
            raise HTTPException(status_code=404, detail="proxy not found")
        if payload.name is not None or payload.restore_auto_name:
            await proxy_profile_service.rename(
                db,
                profile,
                name=payload.name,
                restore_auto_name=bool(payload.restore_auto_name) or (payload.name is not None and not str(payload.name).strip()),
            )
        if payload.status is not None:
            profile.status = payload.status
        profile.updated_at = utcnow()
        await db.commit()
        return {"ok": True, "item": proxy_profile_service.serialize(profile)}

    @router.post("/resources/proxies/{proxy_id}/probe")
    async def probe_proxy(
        proxy_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await proxy_probe_service.probe_profile(db, proxy_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.post("/resources/proxies/probe-all")
    async def probe_all_proxies(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return _accepted(await proxy_probe_service.probe_all(db))

    @router.post("/resources/proxies/repair")
    async def repair_proxies(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        linked = await console_actions.repair_proxy_profiles_from_accounts(db)
        names = await proxy_profile_service.repair_legacy_names(db)
        await db.commit()
        return {
            "ok": True,
            "linked": linked.get("linked", 0),
            "renamed": names.get("changed", 0),
            "skipped": names.get("skipped", 0),
        }

    @router.get("/settings")
    async def settings_view(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.settings_view(db)

    @router.patch("/settings")
    async def settings_update(
        payload: SettingsPatch,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        try:
            return await save_console_settings(db, payload)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post("/settings/probe")
    async def settings_probe(
        payload: ConnectionProbeRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        body = payload.connections.model_dump() if payload.connections is not None else {}
        target = payload.target or "all"
        empty = {"ok": None, "skipped": True}
        sub2api = await probe_sub2api(db, body) if target in {"all", "sub2api"} else empty
        hme = await probe_hme(db, body) if target in {"all", "hme"} else empty
        mail = await probe_mail(db, body) if target in {"all", "mail"} else empty
        checked = [item for item in (sub2api, hme, mail) if not item.get("skipped")]
        return {
            "ok": all(bool(item.get("ok")) for item in checked) if checked else False,
            "target": target,
            "sub2api": sub2api,
            "hme": hme,
            "mail": mail,
        }

    return router
