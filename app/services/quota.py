"""Phase 3 Official Quota。

本地 Token + Workspace ID + 代理直打 OpenAI。
失败只写 quota_snapshots，不改业务状态、不踢人、不动 Sub2API schedulable。
定时任务默认关。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.integrations.openai.quota import (
    SOURCE_OFFICIAL,
    QuotaResult,
    openai_quota_client,
)
from app.models import Account, QuotaSnapshot, WorkspaceMembership
from app.services.encryption import encryption_service
from app.services.settings import settings_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_PROBE_ENABLED = False
DEFAULT_QUOTA_PROBE_INTERVAL_MINUTES = 60
DEFAULT_QUOTA_PROBE_STAGGER_MINUTES = 60
DEFAULT_QUOTA_PROBE_BATCH_SIZE = 1
DEFAULT_QUOTA_PROBE_SCAN_MINUTES = 2
MIN_QUOTA_PROBE_INTERVAL_MINUTES = 5
MAX_QUOTA_PROBE_INTERVAL_MINUTES = 24 * 60
MIN_QUOTA_PROBE_STAGGER_MINUTES = 5
MAX_QUOTA_PROBE_STAGGER_MINUTES = 24 * 60
MIN_QUOTA_PROBE_BATCH_SIZE = 1
MAX_QUOTA_PROBE_BATCH_SIZE = 3
MIN_QUOTA_PROBE_SCAN_MINUTES = 1
MAX_QUOTA_PROBE_SCAN_MINUTES = 10
SUCCESS_JITTER_SECONDS = 90
FAILURE_BACKOFF_MINUTES = (5, 15, 30, 60)
TRUTHY = {"1", "true", "yes", "on"}
SKIP_OPERATIONAL_STATES = {"disabled", "archived"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in TRUTHY


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_quota_probe_interval_minutes(value: Any) -> int:
    return max(
        MIN_QUOTA_PROBE_INTERVAL_MINUTES,
        min(MAX_QUOTA_PROBE_INTERVAL_MINUTES, _safe_int(value, DEFAULT_QUOTA_PROBE_INTERVAL_MINUTES)),
    )


def clamp_quota_probe_stagger_minutes(value: Any) -> int:
    return max(
        MIN_QUOTA_PROBE_STAGGER_MINUTES,
        min(MAX_QUOTA_PROBE_STAGGER_MINUTES, _safe_int(value, DEFAULT_QUOTA_PROBE_STAGGER_MINUTES)),
    )


def clamp_quota_probe_batch_size(value: Any) -> int:
    return max(
        MIN_QUOTA_PROBE_BATCH_SIZE,
        min(MAX_QUOTA_PROBE_BATCH_SIZE, _safe_int(value, DEFAULT_QUOTA_PROBE_BATCH_SIZE)),
    )


def clamp_quota_probe_scan_minutes(value: Any) -> int:
    return max(
        MIN_QUOTA_PROBE_SCAN_MINUTES,
        min(MAX_QUOTA_PROBE_SCAN_MINUTES, _safe_int(value, DEFAULT_QUOTA_PROBE_SCAN_MINUTES)),
    )


def quota_slot_minute_for(account_id: int) -> int:
    digest = hashlib.sha1(f"official-quota:{int(account_id)}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 60


def initial_next_quota_probe_at(account_id: int, now: datetime, stagger_minutes: int) -> datetime:
    window = max(1, int(stagger_minutes))
    digest = hashlib.sha1(f"official-quota-init:{int(account_id)}".encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % (window * 60)
    return now + timedelta(seconds=offset)


def success_next_quota_probe_at(
    now: datetime,
    slot_minute: int,
    *,
    jitter_seconds: int = SUCCESS_JITTER_SECONDS,
    rng: Optional[random.Random] = None,
) -> datetime:
    slot = int(slot_minute) % 60
    next_slot = now.replace(minute=slot, second=0, microsecond=0)
    if next_slot <= now:
        next_slot += timedelta(hours=1)
    spread = max(0, int(jitter_seconds))
    dice = rng or random
    jitter = dice.randint(0, spread) if spread else 0
    return next_slot + timedelta(seconds=jitter)


def failure_next_quota_probe_at(now: datetime, fail_count: int) -> datetime:
    attempts = max(1, int(fail_count))
    if attempts <= len(FAILURE_BACKOFF_MINUTES):
        minutes = FAILURE_BACKOFF_MINUTES[attempts - 1]
    else:
        minutes = FAILURE_BACKOFF_MINUTES[-1]
    return now + timedelta(minutes=minutes)


def due_quota_account_ids(
    accounts: Sequence[Account],
    now: datetime,
    *,
    limit: int,
) -> List[int]:
    due: List[tuple[datetime, int]] = []
    for account in accounts:
        scheduled = account.next_quota_probe_at
        if scheduled is None or scheduled <= now:
            due.append((scheduled or now, int(account.id)))
    due.sort()
    cap = max(0, int(limit))
    return [account_id for _, account_id in due[:cap]]


def decrypt_access_token(account: Account) -> Optional[str]:
    raw = account.access_token_encrypted
    if not raw:
        return None
    try:
        token = encryption_service.decrypt_token(raw)
    except Exception:
        return None
    token = str(token or "").strip()
    return token or None


def resolve_chatgpt_account_id(account: Account) -> Optional[str]:
    official = str(account.official_account_id or "").strip()
    if official:
        return official
    for membership in account.memberships or []:
        workspace = membership.workspace
        if workspace is None:
            continue
        workspace_id = str(workspace.official_workspace_id or "").strip()
        if workspace_id:
            return workspace_id
    for workspace in account.owned_workspaces or []:
        workspace_id = str(workspace.official_workspace_id or "").strip()
        if workspace_id:
            return workspace_id
    return None


def snapshot_from_result(account_id: int, result: QuotaResult, now: datetime) -> QuotaSnapshot:
    queried_at = result.queried_at or now
    return QuotaSnapshot(
        account_id=account_id,
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


def serialize_snapshot(row: QuotaSnapshot) -> Dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
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


def official_overrides_sub2api_stale(official: Optional[QuotaSnapshot], sub2api_kind: Optional[str]) -> bool:
    """Sub2API 打 429 标签时，官方 7 日 0% 仍以官方为准。失败快照不能覆盖。"""
    if official is None or not official.success or official.source != SOURCE_OFFICIAL:
        return False
    if official.seven_day_used_percent is None:
        return False
    return str(sub2api_kind or "") == "429" and int(official.seven_day_used_percent) < 100


class QuotaService:
    def __init__(self, client=openai_quota_client):
        self.client = client

    async def load_settings(self, db_session: AsyncSession) -> Dict[str, Any]:
        enabled_raw = await settings_service.get_setting(
            db_session,
            "official_quota_probe_enabled",
            str(DEFAULT_QUOTA_PROBE_ENABLED).lower(),
        )
        interval_raw = await settings_service.get_setting(
            db_session,
            "official_quota_probe_interval_minutes",
            str(DEFAULT_QUOTA_PROBE_INTERVAL_MINUTES),
        )
        stagger_raw = await settings_service.get_setting(
            db_session,
            "official_quota_probe_stagger_minutes",
            str(DEFAULT_QUOTA_PROBE_STAGGER_MINUTES),
        )
        batch_raw = await settings_service.get_setting(
            db_session,
            "official_quota_probe_batch_size",
            str(DEFAULT_QUOTA_PROBE_BATCH_SIZE),
        )
        scan_raw = await settings_service.get_setting(
            db_session,
            "official_quota_probe_scan_minutes",
            str(DEFAULT_QUOTA_PROBE_SCAN_MINUTES),
        )
        return {
            "enabled": _as_bool(enabled_raw, DEFAULT_QUOTA_PROBE_ENABLED),
            "interval_minutes": clamp_quota_probe_interval_minutes(interval_raw),
            "stagger_minutes": clamp_quota_probe_stagger_minutes(stagger_raw),
            "batch_size": clamp_quota_probe_batch_size(batch_raw),
            "scan_minutes": clamp_quota_probe_scan_minutes(scan_raw),
        }

    async def latest_official(self, db_session: AsyncSession, account_id: int) -> Optional[QuotaSnapshot]:
        result = await db_session.execute(
            select(QuotaSnapshot)
            .where(
                QuotaSnapshot.account_id == account_id,
                QuotaSnapshot.source == SOURCE_OFFICIAL,
            )
            .order_by(QuotaSnapshot.queried_at.desc(), QuotaSnapshot.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def run_probe_once(
        self,
        db_session: AsyncSession,
        *,
        now: Optional[datetime] = None,
        settings: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        cfg = settings or await self.load_settings(db_session)
        stamp = now or get_now()
        stats: Dict[str, Any] = {
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

        result = await db_session.execute(
            select(Account)
            .options(
                selectinload(Account.memberships).selectinload(WorkspaceMembership.workspace),
                selectinload(Account.owned_workspaces),
            )
            .where(Account.access_token_encrypted.is_not(None))
        )
        accounts = [
            account
            for account in result.scalars().all()
            if str(account.operational_state or "") not in SKIP_OPERATIONAL_STATES
        ]
        stats["scanned"] = len(accounts)
        if not accounts:
            return stats

        stagger_minutes = int(cfg["stagger_minutes"])
        batch_size = clamp_quota_probe_batch_size(cfg.get("batch_size"))
        by_id = {int(account.id): account for account in accounts}
        for account in accounts:
            if account.quota_slot_minute is None:
                account.quota_slot_minute = quota_slot_minute_for(account.id)
            if account.next_quota_probe_at is None:
                account.next_quota_probe_at = initial_next_quota_probe_at(
                    account.id, stamp, stagger_minutes
                )
            if account.quota_probe_fail_count is None:
                account.quota_probe_fail_count = 0

        due_ids = due_quota_account_ids(accounts, stamp, limit=batch_size)
        stats["due"] = len(due_ids)
        stats["account_ids"] = due_ids
        if not due_ids:
            await db_session.commit()
            return stats

        for account_id in due_ids:
            account = by_id[account_id]
            before_state = account.operational_state
            before_purpose = account.local_purpose
            try:
                result_row = await self.probe_account(db_session, account, now=stamp)
            except Exception as exc:  # noqa: BLE001
                result_row = QuotaResult(
                    success=False,
                    error_code="probe_exception",
                    error_message=str(exc)[:500],
                    queried_at=stamp,
                )
                db_session.add(snapshot_from_result(account.id, result_row, stamp))
                account.quota_probe_fail_count = int(account.quota_probe_fail_count or 0) + 1
                account.next_quota_probe_at = failure_next_quota_probe_at(
                    stamp, account.quota_probe_fail_count
                )
                stats["failed"] += 1
                logger.warning(
                    "official quota probe 异常: account_id=%s email=%s error=%s",
                    account.id,
                    account.email,
                    exc,
                )
            else:
                if result_row.success:
                    stats["probed"] += 1
                else:
                    stats["failed"] += 1
            if account.operational_state != before_state or account.local_purpose != before_purpose:
                logger.error(
                    "official quota probe 试图改业务状态，已拒绝: account_id=%s",
                    account.id,
                )
                account.operational_state = before_state
                account.local_purpose = before_purpose

        await db_session.commit()
        return stats

    async def probe_account(
        self,
        db_session: AsyncSession,
        account: Account,
        *,
        now: Optional[datetime] = None,
    ) -> QuotaResult:
        stamp = now or get_now()
        before_state = account.operational_state
        before_purpose = account.local_purpose
        token = decrypt_access_token(account)
        chatgpt_account_id = resolve_chatgpt_account_id(account)
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
                db_session=db_session,
                workspace_id=chatgpt_account_id,
                identifier=account.email or "default",
                now=stamp,
            )
        db_session.add(snapshot_from_result(account.id, result, stamp))
        if result.success:
            account.quota_probe_fail_count = 0
            account.next_quota_probe_at = success_next_quota_probe_at(
                stamp, account.quota_slot_minute or quota_slot_minute_for(account.id)
            )
        else:
            account.quota_probe_fail_count = int(account.quota_probe_fail_count or 0) + 1
            account.next_quota_probe_at = failure_next_quota_probe_at(
                stamp, account.quota_probe_fail_count
            )
        account.operational_state = before_state
        account.local_purpose = before_purpose
        logger.info(
            "official quota probe: account_id=%s email=%s success=%s 7d=%s 5h=%s next=%s error=%s",
            account.id,
            account.email,
            result.success,
            result.seven_day_used_percent,
            result.five_hour_used_percent,
            account.next_quota_probe_at,
            result.error_code,
        )
        return result


quota_service = QuotaService()


async def _run_cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 Official Quota")
    parser.add_argument("command", choices=("probe", "latest"), help="probe 错峰探测一轮；latest 读最近官方快照")
    parser.add_argument("--email", help="latest 指定邮箱")
    parser.add_argument("--force", action="store_true", help="忽略 enabled=false，仍只探测到期账号")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        if args.command == "probe":
            report = await quota_service.run_probe_once(session, force=bool(args.force))
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0
        if not args.email:
            raise SystemExit("latest 需要 --email")
        account = (
            await session.execute(select(Account).where(Account.email == str(args.email).strip().lower()))
        ).scalar_one_or_none()
        if account is None:
            raise SystemExit(f"找不到账号 {args.email}")
        row = await quota_service.latest_official(session, account.id)
        payload = serialize_snapshot(row) if row else None
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    import asyncio

    raise SystemExit(asyncio.run(_run_cli(argv)))


if __name__ == "__main__":
    main()
