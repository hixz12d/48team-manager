"""Local purchase ledger for a team: payment card, cost, note, owner 2FA.

One purchase per team (teams are replaced, not renewed). The 2FA secret is
write-only: payloads only expose ``totp_state``; plaintext leaves through
``reveal_owner_totp`` alone and is never logged.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.application.tokens import decrypt_secret, encrypt_secret
from app.core.time import isoformat, utcnow
from app.persistence.models.identity import Workspace

PURCHASE_CURRENCIES = ("USD", "CNY")
DEFAULT_PURCHASE_CURRENCY = "USD"
PURCHASE_FIELDS = ("payment_card", "cost", "currency", "note", "totp_secret")
TOTP_PATTERN = re.compile(r"[A-Z2-7]{16,64}", flags=re.ASCII)
CENT = Decimal("0.01")
MAX_COST = Decimal("9999999999.99")


def _optional_text(value, *, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文字")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_length:
        raise ValueError(f"{label}最长 {max_length} 个字")
    return text


def normalize_payment_card(value) -> str | None:
    return _optional_text(value, label="支付卡", max_length=100)


def normalize_note(value) -> str | None:
    return _optional_text(value, label="备注", max_length=2000)


def normalize_cost(value) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("费用必须是数字")
    try:
        cost = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("费用必须是数字，例如 125 或 125.50") from exc
    if not cost.is_finite() or cost < 0:
        raise ValueError("费用不能小于 0")
    if cost != cost.quantize(CENT):
        raise ValueError("费用最多保留 2 位小数")
    if cost > MAX_COST:
        raise ValueError("费用太大，请检查")
    return cost.quantize(CENT)


def normalize_currency(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("币种只能是 USD 或 CNY")
    text = value.strip().upper()
    if not text:
        return None
    if text not in PURCHASE_CURRENCIES:
        raise ValueError("币种只能是 USD 或 CNY")
    return text


def normalize_totp_secret(value) -> str | None:
    """Empty string or None clears the saved secret."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("2FA 密钥必须是文字")
    text = re.sub(r"\s+", "", value).upper()
    if not text:
        return None
    if not TOTP_PATTERN.fullmatch(text):
        raise ValueError("2FA 密钥格式不对：只能包含字母 A-Z 和数字 2-7，长度 16–64 位")
    return text


NORMALIZERS = {
    "payment_card": normalize_payment_card,
    "cost": normalize_cost,
    "currency": normalize_currency,
    "note": normalize_note,
    "totp_secret": normalize_totp_secret,
}


def _format_cost(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(CENT))


def purchase_record(workspace: Workspace) -> dict:
    return {
        "payment_card": workspace.purchase_payment_card,
        "cost": _format_cost(workspace.purchase_cost),
        "currency": workspace.purchase_currency,
        "note": workspace.purchase_note,
        "totp_state": "stored" if workspace.owner_totp_secret_encrypted else "missing",
        "updated_at": isoformat(workspace.purchase_updated_at),
    }


async def update_workspace_purchase(db, workspace_id: int, fields: dict) -> dict:
    """Apply only the keys present in ``fields``; a key with None clears that field."""
    unknown = set(fields) - set(PURCHASE_FIELDS)
    if unknown:
        return {"ok": False, "error_code": "invalid", "error": f"不支持的字段：{', '.join(sorted(unknown))}"}
    try:
        values = {key: NORMALIZERS[key](value) for key, value in fields.items()}
    except ValueError as exc:
        return {"ok": False, "error_code": "invalid", "error": str(exc)}
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return {"ok": False, "error_code": "not_found", "error": "团队不存在"}
    if "payment_card" in values:
        workspace.purchase_payment_card = values["payment_card"]
    if "cost" in values:
        workspace.purchase_cost = values["cost"]
    if "currency" in values:
        workspace.purchase_currency = values["currency"]
    if "note" in values:
        workspace.purchase_note = values["note"]
    if "totp_secret" in values:
        workspace.owner_totp_secret_encrypted = encrypt_secret(values["totp_secret"])
    if workspace.purchase_cost is not None and not workspace.purchase_currency:
        workspace.purchase_currency = DEFAULT_PURCHASE_CURRENCY
    workspace.purchase_updated_at = utcnow()
    await db.commit()
    return {"ok": True, "workspace_id": workspace.id, "purchase": purchase_record(workspace)}


async def reveal_owner_totp(db, workspace_id: int) -> dict:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return {"ok": False, "error_code": "not_found", "error": "团队不存在"}
    secret = decrypt_secret(workspace.owner_totp_secret_encrypted)
    if not secret:
        return {"ok": False, "error_code": "missing", "error": "未保存 2FA"}
    return {"ok": True, "secret": secret}
