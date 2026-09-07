"""Durable workspace-sync queue on the existing scheduler and Operation store."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import aliased

from app.application.operations import WORKER_ID, new_public_id, operation_store, pack_input
from app.core.time import utcnow
from app.domain.automation import ACTIVE_STATES, WORKSPACE_LOCK_ACTIONS
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.operations import Operation


async def enqueue_workspace_sync(db, workspace_id: int, *, source: str = "manual") -> dict:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return {"ok": False, "error_code": "not_found", "error": "workspace not found"}
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    if not workspace.official_workspace_id or owner is None or not owner.access_token_encrypted:
        return {"ok": False, "error_code": "credentials_missing", "error": "Workspace requires an owner access token before synchronization"}
    if workspace.status in {"archived", "disabled"} or owner.operational_state in {"archived", "disabled"}:
        return {"ok": False, "error_code": "workspace_unavailable", "error": "Workspace or owner is disabled"}
    key = f"workspace_sync:{workspace.id}"
    stamp = utcnow()
    public_id = new_public_id()
    # The partial unique index is the arbiter, including across browser tabs/processes.
    inserted = await db.scalar(insert(Operation).values(
        public_id=public_id, op_type="workspace_sync", entity_type="workspace",
        entity_id=workspace.id, workspace_id=workspace.id, account_id=owner.id,
        email=owner.email, source=source, state="queued", current_step="queued",
        idempotency_key=key, input_json=pack_input({"workspace_id": workspace.id}),
        cancel_requested=False, created_at=stamp, updated_at=stamp,
    ).on_conflict_do_nothing().returning(Operation.id))
    row = await db.scalar(select(Operation).where(
        Operation.idempotency_key == key, Operation.state.in_(ACTIVE_STATES),
        Operation.op_type == "workspace_sync",
    ))
    await db.commit()
    return {"success": True, "ok": True, "status": row.state,
            "operation_id": row.public_id, "reused": inserted is None,
            "workspace_id": workspace.id, "target": {"kind": "workspace", "id": workspace.id}}


async def enqueue_all_workspace_syncs(db, *, source: str = "manual") -> dict:
    ids = list((await db.scalars(select(Workspace.id).where(Workspace.status.not_in(("archived", "disabled"))).order_by(Workspace.id))).all())
    results = []
    for workspace_id in ids:
        results.append({"workspace_id": workspace_id, **await enqueue_workspace_sync(db, workspace_id, source=source)})
    accepted = sum(bool(item.get("ok")) for item in results)
    return {"success": accepted == len(results), "ok": accepted == len(results),
            "status": "queued" if accepted == len(results) else "partial", "items": results,
            "queued": accepted, "failed": len(results) - accepted,
            "reused": sum(bool(item.get("reused")) for item in results)}


async def dispatch_workspace_sync(db) -> dict:
    from app.application.workspace_sync import workspace_sync_service

    stamp = utcnow()
    blocker = aliased(Operation)
    unblocked = ~exists(select(blocker.id).where(
        blocker.workspace_id == Operation.workspace_id,
        blocker.id != Operation.id,
        blocker.op_type.in_(WORKSPACE_LOCK_ACTIONS),
        blocker.state.in_(ACTIVE_STATES),
    ))
    candidate = await db.scalar(select(Operation.id).where(
        Operation.op_type == "workspace_sync", Operation.state == "queued", unblocked,
    ).order_by(Operation.created_at, Operation.id).limit(1))
    if candidate is None:
        return {"claimed": False}
    claimed = await db.execute(update(Operation).where(
        Operation.id == candidate, Operation.state == "queued", unblocked,
    ).values(state="running", current_step="fetch_members", locked_by=WORKER_ID,
             lease_expires_at=stamp + timedelta(seconds=180), started_at=stamp, updated_at=stamp))
    await db.commit()
    if claimed.rowcount != 1:
        return {"claimed": False}
    operation = await db.get(Operation, candidate)
    public_id = operation.public_id
    try:
        async with asyncio.timeout(120):
            result = await workspace_sync_service.sync_workspace(db, operation.workspace_id, operation=operation)
        if operation.state in ACTIVE_STATES:
            await operation_store.finish(db, operation, {**result, "success": bool(result.get("ok"))})
            await db.commit()
        return {"claimed": True, "operation_id": public_id, "result": result}
    except Exception:
        # Do not persist arbitrary upstream exceptions or credentials in task summaries.
        await db.rollback()
        operation = await operation_store.get_by_public_id(db, public_id)
        if operation is not None and operation.state in ACTIVE_STATES:
            await operation_store.finish(db, operation, {"success": False, "status": "failed",
                "error_code": "sync_read_failed", "error": "Workspace read failed; previous snapshot retained"})
            await db.commit()
        return {"claimed": True, "operation_id": public_id, "error_code": "sync_read_failed"}
