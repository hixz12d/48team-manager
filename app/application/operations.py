"""Persistent operations with DB leases. Restart recovery never resumes Playwright."""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import token_cipher
from app.core.proxy import mask_proxy_url
from app.core.time import isoformat, utcnow
from app.domain.automation import (
    ACTIVE_STATES,
    BROWSER_ACTIONS,
    DEFAULT_LEASE_SECONDS,
    MAX_LOG_ITEMS,
    SENSITIVE_INPUT_KEYS,
    REDACT_VALUE_MARKERS,
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


def _looks_sensitive(key: str, value: Any) -> bool:
    name = str(key or "").lower()
    if name in SENSITIVE_INPUT_KEYS:
        return True
    if any(marker in name for marker in REDACT_VALUE_MARKERS):
        return True
    text = str(value or "")
    if "://" in text and "@" in text:
        return True
    return False


def _fingerprint(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _redact_value(key: str, value: Any) -> Any:
    if value in (None, "", [], {}):
        return value
    if isinstance(value, dict):
        return {inner: _redact_value(inner, nested) for inner, nested in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    text = str(value)
    if "://" in text and "@" in text:
        return mask_proxy_url(text) or "***"
    if _looks_sensitive(key, value):
        return {"redacted": True, "fingerprint": _fingerprint(text)}
    return value


def pack_input(payload: dict[str, Any] | None) -> str:
    packed: dict[str, Any] = {}
    cipher = token_cipher()
    for key, value in dict(payload or {}).items():
        if key in {"password", "login_password", "code_verifier"} and value:
            packed[key] = {"enc": cipher.encrypt(str(value))}
        else:
            packed[key] = _redact_value(key, value)
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
        elif isinstance(value, dict) and value.get("redacted"):
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


def serialize_operation(
    row: Operation,
    *,
    steps: list[OperationStep] | None = None,
    workspace_name: str | None = None,
    account_email: str | None = None,
    proxy_label: str | None = None,
) -> dict[str, Any]:
    from app.application.presenters import business_step_label, operation_type_label

    log_items = _loads(row.log_json, [])
    if not isinstance(log_items, list):
        log_items = []
    result = _loads(row.result_json, None)
    result_dict = result if isinstance(result, dict) else {}
    outcome = str(result_dict.get("outcome") or "").strip() or None
    target_type = "account" if row.email or row.account_id else ("workspace" if row.workspace_id else "system")
    if row.op_type == "proxy_check":
        target_type = "proxy"
    if row.op_type.startswith("hme"):
        target_type = "hme"
    target_label = (
        proxy_label
        or account_email
        or row.email
        or workspace_name
        or (f"workspace:{row.workspace_id}" if row.workspace_id else None)
        or row.public_id
    )
    business_step = business_step_label(row.current_step, state=row.state)
    payload = {
        "id": row.public_id,
        "operation_id": row.id,
        "status": "running" if row.state in ACTIVE_STATES else row.state,
        "state": row.state,
        "terminal_state": row.state if row.state in TERMINAL_STATES else None,
        "operation": row.op_type,
        "operation_label": operation_type_label(row.op_type),
        "target": target_label,
        "target_type": target_type,
        "target_label": target_label,
        "workspace": workspace_name,
        "workspace_name": workspace_name,
        "current_step": row.current_step or "",
        "business_step": business_step,
        "started": isoformat(row.started_at or row.created_at),
        "duration": duration_text(row),
        "email": row.email or account_email or "",
        "error": row.error_message or "",
        "error_code": row.error_code or "",
        "log": log_items,
        "cancel_requested": bool(row.cancel_requested),
        "result": result,
        "outcome": outcome,
        "locked_by": row.locked_by or "",
        "lease_expires_at": isoformat(row.lease_expires_at),
        "updated": isoformat(row.updated_at or row.finished_at or row.started_at or row.created_at),
        "created": isoformat(row.created_at),
        "finished": isoformat(row.finished_at),
        "workspace_id": row.workspace_id,
        "account_id": row.account_id,
        "source": getattr(row, "source", None) or "manual",
        "archived_at": isoformat(getattr(row, "archived_at", None)),
        "archive_reason": getattr(row, "archive_reason", None),
        "archived": bool(getattr(row, "archived_at", None)),
    }
    if steps is not None:
        payload["steps"] = [
            {
                "step_name": item.step_name,
                "step_label": business_step_label(item.step_name, state=item.state),
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
        source: str = "manual",
        state: str = "running",
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
            source=source or "manual",
            state=state or "running",
            current_step="queued",
            locked_by=None if (state or "running") == "queued" else WORKER_ID,
            lease_expires_at=None if (state or "running") == "queued" else stamp + timedelta(seconds=lease_seconds),
            cancel_requested=False,
            input_json=pack_input(input_payload),
            log_json=_dumps([{"ts": _now_text(stamp), "stage": "queued", "message": "queued"}]),
            resolved_proxy=str(resolved_proxy or "").strip() or None,
            resolved_proxy_profile_id=resolved_proxy_profile_id,
            created_at=stamp,
            started_at=None if (state or "running") == "queued" else stamp,
            updated_at=stamp,
        )
        session.add(row)
        await session.flush()
        return row

    async def list_recent(self, session: AsyncSession, *, limit: int = 100) -> list[Operation]:
        result = await session.execute(
            select(Operation)
            .where(Operation.archived_at.is_(None))
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_filtered(
        self,
        session: AsyncSession,
        *,
        q: str = "",
        state: str = "",
        op_type: str = "",
        source: str = "",
        workspace_id: int | None = None,
        account_id: int | None = None,
        date_from=None,
        date_to=None,
        include_archived: bool = False,
        archived_only: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        from sqlalchemy import and_, func

        page = max(1, int(page or 1))
        page_size = max(1, min(100, int(page_size or 50)))
        filters = []
        if archived_only:
            filters.append(Operation.archived_at.is_not(None))
        elif not include_archived:
            filters.append(Operation.archived_at.is_(None))
        if state:
            wanted = [item.strip() for item in str(state).split(",") if item.strip()]
            if wanted:
                filters.append(Operation.state.in_(wanted))
        if op_type:
            wanted = [item.strip() for item in str(op_type).split(",") if item.strip()]
            if wanted:
                filters.append(Operation.op_type.in_(wanted))
        if source:
            filters.append(Operation.source == str(source).strip())
        if workspace_id:
            filters.append(Operation.workspace_id == int(workspace_id))
        if account_id:
            filters.append(Operation.account_id == int(account_id))
        if date_from is not None:
            filters.append(Operation.created_at >= date_from)
        if date_to is not None:
            filters.append(Operation.created_at <= date_to)
        query_text = str(q or "").strip()
        if query_text:
            like = f"%{query_text.lower()}%"
            filters.append(
                or_(
                    func.lower(Operation.public_id).like(like),
                    func.lower(Operation.email).like(like),
                    func.lower(Operation.op_type).like(like),
                    func.lower(Operation.error_message).like(like),
                )
            )
        where_clause = and_(*filters) if filters else True
        total = int((await session.execute(select(func.count()).select_from(Operation).where(where_clause))).scalar_one() or 0)
        rows = list(
            (
                await session.execute(
                    select(Operation)
                    .where(where_clause)
                    .order_by(Operation.created_at.desc(), Operation.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).scalars()
        )
        state_rows = (
            await session.execute(
                select(Operation.state, func.count())
                .where(where_clause)
                .group_by(Operation.state)
            )
        ).all()
        type_rows = (
            await session.execute(
                select(Operation.op_type, func.count())
                .where(where_clause)
                .group_by(Operation.op_type)
            )
        ).all()
        return {
            "items": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": page * page_size < total,
            "facets": {
                "states": {str(name): int(count) for name, count in state_rows if name},
                "types": {str(name): int(count) for name, count in type_rows if name},
            },
        }

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
        stmt = (
            select(Operation)
            .where(Operation.state.in_(("running", "waiting")), Operation.op_type.in_(BROWSER_ACTIONS))
            .order_by(Operation.started_at.desc(), Operation.id.desc())
        )
        return (await session.execute(stmt)).scalars().first()

    async def claim_next_reauth(
        self,
        session: AsyncSession,
        *,
        worker_id: str = WORKER_ID,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> Operation | None:
        stamp = now or utcnow()
        leftover_lock = or_(
            Operation.locked_by.is_(None),
            Operation.lease_expires_at.is_(None),
            Operation.lease_expires_at <= stamp,
        )
        candidate = await session.scalar(
            select(Operation.id)
            .where(Operation.op_type == "reauth", Operation.state == "queued", leftover_lock)
            .order_by(Operation.created_at.asc(), Operation.id.asc())
            .limit(1)
        )
        if candidate is None:
            return None
        claimed = await session.execute(
            update(Operation)
            .where(Operation.id == int(candidate), Operation.state == "queued", leftover_lock)
            .values(
                state="running",
                current_step="preflight",
                locked_by=worker_id,
                lease_expires_at=stamp + timedelta(seconds=lease_seconds),
                started_at=stamp,
                updated_at=stamp,
            )
        )
        if claimed.rowcount != 1:
            await session.rollback()
            return None
        await session.commit()
        return await session.get(Operation, int(candidate))

    async def runtime_summary(self, session: AsyncSession) -> dict[str, Any]:
        queued = int(await session.scalar(select(func.count()).select_from(Operation).where(Operation.op_type == "reauth", Operation.state == "queued")) or 0)
        stale = int(
            await session.scalar(
                select(func.count()).select_from(Operation).where(
                    Operation.op_type == "reauth",
                    Operation.state.in_(("running", "waiting")),
                    Operation.lease_expires_at < utcnow(),
                )
            )
            or 0
        )
        active = await self.browser_busy(session)
        return {
            "queued_count": queued,
            "stale_lease_count": stale,
            "active_browser_operation": active.public_id if active else None,
        }

    async def check_cancel(self, session: AsyncSession, row: Operation, *, destructive_started: bool = False) -> dict[str, Any] | None:
        if not row.cancel_requested:
            return None
        if destructive_started:
            return {
                "success": False,
                "status": "partial",
                "partial": True,
                "error_code": "cancel_after_side_effect",
                "error": "cancel requested after an external side effect; left partial/manual_required",
            }
        return {
            "success": False,
            "status": "cancelled",
            "error_code": "cancelled",
            "error": "operation cancelled before further side effects",
        }

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

    async def heartbeat_active(
        self,
        session: AsyncSession,
        public_id: str,
        *,
        worker_id: str = WORKER_ID,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        stamp = now or utcnow()
        result = await session.execute(
            update(Operation)
            .where(
                Operation.public_id == public_id,
                Operation.state.in_(("running", "waiting")),
            )
            .values(
                locked_by=worker_id,
                lease_expires_at=stamp + timedelta(seconds=lease_seconds),
                updated_at=stamp,
            )
        )
        return result.rowcount == 1

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
        elif requested_state == "partial" or bool(result.get("partial")):
            row.state = "partial"
            result = {**result, "success": False, "status": "partial", "partial": True}
        elif requested_state == "manual_required":
            row.state = "manual_required"
        elif success:
            row.state = "success"
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
        reclaim_all_active: bool = False,
    ) -> list[Operation]:
        stamp = now or utcnow()
        if reclaim_all_active:
            stmt = select(Operation).where(Operation.state.in_(("running", "waiting")))
        else:
            stmt = select(Operation).where(
                Operation.state.in_(("running", "waiting")),
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
