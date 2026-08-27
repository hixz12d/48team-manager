"""拉人任务进度。进程内可轮询，关键状态同时写回子号。"""
from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, Optional

from app.utils.time_utils import get_now

_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}


def _now_text() -> str:
    return get_now().strftime("%H:%M:%S")


def create_job(*, team_id: int, email: str, action: str = "onboard") -> Dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
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
    }
    with _LOCK:
        _JOBS[job_id] = job
        return dict(job)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def latest_job_for_email(email: str) -> Optional[Dict[str, Any]]:
    target = (email or "").strip().lower()
    with _LOCK:
        matches = [job for job in _JOBS.values() if str(job.get("email") or "").lower() == target]
    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return dict(matches[0])


def active_job_for_email(email: str) -> Optional[Dict[str, Any]]:
    job = latest_job_for_email(email)
    if job and job.get("status") == "running":
        return job
    return None


def update_email(job_id: Optional[str], email: str) -> None:
    if not job_id:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["email"] = email
        job["updated_at"] = get_now().isoformat()


def update_phone(job_id: Optional[str], phone: str) -> None:
    if not job_id:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["phone"] = phone
        job["updated_at"] = get_now().isoformat()

def note(job_id: Optional[str], stage: str, message: str, *, error: str = "", error_code: str = "") -> None:
    if not job_id:
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


def request_cancel(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        job["cancel_requested"] = True
        job["message"] = "已请求停止，将在当前步骤结束后退出"
        job["updated_at"] = get_now().isoformat()
        job["log"].append({"ts": _now_text(), "stage": job.get("stage") or "cancel", "message": job["message"]})
        return dict(job)


def is_cancelled(job_id: Optional[str]) -> bool:
    if not job_id:
        return False
    with _LOCK:
        job = _JOBS.get(job_id)
        return bool(job and job.get("cancel_requested"))


def finish(job_id: Optional[str], result: Dict[str, Any]) -> None:
    if not job_id:
        return
    success = bool(result.get("success"))
    cancelled = result.get("status") == "cancelled" or result.get("error_code") == "cancelled"
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "cancelled" if cancelled else ("success" if success else "failed")
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
