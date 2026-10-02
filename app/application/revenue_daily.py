"""Daily Sub2API user cost per remote account, and the overview's today / 7-day / running totals.

One row per (remote account, local day). Rows only grow within a day and survive binding,
account and team deletion, so departed accounts still count toward today and the last 7 days.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.sub2api_usage import USAGE_STALE_AFTER, _decimal, _money_text, _safe_error
from app.core.config import load_settings
from app.core.time import as_utc, utcnow, zone
from app.domain.identity import BINDING_VERIFIED, PROVIDER_SUB2API
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, ExternalBinding
from app.persistence.models.revenue import Sub2ApiRevenueDaily, Sub2ApiRevenueEntry
from app.persistence.models.sub2api import Sub2ApiUsageSnapshot

logger = logging.getLogger(__name__)

RECENT_DAYS = 7

# Sub2API `server_timezone`, detected by the startup backfill; None until known.
_remote_timezone: str | None = None


def local_today(now: datetime | None = None) -> date:
    return as_utc(now or utcnow()).astimezone(zone(load_settings().timezone)).date()


def local_day(value: datetime | None) -> date | None:
    stamp = as_utc(value)
    return None if stamp is None else stamp.astimezone(zone(load_settings().timezone)).date()


def _boundary_note() -> str | None:
    local_name = load_settings().timezone
    if _remote_timezone and _remote_timezone != local_name:
        return f"Sub2API 按 {_remote_timezone} 0 点切日，本地按 {local_name}，今日 / 近 7 天边界可能错开"
    return None


def _result(amount: Decimal | None, synced: int, total: int, stale: bool, notes: list[str]) -> dict[str, Any]:
    return {
        "user_cost": _money_text(_decimal(amount)) if amount is not None else None,
        "synced": synced,
        "total": total,
        "stale": bool(stale),
        "note": "；".join(notes) or None,
    }


class RevenueDaily:
    async def _upsert(
        self,
        db: AsyncSession,
        *,
        remote_account_id: str,
        day: date,
        user_cost: Decimal,
        source: str,
        workspace_id: int | None,
        account_id: int | None,
        email: str | None,
        now: datetime,
    ) -> Sub2ApiRevenueDaily:
        row = (
            await db.execute(
                select(Sub2ApiRevenueDaily).where(
                    Sub2ApiRevenueDaily.remote_account_id == remote_account_id,
                    Sub2ApiRevenueDaily.day == day,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = Sub2ApiRevenueDaily(
                remote_account_id=remote_account_id,
                day=day,
                workspace_id=workspace_id,
                account_id=account_id,
                email=email,
                user_cost=user_cost,
                source=source,
                updated_at=now,
            )
            db.add(row)
        else:
            # Same-day cost only grows; a partial or zero read never lowers a stored amount.
            if user_cost > _decimal(row.user_cost or 0):
                row.user_cost = user_cost
                row.source = source
            if workspace_id is not None:
                row.workspace_id = workspace_id
            if account_id is not None:
                row.account_id = account_id
            if email:
                row.email = email
            row.updated_at = now
        await db.flush()
        return row

    async def record_day(
        self,
        db: AsyncSession,
        *,
        remote_account_id: str,
        day: date,
        user_cost: Any,
        source: str,
        workspace_id: int | None = None,
        account_id: int | None = None,
        email: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Keep the larger of stored and new amount; flushes only. Never raises."""
        try:
            remote_id = str(remote_account_id or "").strip()
            if not remote_id:
                return False
            values = dict(
                remote_account_id=remote_id,
                day=day,
                user_cost=_decimal(user_cost),
                source=source,
                workspace_id=workspace_id,
                account_id=account_id,
                email=email,
                now=now or utcnow(),
            )
            try:
                async with db.begin_nested():
                    await self._upsert(db, **values)
            except IntegrityError:
                # A concurrent writer inserted the same (remote id, day); merge into that row.
                async with db.begin_nested():
                    await self._upsert(db, **values)
            return True
        except Exception as exc:
            logger.warning(
                "revenue daily: record failed for remote %s on %s: %s", remote_account_id, day, _safe_error(exc)
            )
            return False

    async def record_settle(
        self, db: AsyncSession, binding: ExternalBinding, *, summary: dict[str, Any] | None = None
    ) -> bool:
        """Book today's amount when a member leaves. Uses the fresh remote read if given, else the local snapshot."""
        try:
            remote_id = str(binding.remote_account_id or "").strip()
            if not remote_id:
                return False
            now = utcnow()
            today = local_today(now)
            amount = None
            if isinstance(summary, dict) and "today" in summary:
                part = summary.get("today")
                if part is None:
                    # Sub2API sends `today: null` when the account has no usage today.
                    amount = Decimal("0")
                elif isinstance(part, dict) and str(part.get("date") or "") == today.isoformat():
                    amount = _decimal(part.get("user_cost") or 0)
            if amount is None:
                snapshot = (
                    await db.execute(
                        select(Sub2ApiUsageSnapshot).where(
                            Sub2ApiUsageSnapshot.binding_id == binding.id,
                            Sub2ApiUsageSnapshot.remote_account_id == remote_id,
                            Sub2ApiUsageSnapshot.window_kind == "today",
                        )
                    )
                ).scalar_one_or_none()
                if (
                    snapshot is not None
                    and snapshot.last_success_at
                    and snapshot.user_cost is not None
                    and local_day(snapshot.window_start_at) == today
                ):
                    amount = _decimal(snapshot.user_cost)
            if amount is None:
                return False
            account = await db.get(Account, binding.local_account_id) if binding.local_account_id else None
            return await self.record_day(
                db,
                remote_account_id=remote_id,
                day=today,
                user_cost=amount,
                source="settle",
                workspace_id=binding.workspace_id,
                account_id=binding.local_account_id,
                email=(account.email if account else None) or binding.verified_email,
                now=now,
            )
        except Exception as exc:
            logger.warning(
                "revenue daily: settle record failed for binding %s: %s", getattr(binding, "id", None), _safe_error(exc)
            )
            return False

    async def _bound_remote_ids(self, db: AsyncSession) -> set[str]:
        rows = await db.execute(
            select(ExternalBinding.remote_account_id).where(
                ExternalBinding.provider == PROVIDER_SUB2API,
                ExternalBinding.binding_state == BINDING_VERIFIED,
            )
        )
        return {str(value) for (value,) in rows.all() if value not in (None, "")}

    async def backfill_recent(self, db: AsyncSession) -> int:
        """Startup: check Sub2API's day boundary, then fill the last 7 days of recently departed accounts.

        Read-only against Sub2API. Safe to repeat (unique key + larger value wins).
        """
        global _remote_timezone
        now = utcnow()
        today = local_today(now)
        first_day = today - timedelta(days=RECENT_DAYS - 1)
        local_zone = zone(load_settings().timezone)
        window_start = as_utc(local_zone.localize(datetime.combine(first_day, datetime.min.time())))
        bound = await self._bound_remote_ids(db)
        entries = [
            entry
            for entry in (await db.execute(select(Sub2ApiRevenueEntry))).scalars()
            if as_utc(entry.settled_at) and as_utc(entry.settled_at) >= window_start
        ]
        # remote id -> (first day to fill, last day to fill, workspace, account, email)
        targets: dict[str, list[Any]] = {}
        for entry in entries:
            remote_id = str(entry.remote_account_id or "").strip()
            if not remote_id or remote_id in bound or not remote_id.isdigit():
                continue
            start = max(first_day, local_day(entry.bound_at) or first_day)
            end = min(today, local_day(entry.settled_at) or today)
            if start > end:
                continue
            current = targets.get(remote_id)
            if current is None:
                targets[remote_id] = [start, end, entry.workspace_id, entry.account_id, entry.email]
            else:
                current[0] = min(current[0], start)
                if end >= current[1]:
                    current[1:] = [end, entry.workspace_id, entry.account_id, entry.email]

        if not (await sub2api_client.load_config(db)).get("configured"):
            logger.info("revenue daily: Sub2API not configured; skip day-boundary check and backfill")
            return 0
        client, headers, _cfg = await sub2api_client._with_client(db)
        written = 0
        try:
            try:
                public = await sub2api_client._request_json(client, headers, "GET", "/api/v1/settings/public", retries=0)
                remote_tz = str((public or {}).get("server_timezone") or "").strip() if isinstance(public, dict) else ""
                if remote_tz and remote_tz != "Local":
                    _remote_timezone = remote_tz
                    if remote_tz != load_settings().timezone:
                        logger.warning(
                            "revenue daily: Sub2API day boundary %s differs from TIMEZONE %s",
                            remote_tz, load_settings().timezone,
                        )
            except Exception as exc:
                logger.info("revenue daily: Sub2API timezone check skipped: %s", _safe_error(exc))

            for remote_id, (start, end, workspace_id, account_id, email) in targets.items():
                wanted = (end - start).days + 1
                have = (
                    await db.execute(
                        select(func.count(Sub2ApiRevenueDaily.id)).where(
                            Sub2ApiRevenueDaily.remote_account_id == remote_id,
                            Sub2ApiRevenueDaily.day >= start,
                            Sub2ApiRevenueDaily.day <= end,
                        )
                    )
                ).scalar_one()
                if int(have or 0) >= wanted:
                    continue
                try:
                    payload = await sub2api_client._request_json(
                        client, headers, "GET", f"/api/v1/admin/accounts/{int(remote_id)}/stats",
                        params={"days": str(RECENT_DAYS)},
                    )
                except Exception as exc:
                    logger.info("revenue daily: history read failed for remote %s: %s", remote_id, _safe_error(exc))
                    continue
                history = payload.get("history") if isinstance(payload, dict) else None
                if not isinstance(history, list):
                    logger.warning("revenue daily: Sub2API stats has no daily history; backfill not supported")
                    break
                by_day: dict[date, Decimal] = {}
                for item in history:
                    if not isinstance(item, dict):
                        continue
                    try:
                        day = date.fromisoformat(str(item.get("date") or ""))
                        by_day[day] = _decimal(item.get("user_cost") or 0)
                    except ValueError:
                        continue
                day = start
                while day <= end:
                    # Days without usage are booked as 0 so the next startup does not refetch them.
                    if await self.record_day(
                        db, remote_account_id=remote_id, day=day, user_cost=by_day.get(day, Decimal("0")),
                        source="backfill", workspace_id=workspace_id, account_id=account_id, email=email, now=now,
                    ):
                        written += 1
                    day += timedelta(days=1)
                await db.commit()
        finally:
            await client.aclose()
        return written

    async def overview_totals(
        self, db: AsyncSession, groups: list[dict[str, Any]], *, now: datetime | None = None
    ) -> dict[str, Any]:
        stamp = as_utc(now or utcnow())
        today = local_today(stamp)
        first_day = today - timedelta(days=RECENT_DAYS - 1)

        # Running: same numbers as each team card's in-seat lifetime aggregate.
        running_amount: Decimal | None = None
        running_synced = running_total = 0
        running_stale = False
        for group in groups:
            seated = ([group["mother"]] if group.get("mother") else []) + list(group.get("current_children") or [])
            lifetime = ((group.get("usage") or {}).get("windows") or {}).get("lifetime")
            if lifetime:
                coverage = lifetime.get("coverage") or {}
                running_synced += int(coverage.get("synced") or 0)
                running_total += int(coverage.get("total") or len(seated))
                running_stale = running_stale or bool(lifetime.get("stale"))
                running_amount = (running_amount or Decimal("0")) + _decimal(lifetime.get("user_cost") or 0)
            else:
                running_total += len(seated)

        # Bound accounts: today / natural 7-day snapshots, one per remote id.
        bindings = list(
            (
                await db.execute(
                    select(ExternalBinding).where(
                        ExternalBinding.provider == PROVIDER_SUB2API,
                        ExternalBinding.binding_state == BINDING_VERIFIED,
                    )
                )
            ).scalars()
        )
        binding_remote = {row.id: str(row.remote_account_id) for row in bindings if row.remote_account_id not in (None, "")}
        bound = set(binding_remote.values())
        snapshots: dict[tuple[str, str], Sub2ApiUsageSnapshot] = {}
        if binding_remote:
            rows = (
                await db.execute(
                    select(Sub2ApiUsageSnapshot).where(
                        Sub2ApiUsageSnapshot.binding_id.in_(list(binding_remote)),
                        Sub2ApiUsageSnapshot.window_kind.in_(("today", "seven_day")),
                        Sub2ApiUsageSnapshot.last_success_at.is_not(None),
                        Sub2ApiUsageSnapshot.user_cost.is_not(None),
                    )
                )
            ).scalars()
            for row in rows:
                if binding_remote.get(row.binding_id) != str(row.remote_account_id):
                    continue
                expected = today if row.window_kind == "today" else first_day
                if local_day(row.window_start_at) != expected:
                    continue  # yesterday's window is not today's number
                key = (str(row.remote_account_id), row.window_kind)
                previous = snapshots.get(key)
                if previous is None or as_utc(row.last_success_at) > as_utc(previous.last_success_at):
                    snapshots[key] = row

        def bound_part(kind: str) -> tuple[Decimal | None, int, bool]:
            amount: Decimal | None = None
            synced = 0
            stale = False
            for remote_id in bound:
                row = snapshots.get((remote_id, kind))
                if row is None:
                    continue
                synced += 1
                amount = (amount or Decimal("0")) + _decimal(row.user_cost)
                stale = stale or stamp - as_utc(row.last_success_at) > USAGE_STALE_AFTER
            return amount, synced, stale

        # Not bound any more: daily records.
        daily_rows = (
            await db.execute(
                select(Sub2ApiRevenueDaily.remote_account_id, Sub2ApiRevenueDaily.day, Sub2ApiRevenueDaily.user_cost)
                .where(Sub2ApiRevenueDaily.day >= first_day, Sub2ApiRevenueDaily.day <= today)
            )
        ).all()
        departed_today: Decimal | None = None
        departed_seven: Decimal | None = None
        for remote_id, day, user_cost in daily_rows:
            if str(remote_id) in bound:
                continue
            amount = _decimal(user_cost or 0)
            departed_seven = (departed_seven or Decimal("0")) + amount
            if day == today:
                departed_today = (departed_today or Decimal("0")) + amount
        earliest = (await db.execute(select(func.min(Sub2ApiRevenueDaily.day)))).scalar_one_or_none()

        boundary = _boundary_note()
        today_amount, today_synced, today_stale = bound_part("today")
        seven_amount, seven_synced, seven_stale = bound_part("seven_day")

        def plus(left: Decimal | None, right: Decimal | None) -> Decimal | None:
            if left is None and right is None:
                return None
            return (left or Decimal("0")) + (right or Decimal("0"))

        seven_notes = [boundary] if boundary else []
        start = earliest or today
        if isinstance(start, str):
            start = date.fromisoformat(start)
        if start > first_day:
            seven_notes.append(f"离队号从 {start.strftime('%m-%d')} 起统计")
        return {
            "running": _result(running_amount, running_synced, running_total, running_stale, []),
            "today": _result(
                plus(today_amount, departed_today), today_synced, len(bound), today_stale,
                [boundary] if boundary else [],
            ),
            "seven_day": _result(
                plus(seven_amount, departed_seven), seven_synced, len(bound), seven_stale, seven_notes,
            ),
        }


revenue_daily = RevenueDaily()
