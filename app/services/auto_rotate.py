"""工作区自动轮转：第 1 层错峰探测 usage，额度恢复后清限流锁并打开调度。

第 2 层：子号 401 自动接码重授权。第 3 层：封禁/周限满踢拉。
两层各自独立开关，默认关。不要接到质保自动踢人任务上。
"""
from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime, time, timedelta
import pytz

from app.config import settings
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SeatEvent, Sub2ApiUsageProbe, Team
from app.services.child_accounts import ACTIVE_CHILD_STATUSES, child_account_service
from app.services.settings import settings_service
from app.services.sub2api import invalidate_status_cache, sub2api_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

DEFAULT_USAGE_PROBE_ENABLED = True
DEFAULT_USAGE_PROBE_INTERVAL_MINUTES = 60
DEFAULT_USAGE_PROBE_STAGGER_MINUTES = 60
DEFAULT_USAGE_PROBE_BATCH_SIZE = 1
DEFAULT_USAGE_PROBE_FORCE = True
DEFAULT_USAGE_PROBE_SCAN_MINUTES = 2
MIN_USAGE_PROBE_INTERVAL_MINUTES = 5
MAX_USAGE_PROBE_INTERVAL_MINUTES = 24 * 60
MIN_USAGE_PROBE_STAGGER_MINUTES = 5
MAX_USAGE_PROBE_STAGGER_MINUTES = 24 * 60
MIN_USAGE_PROBE_BATCH_SIZE = 1
MAX_USAGE_PROBE_BATCH_SIZE = 3
MIN_USAGE_PROBE_SCAN_MINUTES = 1
MAX_USAGE_PROBE_SCAN_MINUTES = 10
MAX_USAGE_PROBE_BACKOFF_HOURS = 6
SUCCESS_JITTER_SECONDS = 90

DEFAULT_AUTO_REAUTH_ENABLED = False
DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES = 30
DEFAULT_AUTO_ROTATE_ENABLED = False
DEFAULT_AUTO_ROTATE_ON_DEACTIVATED = True
DEFAULT_AUTO_ROTATE_ON_WEEKLY_LIMIT = True
DEFAULT_AUTO_ROTATE_FORCE_REFILL = False
DEFAULT_AUTO_ROTATE_DAILY_LIMIT = 2
MIN_AUTO_REAUTH_INTERVAL_MINUTES = 5
MAX_AUTO_REAUTH_INTERVAL_MINUTES = 24 * 60
MIN_REAUTH_BACKOFF_HOURS = 2
MAX_REAUTH_BACKOFF_HOURS = 6
REAUTH_COOLDOWN_CODES = {"sms_rejected", "phone_pool_empty", "sms_failed", "mail_otp_timeout", "mail_otp_rejected"}

TRUTHY = {"1", "true", "yes", "on"}
BLOCKING_KINDS = {"401", "403", "phone", "error"}
PAUSE_KINDS = {"429", "5h"}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in TRUTHY


def clamp_usage_probe_interval_minutes(value: Any) -> int:
    return max(
        MIN_USAGE_PROBE_INTERVAL_MINUTES,
        min(MAX_USAGE_PROBE_INTERVAL_MINUTES, _safe_int(value, DEFAULT_USAGE_PROBE_INTERVAL_MINUTES)),
    )


def clamp_usage_probe_stagger_minutes(value: Any) -> int:
    return max(
        MIN_USAGE_PROBE_STAGGER_MINUTES,
        min(MAX_USAGE_PROBE_STAGGER_MINUTES, _safe_int(value, DEFAULT_USAGE_PROBE_STAGGER_MINUTES)),
    )


def clamp_usage_probe_batch_size(value: Any) -> int:
    return max(
        MIN_USAGE_PROBE_BATCH_SIZE,
        min(MAX_USAGE_PROBE_BATCH_SIZE, _safe_int(value, DEFAULT_USAGE_PROBE_BATCH_SIZE)),
    )


def clamp_usage_probe_scan_minutes(value: Any) -> int:
    return max(
        MIN_USAGE_PROBE_SCAN_MINUTES,
        min(MAX_USAGE_PROBE_SCAN_MINUTES, _safe_int(value, DEFAULT_USAGE_PROBE_SCAN_MINUTES)),
    )


def clamp_auto_reauth_interval_minutes(value: Any) -> int:
    return max(
        MIN_AUTO_REAUTH_INTERVAL_MINUTES,
        min(MAX_AUTO_REAUTH_INTERVAL_MINUTES, _safe_int(value, DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES)),
    )


def reauth_backoff_at(
    now: datetime,
    fail_count: int,
    *,
    error_code: str = "",
    min_hours: int = MIN_REAUTH_BACKOFF_HOURS,
    max_hours: int = MAX_REAUTH_BACKOFF_HOURS,
) -> datetime:
    attempts = max(1, int(fail_count))
    hours = int(min_hours) * (2 ** (attempts - 1))
    if str(error_code or "") in REAUTH_COOLDOWN_CODES:
        hours = max(hours, int(min_hours))
    return now + timedelta(hours=min(int(max_hours), hours))


def rotate_backoff_at(now: datetime, fail_count: int) -> datetime:
    return failure_next_probe_at(now, fail_count, 30 * 60, max_backoff_hours=6)


def is_owner_account(account: Dict[str, Any]) -> bool:
    """旧踢拉/重授权启发式。新绑定不得走这里；Gmail / 名字 / family 不是身份真相。"""
    summary = sub2api_service.summarize_account(account)
    return str(summary.get("role") or "") == "owner"


def classify_rotate_reason(
    *,
    kind: str,
    last_reauth_code: str = "",
    on_deactivated: bool = True,
    on_weekly_limit: bool = True,
) -> Optional[str]:
    """第 3 层候选：封禁或确认周限满。5h 和掉票 401 不进队。"""
    code = str(last_reauth_code or "")
    label = str(kind or "")
    if on_deactivated and code == "account_deactivated":
        return "deactivated"
    if label in {"401", "5h", "phone", "403"}:
        return None
    if on_weekly_limit and label == "429":
        return "weekly_limit"
    return None


def official_weekly_limit_full(usage: Optional[Dict[str, Any]]) -> Optional[bool]:
    """官方查询后 7 日是否仍满。None 表示没读到额度，不能当满额踢。"""
    payload = usage if isinstance(usage, dict) else {}
    seven = payload.get("seven_day") if isinstance(payload.get("seven_day"), dict) else {}
    util = seven.get("utilization")
    if util in (None, ""):
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        util = extra.get("codex_7d_used_percent")
    if util in (None, ""):
        return None
    try:
        percent = max(0, min(100, int(round(float(util)))))
    except (TypeError, ValueError):
        return None
    return percent >= 100


def official_weekly_reset_at(usage: Optional[Dict[str, Any]]) -> Optional[datetime]:
    """官方 7 日窗口结束时间。返回 naive 本地时间，方便存进 SQLite。"""
    payload = usage if isinstance(usage, dict) else {}
    seven = payload.get("seven_day") if isinstance(payload.get("seven_day"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    raw = seven.get("resets_at") or extra.get("codex_7d_reset_at") or extra.get("codex_secondary_reset_at")
    when = sub2api_service._parse_when(raw)
    if when is None:
        return None
    if when.tzinfo is not None:
        tz = pytz.timezone(settings.timezone)
        return when.astimezone(tz).replace(tzinfo=None)
    return when


def daily_auto_rotate_limit_reached(count: int, limit: int = DEFAULT_AUTO_ROTATE_DAILY_LIMIT) -> bool:
    return int(count or 0) >= max(0, int(limit))


def probe_offset_seconds(account_id: int, stagger_seconds: int) -> int:
    """同一账号在窗口内的固定偏移，避免整点齐射。"""
    window = max(1, int(stagger_seconds))
    digest = hashlib.sha1(f"usage-probe:{int(account_id)}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % window


def initial_next_probe_at(
    account_id: int,
    now: datetime,
    stagger_seconds: int,
) -> datetime:
    return now + timedelta(seconds=probe_offset_seconds(account_id, stagger_seconds))


def success_next_probe_at(
    now: datetime,
    interval_seconds: int,
    *,
    jitter_seconds: int = SUCCESS_JITTER_SECONDS,
    rng: Optional[random.Random] = None,
) -> datetime:
    spread = max(0, int(jitter_seconds))
    dice = rng or random
    jitter = dice.randint(-spread, spread) if spread else 0
    delay = max(60, int(interval_seconds) + jitter)
    return now + timedelta(seconds=delay)


def failure_next_probe_at(
    now: datetime,
    fail_count: int,
    interval_seconds: int,
    *,
    max_backoff_hours: int = MAX_USAGE_PROBE_BACKOFF_HOURS,
) -> datetime:
    attempts = max(1, int(fail_count))
    delay = int(interval_seconds) * (2 ** (attempts - 1))
    cap = max(int(interval_seconds), int(max_backoff_hours) * 3600)
    return now + timedelta(seconds=min(delay, cap))


def due_account_ids(
    account_ids: Sequence[int],
    next_probe_at: Dict[int, datetime],
    now: datetime,
    stagger_seconds: int,
    *,
    limit: int,
) -> List[int]:
    """选出本轮到期账号，按到期时间排序，同一窗口不会一次打完全部。"""
    due: List[tuple[datetime, int]] = []
    for raw in account_ids:
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            continue
        scheduled = next_probe_at.get(account_id)
        if scheduled is None:
            scheduled = initial_next_probe_at(account_id, now, stagger_seconds)
        if scheduled <= now:
            due.append((scheduled, account_id))
    due.sort()
    cap = max(0, int(limit))
    return [account_id for _, account_id in due[:cap]]


def desired_schedulable(kind: str, current: Any) -> Optional[bool]:
    """根据额度/健康状态决定是否改 schedulable。None 表示不动。"""
    label = str(kind or "")
    if label in BLOCKING_KINDS:
        return None
    if label in PAUSE_KINDS:
        return False if current is True else None
    if label == "ok":
        return True if current is False else None
    return None


def should_clear_rate_limit(kind: str, account: Dict[str, Any]) -> bool:
    """额度已恢复时清掉 Sub 本地「限流中 / 429」锁。401/仍满额不动。"""
    if str(kind or "") != "ok":
        return False
    return sub2api_service.has_local_rate_limit_lock(account)


class AutoRotateService:
    async def load_usage_probe_settings(self, db_session: AsyncSession) -> Dict[str, Any]:
        enabled_raw = await settings_service.get_setting(
            db_session,
            "usage_probe_enabled",
            str(DEFAULT_USAGE_PROBE_ENABLED).lower(),
        )
        interval_raw = await settings_service.get_setting(
            db_session,
            "usage_probe_interval_minutes",
            str(DEFAULT_USAGE_PROBE_INTERVAL_MINUTES),
        )
        stagger_raw = await settings_service.get_setting(
            db_session,
            "usage_probe_stagger_minutes",
            str(DEFAULT_USAGE_PROBE_STAGGER_MINUTES),
        )
        batch_raw = await settings_service.get_setting(
            db_session,
            "usage_probe_batch_size",
            str(DEFAULT_USAGE_PROBE_BATCH_SIZE),
        )
        force_raw = await settings_service.get_setting(
            db_session,
            "usage_probe_force",
            str(DEFAULT_USAGE_PROBE_FORCE).lower(),
        )
        scan_raw = await settings_service.get_setting(
            db_session,
            "usage_probe_scan_minutes",
            str(DEFAULT_USAGE_PROBE_SCAN_MINUTES),
        )
        return {
            "enabled": _as_bool(enabled_raw, DEFAULT_USAGE_PROBE_ENABLED),
            "interval_minutes": clamp_usage_probe_interval_minutes(interval_raw),
            "stagger_minutes": clamp_usage_probe_stagger_minutes(stagger_raw),
            "batch_size": clamp_usage_probe_batch_size(batch_raw),
            "force": _as_bool(force_raw, DEFAULT_USAGE_PROBE_FORCE),
            "scan_minutes": clamp_usage_probe_scan_minutes(scan_raw),
        }

    async def load_layer_settings(self, db_session: AsyncSession) -> Dict[str, Any]:
        usage = await self.load_usage_probe_settings(db_session)
        auto_reauth_raw = await settings_service.get_setting(
            db_session,
            "auto_reauth_enabled",
            str(DEFAULT_AUTO_REAUTH_ENABLED).lower(),
        )
        auto_reauth_interval_raw = await settings_service.get_setting(
            db_session,
            "auto_reauth_interval_minutes",
            str(DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES),
        )
        auto_rotate_raw = await settings_service.get_setting(
            db_session,
            "auto_rotate_enabled",
            str(DEFAULT_AUTO_ROTATE_ENABLED).lower(),
        )
        on_deactivated_raw = await settings_service.get_setting(
            db_session,
            "auto_rotate_on_deactivated",
            str(DEFAULT_AUTO_ROTATE_ON_DEACTIVATED).lower(),
        )
        on_weekly_raw = await settings_service.get_setting(
            db_session,
            "auto_rotate_on_weekly_limit",
            str(DEFAULT_AUTO_ROTATE_ON_WEEKLY_LIMIT).lower(),
        )
        force_refill_raw = await settings_service.get_setting(
            db_session,
            "auto_rotate_force_refill",
            str(DEFAULT_AUTO_ROTATE_FORCE_REFILL).lower(),
        )
        return {
            **usage,
            "auto_reauth_enabled": _as_bool(auto_reauth_raw, DEFAULT_AUTO_REAUTH_ENABLED),
            "auto_reauth_interval_minutes": clamp_auto_reauth_interval_minutes(
                auto_reauth_interval_raw
            ),
            "auto_rotate_enabled": _as_bool(auto_rotate_raw, DEFAULT_AUTO_ROTATE_ENABLED),
            "auto_rotate_on_deactivated": _as_bool(
                on_deactivated_raw, DEFAULT_AUTO_ROTATE_ON_DEACTIVATED
            ),
            "auto_rotate_on_weekly_limit": _as_bool(
                on_weekly_raw, DEFAULT_AUTO_ROTATE_ON_WEEKLY_LIMIT
            ),
            "auto_rotate_force_refill": False,
            "auto_rotate_daily_limit": DEFAULT_AUTO_ROTATE_DAILY_LIMIT,
        }

    async def _probe_rows(
        self,
        db_session: AsyncSession,
        account_ids: Iterable[int],
    ) -> Dict[int, Sub2ApiUsageProbe]:
        ids = [int(item) for item in account_ids]
        if not ids:
            return {}
        result = await db_session.execute(
            select(Sub2ApiUsageProbe).where(Sub2ApiUsageProbe.sub2api_account_id.in_(ids))
        )
        return {row.sub2api_account_id: row for row in result.scalars().all()}

    async def _ensure_probe_row(
        self,
        db_session: AsyncSession,
        account: Dict[str, Any],
        now: datetime,
        stagger_seconds: int,
        existing: Optional[Sub2ApiUsageProbe] = None,
    ) -> Sub2ApiUsageProbe:
        account_id = int(account["id"])
        email = sub2api_service._account_email(account) or None
        if existing is None:
            existing = Sub2ApiUsageProbe(
                sub2api_account_id=account_id,
                email=email,
                next_probe_at=initial_next_probe_at(account_id, now, stagger_seconds),
                fail_count=0,
                reauth_fail_count=0,
                rotate_fail_count=0,
                created_at=now,
                updated_at=now,
            )
            db_session.add(existing)
            logger.info(
                "usage probe 首次摊开: account_id=%s email=%s next_probe_at=%s",
                account_id,
                email or "",
                existing.next_probe_at,
            )
            return existing
        if email and existing.email != email:
            existing.email = email
        return existing

    def _schedule_kind(self, account: Dict[str, Any]) -> Dict[str, str]:
        snapshot = dict(account)
        snapshot.pop("schedulable", None)
        return sub2api_service._schedule_state(snapshot)

    async def _resolve_active_child(
        self,
        db_session: AsyncSession,
        account: Dict[str, Any],
        email: str = "",
    ) -> Optional[Any]:
        """只认在籍子号。Sub 没邮箱时用账号 ID 找回本地记录。"""
        child = await child_account_service.get_by_email(db_session, email) if email else None
        if child is None:
            child = await child_account_service.get_by_sub2api_account_id(
                db_session,
                account.get("id"),
            )
        if child is None or child.status not in ACTIVE_CHILD_STATUSES or not child.current_team_id:
            return None
        return child

    async def _write_child_probe(
        self,
        db_session: AsyncSession,
        account: Dict[str, Any],
        schedule: Dict[str, str],
    ) -> None:
        email = sub2api_service._account_email(account)
        child = await child_account_service.get_by_email(db_session, email) if email else None
        if child is None:
            child = await child_account_service.get_by_sub2api_account_id(db_session, account.get("id"))
        if child is None:
            return
        account_id = account.get("id")
        if account_id is not None and child.sub2api_account_id != account_id:
            try:
                child.sub2api_account_id = int(account_id)
            except (TypeError, ValueError):
                pass
        await child_account_service.save_probe(db_session, child, schedule)

    async def _apply_rate_limit_clear(
        self,
        db_session: AsyncSession,
        account: Dict[str, Any],
        kind: str,
    ) -> bool:
        if not should_clear_rate_limit(kind, account):
            return False
        account_id = int(account["id"])
        result = await sub2api_service.clear_account_rate_limit(db_session, account_id)
        patched = result if isinstance(result, dict) else {}
        for key in ("rate_limited_at", "rate_limit_reset_at"):
            account[key] = patched.get(key)
        if "schedulable" in patched:
            account["schedulable"] = patched["schedulable"]
        logger.info(
            "usage probe 清除限流锁: account_id=%s email=%s kind=%s",
            account_id,
            sub2api_service._account_email(account),
            kind,
        )
        return True

    async def _apply_schedulable(
        self,
        db_session: AsyncSession,
        account: Dict[str, Any],
        kind: str,
    ) -> Optional[bool]:
        wanted = desired_schedulable(kind, account.get("schedulable"))
        if wanted is None:
            return None
        account_id = int(account["id"])
        result = await sub2api_service.patch_account_fields(
            db_session,
            account_id,
            {"schedulable": wanted},
        )
        patched = result.get("account") if isinstance(result.get("account"), dict) else {}
        if "schedulable" in patched:
            account["schedulable"] = patched["schedulable"]
        else:
            account["schedulable"] = wanted
        logger.info(
            "usage probe 更新 schedulable: account_id=%s email=%s kind=%s schedulable=%s",
            account_id,
            sub2api_service._account_email(account),
            kind,
            wanted,
        )
        return wanted

    async def run_usage_probe_once(
        self,
        db_session: AsyncSession,
        *,
        now: Optional[datetime] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = settings or await self.load_usage_probe_settings(db_session)
        stamp = now or get_now()
        stats: Dict[str, Any] = {
            "enabled": bool(cfg.get("enabled")),
            "scanned": 0,
            "due": 0,
            "probed": 0,
            "opened": 0,
            "paused": 0,
            "cleared": 0,
            "failed": 0,
            "skipped": 0,
            "account_ids": [],
        }
        if not cfg.get("enabled"):
            stats["skipped"] = 1
            return stats

        accounts = await sub2api_service.list_status_accounts(db_session)
        by_id: Dict[int, Dict[str, Any]] = {}
        for account in accounts:
            try:
                account_id = int(account.get("id"))
            except (TypeError, ValueError):
                continue
            by_id[account_id] = account
        stats["scanned"] = len(by_id)
        if not by_id:
            return stats

        stagger_seconds = int(cfg["stagger_minutes"]) * 60
        interval_seconds = int(cfg["interval_minutes"]) * 60
        batch_size = clamp_usage_probe_batch_size(cfg.get("batch_size"))
        rows = await self._probe_rows(db_session, by_id.keys())
        next_map: Dict[int, datetime] = {}
        for account_id, account in by_id.items():
            row = await self._ensure_probe_row(
                db_session,
                account,
                stamp,
                stagger_seconds,
                rows.get(account_id),
            )
            rows[account_id] = row
            next_map[account_id] = row.next_probe_at

        due_ids = due_account_ids(
            list(by_id.keys()),
            next_map,
            stamp,
            stagger_seconds,
            limit=batch_size,
        )
        stats["due"] = len(due_ids)
        stats["account_ids"] = due_ids
        if not due_ids:
            await db_session.commit()
            return stats
        changed = False
        for account_id in due_ids:
            account = dict(by_id[account_id])
            row = rows[account_id]
            try:
                usage = await sub2api_service.fetch_account_usage(
                    db_session,
                    account_id,
                    source="active",
                    force=bool(cfg.get("force")),
                )
                merged = sub2api_service.merge_usage_into_account(account, usage)
                extra = sub2api_service._account_extra(merged)
                has_quota = any(
                    extra.get(key) not in (None, "")
                    for key in (
                        "codex_7d_used_percent",
                        "codex_5h_used_percent",
                        "codex_primary_used_percent",
                    )
                )
                if not has_quota:
                    fresh = await sub2api_service.get_account(db_session, account_id)
                    if fresh:
                        merged = sub2api_service.merge_usage_into_account(fresh, usage)
                schedule = self._schedule_kind(merged)
                if await self._apply_rate_limit_clear(db_session, merged, schedule["kind"]):
                    stats["cleared"] += 1
                    changed = True
                wanted = await self._apply_schedulable(db_session, merged, schedule["kind"])
                if wanted is True:
                    stats["opened"] += 1
                    changed = True
                elif wanted is False:
                    stats["paused"] += 1
                    changed = True
                await self._write_child_probe(db_session, merged, schedule)
                row.fail_count = 0
                row.last_kind = schedule["kind"]
                row.last_label = schedule["label"]
                row.last_error = None
                row.last_probed_at = stamp
                row.next_probe_at = success_next_probe_at(stamp, interval_seconds)
                row.updated_at = stamp
                stats["probed"] += 1
                logger.info(
                    "usage probe 完成: account_id=%s email=%s kind=%s quota=%s next_probe_at=%s",
                    account_id,
                    sub2api_service._account_email(merged),
                    schedule["kind"],
                    sub2api_service._quota_percent(merged),
                    row.next_probe_at,
                )
            except Exception as exc:  # noqa: BLE001
                row.fail_count = int(row.fail_count or 0) + 1
                row.last_error = str(exc)[:500]
                row.last_probed_at = stamp
                row.next_probe_at = failure_next_probe_at(
                    stamp, row.fail_count, interval_seconds
                )
                row.updated_at = stamp
                stats["failed"] += 1
                logger.warning(
                    "usage probe 账号失败: account_id=%s fail_count=%s next_probe_at=%s error=%s",
                    account_id,
                    row.fail_count,
                    row.next_probe_at,
                    exc,
                )
        await db_session.commit()
        if changed:
            invalidate_status_cache()
        return stats

    async def count_today_auto_rotates(
        self,
        db_session: AsyncSession,
        team_id: int,
        now: Optional[datetime] = None,
    ) -> int:
        stamp = now or get_now()
        start = datetime.combine(stamp.date(), time.min)
        result = await db_session.execute(
            select(func.count()).select_from(SeatEvent).where(
                SeatEvent.team_id == int(team_id),
                SeatEvent.action == "rotate",
                SeatEvent.success.is_(True),
                SeatEvent.created_at >= start,
            )
        )
        return int(result.scalar() or 0)

    async def mark_reauth_outcome(
        self,
        db_session: AsyncSession,
        *,
        email: str,
        error_code: str = "",
        success: bool = False,
        now: Optional[datetime] = None,
    ) -> None:
        target = str(email or "").strip().lower()
        if not target:
            return
        stamp = now or get_now()
        result = await db_session.execute(
            select(Sub2ApiUsageProbe).where(Sub2ApiUsageProbe.email == target)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return
        code = str(error_code or "")[:40]
        if success:
            row.reauth_fail_count = 0
            row.last_reauth_code = None
            row.next_reauth_at = stamp + timedelta(minutes=DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES)
        else:
            row.reauth_fail_count = int(row.reauth_fail_count or 0) + 1
            row.last_reauth_code = code or "browser_failed"
            row.next_reauth_at = reauth_backoff_at(stamp, row.reauth_fail_count, error_code=code)
        row.updated_at = stamp
        await db_session.commit()

    async def run_auto_reauth_once(
        self,
        db_session: AsyncSession,
        *,
        now: Optional[datetime] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from app.routes.seats import start_child_auto_reauth
        from app.services import onboard_jobs
        from app.services.identity import identity_service
        from app.services.mail_otp import parse_mail_line
        from app.services.reauth import auto_reauth_plan

        cfg = settings or await self.load_layer_settings(db_session)
        stamp = now or get_now()
        stats: Dict[str, Any] = {
            "enabled": bool(cfg.get("auto_reauth_enabled")),
            "scanned": 0,
            "queued": 0,
            "skipped": 0,
            "failed": 0,
            "deactivated": 0,
            "conflict": 0,
            "email": "",
        }
        if not cfg.get("auto_reauth_enabled"):
            stats["skipped"] = 1
            return stats
        busy = onboard_jobs.any_running(onboard_jobs.BROWSER_ACTIONS)
        if busy:
            stats["skipped"] = 1
            stats["email"] = str(busy.get("email") or "")
            logger.info("第 2 层跳过：已有浏览器任务 email=%s", stats["email"])
            return stats

        accounts = await sub2api_service.list_status_accounts(db_session)
        rows = await self._probe_rows(
            db_session,
            [int(item["id"]) for item in accounts if item.get("id") is not None],
        )
        candidates: List[tuple[datetime, Dict[str, Any], Sub2ApiUsageProbe, Any, Team]] = []
        for account in accounts:
            try:
                account_id = int(account.get("id"))
            except (TypeError, ValueError):
                continue
            schedule = self._schedule_kind(account)
            if schedule.get("kind") != "401":
                continue
            row = rows.get(account_id)
            if row and row.last_reauth_code in {"account_deactivated", "identity_conflict"}:
                continue
            if row and row.next_reauth_at and row.next_reauth_at > stamp:
                continue
            email = sub2api_service._account_email(account)
            child = await self._resolve_active_child(db_session, account, email)
            gate = await identity_service.automation_gate(
                db_session,
                remote_account_id=account.get("id"),
                email=email,
                child=child,
            )
            if not gate.get("allow"):
                stats["skipped"] += 1
                if gate.get("error_code") == "identity_conflict":
                    stats["conflict"] += 1
                    if not row:
                        row = await self._ensure_probe_row(db_session, account, stamp, 3600, None)
                    row.last_reauth_code = "identity_conflict"
                    row.updated_at = stamp
                logger.info(
                    "第 2 层跳过 account_id=%s email=%s: %s",
                    account_id,
                    email or "",
                    gate.get("reason") or gate.get("error_code"),
                )
                continue
            if child is None:
                stats["skipped"] += 1
                logger.info("第 2 层跳过 account_id=%s: 对不上在籍子号", account_id)
                continue
            email = (email or child.email or "").strip()
            if not email:
                stats["skipped"] += 1
                logger.info("第 2 层跳过 account_id=%s: 对不上邮箱", account_id)
                continue
            team = await db_session.get(Team, int(child.current_team_id))
            if team is None:
                stats["skipped"] += 1
                logger.info("第 2 层跳过 %s: 对不上 Team", email)
                continue
            pickup_url = parse_mail_line(child.mail_raw).get("pickup_url") if child.mail_raw else ""
            from app.services.onboard import onboard_service
            cf_config = await onboard_service._cf_config(db_session)
            plan = auto_reauth_plan(
                email=email,
                role="child",
                password=child_account_service.decrypt_secret(child.password_encrypted),
                pickup_url=pickup_url or "",
                cf_ready=(not pickup_url) and bool(cf_config["admin_password"]),
                proxy=(child.proxy or team.proxy or ""),
            )
            if not plan.get("auto"):
                stats["skipped"] += 1
                logger.info("第 2 层跳过 %s: %s", email, plan.get("reason"))
                continue
            if onboard_jobs.active_job_for_email(email):
                stats["skipped"] += 1
                continue
            due_at = row.next_reauth_at if row and row.next_reauth_at else stamp
            candidates.append((due_at, account, row, child, team))
        stats["scanned"] = len(candidates)
        if not candidates:
            await db_session.commit()
            return stats
        candidates.sort(key=lambda item: (item[0], int(item[1].get("id") or 0)))
        account, row, child, team = candidates[0][1], candidates[0][2], candidates[0][3], candidates[0][4]
        email = (sub2api_service._account_email(account) or child.email or "").strip()
        result = await start_child_auto_reauth(db_session, team=team, email=email, child=child)
        if result.get("skipped") and result.get("error_code") in {"already_running", "browser_busy"}:
            stats["skipped"] += 1
            stats["email"] = email
            return stats
        if not row:
            row = await self._ensure_probe_row(db_session, account, stamp, 3600, None)
        if result.get("success") and not result.get("skipped"):
            row.reauth_fail_count = 0
            row.last_reauth_code = None
            row.next_reauth_at = stamp + timedelta(minutes=int(cfg.get("auto_reauth_interval_minutes") or 30))
            row.updated_at = stamp
            stats["queued"] = 1
            stats["email"] = email
            logger.info("第 2 层已排队重授权: email=%s job_id=%s", email, result.get("job_id"))
        else:
            code = str(result.get("error_code") or "browser_failed")
            row.reauth_fail_count = int(row.reauth_fail_count or 0) + 1
            row.last_reauth_code = code[:40]
            row.next_reauth_at = reauth_backoff_at(stamp, row.reauth_fail_count, error_code=code)
            row.updated_at = stamp
            if code == "account_deactivated":
                stats["deactivated"] = 1
            else:
                stats["failed"] = 1
            stats["email"] = email
            logger.info(
                "第 2 层未启动: email=%s code=%s next_reauth_at=%s error=%s",
                email,
                code,
                row.next_reauth_at,
                result.get("error"),
            )
        await db_session.commit()
        return stats

    async def run_auto_rotate_once(
        self,
        db_session: AsyncSession,
        *,
        now: Optional[datetime] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from app.services import onboard_jobs
        from app.services.onboard import onboard_service

        cfg = settings or await self.load_layer_settings(db_session)
        stamp = now or get_now()
        daily_limit = int(cfg.get("auto_rotate_daily_limit") or DEFAULT_AUTO_ROTATE_DAILY_LIMIT)
        stats: Dict[str, Any] = {
            "enabled": bool(cfg.get("auto_rotate_enabled")),
            "scanned": 0,
            "rotated": 0,
            "kicked_only": 0,
            "skipped": 0,
            "failed": 0,
            "capped": 0,
            "email": "",
            "reason": "",
        }
        if not cfg.get("auto_rotate_enabled"):
            stats["skipped"] = 1
            return stats
        busy = onboard_jobs.any_running(onboard_jobs.BROWSER_ACTIONS)
        if busy:
            stats["skipped"] = 1
            stats["email"] = str(busy.get("email") or "")
            logger.info("第 3 层跳过：已有浏览器任务 email=%s", stats["email"])
            return stats

        accounts = await sub2api_service.list_status_accounts(db_session)
        rows = await self._probe_rows(
            db_session,
            [int(item["id"]) for item in accounts if item.get("id") is not None],
        )
        occupied_teams: set[int] = set()
        for job in onboard_jobs.iter_running(("rotate", "onboard")):
            try:
                occupied_teams.add(int(job.get("team_id")))
            except (TypeError, ValueError):
                continue
        candidates: List[tuple[datetime, Dict[str, Any], Sub2ApiUsageProbe, Any, str]] = []
        for account in accounts:
            if is_owner_account(account):
                continue
            email = sub2api_service._account_email(account)
            if not email:
                continue
            try:
                account_id = int(account.get("id"))
            except (TypeError, ValueError):
                continue
            row = rows.get(account_id)
            schedule = self._schedule_kind(account)
            reason = classify_rotate_reason(
                kind=schedule.get("kind") or "",
                last_reauth_code=(row.last_reauth_code if row else "") or "",
                on_deactivated=bool(cfg.get("auto_rotate_on_deactivated", True)),
                on_weekly_limit=bool(cfg.get("auto_rotate_on_weekly_limit", True)),
            )
            if not reason:
                continue
            if row and row.next_rotate_at and row.next_rotate_at > stamp:
                continue
            child = await child_account_service.get_by_email(db_session, email)
            if child is None or child.status not in ACTIVE_CHILD_STATUSES or not child.current_team_id:
                continue
            team_id = int(child.current_team_id)
            if team_id in occupied_teams:
                continue
            due_at = row.next_rotate_at if row and row.next_rotate_at else stamp
            candidates.append((due_at, account, row, child, reason))
        stats["scanned"] = len(candidates)
        if not candidates:
            return stats
        candidates.sort(key=lambda item: (item[0], int(item[1].get("id") or 0)))
        account, row, child, reason = candidates[0][1], candidates[0][2], candidates[0][3], candidates[0][4]
        email = sub2api_service._account_email(account)
        team_id = int(child.current_team_id)
        today_count = await self.count_today_auto_rotates(db_session, team_id, stamp)
        if daily_auto_rotate_limit_reached(today_count, daily_limit):
            stats["capped"] = 1
            stats["skipped"] = 1
            stats["email"] = email
            logger.info("第 3 层达到每日上限: team_id=%s count=%s limit=%s", team_id, today_count, daily_limit)
            return stats
        if not row:
            row = await self._ensure_probe_row(db_session, account, stamp, 3600, None)
        usage = None
        if reason == "weekly_limit":
            try:
                account_id = int(account.get("id"))
            except (TypeError, ValueError):
                account_id = 0
            if not account_id:
                stats["skipped"] = 1
                stats["email"] = email
                stats["reason"] = reason
                logger.info("第 3 层跳过 %s: 周限满缺少 Sub 账号 ID", email)
                await db_session.commit()
                return stats
            try:
                usage = await sub2api_service.fetch_account_usage(
                    db_session,
                    account_id,
                    source="active",
                    force=True,
                )
            except Exception as exc:  # noqa: BLE001
                row.rotate_fail_count = int(row.rotate_fail_count or 0) + 1
                row.last_rotate_code = "usage_confirm_failed"
                row.next_rotate_at = rotate_backoff_at(stamp, row.rotate_fail_count)
                row.updated_at = stamp
                stats["failed"] = 1
                stats["email"] = email
                stats["reason"] = reason
                logger.warning(
                    "第 3 层踢前官方查询失败: email=%s account_id=%s error=%s",
                    email,
                    account_id,
                    exc,
                )
                await db_session.commit()
                return stats
            still_full = official_weekly_limit_full(usage)
            if still_full is not True:
                merged = sub2api_service.merge_usage_into_account(account, usage)
                schedule = self._schedule_kind(merged)
                await self._write_child_probe(db_session, merged, schedule)
                seven = usage.get("seven_day") if isinstance(usage, dict) else None
                util = seven.get("utilization") if isinstance(seven, dict) else None
                row.last_kind = schedule.get("kind")
                row.last_label = schedule.get("label")
                row.last_rotate_code = "weekly_limit_not_confirmed"
                row.rotate_fail_count = 0
                row.next_rotate_at = stamp + timedelta(hours=1)
                row.updated_at = stamp
                stats["skipped"] = 1
                stats["email"] = email
                stats["reason"] = reason
                logger.info(
                    "第 3 层跳过 %s: 看板 429 但官方 7 日未满 util=%s kind=%s",
                    email,
                    util,
                    schedule.get("kind"),
                )
                await db_session.commit()
                return stats
        next_eligible_at = official_weekly_reset_at(usage) if reason == "weekly_limit" else None
        job = onboard_jobs.create_job(
            team_id=team_id,
            email=email,
            action="rotate",
            input_payload={
                "team_id": team_id,
                "email": email,
                "reason": reason,
                "force_refill": bool(cfg.get("auto_rotate_force_refill")),
                "next_eligible_at": next_eligible_at.isoformat() if next_eligible_at else None,
            },
        )
        result = await onboard_service.kick_and_refill(
            db_session,
            team_id=team_id,
            email=email,
            force_refill=bool(cfg.get("auto_rotate_force_refill")),
            job_id=job["id"],
            reason=reason,
            next_eligible_at=next_eligible_at,
        )
        code = str(result.get("error_code") or "")
        if result.get("success") or result.get("rotated") or code == "vacancy_not_safe_to_refill":
            onboard_jobs.finish(job["id"], {"success": bool(result.get("success")), **result})
            row.rotate_fail_count = 0
            row.last_rotate_code = code[:40] if code else None
            row.next_rotate_at = stamp + timedelta(hours=12)
            row.updated_at = stamp
            stats["email"] = email
            stats["reason"] = reason
            if result.get("success"):
                stats["rotated"] = 1
            else:
                stats["kicked_only"] = 1
            logger.info(
                "第 3 层踢拉完成: email=%s reason=%s success=%s code=%s today=%s",
                email,
                reason,
                result.get("success"),
                code or "ok",
                today_count + 1,
            )
        else:
            onboard_jobs.finish(job["id"], {"success": False, **result})
            row.rotate_fail_count = int(row.rotate_fail_count or 0) + 1
            row.last_rotate_code = (code or "rotate_failed")[:40]
            row.next_rotate_at = rotate_backoff_at(stamp, row.rotate_fail_count)
            row.updated_at = stamp
            stats["failed"] = 1
            stats["email"] = email
            stats["reason"] = reason
            logger.warning(
                "第 3 层踢拉失败: email=%s reason=%s code=%s error=%s",
                email,
                reason,
                code,
                result.get("error"),
            )
        await db_session.commit()
        return stats


auto_rotate_service = AutoRotateService()
