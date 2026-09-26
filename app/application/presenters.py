"""Unify operation outcome → toast tone / Chinese labels."""

from __future__ import annotations

from typing import Any

STATUS_LABELS = {
    "queued": "排队",
    "running": "进行中",
    "waiting": "等待",
    "success": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "manual_required": "需人工",
    "partial": "部分完成",
    "pending": "待确认",
    "verified": "已验证",
    "missing": "缺失",
    "unbound": "未绑定",
    "conflict": "冲突",
    "remote_missing": "远端未找到",
    "remote_unreachable": "远端不可达",
    "not_eligible": "不适用",
    "snapshot_updated": "快照已更新",
    "schema_mismatch": "官方数量不一致",
    "verification_failed": "核对失败",
    "oauth_required": "要授权",
    "needs_auth": "要授权",
    "unknown": "未授权",
    "owner_account_missing": "母号本地档案缺失",
    "identity_conflict": "账号对不上",
    "membership_drift": "本地和官方对不上",
    "needs_management": "有人还没接入",
    "vacancy": "有空位",
    "billing": "账单异常",
    "probe_failed": "检测失败",
}

MEMBERSHIP_STATUS_LABELS = {
    "owner": "母号",
    "managed": "已接入",
    "remote_only": "官方已加入 · 未接入",
    "local_only": "本地有记录 · 官方未找到",
    "invited": "已邀请 · 等待加入",
    "conflict": "账号对不上 · 需人工核对",
}

OPERATION_TYPE_LABELS = {
    "quota_probe": "额度刷新",
    "auth_probe": "检查授权",
    "reauth": "授权",
    "onboard": "拉人",
    "replenish": "补充 Team",
    "rotate": "轮转",
    "kick_member": "踢出成员",
    "purge_child": "永久删除子号",
    "invite_child": "邀请子号",
    "revoke_invite": "撤回邀请",
    "hme_reconcile": "HME 对账",
    "hme_label_retry": "HME 标签重试",
    "workspace_sync": "同步官方成员",
    "sub2api_sync": "Sub2API 对账",
    "sub2api_reconcile": "Sub2API 对账",
    "sub2api_push": "Sub2API 推送",
    "sub2api_usage_sync": "Sub2API 用量同步",
    "sub2api_proxy_sync": "Sub2API 代理同步",
    "proxy_check": "代理检测",
    "free_register": "空闲号注册",
    "reregister": "重注册",
}

BUSINESS_STEP_LABELS = {
    "preflight": "核对补位资源和原席位",
    "confirm_trigger": "确认轮转条件",
    "paused": "已暂停旧账号调度",
    "drained": "等待旧请求结束",
    "official_removed": "已确认旧账号离组",
    "kicked": "旧账号已移出",
    "refill": "完成补位与同步",
    "queued": "排队",
    "done": "结束",
    "success": "结束",
    "failed": "失败",
    "partial": "部分完成",
    "manual_required": "需人工",
    "cancelled": "已取消",
    "fetch_members": "读取官方成员",
    "fetch_invites": "读取官方邀请",
    "commit_snapshot": "提交官方快照",
    "sub2api_sync": "对账 Sub2API",
    "sub2api_reconcile": "对账 Sub2API",
    "sub2api_push": "推送到 Sub2API",
    "sub2api_usage_sync": "同步用量与计费",
    "sub2api_proxy_sync": "同步代理",
    "read_after_write": "复读验证",
    "reconcile": "对账",
    "auth": "刷新授权",
    "quota": "刷新额度",
    "probe": "检测",
    "hme": "领取邮箱",
    "checking": "检查成员与席位",
    "inviting": "发送官方邀请",
    "invited": "邀请已确认",
    "invite_mail": "等待邀请邮件",
    "browser": "注册与入组",
    "signup_runner": "新版注册流程",
    "browser_open": "打开邀请或授权页面",
    "email_otp": "等待邮箱验证码",
    "about_you": "填写注册资料",
    "workspace": "选择工作空间",
    "add_phone": "手机验证",
    "sms_otp": "等待短信验证码",
    "oauth": "OAuth 授权",
    "reconciling": "确认官方入组",
    "active": "已完成入组",
    "mailbox": "绑定邮箱",
    "authorizing": "自动授权",
    "authorized": "已授权",
    "auth_failed": "授权失败",
    "blocked": "已拦截",
    "hme_failed": "领取邮箱失败",
}


# Presentation only: these are workflow stages, not a percentage or proof of completion.
_BROWSER_STAGES = ("browser", "signup_runner", "browser_open", "email_otp", "about_you", "workspace", "add_phone", "sms_otp", "oauth", "browser_failed")
_ONBOARD_PLAN = (
    ("checking", ("checking", "blocked")),
    ("hme", ("hme", "hme_failed")),
    ("inviting", ("inviting", "invited", "invite_mail", "skip_invite", "invite_failed", "invite_role_mismatch")),
    ("browser", _BROWSER_STAGES),
    ("reconciling", ("reconciling", "not_joined")),
    ("authorizing", ("authorizing", "authorized", "auth_failed")),
    ("active", ("active",)),
)
OPERATION_STAGE_PLANS = {
    "onboard": _ONBOARD_PLAN,
    "replenish": _ONBOARD_PLAN,
    "rotate": (
        ("preflight", ("preflight",)), ("confirm_trigger", ("confirm_trigger",)),
        ("paused", ("paused",)), ("drained", ("drained",)),
        ("official_removed", ("official_removed", "kicked")),
        ("refill", tuple(code for _, codes in _ONBOARD_PLAN for code in codes) + ("refill",)),
    ),
    "kick_member": (("official_removed", ("official_removed",)), ("paused", ("paused", "drained")), ("kicked", ("kicked",))),
}


def operation_stage_plan(kind: str) -> list[dict[str, Any]]:
    return [{"code": code, "label": BUSINESS_STEP_LABELS.get(code, code), "stages": list(stages)}
            for code, stages in OPERATION_STAGE_PLANS.get(kind, ())]


def account_attention(item: dict[str, Any]) -> list[dict[str, str]]:
    """Shared local read-model evidence; categories may overlap on one account."""
    reasons = {}
    for context in item.get("contexts") or [item]:
        health = context.get("health") or {}
        code = health.get("code")
        if health.get("needs_auth") or context.get("needs_auth"):
            reasons["auth"] = "需要授权"
        quota = context.get("last_success_quota") or context.get("quota") or {}
        if code == "quota_exhausted" or any(isinstance(quota.get(key), (int, float)) and quota[key] >= 100
                                           for key in ("five_hour_used_percent", "seven_day_used_percent")):
            reasons["quota"] = "额度用尽"
        if code not in {None, "healthy", "disabled", "quota_exhausted"} and not health.get("needs_auth"):
            reasons["check"] = health.get("label") or "检测需处理"
        remote = context.get("remote_status") or {}
        if any(state.get("state") in {"missing", "auth_error", "error", "forbidden", "phone_required", "identity_mismatch", "identity_unconfirmed", "binding_review"}
               for state in remote.get("bindings") or [remote]):
            reasons["remote"] = "远端状态需核对"
        if context.get("state") == "conflict":
            reasons["identity"] = "身份冲突"
    return [{"code": code, "label": label} for code, label in reasons.items()]


AUTH_NEED_STATES = frozenset({
    "refresh_due",
    "oauth_required",
    "phone_required",
    "manual_required",
    "deactivated",
    "unknown",
})


def _account_value(account: Any, key: str, default: Any = None) -> Any:
    if isinstance(account, dict):
        return account.get(key, default)
    return getattr(account, key, default)


def build_auth_status(account: Any, *, missing_reason: str = "account_not_found") -> dict[str, Any]:
    """Present one canonical authorization state for all console read models."""
    if account is None:
        return {
            "auth_state": missing_reason,
            "needs_auth": False,
            "auth_action": None,
            "auth_reason": missing_reason,
        }

    auth_state = str(
        _account_value(account, "auth_state")
        or _account_value(account, "auth")
        or "unknown"
    ).strip().lower()
    explicit_token = _account_value(account, "has_access_token")
    has_access_token = (
        bool(explicit_token)
        if explicit_token is not None
        else bool(_account_value(account, "access_token_encrypted"))
    )
    if auth_state == "deactivated":
        return {
            "auth_state": auth_state,
            "needs_auth": True,
            "auth_action": None,
            "auth_reason": "deactivated",
        }

    if not has_access_token:
        return {
            "auth_state": auth_state,
            "needs_auth": True,
            "auth_action": "authorize" if auth_state in {"unknown", "oauth_required"} else "reauthorize",
            "auth_reason": "missing_token",
        }
    if auth_state not in AUTH_NEED_STATES:
        return {
            "auth_state": auth_state,
            "needs_auth": False,
            "auth_action": None,
            "auth_reason": None,
        }
    return {
        "auth_state": auth_state,
        "needs_auth": True,
        "auth_action": None if auth_state == "deactivated" else "reauthorize",
        "auth_reason": auth_state,
    }


def label_of(mapping: dict[str, str], value: Any, *, fallback: str | None = None) -> str:
    key = str(value or "").strip()
    if not key:
        return fallback or "—"
    return mapping.get(key, fallback if fallback is not None else key)


def tone_for_result(result: dict[str, Any] | None) -> str:
    payload = result or {}
    status = str(payload.get("status") or payload.get("state") or "").strip().lower()
    outcome = str(payload.get("outcome") or "").strip().lower()
    if status in {"partial", "manual_required"} or payload.get("partial"):
        return "warning"
    if outcome in {"remote_missing", "not_eligible", "schema_mismatch", "verification_failed", "conflict"}:
        return "warning"
    if payload.get("ok") or payload.get("success") or status == "success":
        if outcome in {"remote_missing", "warning"}:
            return "warning"
        return "success"
    return "error"


def present_action_result(result: dict[str, Any] | None, *, default_success: str = "已完成", default_error: str = "操作失败") -> dict[str, Any]:
    payload = dict(result or {})
    message = str(payload.get("message") or "").strip()
    if not message:
        if payload.get("ok") or payload.get("success"):
            message = default_success
        else:
            message = str(payload.get("error") or default_error)
    tone = tone_for_result(payload)
    return {
        **payload,
        "message": message,
        "tone": tone,
        "ui_status": label_of(STATUS_LABELS, payload.get("status") or payload.get("state")),
        "ui_outcome": label_of(STATUS_LABELS, payload.get("outcome"), fallback=str(payload.get("outcome") or "") or None),
    }


def membership_status_label(status: str | None) -> str:
    return label_of(MEMBERSHIP_STATUS_LABELS, status)


def operation_type_label(op_type: str | None) -> str:
    return label_of(OPERATION_TYPE_LABELS, op_type)


def business_step_label(step: str | None, *, state: str | None = None) -> str:
    key = str(step or "").strip()
    if not key or key in {"success", "done"}:
        if state in {"success"}:
            return "已完成"
        if state:
            return label_of(STATUS_LABELS, state)
        return "—"
    return label_of(BUSINESS_STEP_LABELS, key)
