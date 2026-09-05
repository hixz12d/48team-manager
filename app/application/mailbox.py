"""Account mailbox routing and layered HME readiness checks."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.resources.hme import HmeConfig, HmeError, hme_client, load_config
from app.core.time import isoformat, utcnow
from app.domain.identity.ids import normalize_email
from app.persistence.models.identity import Account


def normalize_hme_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(message.get("id") or message.get("message_id") or ""),
        "from": str(message.get("from") or ""),
        "to": str(message.get("to") or ""),
        "subject": str(message.get("subject") or ""),
        "date": str(message.get("date") or message.get("received_at") or ""),
        "preview": str(message.get("preview") or message.get("text") or ""),
    }


def mailbox_readiness_snapshot(account: Account) -> dict[str, Any]:
    provider = str(account.mailbox_provider or "")
    binding_ready = bool(provider == "hme" and account.hme_account_id and account.mailbox_binding_verified_at)
    read_state = str(account.mailbox_read_state or "unknown")
    return {
        "provider": provider or None,
        "binding_state": "verified" if binding_ready else "unbound",
        "read_state": read_state,
        "method": account.mailbox_method,
        "checked_at": isoformat(account.mailbox_checked_at) if account.mailbox_checked_at else None,
        "ready": binding_ready and read_state == "ready",
    }


async def _find_hme_owner(cfg: HmeConfig, alias: str, preferred_id: str = "") -> tuple[str, list[dict[str, Any]]]:
    accounts = await asyncio.to_thread(hme_client.list_accounts, cfg)
    candidates = accounts
    wanted = str(preferred_id or cfg.account_id or "").strip()
    if wanted:
        candidates = [item for item in accounts if str(item.get("id") or "") == wanted]
        if not candidates:
            raise HmeError("HME account missing", "hme_no_account")
    matches: list[tuple[str, list[dict[str, Any]]]] = []
    for item in candidates:
        account_id = str(item.get("id") or "").strip()
        if not account_id:
            continue
        aliases = await asyncio.to_thread(hme_client.list_aliases, cfg, account_id)
        exact = [row for row in aliases if normalize_email(row.get("email") or "") == alias and row.get("active")]
        if exact:
            matches.append((account_id, exact))
    if not matches:
        raise HmeError("目标邮箱不属于已连接的 HME 账号", "mailbox_unbound")
    if len(matches) != 1:
        raise HmeError("目标邮箱在多个 HME 账号中出现，需人工确认", "mailbox_ambiguous")
    return matches[0]


async def probe_account_mailbox(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error_code": "not_found", "error": "account not found"}
    cfg = await load_config(db)
    checked_at = utcnow()
    if not cfg.configured:
        account.mailbox_read_state = "failed"
        account.mailbox_checked_at = checked_at
        await db.commit()
        return {"ok": False, "error_code": "hme_unconfigured", "error": "HME 未配置"}
    try:
        hme_account_id, _aliases = await _find_hme_owner(
            cfg,
            normalize_email(account.email),
            str(account.hme_account_id or ""),
        )
        inbox = await asyncio.to_thread(
            hme_client.list_inbox,
            cfg,
            hme_account_id,
            account.email,
            folder="all",
            limit=20,
            days=1,
        )
    except HmeError as exc:
        account.mailbox_read_state = "failed"
        account.mailbox_checked_at = checked_at
        await db.commit()
        return {"ok": False, "error_code": exc.code, "error": str(exc), "checked_at": isoformat(checked_at)}
    except Exception:
        account.mailbox_read_state = "failed"
        account.mailbox_checked_at = checked_at
        await db.commit()
        return {"ok": False, "error_code": "hme_unavailable", "error": "HME 收件服务不可用", "checked_at": isoformat(checked_at)}

    account.mailbox_provider = "hme"
    account.hme_account_id = hme_account_id
    account.mailbox_binding_verified_at = checked_at
    account.mailbox_read_state = "ready"
    account.mailbox_checked_at = checked_at
    account.mailbox_method = str(inbox.get("method") or "unknown")[:20]
    await db.commit()
    return {
        "ok": True,
        "checked_at": isoformat(checked_at),
        "provider": "hme",
        "account_id": hme_account_id,
        "alias": normalize_email(account.email),
        "binding_state": "verified",
        "read_state": "ready",
        "method": account.mailbox_method,
        "message_count": len(inbox.get("messages") or []),
        "end_to_end_verified": False,
    }


def fetch_hme_mailbox(
    *,
    base_url: str,
    service_token: str,
    account_id: str,
    alias: str,
    limit: int = 20,
    days: int = 1,
) -> dict[str, Any]:
    cfg = HmeConfig(base_url=base_url, service_token=service_token, account_id=account_id)
    inbox = hme_client.list_inbox(cfg, account_id, normalize_email(alias), limit=limit, days=days)
    return {
        "method": inbox.get("method") or "unknown",
        "messages": [normalize_hme_message(item) for item in inbox.get("messages") or []],
    }
