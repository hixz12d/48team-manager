"""Phase 4：长任务落库。UI 仍用 job_id，底层写 operations / operation_steps。"""
from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Operation, OperationStep
from app.services.encryption import encryption_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("queued", "running", "waiting")
TERMINAL_STATES = ("success", "failed", "cancelled", "manual_required")
BROWSER_ACTIONS = ("reauth", "onboard", "rotate", "free", "free_register", "reregister")
WORKSPACE_LOCK_ACTIONS = ("rotate", "onboard", "reregister")
SENSITIVE_INPUT_KEYS = ("password", "login_password", "code_verifier")
DEFAULT_LEASE_SECONDS = 180
MAX_LOG_ITEMS = 40
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def new_public_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_text(now: Optional[datetime] = None) -> str:
    return (now or get_now()).strftime("%H:%M:%S")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _encrypt_secret(value: str) -> str:
    return encryption_service.encrypt_token(value)


def _decrypt_secret(value: str) -> str:
    try:
        return encryption_service.decrypt_token(value)
    except Exception:
        return value


def pack_input(payload: Optional[Dict[str, Any]]) -> str:
    data = dict(payload or {})
    packed: Dict[str, Any] = {}
    for key, value in data.items():
        if key in SENSITIVE_INPUT_KEYS and value:
            packed[key] = {"enc": _encrypt_secret(str(value))}
        else:
            packed[key] = value
    return _dumps(packed)


def unpack_input(raw: Optional[str]) -> Dict[str, Any]:
    data = _loads(raw, {})
    if not isinstance(data, dict):
        return {}
    unpacked: Dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "enc" in value:
            unpacked[key] = _decrypt_secret(str(value.get("enc") or ""))
        else:
            unpacked[key] = value
    return unpacked


def serialize_operation(
    row: Operation,
    *,
    steps: Optional[Sequence[OperationStep]] = None,
    include_input: bool = False,
) -> Dict[str, Any]:
    log_items = _loads(row.log_json, [])
    if not isinstance(log_items, list):
        log_items = []
    result = _loads(row.result_json, None)
    payload = {
        "id": row.public_id,
        "operation_id": row.id,
        "team_id": row.workspace_id or 0,
        "email": row.email or "",
        "phone": row.phone or "",
        "action": row.op_type,
        "status": "running" if row.state in ACTIVE_STATES else row.state,
        "state": row.state,
        "stage": row.current_step or "",
        "message": (log_items[-1] or {}).get("message") if log_items else "",
        "error": row.error_message or "",
        "error_code": row.error_code or "",
        "log": log_items,
        "cancel_requested": bool(row.cancel_requested),
        "result": result,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        "started_at": row.started_at.isoformat() if row.started_at else "",
        "finished_at": row.finished_at.isoformat() if row.finished_at else "",
        "locked_by": row.locked_by or "",
        "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else "",
        "current_step": row.current_step or "",
        "input": unpack_input(row.input_json) if include_input else {},
        "resolved_proxy": row.resolved_proxy or "",
        "resolved_proxy_profile_id": row.resolved_proxy_profile_id,
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
    if log_items:
        payload["message"] = str(log_items[-1].get("message") or payload["message"] or "")
    return payload


class OperationStore:
    async def get_by_public_id(self, session: AsyncSession, public_id: str) -> Optional[Operation]:
        if not public_id:
            return None
        return (
            await session.execute(select(Operation).where(Operation.public_id == public_id))
        ).scalar_one_or_none()

    async def create(
        self,
        session: AsyncSession,
        *,
        op_type: str,
        team_id: int = 0,
        email: str = "",
        phone: str = "",
        input_payload: Optional[Dict[str, Any]] = None,
        public_id: Optional[str] = None,
        now: Optional[datetime] = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> Operation:
        stamp = now or get_now()
        row = Operation(
            public_id=public_id or new_public_id(),
            op_type=op_type,
            entity_type="team" if team_id else None,
            entity_id=int(team_id) if team_id else None,
            workspace_id=int(team_id) if team_id else None,
            email=(email or "").strip().lower(),
            phone=phone or "",
            state="running",
            current_step="queued",
            locked_by=WORKER_ID,
            lease_expires_at=stamp + timedelta(seconds=lease_seconds),
            cancel_requested=False,
            input_json=pack_input(input_payload),
            log_json=_dumps([{"ts": _now_text(stamp), "stage": "queued", "message": "已排队，准备拉人"}]),
            created_at=stamp,
            started_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        await session.flush()
        return row

    async def latest_for_email(self, session: AsyncSession, email: str) -> Optional[Operation]:
        target = (email or "").strip().lower()
        if not target:
            return None
        return (
            await session.execute(
                select(Operation)
                .where(Operation.email == target)
                .order_by(Operation.created_at.desc(), Operation.id.desc())
            )
        ).scalars().first()

    async def active_for_email(self, session: AsyncSession, email: str) -> Optional[Operation]:
        row = await self.latest_for_email(session, email)
        if row and row.state in ACTIVE_STATES:
            return row
        return None

    async def active_for_workspace(
        self,
        session: AsyncSession,
        workspace_id: int,
        *,
        actions: Optional[Sequence[str]] = None,
        exclude_public_id: Optional[str] = None,
    ) -> Optional[Operation]:
        try:
            team_id = int(workspace_id or 0)
        except (TypeError, ValueError):
            return None
        if not team_id:
            return None
        stmt = select(Operation).where(
            Operation.state.in_(ACTIVE_STATES),
            Operation.workspace_id == team_id,
        )
        if actions is not None:
            stmt = stmt.where(Operation.op_type.in_(list(actions)))
        exclude = str(exclude_public_id or "").strip()
        if exclude:
            stmt = stmt.where(Operation.public_id != exclude)
        stmt = stmt.order_by(Operation.created_at.desc(), Operation.id.desc())
        return (await session.execute(stmt)).scalars().first()

    async def iter_running(
        self,
        session: AsyncSession,
        actions: Optional[Sequence[str]] = None,
    ) -> List[Operation]:
        stmt = select(Operation).where(Operation.state.in_(ACTIVE_STATES))
        if actions is not None:
            stmt = stmt.where(Operation.op_type.in_(list(actions)))
        return list((await session.execute(stmt)).scalars().all())

    async def list_recent(
        self,
        session: AsyncSession,
        *,
        limit: int = 80,
        email: Optional[str] = None,
        account_id: Optional[int] = None,
        include_steps: bool = False,
    ) -> List[Dict[str, Any]]:
        stmt = select(Operation).order_by(desc(Operation.updated_at), desc(Operation.id))
        if email:
            stmt = stmt.where(Operation.email == email)
        if account_id is not None:
            stmt = stmt.where(Operation.account_id == int(account_id))
        rows = list((await session.execute(stmt.limit(max(1, min(int(limit or 80), 200))))).scalars().all())
        if not include_steps:
            return [serialize_operation(row, include_input=False) for row in rows]
        payloads = []
        for row in rows:
            payloads.append(serialize_operation(row, steps=await self.steps_for(session, row), include_input=False))
        return payloads

    async def steps_for(self, session: AsyncSession, row: Operation) -> List[OperationStep]:
        result = await session.execute(
            select(OperationStep)
            .where(OperationStep.operation_id == row.id)
            .order_by(OperationStep.id.asc())
        )
        return list(result.scalars().all())

    async def heartbeat(
        self,
        session: AsyncSession,
        row: Operation,
        *,
        now: Optional[datetime] = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        stamp = now or get_now()
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
        now: Optional[datetime] = None,
        touch_lease: bool = True,
    ) -> None:
        stamp = now or get_now()
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

    async def request_cancel(self, session: AsyncSession, row: Operation, *, now: Optional[datetime] = None) -> None:
        stamp = now or get_now()
        row.cancel_requested = True
        row.updated_at = stamp
        await self.note(session, row, row.current_step or "cancel", "已请求停止，将在当前步骤结束后退出", now=stamp)

    async def finish(
        self,
        session: AsyncSession,
        row: Operation,
        result: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> None:
        stamp = now or get_now()
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
        row.locked_by = None
        row.lease_expires_at = None
        if success:
            row.current_step = str(result.get("status") or "done")
            row.error_code = None
            row.error_message = None
            await self.note(
                session,
                row,
                row.current_step,
                result.get("message") or "拉人完成",
                now=stamp,
                touch_lease=False,
            )
        else:
            row.current_step = str(result.get("status") or row.current_step or "failed")
            row.error_code = str(result.get("error_code") or row.error_code or "")[:40] or None
            row.error_message = str(result.get("error") or row.error_message or "拉人失败")[:500]
            await self.note(
                session,
                row,
                row.current_step,
                row.error_message or "拉人失败",
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
        result: Optional[Dict[str, Any]] = None,
        error_code: str = "",
        error_message: str = "",
        now: Optional[datetime] = None,
    ) -> OperationStep:
        stamp = now or get_now()
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
        now: Optional[datetime] = None,
        worker_id: str = WORKER_ID,
        reclaim_all_active: bool = True,
    ) -> List[Operation]:
        """回收未完成任务。单实例启动时收回全部 active，避免 PID 变化后空等租约。"""
        stamp = now or get_now()
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
        recovered: List[Operation] = []
        for row in rows:
            row.state = "waiting"
            row.locked_by = None
            row.lease_expires_at = None
            row.updated_at = stamp
            await self.note(
                session,
                row,
                row.current_step or "recover",
                "进程重启，任务待续跑",
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


async def recover_and_resume_stale_operations() -> Dict[str, Any]:
    """启动时回收过期 running，并安全续跑 onboard/rotate。reauth 缺 ticket 则标 manual_required。"""
    from datetime import datetime as dt

    from app.database import AsyncSessionLocal
    from app.routes.seats import FreeOnboardRequest, OnboardRequest, _run_free_onboard_job, _run_onboard_job

    stats = {"recovered": 0, "resumed": 0, "manual": 0, "ids": []}
    async with AsyncSessionLocal() as session:
        recovered = await operation_store.recover_stale(session)
        snapshots = [serialize_operation(row, include_input=True) for row in recovered]
        stats["recovered"] = len(snapshots)
        stats["ids"] = [item["id"] for item in snapshots]

    for item in snapshots:
        public_id = item["id"]
        action = str(item.get("action") or "")
        payload = dict(item.get("input") or {})
        if action in {"onboard", "reregister"}:
            request = OnboardRequest(
                team_id=int(payload.get("team_id") or item.get("team_id") or 0),
                email=str(payload.get("email") or item.get("email") or ""),
                phone=str(payload.get("phone") or ""),
                proxy=str(payload.get("proxy") or ""),
                password=str(payload.get("password") or ""),
                reuse_existing=bool(payload.get("reuse_existing", True)),
                skip_invite=bool(payload.get("skip_invite", False)),
                force=bool(payload.get("force", False)),
            )
            import asyncio

            asyncio.create_task(_run_onboard_job(public_id, request))
            stats["resumed"] += 1
            continue
        if action in {"free_register", "free"}:
            request = FreeOnboardRequest(
                email=str(payload.get("email") or item.get("email") or ""),
                phone=str(payload.get("phone") or ""),
                proxy=str(payload.get("proxy") or ""),
                password=str(payload.get("password") or ""),
            )
            import asyncio

            asyncio.create_task(_run_free_onboard_job(public_id, request))
            stats["resumed"] += 1
            continue
        if action == "rotate":
            raw_eligible = payload.get("next_eligible_at")
            eligible = None
            if raw_eligible:
                try:
                    eligible = dt.fromisoformat(str(raw_eligible))
                except ValueError:
                    eligible = None
            from app.services.auto_rotate import auto_rotate_service
            from app.services import onboard_jobs

            async with AsyncSessionLocal() as session:
                result = await auto_rotate_service.run_rotate_saga(
                    session,
                    job_id=public_id,
                    team_id=int(payload.get("team_id") or item.get("team_id") or 0),
                    email=str(payload.get("email") or item.get("email") or ""),
                    reason=str(payload.get("reason") or ""),
                    force_refill=bool(payload.get("force_refill", False)),
                    next_eligible_at=eligible,
                    email_line=str(payload.get("email_line") or ""),
                    phone_line=str(payload.get("phone") or payload.get("phone_line") or ""),
                    proxy=str(payload.get("proxy") or ""),
                    child_id=payload.get("child_id"),
                )
                onboard_jobs.finish(public_id, {"success": bool(result.get("success")), **result})
            stats["resumed"] += 1
            continue
        if action == "reauth":
            async with AsyncSessionLocal() as session:
                row = await operation_store.get_by_public_id(session, public_id)
                if row:
                    await operation_store.finish(
                        session,
                        row,
                        {
                            "success": False,
                            "error": "进程重启后 OAuth ticket 已失效，请重新发起重授权",
                            "error_code": "oauth_expired",
                            "status": "manual_required",
                        },
                    )
                    await session.commit()
            stats["manual"] += 1
            continue
        async with AsyncSessionLocal() as session:
            row = await operation_store.get_by_public_id(session, public_id)
            if row:
                await operation_store.finish(
                    session,
                    row,
                    {
                        "success": False,
                        "error": f"无法自动续跑 action={action}",
                        "error_code": "resume_unsupported",
                        "status": "manual_required",
                    },
                )
                await session.commit()
            stats["manual"] += 1
    return stats
