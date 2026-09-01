"""Phone pool leases, attempts, and outcome accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.settings import get_setting_value
from app.core.time import utcnow
from app.domain.resources import (
    DEFAULT_COOLDOWN_SEC,
    DEFAULT_MAX_USES,
    DEFAULT_RESERVE_SEC,
    OUTCOME_CANCELLED,
    OUTCOME_INVALID,
    OUTCOME_NO_SMS,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_RECENTLY_USED,
    OUTCOME_RISK,
    OUTCOME_SUCCESS,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_MAXED,
    STATUS_RISK,
    normalize_phone_number,
    parse_phone_line,
)
from app.persistence.models.operations import Operation
from app.persistence.models.resources import PhoneAttempt, PhonePool


class PhonePoolEmpty(Exception):
    def __init__(self, message: str, error_code: str = "phone_pool_empty") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class PhonePoolConfig:
    max_uses: int = DEFAULT_MAX_USES
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC
    reserve_sec: int = DEFAULT_RESERVE_SEC


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def effective_max_uses(row: PhonePool, cfg: PhonePoolConfig) -> int:
    if row.max_uses is None:
        return cfg.max_uses
    try:
        return max(1, int(row.max_uses))
    except (TypeError, ValueError):
        return cfg.max_uses


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
            PhonePool.lease_heartbeat_at.is_(None)
            & or_(PhonePool.reserved_at.is_(None), PhonePool.reserved_at < reserve_cutoff),
        )

    async def _operation_id(self, session: AsyncSession, job_id: str) -> int | None:
        if not job_id:
            return None
        row = await session.scalar(select(Operation.id).where(Operation.public_id == job_id))
        return int(row) if row is not None else None

    async def get_config(self, session: AsyncSession) -> PhonePoolConfig:
        max_uses = await get_setting_value(session, "sms_max_uses_per_phone", str(DEFAULT_MAX_USES))
        cooldown = await get_setting_value(session, "sms_cooldown_sec", str(DEFAULT_COOLDOWN_SEC))
        reserve = await get_setting_value(session, "sms_reserve_sec", str(DEFAULT_RESERVE_SEC))
        return PhonePoolConfig(
            max_uses=_clamp_int(max_uses, DEFAULT_MAX_USES, 1, 20),
            cooldown_sec=_clamp_int(cooldown, DEFAULT_COOLDOWN_SEC, 60, 86400),
            reserve_sec=_clamp_int(reserve, DEFAULT_RESERVE_SEC, 60, 7200),
        )

    def serialize(self, row: PhonePool, cfg: PhonePoolConfig | None = None) -> dict[str, Any]:
        cfg = cfg or PhonePoolConfig()
        max_uses = effective_max_uses(row, cfg)
        remaining = max(0, max_uses - int(row.used_count or 0))
        if row.status != STATUS_ACTIVE:
            remaining = 0
        return {
            "id": row.id,
            "number": row.number,
            "status": row.status,
            "used_count": int(row.used_count or 0),
            "max_uses": max_uses,
            "remaining": remaining,
            "reserved_by": row.reserved_by or "",
            "last_error_type": row.last_error_type or "",
            "risk_count": int(row.risk_count or 0),
            "note": row.note or "",
        }

    async def expire_leases(self, session: AsyncSession, cfg: PhonePoolConfig | None = None) -> int:
        cfg = cfg or await self.get_config(session)
        cutoff = utcnow() - timedelta(seconds=cfg.reserve_sec)
        result = await session.execute(
            update(PhonePool)
            .where(PhonePool.reserved_by.is_not(None), self._lease_free_clause(cutoff))
            .values(
                reserved_by=None,
                reserved_at=None,
                lease_heartbeat_at=None,
                operation_id=None,
                updated_at=utcnow(),
            )
        )
        return int(result.rowcount or 0)

    async def promote_maxed(self, session: AsyncSession, cfg: PhonePoolConfig | None = None) -> int:
        cfg = cfg or await self.get_config(session)
        result = await session.execute(
            update(PhonePool)
            .where(
                PhonePool.status == STATUS_ACTIVE,
                PhonePool.used_count >= func.coalesce(PhonePool.max_uses, cfg.max_uses),
            )
            .values(status=STATUS_MAXED, updated_at=utcnow())
        )
        return int(result.rowcount or 0)

    async def import_lines(self, session: AsyncSession, raw_text: str) -> dict[str, Any]:
        imported = 0
        skipped = 0
        errors: list[dict[str, Any]] = []
        seen: set[str] = set()
        now = utcnow()
        for index, line in enumerate(str(raw_text or "").splitlines(), start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            number_raw, sms_url = parse_phone_line(text)
            if "----" not in text:
                skipped += 1
                errors.append({"line": index, "error": "expected number----sms_url"})
                continue
            try:
                number = normalize_phone_number(number_raw)
            except ValueError as exc:
                skipped += 1
                errors.append({"line": index, "error": str(exc)})
                continue
            if not str(sms_url or "").startswith("http"):
                skipped += 1
                errors.append({"line": index, "error": "sms_url must start with http"})
                continue
            if number in seen or await session.scalar(select(PhonePool).where(PhonePool.number == number)):
                skipped += 1
                errors.append({"line": index, "error": "duplicate"})
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
        return {"imported": imported, "skipped": skipped, "errors": errors}

    async def acquire(self, session: AsyncSession, job_id: str) -> PhonePool:
        job_key = str(job_id or "").strip()
        if not job_key:
            raise ValueError("job_id is required")
        cfg = await self.get_config(session)
        now = utcnow()
        cooldown_since = now - timedelta(seconds=cfg.cooldown_sec)
        reserve_cutoff = now - timedelta(seconds=cfg.reserve_sec)
        for _ in range(8):
            await self.expire_leases(session, cfg)
            await self.promote_maxed(session, cfg)
            await session.flush()
            lease_free = self._lease_free_clause(reserve_cutoff)
            row = (
                await session.execute(
                    select(PhonePool)
                    .where(
                        PhonePool.status == STATUS_ACTIVE,
                        PhonePool.used_count < func.coalesce(PhonePool.max_uses, cfg.max_uses),
                        lease_free,
                        or_(PhonePool.last_used_at.is_(None), PhonePool.last_used_at < cooldown_since),
                    )
                    .order_by(
                        PhonePool.used_count.asc(),
                        case((PhonePool.last_used_at.is_(None), 0), else_=1),
                        PhonePool.last_used_at.asc(),
                        PhonePool.id.asc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                raise PhonePoolEmpty("phone pool has no available number")
            result = await session.execute(
                update(PhonePool)
                .where(PhonePool.id == row.id, PhonePool.status == STATUS_ACTIVE, lease_free)
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
                if fresh is not None:
                    return fresh
        raise PhonePoolEmpty("phone pool acquire conflict")

    async def _load_for_job(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        phone_id: int | None = None,
        number: str = "",
    ) -> PhonePool | None:
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
        now = utcnow()
        result = await session.execute(
            update(PhonePool)
            .where(PhonePool.reserved_by == job_key)
            .values(lease_heartbeat_at=now, reserved_at=now, updated_at=now)
        )
        if int(result.rowcount or 0):
            await session.commit()
        return int(result.rowcount or 0)

    async def record_result(
        self,
        session: AsyncSession,
        *,
        result: str,
        job_id: str = "",
        phone_id: int | None = None,
        number: str = "",
        message: str = "",
        purpose: str = "signup",
        account_id: int | None = None,
    ) -> PhonePool | None:
        row = await self._load_for_job(session, job_id=job_id, phone_id=phone_id, number=number)
        if row is None:
            return None
        cfg = await self.get_config(session)
        now = utcnow()
        started_at = row.reserved_at or row.lease_heartbeat_at or now
        outcome = str(result or "").strip().lower()
        row.last_error = (message or "")[:500]
        row.last_error_type = (
            outcome if outcome not in {OUTCOME_SUCCESS, OUTCOME_CANCELLED, ""} else (row.last_error_type or "")
        )
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
            self._clear_lease(row)
        elif outcome == OUTCOME_INVALID:
            row.last_used_at = now
            row.status = STATUS_DISABLED
            row.last_error_type = OUTCOME_INVALID
            self._clear_lease(row)
        elif outcome == OUTCOME_RECENTLY_USED:
            row.last_used_at = now
            if row.status not in {STATUS_DISABLED, STATUS_RISK, STATUS_MAXED}:
                row.status = STATUS_ACTIVE
            row.last_error_type = OUTCOME_RECENTLY_USED
            self._clear_lease(row)
        elif outcome == OUTCOME_RISK:
            row.risk_count = int(row.risk_count or 0) + 1
            row.status = STATUS_DISABLED if row.risk_count >= 2 else STATUS_RISK
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

    async def list_phones(self, session: AsyncSession) -> list[PhonePool]:
        cfg = await self.get_config(session)
        await self.expire_leases(session, cfg)
        return list((await session.execute(select(PhonePool).order_by(PhonePool.id.asc()))).scalars())


phone_pool_service = PhonePoolService()
