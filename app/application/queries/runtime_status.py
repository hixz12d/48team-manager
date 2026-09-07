"""Authenticated runtime read model: local DB/process evidence, never provider calls."""

from __future__ import annotations

from sqlalchemy import func, select

from app.application.jobs import browser, scheduler as scheduling
from app.application.jobs.dispatcher import reauth_dispatcher
from app.application.presenters import BUSINESS_STEP_LABELS, OPERATION_TYPE_LABELS
from app.application.quota import quota_service
from app.application.reauth import reauth_service
from app.application.rotate import rotate_service
from app.application.tokens import auth_service
from app.core.time import as_utc, isoformat, utcnow
from app.domain.automation import ACTIVE_STATES, BROWSER_ACTIONS, TERMINAL_STATES
from app.persistence.models.identity import Workspace
from app.persistence.models.operations import Operation
from app.persistence.models.quota import QuotaProbeState


SAFE_ERRORS = {
    "credentials_missing": "缺少访问凭据，请手动授权后重试",
    "sync_read_failed": "读取失败，保留上次快照",
    "schema_mismatch": "成员响应不完整，保留上次快照",
    "resume_manual": "执行中断，需要人工确认",
    "oauth_expired": "授权会话已过期，请重新授权",
    "cancelled": "已取消",
    "http_429": "请求受限，等待重试",
}
SOURCE_LABELS = {"manual": "手动", "scheduled": "定时", "retry": "重试", "oauth_callback": "授权后续", "automatic": "自动"}


def runtime_operation(row, *, workspace_name=None, now=None, next_retry_at=None):
    stamp = now or utcnow()
    state = row.state
    if state == "manual_required":
        display_state, reason = "waiting_user", "需要人工确认"
    elif next_retry_at and as_utc(next_retry_at) > stamp and state in {"queued", "waiting"}:
        display_state, reason = "waiting_retry", "等待重试时间"
    elif state == "queued" and row.op_type in BROWSER_ACTIONS and browser.lock().locked():
        display_state, reason = "waiting_browser", "等待浏览器槽位"
    elif state == "waiting":
        display_state, reason = "waiting", "等待恢复或人工确认"
    else:
        display_state, reason = state, None
    start = as_utc(row.started_at) if row.started_at else None
    error_code = row.error_code if row.error_code in SAFE_ERRORS else ("operation_failed" if row.error_code else None)
    return {
        "id": row.public_id, "operation_id": row.public_id,
        "kind": row.op_type if row.op_type in OPERATION_TYPE_LABELS else "other",
        "operation_label": OPERATION_TYPE_LABELS.get(row.op_type, "后台任务"),
        "state": state, "status": display_state, "wait_reason": reason,
        "stage_label": BUSINESS_STEP_LABELS.get(row.current_step, "执行中" if state == "running" else "等待状态更新"),
        "stage_code": row.current_step if row.current_step in BUSINESS_STEP_LABELS else None,
        "target": {"kind": "workspace" if row.workspace_id else ("account" if row.account_id else "system"),
                   "id": row.workspace_id or row.account_id},
        "target_label": workspace_name or (f"工作区 #{row.workspace_id}" if row.workspace_id else (f"账号 #{row.account_id}" if row.account_id else "系统")),
        "workspace_id": row.workspace_id, "account_id": row.account_id,
        "trigger_source": row.source if row.source in SOURCE_LABELS else "unknown",
        "trigger_label": SOURCE_LABELS.get(row.source, "来源未知"),
        "created_at": isoformat(row.created_at), "started_at": isoformat(row.started_at),
        "finished_at": isoformat(row.finished_at), "updated_at": isoformat(row.updated_at),
        "elapsed_seconds": max(0, int(((as_utc(row.finished_at) if row.finished_at else stamp) - start).total_seconds())) if start else None,
        "next_retry_at": isoformat(next_retry_at),
        "safe_error_code": error_code,
        "safe_error_message": SAFE_ERRORS.get(error_code, "任务未完成，请检查任务详情" if error_code else None),
        "href": f"/operations?op={row.public_id}",
    }


def policy(key, label, enabled, job_id, *, requested=None):
    job = scheduling.scheduler.get_job(job_id)
    ready = bool(scheduling.scheduler.running and job is not None)
    next_run = getattr(job, "next_run_time", None) if ready and enabled else None
    return {"id": key, "label": label, "enabled": bool(enabled),
            "requested": bool(enabled if requested is None else requested),
            "state": "scheduled" if enabled and ready else ("not_ready" if enabled else "disabled"),
            "scheduler_ready": ready, "next_run_at": isoformat(next_run), "trigger": "periodic"}


async def runtime_status(db, *, now=None):
    stamp = now or utcnow()
    active_states = (*ACTIVE_STATES, "manual_required")
    state_counts = dict((await db.execute(select(Operation.state, func.count()).where(
        Operation.archived_at.is_(None), Operation.state.in_(active_states),
    ).group_by(Operation.state))).all())
    active = list(await db.scalars(select(Operation).where(
        Operation.archived_at.is_(None), Operation.state.in_(active_states),
    ).order_by((Operation.state == "running").desc(), Operation.created_at, Operation.id).limit(8)))
    recent = list(await db.scalars(select(Operation).where(
        Operation.archived_at.is_(None), Operation.state.in_(TERMINAL_STATES),
    ).order_by(Operation.finished_at.desc(), Operation.id.desc()).limit(5)))
    workspace_ids = {row.workspace_id for row in active + recent if row.workspace_id}
    names = {}
    if workspace_ids:
        for row in await db.scalars(select(Workspace).where(Workspace.id.in_(workspace_ids))):
            names[row.id] = row.custom_name or row.official_name or row.name or f"工作区 #{row.id}"
    retries = dict((await db.execute(select(QuotaProbeState.operation_id, QuotaProbeState.next_check_at).where(
        QuotaProbeState.operation_id.in_([row.public_id for row in active]),
    ))).all()) if active else {}
    quota = await quota_service.load_settings(db)
    auth = await auth_service.load_settings(db)
    reauth = await reauth_service.load_settings(db)
    rotate = await rotate_service.load_settings(db)
    from app.integrations.sub2api.client import sub2api_client
    sub2api = await sub2api_client.load_config(db)
    policies = [
        policy("official_quota_probe", "自动额度检查", quota["enabled"], "official_quota_probe_scan"),
        policy("token_refresh", "令牌刷新", auth["enabled"], "auth_probe_scan"),
        policy("auto_reauth", "自动重新授权", reauth["enabled"], "auto_reauth_scan", requested=reauth.get("requested")),
        policy("auto_rotate", "自动轮转", rotate["auto_rotate_enabled"], "auto_rotate_scan"),
        policy("sub2api_usage", "Sub2API 用量同步", sub2api.get("configured"), "sub2api_usage_sync"),
    ]
    heartbeat = scheduling.last_heartbeat_at
    age = (stamp - as_utc(heartbeat)).total_seconds() if heartbeat else None
    runner_state = "unavailable" if not scheduling.scheduler.running else ("unknown" if age is None else ("healthy" if 0 <= age <= 15 else "stale"))
    busy = browser.lock().locked()
    return {
        "generated_at": isoformat(stamp),
        "runner": {"state": runner_state, "last_heartbeat_at": isoformat(heartbeat),
                   "scheduler_running": bool(scheduling.scheduler.running),
                   "reauth_dispatcher_alive": reauth_dispatcher.alive},
        "counts": {"running": int(state_counts.get("running", 0)), "queued": int(state_counts.get("queued", 0)),
                   "waiting": int(state_counts.get("waiting", 0)), "waiting_user": int(state_counts.get("manual_required", 0))},
        "policies": policies,
        "active_operations": [runtime_operation(row, workspace_name=names.get(row.workspace_id), now=stamp, next_retry_at=retries.get(row.public_id)) for row in active],
        "active_total": sum(state_counts.values()),
        "recent_operations": [runtime_operation(row, workspace_name=names.get(row.workspace_id), now=stamp) for row in recent],
        "browser_slot": {"state": "busy" if busy else "free", "operation_id": reauth_dispatcher.active_operation_id if busy else None},
    }
