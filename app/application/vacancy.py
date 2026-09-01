"""Persist kick receipts. is_safe_to_refill stays in domain; this never auto-refills."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.vacancy import (
    HISTORY_LIMIT,
    captured_now,
    present_vacancy,
    vacancy_email,
)
from app.persistence.models.vacancy import SeatVacancyEvent

logger = logging.getLogger(__name__)


class VacancyService:
    def serialize(self, event: SeatVacancyEvent) -> dict[str, Any]:
        presented = present_vacancy(
            {
                "id": event.id,
                "workspace_id": event.workspace_id,
                "team_id": event.workspace_id,
                "account_id": event.account_id,
                "user_id": event.user_id,
                "email": event.email,
                "policy_notice_null": event.policy_notice_null,
                "has_policy_notice": not bool(event.policy_notice_null)
                and (event.vacancy_ordinal is not None or bool(event.policy_kind)),
                "has_billing_notice": bool(event.has_billing_notice),
                "policy_kind": event.policy_kind,
                "billed_seat_delta": event.billed_seat_delta,
                "replacement_required": event.replacement_required,
                "vacancy_ordinal": event.vacancy_ordinal,
                "free_vacancy_threshold": event.free_vacancy_threshold,
                "billing_starts_at": event.billing_starts_at,
                "expires_at": event.expires_at,
                "is_free": event.is_free,
                "captured_at": event.captured_at,
            }
        )
        return presented or {}

    async def record(
        self,
        db: AsyncSession,
        *,
        workspace_id: int,
        account_id: str | None = None,
        user_id: str | None = None,
        email: str | None = None,
        vacancy: dict[str, Any],
    ) -> dict[str, Any]:
        email_norm = vacancy_email(email)
        last = await self._latest_for_target(db, workspace_id, user_id, email_norm)
        if last and self._same_values(last, vacancy):
            return self.serialize(last)

        event = SeatVacancyEvent(
            workspace_id=int(workspace_id),
            account_id=account_id,
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
            captured_at=captured_now(),
        )
        db.add(event)
        await db.flush()
        logger.info(
            "recorded vacancy workspace=%s user=%s email=%s ordinal=%s threshold=%s free=%s billing=%s",
            workspace_id,
            user_id,
            email_norm,
            event.vacancy_ordinal,
            event.free_vacancy_threshold,
            event.is_free,
            event.has_billing_notice,
        )
        return self.serialize(event)

    async def history_for_workspace(
        self,
        db: AsyncSession,
        workspace_id: int,
        *,
        history_limit: int = HISTORY_LIMIT,
    ) -> list[dict[str, Any]]:
        grouped = await self._history_by_workspace(db, [workspace_id])
        return [self.serialize(item) for item in grouped.get(int(workspace_id), [])[:history_limit]]

    async def latest_for_workspace(self, db: AsyncSession, workspace_id: int) -> dict[str, Any] | None:
        history = await self.history_for_workspace(db, workspace_id, history_limit=1)
        return history[0] if history else None

    async def clear_workspace(self, db: AsyncSession, workspace_id: int) -> int:
        result = await db.execute(delete(SeatVacancyEvent).where(SeatVacancyEvent.workspace_id == int(workspace_id)))
        return int(result.rowcount or 0)

    async def _history_by_workspace(
        self,
        db: AsyncSession,
        workspace_ids: Iterable[Any],
    ) -> dict[int, list[SeatVacancyEvent]]:
        ids = [int(workspace_id) for workspace_id in workspace_ids]
        if not ids:
            return {}
        stmt = (
            select(SeatVacancyEvent)
            .where(SeatVacancyEvent.workspace_id.in_(ids))
            .order_by(SeatVacancyEvent.captured_at.desc(), SeatVacancyEvent.id.desc())
        )
        events = (await db.execute(stmt)).scalars().all()
        grouped: dict[int, list[SeatVacancyEvent]] = {}
        for event in events:
            grouped.setdefault(int(event.workspace_id), []).append(event)
        return grouped

    async def _latest_for_target(
        self,
        db: AsyncSession,
        workspace_id: int,
        user_id: str | None,
        email: str | None,
    ) -> SeatVacancyEvent | None:
        stmt = select(SeatVacancyEvent).where(SeatVacancyEvent.workspace_id == workspace_id)
        if user_id:
            stmt = stmt.where(SeatVacancyEvent.user_id == user_id)
        elif email:
            stmt = stmt.where(SeatVacancyEvent.email == email)
        stmt = stmt.order_by(SeatVacancyEvent.captured_at.desc(), SeatVacancyEvent.id.desc()).limit(1)
        return (await db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _same_values(event: SeatVacancyEvent, vacancy: dict[str, Any]) -> bool:
        return (
            bool(event.policy_notice_null) == bool(vacancy.get("policy_notice_null"))
            and event.vacancy_ordinal == vacancy.get("vacancy_ordinal")
            and event.free_vacancy_threshold == vacancy.get("free_vacancy_threshold")
            and event.billing_starts_at == vacancy.get("billing_starts_at")
            and event.expires_at == vacancy.get("expires_at")
            and event.is_free == vacancy.get("is_free")
            and bool(event.has_billing_notice) == bool(vacancy.get("has_billing_notice"))
            and (event.policy_kind or None) == (vacancy.get("policy_kind") or None)
            and event.billed_seat_delta == vacancy.get("billed_seat_delta")
            and event.replacement_required == vacancy.get("replacement_required")
        )


vacancy_service = VacancyService()
