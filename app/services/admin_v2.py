"""Phase 8：/admin/v2 数据组装。

独立于旧后台。前端只 patch 单个 entity；长任务来自 operations 表。
不写 Token，不开第 3 层踢拉。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    ExternalBinding,
    HmeAliasLease,
    PhoneAttempt,
    ProxyProfile,
    QuotaSnapshot,
    Workspace,
    WorkspaceMembership,
)
from app.services.auto_rotate import (
    DEFAULT_AUTO_REAUTH_ENABLED,
    DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES,
    DEFAULT_AUTO_ROTATE_ENABLED,
    auto_rotate_service,
)
from app.services.hme import HME_SETTING_ACCOUNT_ID, HME_SETTING_BASE_URL, HME_SETTING_SERVICE_TOKEN, load_config
from app.services.identity import AUDIT_CONFLICT, identity_service
from app.services.operations import ACTIVE_STATES, operation_store
from app.services.phone_pool import phone_pool_service
from app.services.proxy_profiles import proxy_profile_service
from app.services.quota import (
    DEFAULT_QUOTA_PROBE_ENABLED,
    clamp_quota_probe_interval_minutes,
    clamp_quota_probe_stagger_minutes,
    quota_service,
    serialize_snapshot,
)
from app.services.settings import settings_service
from app.utils.proxy import mask_proxy_url
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

PATCHABLE_ACCOUNT_FIELDS = ("local_purpose", "operational_state")
UNGROUPED_WORKSPACE_ID = 0


class AccountVersionConflict(Exception):
    def __init__(self, account_id: int, current_version: int) -> None:
        super().__init__(f"account {account_id} version conflict")
        self.account_id = account_id
        self.current_version = current_version


def iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def mask_secret(value: Optional[str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 6:
        return "••••"
    return f"{text[:2]}••••{text[-2:]}"


def serialize_quota(row: Optional[QuotaSnapshot]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    payload = serialize_snapshot(row)
    payload["five_hour_reset_at"] = iso(row.five_hour_reset_at)
    payload["seven_day_reset_at"] = iso(row.seven_day_reset_at)
    payload["queried_at"] = iso(row.queried_at)
    return payload


def serialize_proxy(row: Optional[ProxyProfile]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    payload = proxy_profile_service.serialize(row)
    payload["url"] = mask_proxy_url(payload.get("url") or "")
    payload["last_checked_at"] = iso(row.last_checked_at)
    return payload


def serialize_binding(row: Optional[ExternalBinding]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "id": row.id,
        "provider": row.provider,
        "remote_account_id": row.remote_account_id,
        "binding_state": row.binding_state,
        "verified_email": row.verified_email or "",
        "verified_official_account_id": row.verified_official_account_id or "",
        "verified_workspace_id": row.verified_workspace_id or "",
        "last_observed_at": iso(row.last_observed_at),
        "last_error": row.last_error or "",
    }


def serialize_membership(row: Optional[WorkspaceMembership], workspace: Optional[Workspace] = None) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "official_role": row.official_role,
        "membership_state": row.membership_state,
        "local_purpose": row.local_purpose,
        "workspace_name": (workspace.name if workspace is not None else "") or "",
        "official_workspace_id": (workspace.official_workspace_id if workspace is not None else "") or "",
    }


def serialize_account(
    account: Account,
    *,
    workspace: Optional[Workspace] = None,
    membership: Optional[WorkspaceMembership] = None,
    binding: Optional[ExternalBinding] = None,
    quota: Optional[QuotaSnapshot] = None,
    proxy: Optional[ProxyProfile] = None,
    audit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    finding = audit or {}
    quota_payload = serialize_quota(quota)
    seven_day = None
    if quota_payload and quota_payload.get("success"):
        seven_day = quota_payload.get("seven_day_used_percent")
    return {
        "id": account.id,
        "email": account.email,
        "official_plan": account.official_plan,
        "official_user_id": account.official_user_id or "",
        "official_account_id": account.official_account_id or "",
        "auth_state": account.auth_state,
        "operational_state": account.operational_state,
        "local_purpose": account.local_purpose,
        "next_eligible_at": iso(account.next_eligible_at),
        "next_quota_probe_at": iso(account.next_quota_probe_at),
        "quota_probe_fail_count": int(account.quota_probe_fail_count or 0),
        "version": int(account.version or 1),
        "has_access_token": bool(account.access_token_encrypted),
        "has_refresh_token": bool(account.refresh_token_encrypted),
        "workspace_id": workspace.id if workspace is not None else None,
        "workspace_name": (workspace.name if workspace is not None else "") or "",
        "seat_limit": workspace.seat_limit if workspace is not None else None,
        "membership": serialize_membership(membership, workspace),
        "binding": serialize_binding(binding),
        "quota": quota_payload,
        "proxy": serialize_proxy(proxy),
        "audit_result": finding.get("result") or "",
        "audit_reasons": list(finding.get("reasons") or []),
        "automation": finding.get("automation") or "",
        "needs_auth": account.auth_state in {"oauth_required", "manual_required", "refresh_due"},
        "weekly_full": seven_day is not None and int(seven_day) >= 100,
        "identity_conflict": finding.get("result") == AUDIT_CONFLICT,
    }


class AdminV2Service:
    async def latest_official_map(self, session: AsyncSession, account_ids: List[int]) -> Dict[int, QuotaSnapshot]:
        if not account_ids:
            return {}
        rows = list(
            (
                await session.execute(
                    select(QuotaSnapshot)
                    .where(
                        QuotaSnapshot.account_id.in_(account_ids),
                        QuotaSnapshot.source == "official",
                    )
                    .order_by(desc(QuotaSnapshot.queried_at), desc(QuotaSnapshot.id))
                )
            ).scalars().all()
        )
        latest: Dict[int, QuotaSnapshot] = {}
        for row in rows:
            latest.setdefault(row.account_id, row)
        return latest

    async def _load_graph(self, session: AsyncSession) -> Dict[str, Any]:
        accounts = list((await session.execute(select(Account).order_by(Account.id.asc()))).scalars().all())
        workspaces = list((await session.execute(select(Workspace).order_by(Workspace.id.asc()))).scalars().all())
        memberships = list((await session.execute(select(WorkspaceMembership))).scalars().all())
        bindings = list((await session.execute(select(ExternalBinding))).scalars().all())
        proxies = list((await session.execute(select(ProxyProfile))).scalars().all())
        audit = await identity_service.audit(session)
        findings = {int(item["account_id"]): item for item in audit.get("findings") or []}
        quota_map = await self.latest_official_map(session, [row.id for row in accounts])
        memberships_by_account: Dict[int, List[WorkspaceMembership]] = defaultdict(list)
        for row in memberships:
            memberships_by_account[row.account_id].append(row)
        binding_by_account = {row.local_account_id: row for row in bindings}
        workspace_by_id = {row.id: row for row in workspaces}
        proxy_by_id = {row.id: row for row in proxies}
        return {
            "accounts": accounts,
            "workspaces": workspaces,
            "workspace_by_id": workspace_by_id,
            "memberships_by_account": memberships_by_account,
            "binding_by_account": binding_by_account,
            "proxy_by_id": proxy_by_id,
            "findings": findings,
            "quota_map": quota_map,
            "audit_counts": audit.get("counts") or {},
        }

    def _pick_membership(
        self,
        memberships: List[WorkspaceMembership],
        workspace_by_id: Dict[int, Workspace],
    ) -> tuple[Optional[WorkspaceMembership], Optional[Workspace]]:
        active = [
            row
            for row in memberships
            if row.membership_state in {"joined", "invited", "unknown"}
        ]
        pool = active or memberships
        if not pool:
            return None, None
        owners = [row for row in pool if row.official_role == "owner"]
        chosen = (owners or pool)[0]
        return chosen, workspace_by_id.get(chosen.workspace_id)

    def serialize_loaded_account(self, account: Account, graph: Dict[str, Any]) -> Dict[str, Any]:
        membership, workspace = self._pick_membership(
            graph["memberships_by_account"].get(account.id, []),
            graph["workspace_by_id"],
        )
        return serialize_account(
            account,
            workspace=workspace,
            membership=membership,
            binding=graph["binding_by_account"].get(account.id),
            quota=graph["quota_map"].get(account.id),
            proxy=graph["proxy_by_id"].get(account.proxy_profile_id) if account.proxy_profile_id else None,
            audit=graph["findings"].get(account.id),
        )

    def group_accounts(self, payloads: List[Dict[str, Any]], workspaces: List[Workspace]) -> List[Dict[str, Any]]:
        by_ws: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for item in payloads:
            key = int(item.get("workspace_id") or UNGROUPED_WORKSPACE_ID)
            by_ws[key].append(item)
        groups: List[Dict[str, Any]] = []
        for workspace in workspaces:
            members = by_ws.pop(workspace.id, [])
            groups.append(
                {
                    "id": workspace.id,
                    "name": workspace.name or f"Workspace {workspace.id}",
                    "official_workspace_id": workspace.official_workspace_id or "",
                    "status": workspace.status,
                    "seat_limit": workspace.seat_limit,
                    "version": int(workspace.version or 1),
                    "member_count": len(members),
                    "accounts": members,
                }
            )
        leftover = by_ws.pop(UNGROUPED_WORKSPACE_ID, [])
        for workspace_id, members in sorted(by_ws.items()):
            groups.append(
                {
                    "id": workspace_id,
                    "name": f"Workspace {workspace_id}",
                    "official_workspace_id": "",
                    "status": "unknown",
                    "seat_limit": None,
                    "version": 1,
                    "member_count": len(members),
                    "accounts": members,
                }
            )
        if leftover or not groups:
            groups.append(
                {
                    "id": UNGROUPED_WORKSPACE_ID,
                    "name": "未入组",
                    "official_workspace_id": "",
                    "status": "unknown",
                    "seat_limit": None,
                    "version": 1,
                    "member_count": len(leftover),
                    "accounts": leftover,
                }
            )
        return groups

    def overview_from(self, payloads: List[Dict[str, Any]], operations: List[Dict[str, Any]], phone_stats: Dict[str, Any], audit_counts: Dict[str, Any]) -> Dict[str, Any]:
        needs_auth = [item for item in payloads if item.get("needs_auth")]
        weekly_full = [item for item in payloads if item.get("weekly_full")]
        conflicts = [item for item in payloads if item.get("identity_conflict")]
        failed_ops = [item for item in operations if item.get("state") in {"failed", "manual_required"}]
        running_ops = [item for item in operations if item.get("state") in ACTIVE_STATES]
        return {
            "needs_auth": len(needs_auth),
            "weekly_full": len(weekly_full),
            "identity_conflict": len(conflicts),
            "failed_operations": len(failed_ops),
            "running_operations": len(running_ops),
            "phones_available": int(phone_stats.get("remaining_uses") or 0),
            "phones_reserved": int(phone_stats.get("reserved") or 0),
            "audit": audit_counts,
            "alerts": [
                *(
                    [{"kind": "auth", "account_id": item["id"], "email": item["email"], "detail": item["auth_state"]}
                     for item in needs_auth]
                ),
                *(
                    [{"kind": "weekly_full", "account_id": item["id"], "email": item["email"], "detail": "7d 100%"}
                     for item in weekly_full]
                ),
                *(
                    [{"kind": "conflict", "account_id": item["id"], "email": item["email"], "detail": "; ".join(item.get("audit_reasons") or [])}
                     for item in conflicts]
                ),
                *(
                    [{"kind": "operation", "operation_id": item.get("id"), "email": item.get("email") or "", "detail": item.get("error") or item.get("state")}
                     for item in failed_ops[:8]]
                ),
            ],
        }

    async def build_board(self, session: AsyncSession) -> Dict[str, Any]:
        graph = await self._load_graph(session)
        payloads = [self.serialize_loaded_account(account, graph) for account in graph["accounts"]]
        operations = await operation_store.list_recent(session, limit=80, include_steps=True)
        phone_stats = await phone_pool_service.stats(session)
        overview = self.overview_from(payloads, operations, phone_stats, graph["audit_counts"])
        return {
            "overview": overview,
            "workspaces": self.group_accounts(payloads, graph["workspaces"]),
            "operations": operations,
            "generated_at": iso(get_now()),
        }

    async def get_account(self, session: AsyncSession, account_id: int) -> Optional[Dict[str, Any]]:
        graph = await self._load_graph(session)
        account = next((row for row in graph["accounts"] if row.id == int(account_id)), None)
        if account is None:
            return None
        payload = self.serialize_loaded_account(account, graph)
        payload["operations"] = await operation_store.list_recent(
            session,
            limit=20,
            email=account.email,
            include_steps=True,
        )
        return payload

    async def patch_account(
        self,
        session: AsyncSession,
        account_id: int,
        *,
        version: int,
        local_purpose: Optional[str] = None,
        operational_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        account = await session.get(Account, int(account_id))
        if account is None:
            raise KeyError(account_id)
        current = int(account.version or 1)
        if int(version) != current:
            raise AccountVersionConflict(account.id, current)
        if local_purpose is not None:
            account.local_purpose = str(local_purpose).strip() or account.local_purpose
        if operational_state is not None:
            account.operational_state = str(operational_state).strip() or account.operational_state
        account.version = current + 1
        account.updated_at = get_now()
        await session.commit()
        payload = await self.get_account(session, account.id)
        assert payload is not None
        return payload

    async def archive_account(self, session: AsyncSession, account_id: int, *, version: int) -> Dict[str, Any]:
        payload = await self.patch_account(
            session,
            account_id,
            version=version,
            operational_state="archived",
        )
        payload["removed"] = True
        return payload

    async def refresh_official(self, session: AsyncSession, account_id: int) -> Dict[str, Any]:
        account = await session.get(Account, int(account_id))
        if account is None:
            raise KeyError(account_id)
        await quota_service.probe_account(session, account)
        await session.commit()
        payload = await self.get_account(session, account.id)
        assert payload is not None
        return payload

    async def list_operations(self, session: AsyncSession, *, limit: int = 80, email: str = "") -> List[Dict[str, Any]]:
        return await operation_store.list_recent(
            session,
            limit=limit,
            email=email or None,
            include_steps=True,
        )

    async def get_operation(self, session: AsyncSession, public_id: str) -> Optional[Dict[str, Any]]:
        row = await operation_store.get_by_public_id(session, public_id)
        if row is None:
            return None
        from app.services.operations import serialize_operation

        return serialize_operation(row, steps=await operation_store.steps_for(session, row), include_input=False)

    async def list_resources(self, session: AsyncSession) -> Dict[str, Any]:
        phone_cfg = await phone_pool_service.get_config(session)
        phones = await phone_pool_service.list_phones(session)
        phone_stats = await phone_pool_service.stats(session)
        leases = list((await session.execute(select(HmeAliasLease).order_by(desc(HmeAliasLease.id)))).scalars().all())
        proxies = list((await session.execute(select(ProxyProfile).order_by(ProxyProfile.id.asc()))).scalars().all())
        bound_counts: Dict[int, int] = defaultdict(int)
        accounts = list((await session.execute(select(Account.proxy_profile_id))).all())
        for (profile_id,) in accounts:
            if profile_id:
                bound_counts[int(profile_id)] += 1
        return {
            "phones": {
                "stats": phone_stats,
                "items": [
                    {
                        **phone_pool_service.serialize(row, phone_cfg),
                        "sms_url": mask_secret(row.sms_url),
                    }
                    for row in phones
                ],
            },
            "hme": [
                {
                    "id": row.id,
                    "email": row.email,
                    "local_state": row.local_state,
                    "label_desired": row.label_desired or "",
                    "label_sync_pending": bool(row.label_sync_pending),
                    "job_id": row.job_id or "",
                    "operation_id": row.operation_id,
                    "last_error": row.last_error or "",
                    "expires_at": iso(row.expires_at),
                    "heartbeat_at": iso(row.heartbeat_at),
                    "conflict": bool(row.last_error) or row.local_state in {"quarantined", "manual_review"},
                }
                for row in leases
            ],
            "proxies": [
                {
                    **serialize_proxy(row),
                    "bound_accounts": bound_counts.get(row.id, 0),
                }
                for row in proxies
            ],
        }

    async def list_phone_attempts(self, session: AsyncSession, phone_id: int, *, limit: int = 20) -> List[Dict[str, Any]]:
        rows = list(
            (
                await session.execute(
                    select(PhoneAttempt)
                    .where(PhoneAttempt.phone_id == int(phone_id))
                    .order_by(desc(PhoneAttempt.id))
                    .limit(max(1, min(int(limit or 20), 100)))
                )
            ).scalars().all()
        )
        return [
            {
                "id": row.id,
                "result": row.result,
                "purpose": row.purpose,
                "operation_public_id": row.operation_public_id or "",
                "provider_message": row.provider_message or "",
                "started_at": iso(row.started_at),
                "finished_at": iso(row.finished_at),
            }
            for row in rows
        ]

    async def get_settings(self, session: AsyncSession) -> Dict[str, Any]:
        sub2api_url = await settings_service.get_setting(session, "sub2api_base_url", "") or ""
        sub2api_key = await settings_service.get_setting(session, "sub2api_api_key", "") or ""
        hme = await load_config(session)
        quota = await quota_service.load_settings(session)
        layers = await auto_rotate_service.load_layer_settings(session)
        phone_cfg = (await phone_pool_service.stats(session)).get("config") or {}
        proxy_cfg = await settings_service.get_proxy_config(session)
        return {
            "sub2api_base_url": sub2api_url,
            "sub2api_api_key": mask_secret(sub2api_key),
            "sub2api_api_key_set": bool(str(sub2api_key).strip()),
            "hme_base_url": hme.base_url,
            "hme_service_token": mask_secret(hme.service_token),
            "hme_service_token_set": bool(hme.service_token),
            "hme_account_id": hme.account_id,
            "official_quota_probe_enabled": bool(quota.get("enabled", DEFAULT_QUOTA_PROBE_ENABLED)),
            "official_quota_probe_interval_minutes": int(quota.get("interval_minutes") or 60),
            "official_quota_probe_stagger_minutes": int(quota.get("stagger_minutes") or 60),
            "auto_reauth_enabled": bool(layers.get("auto_reauth_enabled", DEFAULT_AUTO_REAUTH_ENABLED)),
            "auto_reauth_interval_minutes": int(
                layers.get("auto_reauth_interval_minutes") or DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES
            ),
            "auto_rotate_enabled": bool(layers.get("auto_rotate_enabled", DEFAULT_AUTO_ROTATE_ENABLED)),
            "auto_rotate_force_refill": False,
            "phone": phone_cfg,
            "proxy_enabled": bool(proxy_cfg.get("enabled")),
            "proxy": mask_proxy_url(proxy_cfg.get("proxy") or ""),
        }

    async def update_settings(self, session: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
        updates: Dict[str, str] = {}
        if "sub2api_base_url" in payload:
            updates["sub2api_base_url"] = str(payload.get("sub2api_base_url") or "").strip()
        if str(payload.get("sub2api_api_key") or "").strip():
            updates["sub2api_api_key"] = str(payload.get("sub2api_api_key") or "").strip()
        if "hme_base_url" in payload:
            updates[HME_SETTING_BASE_URL] = str(payload.get("hme_base_url") or "").strip()
        if str(payload.get("hme_service_token") or "").strip():
            updates[HME_SETTING_SERVICE_TOKEN] = str(payload.get("hme_service_token") or "").strip()
        if "hme_account_id" in payload:
            updates[HME_SETTING_ACCOUNT_ID] = str(payload.get("hme_account_id") or "").strip()
        if "official_quota_probe_interval_minutes" in payload:
            updates["official_quota_probe_interval_minutes"] = str(
                clamp_quota_probe_interval_minutes(payload.get("official_quota_probe_interval_minutes"))
            )
        if "official_quota_probe_stagger_minutes" in payload:
            updates["official_quota_probe_stagger_minutes"] = str(
                clamp_quota_probe_stagger_minutes(payload.get("official_quota_probe_stagger_minutes"))
            )
        if "official_quota_probe_enabled" in payload:
            updates["official_quota_probe_enabled"] = str(bool(payload.get("official_quota_probe_enabled"))).lower()
        if "auto_reauth_enabled" in payload:
            updates["auto_reauth_enabled"] = str(bool(payload.get("auto_reauth_enabled"))).lower()
        if "auto_rotate_enabled" in payload:
            updates["auto_rotate_enabled"] = str(bool(payload.get("auto_rotate_enabled"))).lower()
        updates["auto_rotate_force_refill"] = "false"
        if updates:
            await settings_service.update_settings(session, updates)
            self._apply_job_updates(updates)
        return await self.get_settings(session)

    def _apply_job_updates(self, updates: Dict[str, str]) -> None:
        try:
            from app.main import (
                DEFAULT_AUTO_REAUTH_SCAN_MINUTES,
                DEFAULT_AUTO_ROTATE_SCAN_MINUTES,
                configure_auto_reauth_job,
                configure_auto_rotate_job,
            )

            if "auto_reauth_enabled" in updates:
                configure_auto_reauth_job(
                    updates["auto_reauth_enabled"] == "true",
                    DEFAULT_AUTO_REAUTH_SCAN_MINUTES,
                )
            if "auto_rotate_enabled" in updates:
                configure_auto_rotate_job(
                    updates["auto_rotate_enabled"] == "true",
                    DEFAULT_AUTO_ROTATE_SCAN_MINUTES,
                )
        except Exception:
            logger.warning("v2 settings saved, scheduler reconfigure skipped", exc_info=True)


admin_v2_service = AdminV2Service()
