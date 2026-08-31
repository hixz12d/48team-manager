"""本地接码号码池：导入、租约领取、按 OpenAI 回执记账。"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Operation, PhoneAttempt, PhonePool
from app.services.sms import parse_phone_line
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_MAXED = "maxed"
STATUS_DISABLED = "disabled"
STATUS_RISK = "risk"

OUTCOME_SUCCESS = "success"
OUTCOME_INVALID = "invalid"
OUTCOME_RECENTLY_USED = "recently_used"
OUTCOME_RISK = "risk"
OUTCOME_NO_SMS = "no_sms"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UNRELATED = "unrelated"
OUTCOME_PROVIDER_ERROR = "provider_error"

SETTING_MAX_USES = "sms_max_uses_per_phone"
SETTING_COOLDOWN_SEC = "sms_cooldown_sec"
SETTING_RESERVE_SEC = "sms_reserve_sec"
SETTING_MAX_RETRIES = "sms_max_phone_retries"

DEFAULT_MAX_USES = 3
DEFAULT_COOLDOWN_SEC = 1200
DEFAULT_RESERVE_SEC = 180
DEFAULT_MAX_RETRIES = 3

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class PhonePoolEmpty(Exception):
    def __init__(self, message: str, error_code: str = "phone_pool_empty") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class PhonePoolConfig:
    max_uses: int = DEFAULT_MAX_USES
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC
    reserve_sec: int = DEFAULT_RESERVE_SEC
    max_retries: int = DEFAULT_MAX_RETRIES


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def normalize_phone_number(value: str) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise ValueError("号码为空")
    if len(digits) == 10:
        digits = "1" + digits
    number = "+" + digits
    if not _E164_RE.fullmatch(number):
        raise ValueError(f"号码格式无效: {raw}")
    return number


def effective_max_uses(row: PhonePool, cfg: PhonePoolConfig) -> int:
    if row.max_uses is None:
        return cfg.max_uses
    try:
        value = int(row.max_uses)
    except (TypeError, ValueError):
        return cfg.max_uses
    return max(1, value)


class PhonePoolService:
    def _lease_anchor(self, row: PhonePool):
        return row.lease_heartbeat_at or row.reserved_at

    def _lease_live(self, row: PhonePool, reserve_cutoff) -> bool:
        if not row.reserved_by:
            return False
        anchor = self._lease_anchor(row)
        return anchor is None or anchor >= reserve_cutoff

    def _lease_free_clause(self, reserve_cutoff):
        return or_(
            PhonePool.reserved_by.is_(None),
            PhonePool.lease_heartbeat_at.is_not(None) & (PhonePool.lease_heartbeat_at < reserve_cutoff),
            PhonePool.lease_heartbeat_at.is_(None) & or_(
                PhonePool.reserved_at.is_(None),
                PhonePool.reserved_at < reserve_cutoff,
            ),
        )

    async def _operation_id(self, session: AsyncSession, job_id: str) -> Optional[int]:
        if not job_id:
            return None
        row = await session.scalar(select(Operation.id).where(Operation.public_id == job_id))
        return int(row) if row is not None else None

    async def get_config(self, session: AsyncSession) -> PhonePoolConfig:
        from app.services.settings import settings_service

        max_uses = await settings_service.get_setting(
            session, SETTING_MAX_USES, str(DEFAULT_MAX_USES), use_cache=False
        )
        cooldown = await settings_service.get_setting(
            session, SETTING_COOLDOWN_SEC, str(DEFAULT_COOLDOWN_SEC), use_cache=False
        )
        reserve = await settings_service.get_setting(
            session, SETTING_RESERVE_SEC, str(DEFAULT_RESERVE_SEC), use_cache=False
        )
        retries = await settings_service.get_setting(
            session, SETTING_MAX_RETRIES, str(DEFAULT_MAX_RETRIES), use_cache=False
        )
        return PhonePoolConfig(
            max_uses=_clamp_int(max_uses, DEFAULT_MAX_USES, 1, 20),
            cooldown_sec=_clamp_int(cooldown, DEFAULT_COOLDOWN_SEC, 60, 86400),
            reserve_sec=_clamp_int(reserve, DEFAULT_RESERVE_SEC, 60, 7200),
            max_retries=_clamp_int(retries, DEFAULT_MAX_RETRIES, 1, 10),
        )

    def serialize(self, row: PhonePool, cfg: Optional[PhonePoolConfig] = None) -> Dict[str, Any]:
        cfg = cfg or PhonePoolConfig()
        max_uses = effective_max_uses(row, cfg)
        remaining = max(0, max_uses - int(row.used_count or 0))
        if row.status != STATUS_ACTIVE:
            remaining = 0
        return {
            "id": row.id,
            "number": row.number,
            "sms_url": row.sms_url,
            "status": row.status,
            "used_count": int(row.used_count or 0),
            "max_uses": max_uses,
            "remaining": remaining,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else "",
            "last_success_at": row.last_success_at.isoformat() if row.last_success_at else "",
            "last_error": row.last_error or "",
            "last_error_type": row.last_error_type or "",
            "reserved_by": row.reserved_by or "",
            "reserved_at": row.reserved_at.isoformat() if row.reserved_at else "",
            "lease_heartbeat_at": row.lease_heartbeat_at.isoformat() if row.lease_heartbeat_at else "",
            "risk_count": int(row.risk_count or 0),
            "no_sms_streak": int(row.no_sms_streak or 0),
            "note": row.note or "",
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        }

    async def expire_leases(self, session: AsyncSession, cfg: Optional[PhonePoolConfig] = None) -> int:
        cfg = cfg or await self.get_config(session)
        cutoff = get_now() - timedelta(seconds=cfg.reserve_sec)
        result = await session.execute(
            update(PhonePool)
            .where(
                PhonePool.reserved_by.is_not(None),
                self._lease_free_clause(cutoff),
            )
            .values(
                reserved_by=None,
                reserved_at=None,
                lease_heartbeat_at=None,
                operation_id=None,
                updated_at=get_now(),
            )
        )
        return int(result.rowcount or 0)

    async def promote_maxed(self, session: AsyncSession, cfg: Optional[PhonePoolConfig] = None) -> int:
        cfg = cfg or await self.get_config(session)
        result = await session.execute(
            update(PhonePool)
            .where(
                PhonePool.status == STATUS_ACTIVE,
                PhonePool.used_count >= func.coalesce(PhonePool.max_uses, cfg.max_uses),
            )
            .values(status=STATUS_MAXED, updated_at=get_now())
        )
        return int(result.rowcount or 0)

    async def import_lines(self, session: AsyncSession, raw_text: str) -> Dict[str, Any]:
        lines = str(raw_text or "").splitlines()
        imported = 0
        skipped = 0
        errors: List[Dict[str, Any]] = []
        seen: set[str] = set()
        now = get_now()
        for index, line in enumerate(lines, start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            number_raw, sms_url = parse_phone_line(text)
            if "----" not in text:
                skipped += 1
                errors.append({"line": index, "text": text, "error": "格式应为 号码----sms_url"})
                continue
            try:
                number = normalize_phone_number(number_raw)
            except ValueError as exc:
                skipped += 1
                errors.append({"line": index, "text": text, "error": str(exc)})
                continue
            if not str(sms_url or "").startswith("http"):
                skipped += 1
                errors.append({"line": index, "text": text, "error": "sms_url 必须以 http 开头"})
                continue
            if number in seen:
                skipped += 1
                errors.append({"line": index, "text": text, "error": "本批重复"})
                continue
            existing = await session.scalar(select(PhonePool).where(PhonePool.number == number))
            if existing:
                skipped += 1
                errors.append({"line": index, "text": text, "error": "号码已在池中"})
                continue
            seen.add(number)
            session.add(
                PhonePool(
                    number=number,
                    sms_url=sms_url.strip(),
                    status=STATUS_ACTIVE,
                    used_count=0,
                    risk_count=0,
                    no_sms_streak=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            imported += 1
        if imported:
            await session.commit()
        else:
            await session.flush()
        return {"imported": imported, "skipped": skipped, "errors": errors, "total_lines": imported + skipped}

    async def empty_reason(self, session: AsyncSession, cfg: PhonePoolConfig) -> str:
        total = int(await session.scalar(select(func.count()).select_from(PhonePool)) or 0)
        if total == 0:
            return "号码池为空，请先在系统中心导入接码"
        now = get_now()
        cooldown_since = now - timedelta(seconds=cfg.cooldown_sec)
        reserve_cutoff = now - timedelta(seconds=cfg.reserve_sec)
        active_rows = list(
            (
                await session.execute(
                    select(PhonePool).where(
                        PhonePool.status == STATUS_ACTIVE,
                        PhonePool.used_count < func.coalesce(PhonePool.max_uses, cfg.max_uses),
                    )
                )
            ).scalars()
        )
        if not active_rows:
            return "号码池没有可用号（都已用尽、停用或风险）"
        cooling = 0
        reserved = 0
        for row in active_rows:
            lease_live = self._lease_live(row, reserve_cutoff)
            if lease_live:
                reserved += 1
                continue
            if row.last_used_at is not None and row.last_used_at >= cooldown_since:
                cooling += 1
        if cooling and cooling + reserved >= len(active_rows):
            return "号码池可用号全在冷却中，请稍后再试或导入新号"
        if reserved and reserved >= len(active_rows):
            return "号码池可用号都在占用中，请稍后再试"
        return "号码池暂时没有可领的号"

    async def acquire(self, session: AsyncSession, job_id: str) -> PhonePool:
        job_key = str(job_id or "").strip()
        if not job_key:
            raise ValueError("领取号码需要 job_id")
        cfg = await self.get_config(session)
        now = get_now()
        cooldown_since = now - timedelta(seconds=cfg.cooldown_sec)
        reserve_cutoff = now - timedelta(seconds=cfg.reserve_sec)
        for _ in range(8):
            await self.expire_leases(session, cfg)
            await self.promote_maxed(session, cfg)
            await session.flush()
            lease_free = self._lease_free_clause(reserve_cutoff)
            cooldown_ok = or_(PhonePool.last_used_at.is_(None), PhonePool.last_used_at < cooldown_since)
            stmt = (
                select(PhonePool)
                .where(
                    PhonePool.status == STATUS_ACTIVE,
                    PhonePool.used_count < func.coalesce(PhonePool.max_uses, cfg.max_uses),
                    lease_free,
                    cooldown_ok,
                )
                .order_by(
                    PhonePool.used_count.asc(),
                    case((PhonePool.last_used_at.is_(None), 0), else_=1),
                    PhonePool.last_used_at.asc(),
                    PhonePool.id.asc(),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise PhonePoolEmpty(await self.empty_reason(session, cfg))
            result = await session.execute(
                update(PhonePool)
                .where(
                    PhonePool.id == row.id,
                    PhonePool.status == STATUS_ACTIVE,
                    lease_free,
                )
                .values(
                    reserved_by=job_key,
                    reserved_at=now,
                    lease_heartbeat_at=now,
                    operation_id=await self._operation_id(session, job_key),
                    updated_at=now,
                )
            )
            await session.commit()
            if int(result.rowcount or 0) == 1:
                fresh = await session.get(PhonePool, row.id)
                if fresh is None:
                    continue
                return fresh
        raise PhonePoolEmpty("号码池领取冲突，请重试")

    async def _load_for_job(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        phone_id: Optional[int] = None,
        number: str = "",
    ) -> Optional[PhonePool]:
        if phone_id:
            row = await session.get(PhonePool, phone_id)
            if row:
                return row
        if number:
            try:
                normalized = normalize_phone_number(number)
            except ValueError:
                normalized = number
            row = await session.scalar(select(PhonePool).where(PhonePool.number == normalized))
            if row:
                return row
        if job_id:
            return await session.scalar(
                select(PhonePool)
                .where(PhonePool.reserved_by == job_id)
                .order_by(PhonePool.reserved_at.desc(), PhonePool.id.desc())
            )
        return None

    def _clear_lease(self, row: PhonePool) -> None:
        row.reserved_by = None
        row.reserved_at = None
        row.lease_heartbeat_at = None
        row.operation_id = None

    async def heartbeat(self, session: AsyncSession, job_id: str) -> int:
        job_key = str(job_id or "").strip()
        if not job_key:
            return 0
        now = get_now()
        result = await session.execute(
            update(PhonePool)
            .where(PhonePool.reserved_by == job_key)
            .values(lease_heartbeat_at=now, reserved_at=now, updated_at=now)
        )
        if int(result.rowcount or 0):
            await session.commit()
        return int(result.rowcount or 0)

    async def release(
        self,
        session: AsyncSession,
        *,
        job_id: str = "",
        phone_id: Optional[int] = None,
        number: str = "",
    ) -> int:
        now = get_now()
        released = 0
        if phone_id or number:
            row = await self._load_for_job(session, job_id=job_id, phone_id=phone_id, number=number)
            if row and (not job_id or not row.reserved_by or row.reserved_by == job_id):
                self._clear_lease(row)
                row.updated_at = now
                released = 1
        elif job_id:
            rows = list(
                (await session.execute(select(PhonePool).where(PhonePool.reserved_by == job_id))).scalars()
            )
            for row in rows:
                self._clear_lease(row)
                row.updated_at = now
                released += 1
        if released:
            await session.commit()
        return released

    async def record_result(
        self,
        session: AsyncSession,
        *,
        result: str,
        job_id: str = "",
        phone_id: Optional[int] = None,
        number: str = "",
        message: str = "",
        purpose: str = "signup",
        account_id: Optional[int] = None,
    ) -> Optional[PhonePool]:
        row = await self._load_for_job(session, job_id=job_id, phone_id=phone_id, number=number)
        if row is None:
            return None
        cfg = await self.get_config(session)
        now = get_now()
        started_at = row.reserved_at or row.lease_heartbeat_at or now
        outcome = str(result or "").strip().lower()
        row.last_error = (message or "")[:500]
        row.last_error_type = outcome if outcome not in {OUTCOME_SUCCESS, OUTCOME_CANCELLED, OUTCOME_UNRELATED, ""} else (row.last_error_type or "")
        row.updated_at = now

        if outcome == OUTCOME_SUCCESS:
            row.used_count = int(row.used_count or 0) + 1
            row.last_used_at = now
            row.last_success_at = now
            row.no_sms_streak = 0
            row.last_error = ""
            row.last_error_type = ""
            if row.used_count >= effective_max_uses(row, cfg):
                row.status = STATUS_MAXED
            elif row.status == STATUS_ACTIVE:
                row.status = STATUS_ACTIVE
            self._clear_lease(row)
        elif outcome == OUTCOME_INVALID:
            row.last_used_at = now
            row.status = STATUS_DISABLED
            row.last_error_type = OUTCOME_INVALID
            self._clear_lease(row)
        elif outcome == OUTCOME_RECENTLY_USED:
            row.last_used_at = now
            if row.status == STATUS_MAXED:
                pass
            elif row.status not in {STATUS_DISABLED, STATUS_RISK}:
                row.status = STATUS_ACTIVE
            row.last_error_type = OUTCOME_RECENTLY_USED
            self._clear_lease(row)
        elif outcome == OUTCOME_RISK:
            row.risk_count = int(row.risk_count or 0) + 1
            row.no_sms_streak = 0
            row.last_error_type = OUTCOME_RISK
            if row.risk_count >= 2:
                row.status = STATUS_DISABLED
            else:
                row.status = STATUS_RISK
            self._clear_lease(row)
        elif outcome == OUTCOME_NO_SMS:
            row.no_sms_streak = int(row.no_sms_streak or 0) + 1
            row.last_error_type = OUTCOME_NO_SMS
            if row.no_sms_streak >= 2:
                row.status = STATUS_RISK
                row.risk_count = int(row.risk_count or 0) + 1
                if row.risk_count >= 2:
                    row.status = STATUS_DISABLED
                row.no_sms_streak = 0
            self._clear_lease(row)
        else:
            self._clear_lease(row)
        session.add(
            PhoneAttempt(
                phone_id=row.id,
                operation_id=await self._operation_id(session, job_id),
                operation_public_id=job_id or None,
                account_id=account_id,
                purpose=purpose if purpose in {"signup", "reauth"} else "signup",
                result=outcome or OUTCOME_PROVIDER_ERROR,
                provider_message=(message or "")[:500],
                started_at=started_at,
                finished_at=now,
                created_at=now,
            )
        )
        await session.commit()
        return row

    async def list_phones(
        self,
        session: AsyncSession,
        *,
        status: str = "",
        q: str = "",
    ) -> List[PhonePool]:
        cfg = await self.get_config(session)
        await self.expire_leases(session, cfg)
        stmt = select(PhonePool)
        if status:
            stmt = stmt.where(PhonePool.status == status)
        needle = str(q or "").strip()
        if needle:
            like = f"%{needle}%"
            stmt = stmt.where(or_(PhonePool.number.like(like), PhonePool.note.like(like), PhonePool.last_error.like(like)))
        stmt = stmt.order_by(
            case(
                (PhonePool.status == STATUS_ACTIVE, 0),
                (PhonePool.status == STATUS_RISK, 1),
                (PhonePool.status == STATUS_MAXED, 2),
                else_=3,
            ),
            PhonePool.used_count.asc(),
            PhonePool.id.asc(),
        )
        return list((await session.execute(stmt)).scalars())

    async def stats(self, session: AsyncSession) -> Dict[str, Any]:
        cfg = await self.get_config(session)
        await self.expire_leases(session, cfg)
        await self.promote_maxed(session, cfg)
        await session.flush()
        rows = list((await session.execute(select(PhonePool))).scalars())
        now = get_now()
        cooldown_since = now - timedelta(seconds=cfg.cooldown_sec)
        reserve_cutoff = now - timedelta(seconds=cfg.reserve_sec)
        counts = {STATUS_ACTIVE: 0, STATUS_MAXED: 0, STATUS_RISK: 0, STATUS_DISABLED: 0}
        remaining_uses = 0
        cooling = 0
        reserved = 0
        for row in rows:
            status = row.status or STATUS_ACTIVE
            counts[status] = counts.get(status, 0) + 1
            if status != STATUS_ACTIVE:
                continue
            max_uses = effective_max_uses(row, cfg)
            left = max(0, max_uses - int(row.used_count or 0))
            lease_live = self._lease_live(row, reserve_cutoff)
            cooling_now = row.last_used_at is not None and row.last_used_at >= cooldown_since
            if lease_live:
                reserved += 1
                continue
            if cooling_now:
                cooling += 1
                continue
            remaining_uses += left
        return {
            "total": len(rows),
            "active": counts.get(STATUS_ACTIVE, 0),
            "maxed": counts.get(STATUS_MAXED, 0),
            "risk": counts.get(STATUS_RISK, 0),
            "disabled": counts.get(STATUS_DISABLED, 0),
            "cooling": cooling,
            "reserved": reserved,
            "remaining_uses": remaining_uses,
            "config": {
                "sms_max_uses_per_phone": cfg.max_uses,
                "sms_cooldown_sec": cfg.cooldown_sec,
                "sms_reserve_sec": cfg.reserve_sec,
                "sms_max_phone_retries": cfg.max_retries,
            },
        }

    async def set_enabled(self, session: AsyncSession, phone_id: int, enabled: bool) -> PhonePool:
        row = await session.get(PhonePool, phone_id)
        if row is None:
            raise ValueError("号码不存在")
        cfg = await self.get_config(session)
        now = get_now()
        if enabled:
            if int(row.used_count or 0) >= effective_max_uses(row, cfg):
                row.status = STATUS_MAXED
            else:
                row.status = STATUS_ACTIVE
            row.risk_count = 0
            row.no_sms_streak = 0
        else:
            row.status = STATUS_DISABLED
        row.updated_at = now
        await session.commit()
        return row

    async def clear_status(self, session: AsyncSession, status: str) -> int:
        target = str(status or "").strip().lower()
        if target not in {STATUS_MAXED, STATUS_DISABLED}:
            raise ValueError("只能清理 maxed 或 disabled")
        rows = list((await session.execute(select(PhonePool).where(PhonePool.status == target))).scalars())
        for row in rows:
            await session.delete(row)
        await session.commit()
        return len(rows)

    async def run_attempts(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        attempt: Callable[[str, str], Awaitable[str] | str],
        on_log: Optional[Callable[[str, str], None]] = None,
        phone_line: str = "",
    ) -> Dict[str, Any]:
        """表单空则领池并按回执换号；非空走旧路径且不碰池。"""
        manual = str(phone_line or "").strip()
        if manual:
            number, sms_url = parse_phone_line(manual)
            if not number or not str(sms_url or "").startswith("http"):
                return {
                    "ok": False,
                    "from_pool": False,
                    "error": "接码格式无效，需要 号码----https://...",
                    "error_code": "sms_missing",
                }
            outcome = await _maybe_await(attempt(number, sms_url))
            return {
                "ok": outcome == OUTCOME_SUCCESS,
                "from_pool": False,
                "number": number,
                "sms_url": sms_url,
                "outcome": outcome,
            }

        cfg = await self.get_config(session)
        last_error = "号码池暂时没有可领的号"
        last_code = "phone_pool_empty"
        used: List[str] = []
        for index in range(cfg.max_retries):
            try:
                row = await self.acquire(session, job_id)
            except PhonePoolEmpty as exc:
                last_error = str(exc)
                last_code = exc.error_code
                break
            number, sms_url = row.number, row.sms_url
            used.append(number)
            if on_log:
                on_log("add_phone", f"领取 {number}")
            outcome = str(await _maybe_await(attempt(number, sms_url)) or "").strip().lower()
            await self.record_result(
                session,
                result=outcome or OUTCOME_UNRELATED,
                job_id=job_id,
                phone_id=row.id,
                number=number,
            )
            if outcome == OUTCOME_SUCCESS:
                return {
                    "ok": True,
                    "from_pool": True,
                    "number": number,
                    "sms_url": sms_url,
                    "phone_id": row.id,
                    "outcome": outcome,
                    "tried": used,
                }
            reason = outcome or "失败"
            if on_log:
                on_log("add_phone", f"换号原因：{reason}")
            last_error = f"号码 {number} {reason}"
            last_code = "sms_rejected" if outcome in {OUTCOME_INVALID, OUTCOME_RECENTLY_USED, OUTCOME_RISK} else (
                "sms_failed" if outcome == OUTCOME_NO_SMS else "phone_pool_empty"
            )
            if index + 1 >= cfg.max_retries:
                last_error = f"号码池已换 {cfg.max_retries} 个号仍未通过（最后：{reason}）"
                last_code = "sms_rejected" if outcome != OUTCOME_NO_SMS else "sms_failed"
        await self.release(session, job_id=job_id)
        return {
            "ok": False,
            "from_pool": True,
            "error": last_error,
            "error_code": last_code,
            "tried": used,
        }

    def make_sync_source(self, job_id: str, on_log: Optional[Callable[[str, str], None]] = None):
        """给 Playwright 线程用：acquire/record/release 回到主事件循环。"""
        loop = asyncio.get_running_loop()
        state: Dict[str, Any] = {"phone_id": None, "number": "", "sms_url": "", "attempts": 0, "closed": False}

        async def handle(action: str, **kwargs: Any) -> Dict[str, Any]:
            from app.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                if action == "acquire":
                    if state["closed"]:
                        return {"ok": False, "error": "号码租约已结束", "error_code": "phone_pool_empty"}
                    cfg = await self.get_config(session)
                    if int(state["attempts"]) >= cfg.max_retries:
                        return {
                            "ok": False,
                            "error": f"号码池已换 {cfg.max_retries} 个号仍未通过",
                            "error_code": "sms_rejected",
                        }
                    if state["phone_id"]:
                        await self.release(session, job_id=job_id, phone_id=int(state["phone_id"]))
                        state["phone_id"] = None
                    try:
                        row = await self.acquire(session, job_id)
                    except PhonePoolEmpty as exc:
                        return {"ok": False, "error": str(exc), "error_code": exc.error_code}
                    state["attempts"] = int(state["attempts"]) + 1
                    state["phone_id"] = row.id
                    state["number"] = row.number
                    state["sms_url"] = row.sms_url
                    if on_log:
                        on_log("add_phone", f"领取 {row.number}")
                    return {"ok": True, "id": row.id, "number": row.number, "sms_url": row.sms_url}
                if action == "record":
                    outcome = str(kwargs.get("result") or OUTCOME_UNRELATED)
                    message = str(kwargs.get("message") or "")
                    row = await self.record_result(
                        session,
                        result=outcome,
                        job_id=job_id,
                        phone_id=state.get("phone_id"),
                        number=str(kwargs.get("number") or state.get("number") or ""),
                        message=message,
                    )
                    if outcome in {OUTCOME_SUCCESS, OUTCOME_INVALID, OUTCOME_RECENTLY_USED, OUTCOME_RISK, OUTCOME_NO_SMS, OUTCOME_CANCELLED, OUTCOME_UNRELATED}:
                        if outcome != OUTCOME_SUCCESS:
                            state["phone_id"] = None
                        else:
                            state["closed"] = True
                            state["phone_id"] = None
                    if on_log and outcome not in {OUTCOME_SUCCESS, OUTCOME_UNRELATED, OUTCOME_CANCELLED, ""}:
                        on_log("add_phone", f"换号原因：{message or outcome}")
                    return {"ok": True, "status": row.status if row else ""}
                if action == "release":
                    await self.release(session, job_id=job_id, phone_id=state.get("phone_id"))
                    state["phone_id"] = None
                    return {"ok": True}
                return {"ok": False, "error": f"未知动作 {action}"}

        def source(action: str, **kwargs: Any) -> Dict[str, Any]:
            try:
                return asyncio.run_coroutine_threadsafe(handle(action, **kwargs), loop).result(timeout=45)
            except PhonePoolEmpty as exc:
                return {"ok": False, "error": str(exc), "error_code": exc.error_code}
            except Exception as exc:  # noqa: BLE001
                logger.warning("号码池同步回调失败: %s", exc)
                message = str(exc)
                code = getattr(exc, "error_code", "") or "phone_pool_empty"
                if isinstance(exc, Exception) and "PhonePoolEmpty" in type(exc).__name__:
                    code = "phone_pool_empty"
                return {"ok": False, "error": message, "error_code": code}

        return source


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


phone_pool_service = PhonePoolService()
