"""Team revenue ledger: book a member's lifetime Sub2API user cost when it leaves a team."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.sub2api_usage import _decimal, _money_text, _safe_error, lifetime_window
from app.core.config import load_settings
from app.core.time import as_utc, isoformat, utcnow, zone
from app.domain.identity import BINDING_VERIFIED, MEMBERSHIP_STATE_REMOVED, PROVIDER_SUB2API
from app.domain.workspaces.names import resolve_display_name
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.models.revenue import Sub2ApiRevenueEntry
from app.persistence.models.sub2api import Sub2ApiUsageSnapshot

logger = logging.getLogger(__name__)

# Higher is more trustworthy; a later settle never overwrites a better amount with a worse one.
AMOUNT_RANK = {
    "lifetime": 3,
    "lifetime_capped_90d": 3,
    "cache_lifetime": 2,
    "cache_seven_day": 1,
    "missing": 0,
}


class RevenueLedger:
    async def _remote_amount(
        self, db: AsyncSession, binding: ExternalBinding
    ) -> tuple[Decimal, str, int]:
        days, capped = lifetime_window(binding.created_at)
        summary = await sub2api_client.fetch_account_stats_summary(
            db, int(binding.remote_account_id), days
        )
        return _decimal(summary.get("total_user_cost")), ("lifetime_capped_90d" if capped else "lifetime"), days

    async def _cached_amount(
        self, db: AsyncSession, binding: ExternalBinding
    ) -> tuple[Decimal, str, int | None]:
        rows = {
            row.window_kind: row
            for row in (
                await db.execute(
                    select(Sub2ApiUsageSnapshot).where(
                        Sub2ApiUsageSnapshot.binding_id == binding.id,
                        Sub2ApiUsageSnapshot.remote_account_id == str(binding.remote_account_id),
                        Sub2ApiUsageSnapshot.window_kind.in_(("lifetime", "seven_day")),
                    )
                )
            ).scalars()
        }
        lifetime = rows.get("lifetime")
        if lifetime is not None and lifetime.last_success_at and lifetime.user_cost is not None:
            days = None
            start, end = as_utc(lifetime.window_start_at), as_utc(lifetime.window_end_at)
            if start and end:
                local_zone = zone(load_settings().timezone)
                days = (end.astimezone(local_zone).date() - start.astimezone(local_zone).date()).days + 1
            return _decimal(lifetime.user_cost), "cache_lifetime", days
        seven = rows.get("seven_day")
        if seven is not None and seven.last_success_at and seven.user_cost is not None:
            return _decimal(seven.user_cost), "cache_seven_day", 7
        return _decimal(0), "missing", None

    async def _upsert(
        self,
        db: AsyncSession,
        binding: ExternalBinding,
        *,
        amount: Decimal,
        amount_source: str,
        window_days: int | None,
        source: str,
        operation_id: str | None,
        now: datetime,
    ) -> tuple[Sub2ApiRevenueEntry, bool]:
        workspace_id = int(binding.workspace_id)
        remote_id = str(binding.remote_account_id)
        existing = (
            await db.execute(
                select(Sub2ApiRevenueEntry).where(
                    Sub2ApiRevenueEntry.workspace_id == workspace_id,
                    Sub2ApiRevenueEntry.remote_account_id == remote_id,
                )
            )
        ).scalar_one_or_none()
        created = existing is None
        if existing is None:
            workspace = await db.get(Workspace, workspace_id)
            account = await db.get(Account, binding.local_account_id)
            existing = Sub2ApiRevenueEntry(
                workspace_id=workspace_id,
                workspace_name=resolve_display_name(workspace)["display_name"] if workspace else None,
                account_id=binding.local_account_id,
                email=(account.email if account else None) or binding.verified_email,
                remote_account_id=remote_id,
                user_cost=amount,
                amount_source=amount_source,
                window_days=window_days,
                departure_source=source,
                operation_id=operation_id,
                bound_at=binding.created_at,
                settled_at=now,
                updated_at=now,
            )
            db.add(existing)
        else:
            if AMOUNT_RANK.get(amount_source, 0) >= AMOUNT_RANK.get(existing.amount_source, 0):
                existing.user_cost = amount
                existing.amount_source = amount_source
                existing.window_days = window_days
            if not existing.operation_id and operation_id:
                existing.operation_id = operation_id
            existing.updated_at = now
        await db.flush()
        return existing, created

    async def settle_binding(
        self,
        db: AsyncSession,
        binding: ExternalBinding,
        *,
        source: str,
        operation_id: str | None = None,
        allow_remote: bool = True,
    ) -> dict[str, Any]:
        """Book or refresh one entry; flushes only. Never raises into the caller's flow."""
        try:
            if binding.workspace_id is None:
                return {"ok": False, "error_code": "no_workspace", "error": "绑定没有所属团队，不记账"}
            remote_error = None
            fetched = None
            if allow_remote:
                try:
                    fetched = await self._remote_amount(db, binding)
                except Exception as exc:
                    remote_error = _safe_error(exc)
                    logger.warning(
                        "revenue ledger: remote lifetime read failed for binding %s: %s", binding.id, remote_error
                    )
            amount, amount_source, window_days = fetched or await self._cached_amount(db, binding)
            now = utcnow()
            try:
                async with db.begin_nested():
                    entry, created = await self._upsert(
                        db, binding, amount=amount, amount_source=amount_source, window_days=window_days,
                        source=source, operation_id=operation_id, now=now,
                    )
            except IntegrityError:
                # A concurrent settle inserted the same (workspace, remote id); update that row instead.
                async with db.begin_nested():
                    entry, created = await self._upsert(
                        db, binding, amount=amount, amount_source=amount_source, window_days=window_days,
                        source=source, operation_id=operation_id, now=now,
                    )
            return {
                "ok": True,
                "entry_id": entry.id,
                "created": created,
                "user_cost": _money_text(entry.user_cost),
                "amount_source": entry.amount_source,
                "window_days": entry.window_days,
                "remote_error": remote_error,
            }
        except Exception as exc:
            error = _safe_error(exc)
            logger.warning("revenue ledger: settle failed for binding %s: %s", getattr(binding, "id", None), error)
            return {"ok": False, "error_code": "settle_failed", "error": error}

    async def settle_departure(
        self,
        db: AsyncSession,
        *,
        workspace_id: int,
        account_id: int,
        source: str,
        operation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Book the verified Sub2API binding of this account in this team; None when there is none."""
        try:
            binding = (
                await db.execute(
                    select(ExternalBinding).where(
                        ExternalBinding.provider == PROVIDER_SUB2API,
                        ExternalBinding.binding_state == BINDING_VERIFIED,
                        ExternalBinding.workspace_id == int(workspace_id),
                        ExternalBinding.local_account_id == int(account_id),
                    )
                )
            ).scalar_one_or_none()
        except Exception as exc:
            error = _safe_error(exc)
            logger.warning(
                "revenue ledger: binding lookup failed for workspace %s account %s: %s", workspace_id, account_id, error
            )
            return {"ok": False, "error_code": "settle_failed", "error": error}
        if binding is None:
            return None
        return await self.settle_binding(db, binding, source=source, operation_id=operation_id)

    async def backfill_departures(self, db: AsyncSession) -> int:
        """Book verified bindings whose member already left that team but has no ledger entry yet."""
        booked = (
            select(Sub2ApiRevenueEntry.id)
            .where(
                Sub2ApiRevenueEntry.workspace_id == ExternalBinding.workspace_id,
                Sub2ApiRevenueEntry.remote_account_id == ExternalBinding.remote_account_id,
            )
            .exists()
        )
        bindings = list(
            (
                await db.execute(
                    select(ExternalBinding)
                    .join(
                        WorkspaceMembership,
                        (WorkspaceMembership.workspace_id == ExternalBinding.workspace_id)
                        & (WorkspaceMembership.account_id == ExternalBinding.local_account_id),
                    )
                    .join(Workspace, Workspace.id == ExternalBinding.workspace_id)
                    .where(
                        ExternalBinding.provider == PROVIDER_SUB2API,
                        ExternalBinding.binding_state == BINDING_VERIFIED,
                        WorkspaceMembership.membership_state == MEMBERSHIP_STATE_REMOVED,
                        or_(Workspace.owner_account_id.is_(None), Workspace.owner_account_id != ExternalBinding.local_account_id),
                        ~booked,
                    )
                )
            ).scalars()
        )
        count = 0
        for binding in bindings:
            result = await self.settle_binding(db, binding, source="sync_departure")
            await db.commit()
            if result.get("ok"):
                count += 1
        return count

    async def totals(self, db: AsyncSession) -> dict[str, Any]:
        local_zone = zone(load_settings().timezone)
        local_now = utcnow().astimezone(local_zone)
        month_start = as_utc(local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        rows = (
            await db.execute(
                select(
                    Sub2ApiRevenueEntry.workspace_id,
                    Sub2ApiRevenueEntry.user_cost,
                    Sub2ApiRevenueEntry.settled_at,
                )
            )
        ).all()
        total = Decimal("0")
        month = Decimal("0")
        by_workspace: dict[int, Decimal] = {}
        for workspace_id, user_cost, settled_at in rows:
            amount = _decimal(user_cost or 0)
            total += amount
            if as_utc(settled_at) >= month_start:
                month += amount
            by_workspace[int(workspace_id)] = by_workspace.get(int(workspace_id), Decimal("0")) + amount
        return {
            "total": _money_text(_decimal(total)),
            "month": _money_text(_decimal(month)),
            "by_workspace": {key: _money_text(_decimal(value)) for key, value in by_workspace.items()},
            "count": len(rows),
        }

    async def entries(self, db: AsyncSession, workspace_id: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = (
            await db.execute(
                select(Sub2ApiRevenueEntry)
                .where(Sub2ApiRevenueEntry.workspace_id == int(workspace_id))
                .order_by(Sub2ApiRevenueEntry.settled_at.desc(), Sub2ApiRevenueEntry.id.desc())
                .limit(max(1, int(limit)))
            )
        ).scalars()
        return [
            {
                "email": row.email,
                "remote_account_id": row.remote_account_id,
                "user_cost": _money_text(row.user_cost),
                "amount_source": row.amount_source,
                "window_days": row.window_days,
                "departure_source": row.departure_source,
                "operation_id": row.operation_id,
                "settled_at": isoformat(row.settled_at),
            }
            for row in rows
        ]


revenue_ledger = RevenueLedger()
