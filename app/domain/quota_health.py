"""Evidence-based quota health. No automation or account mutation in presenters."""
from __future__ import annotations

from datetime import timedelta
from email.utils import parsedate_to_datetime

from app.core.time import as_utc, isoformat, utcnow

AUTH_CODES = {"token_revoked", "token_invalidated", "invalid_token", "invalid_grant", "http_401"}
LABELS = {
    "healthy": ("检测正常", "success", None),
    "quota_exhausted": ("额度用尽", "warning", None),
    "auth_required": ("授权失效", "error", "reauthorize"),
    "unauthorized": ("尚未授权", "warning", "authorize"),
    "forbidden": ("访问被拒绝 · 待核对权限", "warning", "details"),
    "rate_limited": ("限流 · 等待重试", "warning", None),
    "temporary_failure": ("检测暂时失败", "warning", "check"),
    "parse_error": ("额度响应异常", "warning", "check"),
    "credential_error": ("本地凭证配置异常", "error", "details"),
    "pending": ("凭证已更新 · 待验证", "info", "check"),
    "unchecked": ("尚未检测", "muted", "check"),
    "disabled": ("本地已停用 / 归档", "muted", None),
    "deactivated": ("已停用", "error", None),
}


def result_state(result) -> str:
    if result.success:
        values = (result.five_hour_used_percent, result.seven_day_used_percent)
        return "quota_exhausted" if any(v is not None and v >= 100 for v in values) else "healthy"
    code = result.error_code
    status = getattr(result, "http_status", None)
    if code == "credential_error":
        return "credential_error"
    if code == "missing_token":
        return "unauthorized"
    if getattr(result, "error_source", None) not in (None, "official_quota"):
        return "temporary_failure"
    if status == 401 or code in AUTH_CODES:
        return "auth_required"
    if status == 403 or code == "http_403":
        return "forbidden"
    if status == 429 or code == "http_429":
        return "rate_limited"
    if code == "parse_error":
        return "parse_error"
    return "temporary_failure"


def health_payload(code, *, actual_401=False):
    label, severity, action = LABELS[code]
    if code == "auth_required" and actual_401:
        label = "401 · 授权失效"
    return {"code": code, "label": label, "severity": severity, "action": action,
            "needs_auth": code in {"auth_required", "unauthorized"}}


def retry_after(value, now):
    if value is None:
        return None
    try:
        seconds = int(str(value).strip())
        if seconds < 0:
            return None
        return now + timedelta(seconds=seconds)
    except (ValueError, TypeError, OverflowError):
        try:
            stamp = as_utc(parsedate_to_datetime(str(value)))
            return stamp if stamp and stamp > as_utc(now) else None
        except (ValueError, TypeError, OverflowError):
            return None


def present_context(account, latest=None, success=None, authority=None, schedule=None):
    now = utcnow()
    revision = int(account.credential_revision or 1)
    same = latest is not None and latest.credential_revision == revision
    code = result_state(latest) if same else ("pending" if latest or revision > 1 else "unchecked")
    # A later timeout is not proof that an earlier authoritative rejection recovered.
    if authority is not None and authority.credential_revision == revision:
        if result_state(authority) == "auth_required":
            code = "auth_required"
    if code in {"unchecked", "temporary_failure"} and account.auth_state in {"oauth_required", "refresh_due", "phone_required", "manual_required"}:
        code = "auth_required"
    if account.access_token_encrypted and not getattr(account, "refresh_token_encrypted", None):
        code = "auth_required"
    if account.auth_state in {"phone_required", "manual_required"}:
        code = "auth_required"
    if not account.access_token_encrypted:
        code = "unauthorized"
    if account.auth_state == "deactivated":
        code = "deactivated"
    if account.operational_state in {"disabled", "archived"}:
        code = "disabled"
    quota = None
    if success is not None:
        stale = (success.credential_revision != revision or not same or not latest.success
                 or as_utc(success.queried_at) < now - timedelta(minutes=75))
        quota = {key: getattr(success, key) for key in ("five_hour_used_percent", "seven_day_used_percent", "source", "credential_revision")}
        quota.update({key: isoformat(getattr(success, key)) for key in ("five_hour_reset_at", "seven_day_reset_at", "queried_at")})
        quota.update(success=True, stale=stale)
    next_at = isoformat(schedule.next_check_at) if schedule else None
    check = None
    if latest is not None:
        check = {"check_id": latest.check_id, "state": result_state(latest), "source": latest.error_source or "official_quota",
                 "http_status": latest.http_status, "error_code": latest.error_code,
                 "message": LABELS[result_state(latest)][0], "checked_at": isoformat(latest.queried_at),
                 "credential_revision": latest.credential_revision, "current_credential": same, "next_check_at": next_at}
    actual_401 = (authority is not None and authority.credential_revision == revision and authority.http_status == 401)
    if same and latest.http_status == 401:
        actual_401 = True
    health = health_payload(code, actual_401=actual_401)
    if code == "auth_required" and not getattr(account, "refresh_token_encrypted", None) and not actual_401:
        health["label"] = "待完成 OAuth · 缺少刷新凭据"
    if code == "rate_limited" and same and latest.http_status == 429:
        health["label"] = "429 · 等待重试"
    return {"credential_revision": revision, "latest_check": check, "last_success_quota": quota,
            "quota": quota or {"five_hour_used_percent": None, "seven_day_used_percent": None,
                               "five_hour_reset_at": None, "seven_day_reset_at": None,
                               "queried_at": None, "success": None, "source": None}, "health": health, "next_check_at": next_at,
            "queued": bool(schedule and schedule.operation_id), "needs_auth": health["needs_auth"],
            "auth_action": health["action"] if health["needs_auth"] else None}
