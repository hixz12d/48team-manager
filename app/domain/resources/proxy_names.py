"""Proxy auto-naming. Never expose internal DB ids like mother-2."""

from __future__ import annotations

import re
from typing import Any

AUTO_MOTHER_RE = re.compile(r"^mother-\d+$", re.I)
AUTO_CHILD_RE = re.compile(r"^child-\d+$", re.I)
AUTO_PROXY_RE = re.compile(r"^proxy-\d+$", re.I)

NAME_SOURCE_AUTO = "auto"
NAME_SOURCE_USER = "user"


def host_port_label(host: str | None, port: int | None) -> str:
    host_text = str(host or "").strip() or "proxy"
    try:
        port_text = str(int(port)) if port is not None else ""
    except (TypeError, ValueError):
        port_text = str(port or "")
    return f"{host_text}:{port_text}" if port_text else host_text


def is_legacy_auto_name(name: str | None) -> bool:
    text = str(name or "").strip()
    if not text:
        return True
    return bool(AUTO_MOTHER_RE.match(text) or AUTO_CHILD_RE.match(text) or AUTO_PROXY_RE.match(text))


def name_for_bindings(
    *,
    host: str | None,
    port: int | None,
    bindings: list[dict[str, Any]] | list[Any],
) -> str:
    rows: list[dict[str, Any]] = []
    for item in bindings or []:
        if isinstance(item, dict):
            rows.append(item)
            continue
        rows.append(
            {
                "email": getattr(item, "email", None),
                "purpose": getattr(item, "local_purpose", None) or getattr(item, "purpose", None),
            }
        )
    if not rows:
        return host_port_label(host, port)
    if len(rows) == 1:
        email = str(rows[0].get("email") or "").strip() or "unknown"
        purpose = str(rows[0].get("purpose") or "").strip().lower()
        if purpose == "mother":
            return f"母号 · {email}"
        if purpose in {"child", "standby"}:
            return f"子号 · {email}"
        return f"账号 · {email}"
    return f"共享代理 · {host_port_label(host, port)}"


def default_name_for_account(*, purpose: str | None, email: str | None, host: str | None = None, port: int | None = None) -> str:
    email_text = str(email or "").strip()
    purpose_text = str(purpose or "").strip().lower()
    if purpose_text == "mother" and email_text:
        return f"母号 · {email_text}"
    if purpose_text in {"child", "standby"} and email_text:
        return f"子号 · {email_text}"
    if email_text:
        return f"账号 · {email_text}"
    return host_port_label(host, port)
