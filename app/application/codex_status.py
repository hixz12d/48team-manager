"""Read-only codex-rs health snapshots. Import state is not runtime health."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.application.settings import get_setting_value, upsert_setting
from app.core.time import as_utc, utcnow
from app.integrations.codex.client import CodexClient, load_config
from app.persistence.models.codex import CodexBinding
from app.persistence.models.identity import Account

STATE_KEY = "codex_rs_health"
CONFIG_KEY = "codex_rs_auto_reauth"
AUTH_REASONS = {"credential_expired", "credential_invalid"}
STALE_SECONDS = 180
_LOCK = asyncio.Lock()
LABELS = {
    "normal": ("正常", "success"), "quota_exhausted": ("额度用尽", "warning"),
    "rate_limited": ("限流", "warning"), "disabled": ("已停用", "muted"),
    "error": ("错误", "error"), "missing": ("远端不存在", "warning"),
    "identity_mismatch": ("绑定身份不一致", "error"), "unknown": ("待核对", "muted"),
}
REASONS = {
    "credential_expired": "授权过期", "credential_invalid": "授权失效",
    "access_token_expired": "访问令牌过期，等待 rs 续期",
    "account_banned": "账号被封禁", "account_unverified": "账号未验证",
}


async def reauth_enabled(db) -> bool:
    return await get_setting_value(db, CONFIG_KEY, "false") == "true"


async def load_snapshot(db) -> dict:
    try:
        data = json.loads(await get_setting_value(db, STATE_KEY, "{}") or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def identity_matches(remote, account, binding) -> bool:
    return bool(
        remote.get("id") == binding.remote_account_id
        and str(remote.get("email") or "").strip().lower() == account.email.strip().lower()
        and binding.official_workspace_id
        and remote.get("accountId") == binding.official_workspace_id
        and account.official_user_id
        and remote.get("userId") == account.official_user_id
    )


def auth_failed(remote) -> bool:
    # Neither an AT expiry, a generic error nor a failed admin request proves RT loss.
    return (remote.get("enabled") is True and remote.get("status") == "error"
            and remote.get("errorReason") in AUTH_REASONS)


def present(snapshot, binding) -> dict:
    row = (snapshot.get("accounts") or {}).get(str(binding.account_id), {})
    matched = (snapshot.get("target_url") == binding.target_url
               and row.get("remote_id") == binding.remote_account_id)
    row = row if matched else {}
    checked = row.get("checked_at")
    try:
        stale = not checked or utcnow() - as_utc(datetime.fromisoformat(checked)) > timedelta(seconds=STALE_SECONDS)
    except (ValueError, TypeError):
        stale = True
    state = row.get("status", "unknown")
    label, severity = LABELS.get(state, LABELS["unknown"])
    reason = row.get("error_reason")
    if state == "error":
        label = REASONS.get(reason, label)
    read_failed = snapshot.get("ok") is False and snapshot.get("target_url") == binding.target_url
    stale = stale or read_failed
    return {
        **row, "state": state, "label": "读取失败" if read_failed else label,
        "last_known_label": label if read_failed and row else None,
        "severity": "warning" if stale else severity, "stale": stale,
        "checked_at": checked, "read_error": snapshot.get("error") if read_failed else None,
        "needs_auth": not stale and state == "error" and reason in AUTH_REASONS,
        "can_reauth": not stale and state == "error" and reason in AUTH_REASONS and row.get("enabled") is True,
    }


async def refresh(db, *, force=False) -> dict:
    async with _LOCK:
        snapshot = await load_snapshot(db)
        try:
            cfg = await load_config(db)
            if not cfg["base_url"] or not cfg["api_key"]:
                return {"ok": False, "configured": False}
            if not force and snapshot.get("target_url") == cfg["base_url"]:
                try:
                    if utcnow() - as_utc(datetime.fromisoformat(snapshot["attempted_at"])) < timedelta(seconds=45):
                        return snapshot
                except (KeyError, ValueError, TypeError):
                    pass
            target = cfg["base_url"]
            if snapshot.get("target_url") != target:
                snapshot = {"target_url": target, "accounts": {}}
            await db.commit()
            items = await CodexClient(**cfg).list_accounts()
            remote_by_id = {item["id"]: item for item in items if isinstance(item.get("id"), str)}
            rows = (await db.execute(select(CodexBinding, Account).join(Account, Account.id == CodexBinding.account_id)
                                    .where(CodexBinding.target_url == target))).all()
            stamp = utcnow().isoformat()
            accounts = {}
            for binding, account in rows:
                remote = remote_by_id.get(binding.remote_account_id)
                record = {"remote_id": binding.remote_account_id, "checked_at": stamp}
                if remote is None:
                    record["status"] = "missing" if binding.remote_account_id else "unknown"
                elif not identity_matches(remote, account, binding):
                    record["status"] = "identity_mismatch"
                else:
                    # Do not persist arbitrary upstream messages: they may include sensitive request data.
                    record.update(status=remote.get("status", "unknown"), enabled=remote.get("enabled"),
                                  error_reason=remote.get("errorReason"), updated_at=remote.get("updatedAt"))
                accounts[str(account.id)] = record
            snapshot = {"target_url": target, "accounts": accounts, "ok": True, "error": None,
                        "attempted_at": stamp, "checked_at": stamp}
        except Exception:
            await db.rollback()
            snapshot.update(ok=False, error="无法读取 codex-rs，保留上次状态，不触发重授权", attempted_at=utcnow().isoformat())
        await upsert_setting(db, STATE_KEY, json.dumps(snapshot, ensure_ascii=False), "codex-rs 运行状态快照")
        await db.commit()
        return snapshot
