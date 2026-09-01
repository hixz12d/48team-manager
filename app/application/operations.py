"""Persistent operations with DB leases. Restart recovery never resumes Playwright."""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import token_cipher
from app.core.time import isoformat, utcnow
from app.domain.automation import (
    ACTIVE_STATES,
    BROWSER_ACTIONS,
    DEFAULT_LEASE_SECONDS,
    MAX_LOG_ITEMS,
    SENSITIVE_INPUT_KEYS,
    TERMINAL_STATES,
)
from app.persistence.models.operations import Operation, OperationStep

logger = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def new_public_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_text(now: datetime | None = None) -> str:
    stamp = now or utcnow()
    return stamp.strftime("%H:%M:%S")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def pack_input(payload: dict[str, Any] | None) -> str:
    packed: dict[str, Any] = {}
    cipher = token_cipher()
    for key, value in dict(payload or {}).items():
        if key in SENSITIVE_INPUT_KEYS and value:
            packed[key] = {"enc": cipher.encrypt(str(value))}
        else:
            packed[key] = value
    return _dumps(packed)


def unpack_input(raw: str | None) -> dict[str, Any]:
    data = _loads(raw, {})
    if not isinstance(data, dict):
        return {}
    cipher = token_cipher()
    unpacked: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "enc" in value:
            try:
                unpacked[key] = cipher.decrypt(str(value.get("enc") or ""))
            except Exception:
                unpacked[key] = ""
        else:
            unpacked[key] = value
    return unpacked


def duration_text(row: Operation) -> str | None:
    start = row.started_at or row.created_at
    end = row.finished_at or utcnow()
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=end.tzinfo)
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rem}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def serialize_operation(row: Operation, *, steps: list[OperationStep] | None = None) -> dict[str, Any]:
    log_items = _loads(row.log_json, [])
    if not isinstance(log_items, list):
        log_items = []
    result = _loads(row.result_json, None)
    payload = {
        "id": row.public_id,
        "operation_id": row.id,
        "status": "running" if row.state in ACTIVE_STATES else row.state,
        "state": row.state,
        "operation": row.op_type,
        "target": row.email or (f"workspace:{row.workspace_id}" if row.workspace_id else None),
        "current_step": row.current_step or "",
        "started": isoformat(row.started_at or row.created_at),
        "duration": duration_text(row),
        "email": row.email or "",
        "error": row.error_message or "",
        "error_code": row.error_code or "",
        "log": log_items,
        "cancel_requested": bool(row.cancel_requested),
        "result": result,
        "locked_by": row.locked_by or "",
        "lease_expires_at": isoformat(row.lease_expires_at),
        "updated": isoformat(row.updated_at or row.finished_at or row.started_at or row.created_at),
        "workspace_id": row.workspace_id,
    }
    if steps is not None:
        payload["steps"] = [
            {
                "step_name": item.step_name,
                "state": item.state,
                "attempt": item.attempt,
                "error_code": item.error_code,
                "error_message": item.error_message,
                "result": _loads(item.result_snapshot, None),
            }
            for item in steps
        ]
    return payload


class OperationStore:
    async def get_by_public_id(self, session: AsyncSession, public_id: str) -> Operation | None:
        if not public_id:
            return None
        return (await session.execute(select(Operation).where(Operation.public_id == public_id))).scalar_one_or_none()

    async def create(
        self,
        session: AsyncSession,
        *,
        op_type: str,
        workspace_id: int = 0,
        account_id: int | None = None,
        email: str = "",
        phone: str = "",
        input_payload: dict[str, Any] | None = None,
        public_id: str | None = None,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        resolved_proxy: str = "",
        resolved_proxy_profile_id: int | None = None,
    ) -> Operation:
        stamp = now or utcnow()
        row = Operation(
            public_id=public_id or new_public_id(),
            op_type=op_type,
            entity_type="workspace" if workspace_id else ("account" if account_id else None),
            entity_id=int(workspace_id) if workspace_id else account_id,
            workspace_id=int(workspace_id) if workspace_id else None,
            account_id=account_id,
            email=(email or "").strip().lower(),
            phone=phone or "",
            state="running",
            current_step="queued",
            locked_by=WORKER_ID,
            lease_expires_at=stamp + timedelta(seconds=lease_seconds),
            cancel_requested=False,
            input_json=pack_input(input_payload),
            log_json=_dumps([{"ts": _now_text(stamp), "stage": "queued", "message": "queued"}]),
            resolved_proxy=resolved_proxy or None,
            resolved_proxy_profile_id=resolved_proxy_profile_id,
            created_at=stamp,
            started_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        await session.flush()
        return row

    async def list_recent(self, session: AsyncSession, *, limit: int = 100) -> list[Operation]:
        result = await session.execute(select(Operation).order_by(Operation.created_at.desc(), Operation.id.desc()).limit(limit))
        return list(result.scalars().all())

    async def active_for_email(self, session: AsyncSession, email: str, *, actions: tuple[str, ...] | None = None) -> Operation | None:
        target = (email or "").strip().lower()
        if not target:
            return None
        stmt = select(Operation).where(Operation.email == target, Operation.state.in_(ACTIVE_STATES))
        if actions:
            stmt = stmt.where(Operation.op_type.in_(actions))
        stmt = stmt.order_by(Operation.created_at.desc(), Operation.id.desc())
        return (await session.execute(stmt)).scalars().first()

    async def any_running(self, session: AsyncSession, actions: tuple[str, ...] | None = None) -> Operation | None:
        stmt = select(Operation).where(Operation.state.in_(ACTIVE_STATES))
        if actions:
            stmt = stmt.where(Operation.op_type.in_(actions))
        stmt = stmt.order_by(Operation.started_at.desc(), Operation.id.desc())
        return (await session.execute(stmt)).scalars().first()

    async def active_for_workspace(
        self,
        session: AsyncSession,
        workspace_id: int,
        *,
        actions: tuple[str, ...] | None = None,
        exclude_public_id: str | None = None,
    ) -> Operation | None:
        try:
            target = int(workspace_id or 0)
        except (TypeError, ValueError):
            return None
        if not target:
            return None
        stmt = select(Operation).where(Operation.state.in_(ACTIVE_STATES), Operation.workspace_id == target)
        if actions:
            stmt = stmt.where(Operation.op_type.in_(actions))
        exclude = str(exclude_public_id or "").strip()
        if exclude:
            stmt = stmt.where(Operation.public_id != exclude)
        stmt = stmt.order_by(Operation.created_at.desc(), Operation.id.desc())
        return (await session.execute(stmt)).scalars().first()

    async def iter_running(self, session: AsyncSession, actions: tuple[str, ...] | None = None) -> list[Operation]:
        stmt = select(Operation).where(Operation.state.in_(ACTIVE_STATES))
        if actions:
            stmt = stmt.where(Operation.op_type.in_(actions))
        return list((await session.execute(stmt)).scalars().all())

    async def browser_busy(self, session: AsyncSession) -> Operation | None:
        return await self.any_running(session, BROWSER_ACTIONS)

    async def heartbeat(
        self,
        session: AsyncSession,
        row: Operation,
        *,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        stamp = now or utcnow()
        row.locked_by = WORKER_ID
        row.lease_expires_at = stamp + timedelta(seconds=lease_seconds)
        row.updated_at = stamp

    async def note(
        self,
        session: AsyncSession,
        row: Operation,
        stage: str,
        message: str,
        *,
        error: str = "",
        error_code: str = "",
        now: datetime | None = None,
        touch_lease: bool = True,
    ) -> None:
        stamp = now or utcnow()
        log_items = _loads(row.log_json, [])
        if not isinstance(log_items, list):
            log_items = []
        log_items.append({"ts": _now_text(stamp), "stage": stage, "message": message})
        row.log_json = _dumps(log_items[-MAX_LOG_ITEMS:])
        row.current_step = stage
        row.updated_at = stamp
        if error:
            row.error_message = str(error)[:500]
            row.error_code = error_code or row.error_code
        if touch_lease:
            await self.heartbeat(session, row, now=stamp)

    async def finish(
        self,
        session: AsyncSession,
        row: Operation,
        result: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        stamp = now or utcnow()
        success = bool(result.get("success"))
        cancelled = result.get("status") == "cancelled" or result.get("error_code") == "cancelled"
        requested_state = str(result.get("status") or "")
        if cancelled:
            row.state = "cancelled"
        elif success:
            row.state = "success"
        elif requested_state == "manual_required":
            row.state = "manual_required"
        else:
            row.state = "failed"
        row.result_json = _dumps(result)
        row.finished_at = stamp
        row.updated_at = stamp
        if success:
            row.current_step = str(result.get("status") or "done")
            row.error_code = None
            row.error_message = None
        else:
            row.current_step = str(result.get("status") or row.current_step or "failed")
            row.error_code = str(result.get("error_code") or row.error_code or "")[:40] or None
            row.error_message = str(result.get("error") or row.error_message or "operation failed")[:500]
        await self.note(
            session,
            row,
            row.current_step or "done",
            row.error_message or result.get("message") or row.state,
            error=row.error_message or "",
            error_code=row.error_code or "",
            now=stamp,
            touch_lease=False,
        )
        row.locked_by = None
        row.lease_expires_at = None

    async def mark_step(
        self,
        session: AsyncSession,
        row: Operation,
        step_name: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
        now: datetime | None = None,
    ) -> OperationStep:
        stamp = now or utcnow()
        existing = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == row.id,
                    OperationStep.step_name == step_name,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = OperationStep(
                operation_id=row.id,
                step_name=step_name,
                state=state,
                attempt=1,
                started_at=stamp,
            )
            session.add(existing)
        else:
            existing.attempt = int(existing.attempt or 1) + (0 if existing.state == state else 1)
        existing.state = state
        if result is not None:
            existing.result_snapshot = _dumps(result)
        if error_code:
            existing.error_code = error_code[:40]
        if error_message:
            existing.error_message = str(error_message)[:500]
        if state in TERMINAL_STATES or state == "success":
            existing.finished_at = stamp
        row.current_step = step_name
        row.updated_at = stamp
        await session.flush()
        return existing

    async def step_succeeded(self, session: AsyncSession, row: Operation, step_name: str) -> bool:
        existing = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == row.id,
                    OperationStep.step_name == step_name,
                    OperationStep.state == "success",
                )
            )
        ).scalar_one_or_none()
        return existing is not None

    async def recover_stale(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        worker_id: str = WORKER_ID,
        reclaim_all_active: bool = True,
    ) -> list[Operation]:
        stamp = now or utcnow()
        if reclaim_all_active:
            stmt = select(Operation).where(Operation.state.in_(ACTIVE_STATES))
        else:
            stmt = select(Operation).where(
                Operation.state.in_(ACTIVE_STATES),
                or_(
                    Operation.locked_by == worker_id,
                    Operation.locked_by.is_(None),
                    Operation.lease_expires_at.is_(None),
                    Operation.lease_expires_at <= stamp,
                ),
            )
        rows = list((await session.execute(stmt)).scalars().all())
        recovered: list[Operation] = []
        for row in rows:
            row.state = "waiting"
            row.locked_by = None
            row.lease_expires_at = None
            row.updated_at = stamp
            await self.note(
                session,
                row,
                row.current_step or "recover",
                "process restarted; waiting for a safe resume",
                now=stamp,
                touch_lease=False,
            )
            row.locked_by = None
            row.lease_expires_at = None
            recovered.append(row)
        if recovered:
            await session.commit()
        return recovered


operation_store = OperationStore()


async def recover_stale_operations(session: AsyncSession) -> dict[str, Any]:
    """Reclaim leases. Browser / rotate / reauth stay waiting or manual_required."""
    recovered = await operation_store.recover_stale(session)
    stats = {"recovered": len(recovered), "manual": 0, "ids": []}
    for row in recovered:
        stats["ids"].append(row.public_id)
        if row.op_type in {"reauth", "onboard", "rotate", "free_register", "reregister", "free"}:
            await operation_store.finish(
                session,
                row,
                {
                    "success": False,
                    "error": "process restarted; browser tickets are invalid, resume is manual",
                    "error_code": "resume_manual",
                    "status": "manual_required",
                },
            )
            stats["manual"] += 1
    if recovered:
        await session.commit()
    logger.info("recovered stale operations recovered=%s manual=%s", stats["recovered"], stats["manual"])
    return stats
