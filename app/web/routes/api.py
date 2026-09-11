"""Console APIs. List queries stay local; action endpoints may call integrations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from app.application.codex_export import CodexTransferError, export_document
from app.web.schemas.accounts import CodexTransferRequest, CodexPushRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import console_actions
from app.application.commands.workspaces import RegisterWorkspaceError, complete_workspace_oauth, start_workspace_oauth
from app.application.connection_probe import probe_hme, probe_mail, probe_sub2api
from app.application.mailbox import probe_account_mailbox
from app.application.operations import operation_store
from app.application.reauth import reauth_service
from app.application.queries import console as console_query
from app.application.queries.identity import identity_audit_query
from app.application.resources.phones import phone_pool_service
from app.application.sub2api_proxy_catalog import sub2api_proxy_catalog
from app.application.settings import save_console_settings
from app.application.workspace_sync import workspace_sync_service
from app.application.sub2api_usage import sub2api_usage_service
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account
from app.web.deps import require_admin
from app.web.schemas.accounts import AccountPhonePatch, DeleteAccountRequest, DeleteAccountsRequest, RegisterAccountRequest
from app.web.schemas.resources import (
    AccountProxyPatch,
    AccountAutomationPatch,
    KickRequest,
    OnboardRequest,
    ReplenishRequest,
    OperationArchiveRequest,
    OperationBulkArchiveRequest,
    PhoneImportRequest,
    PhoneStatusPatch,
    RevokeInviteRequest,
    RotateRequest,
    Sub2ApiPushRequest,
    Sub2ApiUsageSyncRequest,
    WorkspaceAddChildRequest,
    WorkspaceLinkMemberRequest,
    WorkspaceMemberRolePatch,
    WorkspaceNamePatch,
    WorkspacePurgeChildRequest,
    WorkspaceRemoveChildRequest,
)
from app.web.schemas.settings import ConnectionProbeRequest, SettingsPatch
from app.web.schemas.workspaces import CompleteAccountOAuthRequest, CompleteWorkspaceOAuthRequest, StartWorkspaceOAuthRequest


def _accepted(payload: dict) -> dict:
    return payload


def _error_detail(payload: dict, fallback: str) -> dict[str, str]:
    return {
        "message": str(payload.get("error") or fallback),
        "error_code": str(payload.get("error_code") or "request_failed"),
    }


def build_api_router(get_db) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/runtime/status")
    async def runtime_status(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        from app.application.queries.runtime_status import runtime_status as query_runtime_status
        return await query_runtime_status(db)

    @router.get("/accounts/codex/status")
    async def codex_status(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
        from app.application.codex_publish import binding_status
        return JSONResponse(await binding_status(db), headers={"Cache-Control": "no-store"})

    @router.post("/accounts/codex/push")
    async def push_codex_accounts(payload: CodexPushRequest, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
        from app.application.codex_publish import push_accounts
        try:
            result = await push_accounts(db, payload.account_ids, expected_target=payload.expected_target)
        except CodexTransferError as exc:
            raise HTTPException(status_code=exc.status, detail={"message": str(exc), "error_code": exc.code}) from None
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/accounts/codex/export")
    async def export_codex_accounts(
        payload: CodexTransferRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ):
        try:
            document = await export_document(db, payload.account_ids)
        except CodexTransferError as exc:
            raise HTTPException(status_code=exc.status, detail={"message": str(exc), "error_code": exc.code}) from None
        return JSONResponse(document, headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Disposition": 'attachment; filename="team48-codex-at-only.json"',
            "X-Content-Type-Options": "nosniff",
        })

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
            return await start_workspace_oauth(
                db,
                email=payload.email,
                proxy=payload.proxy,
                proxy_selection=payload.proxy_selection.model_dump() if payload.proxy_selection else None,
            )
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

    @router.post("/workspaces/sync", status_code=status.HTTP_202_ACCEPTED)
    async def sync_all_workspaces(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        from app.application.jobs.workspace_sync import enqueue_all_workspace_syncs
        return await enqueue_all_workspace_syncs(db)

    @router.post("/workspaces/{workspace_id}/sync", status_code=status.HTTP_202_ACCEPTED)
    async def sync_workspace(
        workspace_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        from app.application.jobs.workspace_sync import enqueue_workspace_sync
        result = await enqueue_workspace_sync(db, workspace_id)
        if result.get("error_code") == "credentials_missing":
            raise HTTPException(status_code=400, detail=_error_detail(result, "credentials missing"))
        if result.get("error_code") == "workspace_unavailable":
            raise HTTPException(status_code=409, detail=_error_detail(result, "workspace unavailable"))
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
            proxy_selection=payload.proxy_selection.model_dump() if payload.proxy_selection else None,
            password=payload.password,
            oauth_signup=True,
            force=payload.force,
            skip_invite=payload.skip_invite,
            role=payload.role,
            seat_intent=payload.seat_intent,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if result.get("error_code") in {"proxy_choice_conflict", "invalid_proxy_source", "invalid_remote_id", "proxy_not_found", "proxy_disabled", "proxy_unresolvable"}:
            raise HTTPException(status_code=400, detail=result.get("error") or "proxy selection invalid")
        if result.get("error_code") == "remote_catalog_unavailable":
            raise HTTPException(status_code=502, detail=result.get("error") or "proxy catalog unavailable")
        return _accepted(result)

    @router.post("/workspaces/{workspace_id}/replenish", status_code=status.HTTP_202_ACCEPTED)
    async def replenish_workspace(
        workspace_id: int,
        payload: ReplenishRequest | None = None,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        body = payload or ReplenishRequest()
        result = await console_actions.start_workspace_replenish(
            db,
            workspace_id,
            role=body.role,
            seat_intent=body.seat_intent,
            phone_line=body.phone_line,
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
            role=payload.role,
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
        if result.get("error_code") in {"not_found", "account_not_found"}:
            raise HTTPException(status_code=404, detail=_error_detail(result, "not found"))
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=_error_detail(result, "link failed"))
        return result

    @router.post("/workspaces/{workspace_id}/members/add")
    async def add_workspace_child(
        workspace_id: int,
        payload: WorkspaceAddChildRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.invite_workspace_child(db, workspace_id, email=payload.email, role=payload.role, seat_intent=payload.seat_intent)
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

    @router.patch("/workspaces/{workspace_id}/members/role")
    async def patch_workspace_member_role(
        workspace_id: int,
        payload: WorkspaceMemberRolePatch,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.update_workspace_member_role(
            db,
            workspace_id,
            email=payload.email,
            role=payload.role,
            user_id=payload.user_id,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "role update failed")
        return result

    @router.delete("/workspaces/{workspace_id}")
    async def delete_workspace(
        workspace_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.delete_local_workspace(db, workspace_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "delete failed")
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

    @router.post("/accounts", status_code=201)
    async def register_account(payload: RegisterAccountRequest, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        result = await console_actions.register_local_account(db, email=payload.email, purpose=payload.purpose)
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=_error_detail(result, "account exists"))
        return result

    @router.delete("/accounts/{account_id}")
    async def delete_account(account_id: int, payload: DeleteAccountRequest, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        from app.application.account_deletion import AccountDeletionError, delete_unassigned_account
        try:
            result = await delete_unassigned_account(db, account_id)
            await db.commit()
            return result
        except AccountDeletionError as exc:
            await db.rollback()
            raise HTTPException(status_code=exc.status, detail={"message": str(exc), "error_code": exc.code}) from exc
        except Exception:
            await db.rollback()
            raise

    @router.post("/accounts/delete-local")
    async def delete_accounts(payload: DeleteAccountsRequest, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        from app.application.account_deletion import AccountDeletionError, delete_unassigned_accounts
        try:
            return await delete_unassigned_accounts(db, payload.account_ids)
        except AccountDeletionError as exc:
            await db.rollback()
            raise HTTPException(status_code=exc.status, detail={"message": str(exc), "error_code": exc.code}) from exc
        except Exception:
            await db.rollback()
            raise

    @router.get("/accounts/portfolio")
    async def accounts_portfolio(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.portfolio(db)

    @router.get("/quota/runtime")
    async def quota_runtime(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        from app.application.quota import quota_service
        return await quota_service.runtime_summary(db)

    @router.post("/accounts/probe-all", status_code=202)
    async def quota_probe_all(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        from app.application.quota import quota_service
        return await quota_service.enqueue_all(db)

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

    @router.post("/accounts/{account_id}/quota/probe", status_code=202)
    async def probe_account_quota(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        workspace_id: int | None = Query(default=None),
    ) -> dict:
        result = await console_actions.account_quota_probe(db, account_id, workspace_id=workspace_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if result.get("error_code") in {"ambiguous_workspace_context", "invalid_workspace_context", "account_disabled"}:
            raise HTTPException(status_code=400, detail=result.get("error") or "ambiguous workspace")
        return _accepted(result)

    @router.post("/workspaces/{workspace_id}/accounts/{account_id}/quota/probe", status_code=202)
    async def probe_workspace_account_quota(
        workspace_id: int,
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_quota_probe(db, account_id, workspace_id=workspace_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=_error_detail(result, "quota probe rejected"))
        return _accepted(result)

    @router.get("/accounts/{account_id}/reauth/readiness")
    async def account_reauth_readiness(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        account = await db.get(Account, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        settings = await reauth_service.load_settings(db)
        context = await reauth_service.execution_context(db, account)
        mailbox = context["mailbox"]
        proxy = context["proxy"]
        blocked = []
        if not account.auto_reauth_opt_in:
            blocked.append("account_not_opted_in")
        if not settings.get("deployment_allowed"):
            blocked.append("deployment_disabled")
        elif not settings.get("requested"):
            blocked.append("automation_not_requested")
        if not mailbox["effective_ready"]:
            blocked.append("mailbox_unverified")
        if not proxy["url"]:
            blocked.append("proxy_missing")
        active = await operation_store.active_for_email(db, account.email, actions=("reauth",))
        if active:
            blocked.append("already_running")
        return {
            "account_id": account.id,
            "eligible": not blocked,
            "effective_enabled": bool(settings.get("effective")) and not blocked,
            "blocked_reasons": blocked,
            "mailbox": mailbox,
            "proxy": {
                "source": proxy["source"],
                "origin": proxy["origin"],
                "remote_id": proxy["remote_id"],
                "resolution_state": "ready" if proxy["url"] else "missing",
            },
            "active_operation_id": active.public_id if active else None,
            "next_eligible_at": account.next_eligible_at.isoformat() if account.next_eligible_at else None,
        }

    @router.post("/accounts/{account_id}/mailbox/probe")
    async def account_mailbox_probe(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await probe_account_mailbox(db, account_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result)
        return result

    @router.patch("/accounts/{account_id}/phone")
    async def patch_account_phone(
        account_id: int,
        payload: AccountPhonePatch,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.bind_account_phone(db, account_id, payload.phone_line)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "phone update failed")
        return result

    @router.post("/accounts/{account_id}/reauth/immediate", status_code=status.HTTP_202_ACCEPTED)
    async def immediate_account_reauth(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.start_account_immediate_reauth(db, account_id)
        if result.get("error_code") in {"not_found", "account_not_found"}:
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        return _accepted(result)

    @router.patch("/accounts/{account_id}/automation")
    async def patch_account_automation(
        account_id: int,
        payload: AccountAutomationPatch,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        account = await db.get(Account, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        account.auto_reauth_opt_in = payload.auto_reauth_opt_in
        await db.commit()
        return {"ok": True, "account_id": account.id, "auto_reauth_opt_in": account.auto_reauth_opt_in}

    @router.post("/accounts/{account_id}/reauth/auto", status_code=status.HTTP_202_ACCEPTED)
    async def queue_account_auto_reauth(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        account = await db.get(Account, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        settings = await reauth_service.load_settings(db)
        if not settings.get("effective"):
            raise HTTPException(
                status_code=409,
                detail={"error_code": "automation_disabled", "blocked_reasons": settings.get("blocked_reasons", [])},
            )
        result = await reauth_service.start_auto_reauth(db, account)
        if result.get("error_code") == "already_running":
            return {
                "ok": True,
                "status": "queued",
                "operation_id": result.get("job_id"),
                "reused_existing": True,
                "message": "已有自动授权任务",
            }
        if not result.get("success"):
            raise HTTPException(status_code=409, detail=result)
        return {
            "ok": True,
            "status": "queued",
            "operation_id": result.get("job_id"),
            "reused_existing": False,
            "message": "已进入自动授权队列",
        }

    @router.post("/accounts/{account_id}/reauth")
    async def reauth_account(
        account_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await console_actions.account_reauth(db, account_id)
        if result.get("error_code") in {"not_found", "account_not_found"}:
            raise HTTPException(status_code=404, detail=_error_detail(result, "account not found"))
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
        if result.get("error_code") in {"not_found", "account_not_found"}:
            raise HTTPException(status_code=404, detail=_error_detail(result, "account not found"))
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=_error_detail(result, "reauth failed"))
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

    @router.post("/accounts/{account_id}/sub2api/preview")
    async def preview_account_sub2api_push(
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
            dry_run=True,
        )
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result.get("error") or "not found")
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=_error_detail(result, "preview failed"))
        return result

    @router.get("/sub2api/status")
    async def sub2api_status(
        _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
    ) -> dict:
        return await sub2api_usage_service.status(db)

    @router.get("/sub2api/capabilities")
    async def sub2api_capabilities(
        _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
    ) -> dict:
        return await sub2api_client.integration_capabilities(db)


    @router.post("/sub2api/usage/sync")
    async def sync_all_sub2api_usage(
        payload: Sub2ApiUsageSyncRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        return _accepted(
            await sub2api_usage_service.sync(db, force_usage=payload.force_usage)
        )

    @router.post("/workspaces/{workspace_id}/sub2api/usage/sync")
    async def sync_workspace_sub2api_usage(
        workspace_id: int,
        payload: Sub2ApiUsageSyncRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        return _accepted(
            await sub2api_usage_service.sync(
                db, workspace_id=workspace_id, force_usage=payload.force_usage
            )
        )

    @router.post("/accounts/{account_id}/sub2api/usage/sync")
    async def sync_account_sub2api_usage(
        account_id: int,
        payload: Sub2ApiUsageSyncRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        workspace_id: int | None = Query(default=None),
    ) -> dict:
        return _accepted(
            await sub2api_usage_service.sync(
                db,
                account_id=account_id,
                workspace_id=workspace_id,
                force_usage=payload.force_usage,
            )
        )


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
            proxy_selection=payload.proxy_selection.model_dump() if payload.proxy_selection else None,
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
    async def proxies(
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        q: str = Query(default="", max_length=120),
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict:
        try:
            return await sub2api_proxy_catalog.list(db, q=q, cursor=cursor, limit=limit)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "无法读取 Sub2API 代理目录",
                    "error_code": "remote_catalog_unavailable",
                },
            ) from exc

    @router.post("/resources/proxies/{remote_id}/probe")
    async def probe_proxy(
        remote_id: int,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await sub2api_proxy_catalog.probe(db, remote_id)
        if result.get("error_code") == "not_found":
            raise HTTPException(status_code=404, detail=result)
        if result.get("error_code") == "invalid_remote_id":
            raise HTTPException(status_code=400, detail=result)
        return result

    @router.get("/runtime/reauth")
    async def reauth_runtime(_: dict = Depends(require_admin)) -> dict:
        from app.application.jobs.dispatcher import reauth_dispatcher

        return await reauth_dispatcher.summary()

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
