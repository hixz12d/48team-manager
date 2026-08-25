"""解析并保存踢人响应里的 policy_notice / billing_notice。

这些字段没有稳定公开语义，只能当排查证据，不能当作自动补位许可。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import pytz
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SeatVacancyEvent, Team
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 30


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def _as_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _json_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def to_local_naive(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    tz = pytz.timezone(settings.timezone)
    return dt.astimezone(tz).replace(tzinfo=None)


def parse_policy_notice(payload: Any) -> Optional[Dict[str, Any]]:
    """兼容旧名：从踢人响应抽出 vacancy / 账单回执。"""
    return parse_removal_notices(payload)


def parse_removal_notices(payload: Any) -> Optional[Dict[str, Any]]:
    """抽出踢人回执。没有 policy_notice / billing_notice 就返回 None。"""
    if not isinstance(payload, dict):
        return None
    has_policy_key = "policy_notice" in payload
    has_billing_key = "billing_notice" in payload
    if not has_policy_key and not has_billing_key:
        return None

    policy = payload.get("policy_notice") if has_policy_key else None
    billing = payload.get("billing_notice") if has_billing_key else None
    policy_null = has_policy_key and policy is None
    policy_obj = policy if isinstance(policy, dict) else None

    ordinal = _as_int(policy_obj.get("vacancy_ordinal")) if policy_obj else None
    threshold = _as_int(policy_obj.get("free_vacancy_threshold")) if policy_obj else None
    is_free = None
    if ordinal is not None and threshold is not None:
        is_free = ordinal < threshold

    return {
        "policy_notice_null": policy_null,
        "has_policy_notice": policy_obj is not None,
        "has_billing_notice": billing is not None,
        "policy_kind": _as_text(policy_obj.get("kind")) if policy_obj else None,
        "billed_seat_delta": _as_int(policy_obj.get("billed_seat_delta")) if policy_obj else None,
        "replacement_required": _as_bool(policy_obj.get("replacement_required")) if policy_obj else None,
        "vacancy_ordinal": ordinal,
        "free_vacancy_threshold": threshold,
        "billing_starts_at": to_local_naive(policy_obj.get("billing_starts_at")) if policy_obj else None,
        "expires_at": to_local_naive(policy_obj.get("expires_at")) if policy_obj else None,
        "is_free": is_free,
        "policy_notice_json": _json_text(policy) if has_policy_key else None,
        "billing_notice_json": _json_text(billing) if has_billing_key else None,
    }


def pick_chatgpt_user_id(*candidates: Any) -> Optional[str]:
    values: List[str] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("id", "account_user_id", "user_id"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
        elif isinstance(candidate, str) and candidate.strip():
            values.append(candidate.strip())
    if not values:
        return None
    for value in values:
        if value.startswith("user-"):
            return value
    return values[0]


def _normalize_email(email: Optional[str]) -> Optional[str]:
    if not isinstance(email, str):
        return None
    text = email.strip().lower()
    return text or None


def _format_metric(value: Any, *, null_policy: bool) -> str:
    if null_policy and value is None:
        return "null"
    if value is None:
        return "--"
    return str(value)


def _format_time(value: Any, *, null_policy: bool) -> str:
    if null_policy and value is None:
        return "null"
    if value is None:
        return "--"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    local = to_local_naive(value)
    return local.strftime("%Y-%m-%d %H:%M:%S") if local else "--"


def is_safe_to_refill(vacancy: Optional[Dict[str, Any]]) -> bool:
    """只有明确低于阈值、且没有账单回执/强制替换时，才允许自动补位。"""
    if not vacancy:
        return False
    if vacancy.get("has_billing_notice"):
        return False
    if vacancy.get("replacement_required") is True:
        return False
    return vacancy.get("is_free") is True


def present_vacancy(vacancy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not vacancy:
        return None

    null_policy = bool(vacancy.get("policy_notice_null"))
    ordinal = vacancy.get("vacancy_ordinal")
    threshold = vacancy.get("free_vacancy_threshold")
    is_free = vacancy.get("is_free")
    has_billing = bool(vacancy.get("has_billing_notice"))
    replacement_required = vacancy.get("replacement_required")
    safe = is_safe_to_refill({
        **vacancy,
        "is_free": is_free,
        "has_billing_notice": has_billing,
        "replacement_required": replacement_required,
    })

    if null_policy:
        comparison = "policy_notice: null"
        status_label = "无阈值"
    elif ordinal is not None and threshold is not None:
        comparison = f"{ordinal} {'<' if ordinal < threshold else '≥'} {threshold}"
        status_label = "可释放" if is_free else "未释放"
    elif vacancy.get("has_policy_notice"):
        comparison = "--"
        status_label = "未知"
    else:
        comparison = "无 policy_notice"
        status_label = "未知"

    if has_billing:
        status_label = "有账单回执"
    elif replacement_required is True:
        status_label = "需替换席位"

    if safe:
        tone = "ok"
    elif is_free is False or has_billing or replacement_required is True:
        tone = "warn"
    else:
        tone = "muted"

    captured_at = vacancy.get("captured_at")
    return {
        "id": vacancy.get("id"),
        "team_id": vacancy.get("team_id"),
        "account_id": vacancy.get("account_id") or "",
        "user_id": vacancy.get("user_id") or "",
        "email": vacancy.get("email") or "",
        "policy_notice_null": null_policy,
        "has_policy_notice": bool(vacancy.get("has_policy_notice")),
        "has_billing_notice": has_billing,
        "policy_kind": vacancy.get("policy_kind") or "",
        "billed_seat_delta": vacancy.get("billed_seat_delta"),
        "replacement_required": replacement_required,
        "vacancy_ordinal": ordinal,
        "free_vacancy_threshold": threshold,
        "vacancy_ordinal_text": _format_metric(ordinal, null_policy=null_policy),
        "free_vacancy_threshold_text": _format_metric(threshold, null_policy=null_policy),
        "billing_starts_at": vacancy.get("billing_starts_at").isoformat()
        if isinstance(vacancy.get("billing_starts_at"), datetime)
        else vacancy.get("billing_starts_at"),
        "expires_at": vacancy.get("expires_at").isoformat()
        if isinstance(vacancy.get("expires_at"), datetime)
        else vacancy.get("expires_at"),
        "billing_starts_at_text": _format_time(vacancy.get("billing_starts_at"), null_policy=null_policy),
        "expires_at_text": _format_time(vacancy.get("expires_at"), null_policy=null_policy),
        "is_free": is_free,
        "safe_to_refill": safe,
        "refill_label": "可自动补位" if safe else "勿自动补位",
        "billing_notice_text": "有" if has_billing else "无",
        "comparison_text": comparison,
        "status_label": status_label,
        "tone": tone,
        "captured_at": captured_at.isoformat() if isinstance(captured_at, datetime) else captured_at,
        "captured_at_text": _format_time(captured_at, null_policy=False) if captured_at else "--",
    }


def summarize_for_message(vacancy: Optional[Dict[str, Any]]) -> str:
    presented = present_vacancy(vacancy) if vacancy and "status_label" not in vacancy else vacancy
    if not presented:
        return ""
    parts = [presented.get("status_label") or "", presented.get("comparison_text") or ""]
    if presented.get("refill_label"):
        parts.append(presented["refill_label"])
    if presented.get("is_free") is False and presented.get("expires_at_text") not in {None, "", "--", "null"}:
        parts.append(f"释放 {presented['expires_at_text']}")
    return "，".join(part for part in parts if part and part != "--")


class VacancyService:
    def serialize(self, event: SeatVacancyEvent) -> Dict[str, Any]:
        presented = present_vacancy({
            "id": event.id,
            "team_id": event.team_id,
            "account_id": event.account_id,
            "user_id": event.user_id,
            "email": event.email,
            "policy_notice_null": event.policy_notice_null,
            "has_policy_notice": not bool(event.policy_notice_null) and (
                event.vacancy_ordinal is not None or bool(getattr(event, "policy_kind", None))
            ),
            "has_billing_notice": bool(getattr(event, "has_billing_notice", False)),
            "policy_kind": getattr(event, "policy_kind", None),
            "billed_seat_delta": getattr(event, "billed_seat_delta", None),
            "replacement_required": getattr(event, "replacement_required", None),
            "vacancy_ordinal": event.vacancy_ordinal,
            "free_vacancy_threshold": event.free_vacancy_threshold,
            "billing_starts_at": event.billing_starts_at,
            "expires_at": event.expires_at,
            "is_free": event.is_free,
            "captured_at": event.captured_at,
        })
        return presented or {}

    async def record(
        self,
        db_session: AsyncSession,
        *,
        team: Team,
        user_id: Optional[str],
        email: Optional[str],
        vacancy: Dict[str, Any],
    ) -> Dict[str, Any]:
        email_norm = _normalize_email(email)
        last = await self._latest_for_target(db_session, team.id, user_id, email_norm)
        if last and self._same_values(last, vacancy):
            return self.serialize(last)

        event = SeatVacancyEvent(
            team_id=team.id,
            account_id=team.account_id,
            user_id=user_id,
            email=email_norm,
            policy_notice_null=bool(vacancy.get("policy_notice_null")),
            vacancy_ordinal=vacancy.get("vacancy_ordinal"),
            free_vacancy_threshold=vacancy.get("free_vacancy_threshold"),
            billing_starts_at=vacancy.get("billing_starts_at"),
            expires_at=vacancy.get("expires_at"),
            is_free=vacancy.get("is_free"),
            has_billing_notice=bool(vacancy.get("has_billing_notice")),
            policy_kind=vacancy.get("policy_kind"),
            billed_seat_delta=vacancy.get("billed_seat_delta"),
            replacement_required=vacancy.get("replacement_required"),
            policy_notice_json=vacancy.get("policy_notice_json"),
            billing_notice_json=vacancy.get("billing_notice_json"),
            captured_at=get_now(),
        )
        db_session.add(event)
        await db_session.flush()
        logger.info(
            "记录席位回执: team=%s user=%s email=%s ordinal=%s threshold=%s free=%s billing=%s",
            team.id,
            user_id,
            email_norm,
            event.vacancy_ordinal,
            event.free_vacancy_threshold,
            event.is_free,
            event.has_billing_notice,
        )
        return self.serialize(event)

    async def attach_to_cards(
        self,
        db_session: AsyncSession,
        cards: List[Dict[str, Any]],
        *,
        history_limit: int = HISTORY_LIMIT,
    ) -> List[Dict[str, Any]]:
        from app.services.team_view import attach_operational_view

        team_ids = [card.get("id") for card in cards if card.get("id") is not None]
        grouped = await self._history_by_team(db_session, team_ids)
        for card in cards:
            history = grouped.get(int(card["id"]), [])[:history_limit]
            card["vacancy_history"] = [self.serialize(item) for item in history]
            card["vacancy"] = card["vacancy_history"][0] if card["vacancy_history"] else None
        return attach_operational_view(cards)

    async def clear_team(self, db_session: AsyncSession, team_id: int) -> int:
        result = await db_session.execute(
            delete(SeatVacancyEvent).where(SeatVacancyEvent.team_id == team_id)
        )
        await db_session.commit()
        return int(result.rowcount or 0)

    async def _history_by_team(
        self,
        db_session: AsyncSession,
        team_ids: Iterable[Any],
    ) -> Dict[int, List[SeatVacancyEvent]]:
        ids = [int(team_id) for team_id in team_ids]
        if not ids:
            return {}
        stmt = (
            select(SeatVacancyEvent)
            .where(SeatVacancyEvent.team_id.in_(ids))
            .order_by(SeatVacancyEvent.captured_at.desc(), SeatVacancyEvent.id.desc())
        )
        events = (await db_session.execute(stmt)).scalars().all()
        grouped: Dict[int, List[SeatVacancyEvent]] = {}
        for event in events:
            grouped.setdefault(int(event.team_id), []).append(event)
        return grouped

    async def _latest_for_target(
        self,
        db_session: AsyncSession,
        team_id: int,
        user_id: Optional[str],
        email: Optional[str],
    ) -> Optional[SeatVacancyEvent]:
        stmt = select(SeatVacancyEvent).where(SeatVacancyEvent.team_id == team_id)
        if user_id:
            stmt = stmt.where(SeatVacancyEvent.user_id == user_id)
        elif email:
            stmt = stmt.where(SeatVacancyEvent.email == email)
        stmt = stmt.order_by(SeatVacancyEvent.captured_at.desc(), SeatVacancyEvent.id.desc()).limit(1)
        return (await db_session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _same_values(event: SeatVacancyEvent, vacancy: Dict[str, Any]) -> bool:
        return (
            bool(event.policy_notice_null) == bool(vacancy.get("policy_notice_null"))
            and event.vacancy_ordinal == vacancy.get("vacancy_ordinal")
            and event.free_vacancy_threshold == vacancy.get("free_vacancy_threshold")
            and event.billing_starts_at == vacancy.get("billing_starts_at")
            and event.expires_at == vacancy.get("expires_at")
            and event.is_free == vacancy.get("is_free")
            and bool(getattr(event, "has_billing_notice", False)) == bool(vacancy.get("has_billing_notice"))
            and (getattr(event, "policy_kind", None) or None) == (vacancy.get("policy_kind") or None)
            and getattr(event, "billed_seat_delta", None) == vacancy.get("billed_seat_delta")
            and getattr(event, "replacement_required", None) == vacancy.get("replacement_required")
        )


vacancy_service = VacancyService()
