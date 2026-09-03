"""Official quota probe. Failures write snapshots only; never kick or pause Sub2API."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.application.settings import as_bool, get_setting_value
from app.core.config import load_settings
from app.core.crypto import token_cipher
from app.core.time import utcnow
from app.domain.quota import (
    DEFAULT_QUOTA_PROBE_BATCH_SIZE,
    DEFAULT_QUOTA_PROBE_ENABLED,
    DEFAULT_QUOTA_PROBE_STAGGER_MINUTES,
    MAX_QUOTA_PROBE_BATCH_SIZE,
    MIN_QUOTA_PROBE_BATCH_SIZE,
    SKIP_OPERATIONAL_STATES,
    SOURCE_OFFICIAL,
    QuotaResult,
    due_quota_account_ids,
    failure_next_quota_probe_at,
    initial_next_quota_probe_at,
    quota_slot_minute_for,
    success_next_quota_probe_at,
)
from app.integrations.openai.quota import OpenAIQuotaClient
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership
from app.persistence.models.quota import QuotaSnapshot

logger = logging.getLogger(__name__)


def clamp_batch_size(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_QUOTA_PROBE_BATCH_SIZE
    return max(MIN_QUOTA_PROBE_BATCH_SIZE, min(MAX_QUOTA_PROBE_BATCH_SIZE, number))


def decrypt_access_token(account: Account) -> str | None:
    raw = account.access_token_encrypted
    if not raw:
        return None
    try:
        token = token_cipher().decrypt(raw)
    except Exception:
        return None
    token = str(token or "").strip()
    return token or None


def resolve_chatgpt_account_id(account: Account, workspace: Workspace | None = None) -> str | None:
    if workspace is not None:
        official = str(workspace.official_workspace_id or "").strip()
        if official:
            return official
    official = str(account.official_account_id or "").strip()
    if official:
        return official
    return None


def snapshot_from_result(
    account_id: int,
    result: QuotaResult,
    now: datetime,
    *,
    workspace_id: int | None = None,
) -> QuotaSnapshot:
    queried_at = result.queried_at or now
    return QuotaSnapshot(
        account_id=account_id,
        workspace_id=workspace_id,
        five_hour_used_percent=result.five_hour_used_percent,
        five_hour_reset_at=result.five_hour_reset_at,
        seven_day_used_percent=result.seven_day_used_percent,
        seven_day_reset_at=result.seven_day_reset_at,
        source=result.source or SOURCE_OFFICIAL,
        queried_at=queried_at,
        success=bool(result.success),
        error_code=result.error_code,
        error_message=(result.error_message or None) and str(result.error_message)[:500],
        created_at=now,
    )


def serialize_snapshot(row: QuotaSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "workspace_id": getattr(row, "workspace_id", None),
        "five_hour_used_percent": row.five_hour_used_percent,
        "five_hour_reset_at": row.five_hour_reset_at,
        "seven_day_used_percent": row.seven_day_used_percent,
        "seven_day_reset_at": row.seven_day_reset_at,
        "source": row.source,
        "queried_at": row.queried_at,
        "success": bool(row.success),
        "error_code": row.error_code,
        "error_message": row.error_message,
    }


class QuotaService:
    def __init__(self, client: OpenAIQuotaClient | None = None):
        self.client = client or OpenAIQuotaClient()

    async def load_settings(self, db: AsyncSession) -> dict[str, Any]:
        env = load_settings()
        enabled_raw = await get_setting_value(
            db,
            "official_quota_probe_enabled",
            str(bool(env.official_quota_probe_enabled and DEFAULT_QUOTA_PROBE_ENABLED)).lower(),
        )
        stagger_raw = await get_setting_value(
            db, "official_quota_probe_stagger_minutes", str(DEFAULT_QUOTA_PROBE_STAGGER_MINUTES)
        )
        batch_raw = await get_setting_value(db, "official_quota_probe_batch_size", str(DEFAULT_QUOTA_PROBE_BATCH_SIZE))
        return {
            "enabled": as_bool(enabled_raw, DEFAULT_QUOTA_PROBE_ENABLED),
            "stagger_minutes": int(stagger_raw or DEFAULT_QUOTA_PROBE_STAGGER_MINUTES),
            "batch_size": clamp_batch_size(batch_raw),
        }

    async def latest_official(self, db: AsyncSession, account_id: int) -> QuotaSnapshot | None:
        result = await db.execute(
            select(QuotaSnapshot)
            .where(QuotaSnapshot.account_id == account_id, QuotaSnapshot.source == SOURCE_OFFICIAL)
            .order_by(QuotaSnapshot.queried_at.desc(), QuotaSnapshot.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def latest_official_by_accounts(self, db: AsyncSession) -> dict[int, QuotaSnapshot]:
        rows = list((await db.execute(select(QuotaSnapshot).where(QuotaSnapshot.source == SOURCE_OFFICIAL))).scalars())
        latest: dict[int, QuotaSnapshot] = {}
        for row in rows:
            current = latest.get(row.account_id)
            if current is None or (row.queried_at, row.id) > (current.queried_at, current.id):
                latest[row.account_id] = row
        return latest

    async def latest_official_by_contexts(self, db: AsyncSession) -> dict[tuple[int, int | None], QuotaSnapshot]:
        rows = list((await db.execute(select(QuotaSnapshot).where(QuotaSnapshot.source == SOURCE_OFFICIAL))).scalars())
        latest: dict[tuple[int, int | None], QuotaSnapshot] = {}
        for row in rows:
            key = (row.account_id, getattr(row, "workspace_id", None))
            current = latest.get(key)
            if current is None or (row.queried_at, row.id) > (current.queried_at, current.id):
                latest[key] = row
        return latest

    async def resolve_probe_workspace(self, db: AsyncSession, account: Account, workspace_id: int | None = None) -> Workspace | None:
        from app.domain.identity.binding import AmbiguousWorkspaceContext, resolve_workspace_context
        from app.persistence.repositories import identity as identity_repo

        memberships = list(
            (await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.account_id == account.id))).scalars()
        )
        workspaces_by_id = {row.id: row for row in await identity_repo.list_workspaces(db)}
        return resolve_workspace_context(
            account,
            memberships=memberships,
            workspaces_by_id=workspaces_by_id,
            workspace_id=workspace_id,
        )

    async def run_probe_once(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        settings: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        cfg = settings or await self.load_settings(db)
        stamp = now or utcnow()
        stats: dict[str, Any] = {
            "enabled": bool(cfg.get("enabled")),
            "scanned": 0,
            "due": 0,
            "probed": 0,
            "failed": 0,
            "skipped": 0,
            "account_ids": [],
        }
        if not cfg.get("enabled") and not force:
            stats["skipped"] = 1
            return stats

        result = await db.execute(
            select(Account).options(
                selectinload(Account.memberships).selectinload(WorkspaceMembership.workspace),
                selectinload(Account.owned_workspaces),
            )
        )
        accounts = [
            account
            for account in result.scalars().all()
            if account.access_token_encrypted and str(account.operational_state or "") not in SKIP_OPERATIONAL_STATES
        ]
        stats["scanned"] = len(accounts)
        if not accounts:
            return stats

        stagger_minutes = int(cfg["stagger_minutes"])
        batch_size = clamp_batch_size(cfg.get("batch_size"))
        by_id = {int(account.id): account for account in accounts}
        for account in accounts:
            if account.quota_slot_minute is None:
                account.quota_slot_minute = quota_slot_minute_for(account.id)
            if account.next_quota_probe_at is None:
                account.next_quota_probe_at = initial_next_quota_probe_at(account.id, stamp, stagger_minutes)
            if account.quota_probe_fail_count is None:
                account.quota_probe_fail_count = 0

        due_ids = due_quota_account_ids(accounts, stamp, limit=batch_size)
        stats["due"] = len(due_ids)
        stats["account_ids"] = due_ids
        if not due_ids:
            await db.commit()
            return stats

        for account_id in due_ids:
            account = by_id[account_id]
            before_state = account.operational_state
            before_purpose = account.local_purpose
            try:
                from app.domain.identity.binding import AmbiguousWorkspaceContext, workspace_contexts

                try:
                    result_row = await self.probe_account(db, account, now=stamp)
                except AmbiguousWorkspaceContext:
                    memberships = list(account.memberships or [])
                    workspaces_by_id = {row.id: row for row in (account.owned_workspaces or [])}
                    for membership in memberships:
                        if membership.workspace is not None:
                            workspaces_by_id[membership.workspace_id] = membership.workspace
                    contexts = workspace_contexts(account, memberships=memberships, workspaces_by_id=workspaces_by_id)
                    result_row = None
                    for workspace in contexts:
                        result_row = await self.probe_account(db, account, now=stamp, workspace=workspace, workspace_id=workspace.id)
                if result_row is None:
                    continue
            except Exception as exc:  # noqa: BLE001
                result_row = QuotaResult(
                    success=False,
                    error_code="probe_exception",
                    error_message=str(exc)[:500],
                    queried_at=stamp,
                )
                db.add(snapshot_from_result(account.id, result_row, stamp))
                account.quota_probe_fail_count = int(account.quota_probe_fail_count or 0) + 1
                account.next_quota_probe_at = failure_next_quota_probe_at(stamp, account.quota_probe_fail_count)
                stats["failed"] += 1
                logger.warning("official quota probe failed account_id=%s error=%s", account.id, exc)
            else:
                if result_row.success:
                    stats["probed"] += 1
                else:
                    stats["failed"] += 1
            if account.operational_state != before_state or account.local_purpose != before_purpose:
                account.operational_state = before_state
                account.local_purpose = before_purpose

        await db.commit()
        return stats

    async def probe_account(
        self,
        db: AsyncSession,
        account: Account,
        *,
        now: datetime | None = None,
        workspace_id: int | None = None,
        workspace: Workspace | None = None,
    ) -> QuotaResult:
        stamp = now or utcnow()
        before_state = account.operational_state
        before_purpose = account.local_purpose
        token = decrypt_access_token(account)
        if workspace is None:
            workspace = await self.resolve_probe_workspace(db, account, workspace_id)
        chatgpt_account_id = resolve_chatgpt_account_id(account, workspace)
        if not token:
            result = QuotaResult(
                success=False,
                error_code="missing_token",
                error_message="local access token missing or undecryptable",
                queried_at=stamp,
            )
        else:
            result = await self.client.fetch_quota(
                access_token=token,
                db_session=db,
                workspace_id=chatgpt_account_id,
                identifier=account.email or "default",
                now=stamp,
            )
        db.add(snapshot_from_result(account.id, result, stamp, workspace_id=workspace.id if workspace is not None else None))
        if result.success:
            account.quota_probe_fail_count = 0
            account.next_quota_probe_at = success_next_quota_probe_at(
                stamp, account.quota_slot_minute or quota_slot_minute_for(account.id)
            )
        else:
            account.quota_probe_fail_count = int(account.quota_probe_fail_count or 0) + 1
            account.next_quota_probe_at = failure_next_quota_probe_at(stamp, account.quota_probe_fail_count)
        account.operational_state = before_state
        account.local_purpose = before_purpose
        return result


quota_service = QuotaService()
