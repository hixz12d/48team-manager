"""Background Sub2API billing synchronization backed by local snapshots."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.operations import operation_store
from app.core.config import load_settings
from app.core.time import as_utc, isoformat, utcnow, zone
from app.domain.identity import BINDING_VERIFIED, PROVIDER_SUB2API
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import ExternalBinding
from app.persistence.models.sub2api import Sub2ApiUsageSnapshot

WINDOW_KINDS = ("five_hour", "today", "seven_day")
USAGE_STALE_AFTER = timedelta(minutes=30)


def _safe_error(exc: BaseException | str) -> str:
    text = str(exc or "Sub2API sync failed")
    text = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1***", text)
    text = re.sub(r"(?i)(x-api-key[=:]\s*)[^\s,;]+", r"\1***", text)
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)
    return text[:500]


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid monetary value") from exc
    if not number.is_finite():
        raise ValueError("invalid monetary value")
    return number.quantize(Decimal("0.0000000001"))


def _integer(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid usage counter") from exc
    if number < 0:
        raise ValueError("invalid usage counter")
    return number


def _parse_remote_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _money_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


class Sub2ApiUsageService:
    def _window_bounds(
        self, kind: str, payload: dict[str, Any], now: datetime
    ) -> tuple[datetime | None, datetime]:
        if kind == "five_hour":
            reset_at = _parse_remote_time(payload.get("resets_at"))
            return (reset_at - timedelta(hours=5) if reset_at else None, now)
        local_zone = zone(load_settings().timezone)
        local_now = now.astimezone(local_zone)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        if kind == "seven_day":
            start_local -= timedelta(days=6)
        return start_local.astimezone(timezone.utc), now

    def _metrics(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload.get("window_stats") if kind == "five_hour" else payload
        if not isinstance(source, dict):
            raise ValueError(f"Sub2API did not return {kind} billing statistics")
        if kind == "seven_day":
            return {
                "request_count": _integer(source.get("total_requests")),
                "total_tokens": _integer(source.get("total_tokens")),
                "standard_cost": _decimal(source.get("total_standard_cost")),
                "user_cost": _decimal(source.get("total_user_cost")),
                # Sub2API AccountUsageSummary.total_cost is account-side cost.
                "account_cost": _decimal(source.get("total_cost")),
            }
        return {
            "request_count": _integer(source.get("requests")),
            "total_tokens": _integer(source.get("tokens")),
            "standard_cost": _decimal(source.get("standard_cost")),
            "user_cost": _decimal(source.get("user_cost")),
            # WindowStats.cost is account-side cost.
            "account_cost": _decimal(source.get("cost")),
        }

    async def _snapshot(
        self, db: AsyncSession, binding_id: int, kind: str
    ) -> Sub2ApiUsageSnapshot | None:
        return (
            await db.execute(
                select(Sub2ApiUsageSnapshot).where(
                    Sub2ApiUsageSnapshot.binding_id == int(binding_id),
                    Sub2ApiUsageSnapshot.window_kind == kind,
                )
            )
        ).scalar_one_or_none()

    async def _record_success(
        self,
        db: AsyncSession,
        binding: ExternalBinding,
        kind: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> Sub2ApiUsageSnapshot:
        row = await self._snapshot(db, binding.id, kind)
        if row is None:
            row = Sub2ApiUsageSnapshot(binding_id=binding.id, window_kind=kind)
            db.add(row)
        metrics = self._metrics(kind, payload)
        start_at, end_at = self._window_bounds(kind, payload, now)
        row.local_account_id = binding.local_account_id
        row.workspace_id = binding.workspace_id
        row.remote_account_id = str(binding.remote_account_id)
        row.window_start_at = start_at
        row.window_end_at = end_at
        row.request_count = metrics["request_count"]
        row.total_tokens = metrics["total_tokens"]
        row.standard_cost = metrics["standard_cost"]
        row.user_cost = metrics["user_cost"]
        row.account_cost = metrics["account_cost"]
        row.sync_status = "success"
        row.last_attempt_at = now
        row.last_success_at = now
        row.error_message = None
        row.updated_at = now
        await db.flush()
        return row

    async def _record_failure(
        self,
        db: AsyncSession,
        binding: ExternalBinding,
        kind: str,
        error: BaseException | str,
        now: datetime,
    ) -> Sub2ApiUsageSnapshot:
        row = await self._snapshot(db, binding.id, kind)
        if row is None:
            row = Sub2ApiUsageSnapshot(
                binding_id=binding.id,
                local_account_id=binding.local_account_id,
                workspace_id=binding.workspace_id,
                remote_account_id=str(binding.remote_account_id),
                window_kind=kind,
            )
            db.add(row)
        row.sync_status = "failed"
        row.last_attempt_at = now
        row.error_message = _safe_error(error)
        row.updated_at = now
        await db.flush()
        return row

    async def _bindings(
        self,
        db: AsyncSession,
        *,
        workspace_id: int | None = None,
        account_id: int | None = None,
    ) -> list[ExternalBinding]:
        stmt = select(ExternalBinding).where(
            ExternalBinding.provider == PROVIDER_SUB2API,
            ExternalBinding.binding_state == BINDING_VERIFIED,
        )
        if workspace_id is not None:
            stmt = stmt.where(ExternalBinding.workspace_id == int(workspace_id))
        if account_id is not None:
            stmt = stmt.where(ExternalBinding.local_account_id == int(account_id))
        return list((await db.execute(stmt.order_by(ExternalBinding.id.asc()))).scalars())

    async def sync(
        self,
        db: AsyncSession,
        *,
        workspace_id: int | None = None,
        account_id: int | None = None,
        source: str = "manual",
        force_usage: bool = False,
    ) -> dict[str, Any]:
        bindings = await self._bindings(db, workspace_id=workspace_id, account_id=account_id)
        operation = await operation_store.create(
            db,
            op_type="sub2api_usage_sync",
            workspace_id=workspace_id or 0,
            account_id=account_id,
            source=source,
            input_payload={
                "workspace_id": workspace_id,
                "account_id": account_id,
                "binding_count": len(bindings),
            },
        )
        if not bindings:
            payload = {
                "success": True,
                "ok": True,
                "status": "success",
                "message": "没有符合条件的 verified Sub2API Binding",
                "binding_count": 0,
                "updated_windows": 0,
                "failed_windows": 0,
                "operation_id": operation.public_id,
            }
            await operation_store.finish(db, operation, payload)
            await db.commit()
            return payload

        usable: list[tuple[ExternalBinding, int]] = []
        invalid: list[ExternalBinding] = []
        for binding in bindings:
            try:
                remote_id = int(binding.remote_account_id)
            except (TypeError, ValueError):
                invalid.append(binding)
                continue
            if remote_id > 0:
                usable.append((binding, remote_id))
            else:
                invalid.append(binding)

        now = utcnow()
        updated = 0
        failed = 0
        for binding in invalid:
            for kind in WINDOW_KINDS:
                await self._record_failure(db, binding, kind, "invalid remote_account_id", now)
                failed += 1

        remote_ids = [remote_id for _binding, remote_id in usable]
        try:
            windows = await sub2api_client.fetch_billing_windows(
                db, remote_ids, force_usage=force_usage
            )
        except Exception as exc:
            error = _safe_error(exc)
            for binding, _remote_id in usable:
                for kind in WINDOW_KINDS:
                    await self._record_failure(db, binding, kind, error, now)
                    failed += 1
            payload = {
                "success": False,
                "ok": False,
                "status": "failed",
                "error_code": "remote_unreachable",
                "error": error,
                "message": "Sub2API 用量同步失败，已保留上一次成功数据",
                "binding_count": len(bindings),
                "updated_windows": updated,
                "failed_windows": failed,
                "operation_id": operation.public_id,
            }
            await operation_store.mark_step(
                db, operation, "sub2api_usage_sync", state="failed", result=payload, error_message=error
            )
            await operation_store.finish(db, operation, payload)
            await db.commit()
            return payload

        for binding, remote_id in usable:
            key = str(remote_id)
            remote_errors = windows.get("errors", {}).get(key, {})
            for kind in WINDOW_KINDS:
                item = windows.get(kind, {}).get(key)
                if isinstance(item, dict):
                    try:
                        await self._record_success(db, binding, kind, item, now)
                        updated += 1
                        continue
                    except Exception as exc:
                        remote_errors = {**remote_errors, kind: _safe_error(exc)}
                error = remote_errors.get(kind) or f"Sub2API did not return {kind} statistics"
                await self._record_failure(db, binding, kind, error, now)
                failed += 1

        status = "success" if failed == 0 else ("partial" if updated else "failed")
        payload = {
            "success": failed == 0,
            "ok": failed == 0,
            "partial": bool(updated and failed),
            "status": status,
            "message": f"Sub2API 用量同步完成：更新 {updated} 个窗口，失败 {failed} 个窗口",
            "binding_count": len(bindings),
            "updated_windows": updated,
            "failed_windows": failed,
            "operation_id": operation.public_id,
        }
        await operation_store.mark_step(
            db,
            operation,
            "sub2api_usage_sync",
            state="success" if failed == 0 else status,
            result=payload,
            error_message="" if failed == 0 else "one or more usage windows failed",
        )
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return payload

    def serialize(self, row: Sub2ApiUsageSnapshot, *, now: datetime | None = None) -> dict[str, Any]:
        stamp = as_utc(now or utcnow())
        last_success = as_utc(row.last_success_at)
        stale = last_success is None or bool(stamp and stamp - last_success > USAGE_STALE_AFTER)
        margin = None
        if row.user_cost is not None and row.account_cost is not None:
            margin = row.user_cost - row.account_cost
        return {
            "window_kind": row.window_kind,
            "window_start_at": isoformat(row.window_start_at),
            "window_end_at": isoformat(row.window_end_at),
            "requests": row.request_count,
            "tokens": row.total_tokens,
            "standard_cost": _money_text(row.standard_cost),
            "user_cost": _money_text(row.user_cost),
            "account_cost": _money_text(row.account_cost),
            "billing_margin": _money_text(margin),
            "sync_status": row.sync_status,
            "stale": stale,
            "last_attempt_at": isoformat(row.last_attempt_at),
            "last_success_at": isoformat(row.last_success_at),
            "error": row.error_message or None,
        }

    async def payloads_by_context(
        self, db: AsyncSession
    ) -> dict[tuple[int, int | None], dict[str, Any]]:
        rows = list(
            (
                await db.execute(
                    select(Sub2ApiUsageSnapshot)
                    .join(ExternalBinding, ExternalBinding.id == Sub2ApiUsageSnapshot.binding_id)
                    .where(
                        ExternalBinding.provider == PROVIDER_SUB2API,
                        ExternalBinding.binding_state == BINDING_VERIFIED,
                        ExternalBinding.remote_account_id == Sub2ApiUsageSnapshot.remote_account_id,
                    )
                    .order_by(Sub2ApiUsageSnapshot.id.asc())
                )
            ).scalars()
        )
        result: dict[tuple[int, int | None], dict[str, Any]] = {}
        for row in rows:
            key = (row.local_account_id, row.workspace_id)
            payload = result.setdefault(
                key,
                {
                    "remote_account_id": row.remote_account_id,
                    "windows": {},
                    "available": False,
                },
            )
            serialized = self.serialize(row)
            payload["windows"][row.window_kind] = serialized
            if row.last_success_at is not None:
                payload["available"] = True
        return result

    def aggregate(self, usages: list[dict[str, Any] | None]) -> dict[str, Any] | None:
        present = [item for item in usages if item]
        if not present:
            return None
        windows: dict[str, Any] = {}
        for kind in WINDOW_KINDS:
            members = [item.get("windows", {}).get(kind) for item in present]
            members = [item for item in members if item and item.get("last_success_at")]
            if not members:
                continue
            complete = len(members) == len(usages)
            windows[kind] = {
                "window_kind": kind,
                "requests": sum(int(item["requests"] or 0) for item in members),
                "tokens": sum(int(item["tokens"] or 0) for item in members),
                "standard_cost": _money_text(sum((_decimal(item["standard_cost"]) for item in members), Decimal("0"))),
                "user_cost": _money_text(sum((_decimal(item["user_cost"]) for item in members), Decimal("0"))),
                "account_cost": _money_text(sum((_decimal(item["account_cost"]) for item in members), Decimal("0"))),
                "billing_margin": _money_text(sum((_decimal(item["billing_margin"]) for item in members), Decimal("0"))),
                "sync_status": "success" if complete and all(item["sync_status"] == "success" for item in members) else "partial",
                "stale": any(bool(item["stale"]) for item in members),
                "last_success_at": min(item["last_success_at"] for item in members),
                "coverage": {"synced": len(members), "total": len(usages)},
            }
        return {"available": bool(windows), "windows": windows}

    async def status(self, db: AsyncSession) -> dict[str, Any]:
        bindings = await self._bindings(db)
        rows = list(
            (
                await db.execute(
                    select(Sub2ApiUsageSnapshot)
                    .join(ExternalBinding, ExternalBinding.id == Sub2ApiUsageSnapshot.binding_id)
                    .where(
                        ExternalBinding.provider == PROVIDER_SUB2API,
                        ExternalBinding.binding_state == BINDING_VERIFIED,
                        ExternalBinding.remote_account_id == Sub2ApiUsageSnapshot.remote_account_id,
                    )
                )
            ).scalars()
        )
        successes = [as_utc(row.last_success_at) for row in rows if row.last_success_at]
        attempts = [as_utc(row.last_attempt_at) for row in rows if row.last_attempt_at]
        return {
            "verified_bindings": len(bindings),
            "snapshot_count": len(rows),
            "failed_windows": sum(1 for row in rows if row.sync_status == "failed"),
            "last_success_at": isoformat(max(successes)) if successes else None,
            "last_attempt_at": isoformat(max(attempts)) if attempts else None,
            "stale": not successes or utcnow() - max(successes) > USAGE_STALE_AFTER,
        }


sub2api_usage_service = Sub2ApiUsageService()
