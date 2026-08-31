"""拉人任务进度。内存可轮询，同时写入 operations 表，重启可恢复。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from app.services.operations import (
    BROWSER_ACTIONS as OPERATION_BROWSER_ACTIONS,
    DEFAULT_LEASE_SECONDS,
    WORKER_ID,
    new_public_id,
    pack_input,
    unpack_input,
)
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}
BROWSER_ACTIONS = OPERATION_BROWSER_ACTIONS


def _now_text() -> str:
    return get_now().strftime("%H:%M:%S")


def _in_test_process() -> bool:
    argv = " ".join(sys.argv).lower()
    return "unittest" in argv or "pytest" in argv or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _db_path() -> Optional[Path]:
    try:
        from app.config import settings
    except Exception:
        return None
    url = str(getattr(settings, "database_url", "") or "")
    if "sqlite" not in url:
        return None
    raw = url.split("///")[-1]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw)


def _connect() -> Optional[sqlite3.Connection]:
    if _in_test_process():
        return None
    path = _db_path()
    if path is None or not path.exists():
        return None
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone()
        if not exists:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        conn.close()
        return None


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _row_to_job(row: sqlite3.Row) -> Dict[str, Any]:

    log_items = _loads(row["log_json"], [])
    if not isinstance(log_items, list):
        log_items = []
    state = row["state"] or "queued"
    status = "running" if state in {"queued", "running", "waiting"} else state
    payload = unpack_input(row["input_json"])
    return {
        "id": row["public_id"],
        "team_id": row["workspace_id"] or 0,
        "email": row["email"] or "",
        "phone": row["phone"] or "",
        "action": row["type"],
        "status": status,
        "state": state,
        "stage": row["current_step"] or "",
        "message": (log_items[-1] or {}).get("message") if log_items else "",
        "error": row["error_message"] or "",
        "error_code": row["error_code"] or "",
        "log": log_items,
        "cancel_requested": bool(row["cancel_requested"]),
        "result": _loads(row["result_json"], None),
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "input": payload,
        "resume": payload,
    }


def _persist_insert(job: Dict[str, Any], input_payload: Optional[Dict[str, Any]] = None) -> None:
    conn = _connect()
    if conn is None:
        return
    stamp = get_now().isoformat()
    payload = dict(input_payload or {})
    payload.setdefault("team_id", job.get("team_id") or 0)
    payload.setdefault("email", job.get("email") or "")
    payload.setdefault("action", job.get("action") or "onboard")
    try:
        conn.execute(
            """
            INSERT INTO operations (
                public_id, type, entity_type, entity_id, workspace_id, email, phone,
                state, current_step, locked_by, lease_expires_at, cancel_requested,
                input_json, log_json, created_at, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job.get("action") or "onboard",
                "team" if job.get("team_id") else None,
                job.get("team_id") or None,
                job.get("team_id") or None,
                (job.get("email") or "").strip().lower(),
                job.get("phone") or "",
                "running",
                job.get("stage") or "queued",
                WORKER_ID,
                (get_now() + timedelta(seconds=DEFAULT_LEASE_SECONDS)).isoformat(),
                pack_input(payload),
                json.dumps(job.get("log") or [], ensure_ascii=False),
                stamp,
                stamp,
                stamp,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("persist job insert failed id=%s error=%s", job.get("id"), exc)
    finally:
        conn.close()


def _persist_update(job_id: str, fields: Dict[str, Any]) -> None:
    conn = _connect()
    if conn is None:
        return
    assignments = []
    values = []
    mapping = {
        "email": "email",
        "phone": "phone",
        "stage": "current_step",
        "error": "error_message",
        "error_code": "error_code",
        "status": "state",
        "cancel_requested": "cancel_requested",
        "result": "result_json",
        "log": "log_json",
        "input": "input_json",
    }

    for key, column in mapping.items():
        if key not in fields:
            continue
        value = fields[key]
        if key == "status":
            if value == "running":
                value = "running"
            assignments.append("state = ?")
            values.append(value)
            continue
        if key in {"result", "log"}:
            assignments.append(f"{column} = ?")
            values.append(json.dumps(value, ensure_ascii=False, default=str) if value is not None else None)
            continue
        if key == "input":
            assignments.append("input_json = ?")
            values.append(pack_input(value if isinstance(value, dict) else {}))
            continue
        if key == "cancel_requested":
            assignments.append("cancel_requested = ?")
            values.append(1 if value else 0)
            continue
        assignments.append(f"{column} = ?")
        values.append(value)
    assignments.append("updated_at = ?")
    values.append(get_now().isoformat())
    if fields.get("status") in {"success", "failed", "cancelled", "manual_required"}:
        assignments.append("finished_at = ?")
        values.append(get_now().isoformat())
        assignments.append("locked_by = NULL")
        assignments.append("lease_expires_at = NULL")
    elif fields.get("status") == "running" or "stage" in fields:
        assignments.append("locked_by = ?")
        values.append(WORKER_ID)

        assignments.append("lease_expires_at = ?")
        values.append((get_now() + timedelta(seconds=DEFAULT_LEASE_SECONDS)).isoformat())
    values.append(job_id)
    try:
        conn.execute(
            f"UPDATE operations SET {', '.join(assignments)} WHERE public_id = ?",
            values,
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("persist job update failed id=%s error=%s", job_id, exc)
    finally:
        conn.close()


def _load_from_db(job_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM operations WHERE public_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def create_job(
    *,
    team_id: int,
    email: str,
    action: str = "onboard",
    input_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    job_id = new_public_id()
    job = {
        "id": job_id,
        "team_id": team_id,
        "email": email,
        "phone": "",
        "action": action,
        "status": "running",
        "stage": "queued",
        "message": "已排队，准备拉人",
        "error": "",
        "error_code": "",
        "log": [{"ts": _now_text(), "stage": "queued", "message": "已排队，准备拉人"}],
        "cancel_requested": False,
        "result": None,
        "created_at": get_now().isoformat(),
        "updated_at": get_now().isoformat(),
        "resume": dict(input_payload or {}),
    }
    with _LOCK:
        _JOBS[job_id] = job
        snapshot = dict(job)
    _persist_insert(snapshot, input_payload)
    return snapshot


def _ensure_cached(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            return job
    loaded = _load_from_db(job_id)
    if not loaded:
        return None
    with _LOCK:
        _JOBS[job_id] = loaded
        return loaded


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            return dict(job)
    loaded = _load_from_db(job_id)
    if loaded:
        with _LOCK:
            _JOBS[job_id] = loaded
        return dict(loaded)
    return None


def latest_job_for_email(email: str) -> Optional[Dict[str, Any]]:
    target = (email or "").strip().lower()
    with _LOCK:
        matches = [job for job in _JOBS.values() if str(job.get("email") or "").lower() == target]
    if matches:
        matches.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return dict(matches[0])
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM operations WHERE email = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (target,),
        ).fetchone()
        return _row_to_job(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def active_job_for_email(email: str) -> Optional[Dict[str, Any]]:
    job = latest_job_for_email(email)
    if job and job.get("status") == "running":
        return job
    return None


def iter_running(actions: Optional[Sequence[str]] = None) -> list:
    wanted = {str(item) for item in actions} if actions is not None else None
    found: Dict[str, Dict[str, Any]] = {}
    with _LOCK:
        for job in _JOBS.values():
            if job.get("status") != "running":
                continue
            if wanted is not None and str(job.get("action") or "") not in wanted:
                continue
            found[str(job.get("id"))] = dict(job)
    conn = _connect()
    if conn is not None:
        try:
            sql = "SELECT * FROM operations WHERE state IN ('queued', 'running', 'waiting')"
            params: list[Any] = []
            if wanted is not None:
                placeholders = ",".join("?" for _ in wanted)
                sql += f" AND type IN ({placeholders})"
                params.extend(sorted(wanted))
            for row in conn.execute(sql, params).fetchall():
                job = _row_to_job(row)
                found.setdefault(job["id"], job)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return list(found.values())


def any_running(actions: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
    jobs = iter_running(actions)
    return jobs[0] if jobs else None


def update_email(job_id: Optional[str], email: str) -> None:
    if not job_id:
        return
    job = _ensure_cached(job_id)
    if not job:
        return
    with _LOCK:
        job["email"] = email
        job["updated_at"] = get_now().isoformat()
    _persist_update(job_id, {"email": email})


def update_phone(job_id: Optional[str], phone: str) -> None:
    if not job_id:
        return
    job = _ensure_cached(job_id)
    if not job:
        return
    with _LOCK:
        job["phone"] = phone
        job["updated_at"] = get_now().isoformat()
    _persist_update(job_id, {"phone": phone})


def note(job_id: Optional[str], stage: str, message: str, *, error: str = "", error_code: str = "") -> None:
    if not job_id:
        return
    if _ensure_cached(job_id) is None:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["stage"] = stage
        job["message"] = message
        job["updated_at"] = get_now().isoformat()
        if error:
            job["error"] = error
            job["error_code"] = error_code or job.get("error_code") or ""
        job["log"].append({"ts": _now_text(), "stage": stage, "message": message})
        job["log"] = job["log"][-40:]
        snapshot = {
            "stage": job["stage"],
            "error": job.get("error") or "",
            "error_code": job.get("error_code") or "",
            "log": list(job["log"]),
            "status": job.get("status") or "running",
        }
    _persist_update(job_id, snapshot)


def request_cancel(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            loaded = None
        else:
            job["cancel_requested"] = True
            job["message"] = "已请求停止，将在当前步骤结束后退出"
            job["updated_at"] = get_now().isoformat()
            job["log"].append({"ts": _now_text(), "stage": job.get("stage") or "cancel", "message": job["message"]})
            loaded = dict(job)
    if loaded is None:
        loaded = get_job(job_id)
        if not loaded:
            return None
        with _LOCK:
            job = _JOBS.get(job_id) or loaded
            job["cancel_requested"] = True
            job["message"] = "已请求停止，将在当前步骤结束后退出"
            job["updated_at"] = get_now().isoformat()
            job.setdefault("log", []).append(
                {"ts": _now_text(), "stage": job.get("stage") or "cancel", "message": job["message"]}
            )
            _JOBS[job_id] = job
            loaded = dict(job)
    _persist_update(job_id, {"cancel_requested": True, "log": loaded.get("log"), "stage": loaded.get("stage")})
    return loaded


def is_cancelled(job_id: Optional[str]) -> bool:
    if not job_id:
        return False
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            return bool(job.get("cancel_requested"))
    loaded = get_job(job_id)
    return bool(loaded and loaded.get("cancel_requested"))


def finish(job_id: Optional[str], result: Dict[str, Any]) -> None:
    if not job_id:
        return
    success = bool(result.get("success"))
    cancelled = result.get("status") == "cancelled" or result.get("error_code") == "cancelled"
    if _ensure_cached(job_id) is None:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        if cancelled:
            job["status"] = "cancelled"
        elif success:
            job["status"] = "success"
        elif result.get("status") == "manual_required":
            job["status"] = "manual_required"
        else:
            job["status"] = "failed"
        job["result"] = result
        job["updated_at"] = get_now().isoformat()
        if success:
            job["stage"] = result.get("status") or "done"
            job["message"] = result.get("message") or "拉人完成"
            job["error"] = ""
        else:
            job["stage"] = result.get("status") or job.get("stage") or "failed"
            job["message"] = result.get("error") or "拉人失败"
            job["error"] = result.get("error") or job.get("error") or ""
            job["error_code"] = result.get("error_code") or job.get("error_code") or ""
        job["log"].append({"ts": _now_text(), "stage": job["stage"], "message": job["message"]})
        snapshot = {
            "status": job["status"],
            "stage": job["stage"],
            "error": job.get("error") or "",
            "error_code": job.get("error_code") or "",
            "result": result,
            "log": list(job["log"]),
        }
    _persist_update(job_id, snapshot)


def attach_resume(job_id: Optional[str], payload: Dict[str, Any]) -> None:
    if not job_id:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            merged = dict(job.get("resume") or {})
            merged.update(payload)
            job["resume"] = merged
    _persist_update(job_id, {"input": payload})
