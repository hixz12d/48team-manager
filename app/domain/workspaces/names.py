"""Workspace display-name helpers. Owner email is never a valid fallback name."""

from __future__ import annotations

import re
from typing import Any

from app.domain.identity.ids import normalize_email

NAME_SOURCE_CUSTOM = "custom"
NAME_SOURCE_OFFICIAL = "official"
NAME_SOURCE_PLACEHOLDER = "placeholder"
NAME_SOURCE_LEGACY = "legacy"

_AUTO_ID_RE = re.compile(r"^workspace[_\-\s]*#?\d+$", re.I)


def short_workspace_token(workspace) -> str:
    official = str(getattr(workspace, "official_workspace_id", None) or "").strip()
    if official:
        compact = official.replace("-", "")
        return compact[:8] if len(compact) >= 8 else compact
    raw_id = getattr(workspace, "id", None)
    if raw_id is not None:
        return f"{int(raw_id):04d}"
    return "unknown"


def placeholder_name(workspace) -> str:
    return f"未命名工作区 · {short_workspace_token(workspace)}"


def looks_like_email(value: str | None) -> bool:
    text = str(value or "").strip()
    if "@" not in text:
        return False
    local, _, domain = text.partition("@")
    return bool(local and domain and "." in domain)


def is_placeholder_or_email_name(value: str | None, *, owner_email: str | None = None) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if looks_like_email(text):
        return True
    owner = normalize_email(owner_email or "")
    if owner and normalize_email(text) == owner:
        return True
    if text.startswith("未命名工作区"):
        return True
    if _AUTO_ID_RE.match(text):
        return True
    if text.lower().startswith("workspace #") or text.lower().startswith("workspace#"):
        return True
    return False


def name_diagnostics(workspace) -> dict[str, Any]:
    return {
        "official_name": str(getattr(workspace, "official_name", None) or "").strip() or None,
        "custom_name": str(getattr(workspace, "custom_name", None) or "").strip() or None,
        "name_source": str(getattr(workspace, "name_source", None) or "").strip() or None,
        "official_name_synced_at": getattr(workspace, "official_name_synced_at", None),
        "official_name_last_error": str(getattr(workspace, "official_name_last_error", None) or "").strip() or None,
        "official_name_payload_source": str(getattr(workspace, "official_name_payload_source", None) or "").strip() or None,
    }


def resolve_display_name(workspace, *, owner_email: str | None = None) -> dict[str, Any]:
    diagnostics = name_diagnostics(workspace)
    custom = diagnostics["custom_name"]
    official = diagnostics["official_name"]
    legacy = str(getattr(workspace, "name", None) or "").strip() or None
    source = diagnostics["name_source"]

    if custom:
        return {
            "name": custom,
            "display_name": custom,
            "name_source": NAME_SOURCE_CUSTOM,
            **diagnostics,
            "custom_name": custom,
        }
    if official and not is_placeholder_or_email_name(official, owner_email=owner_email):
        return {
            "name": official,
            "display_name": official,
            "name_source": NAME_SOURCE_OFFICIAL,
            **diagnostics,
            "custom_name": None,
        }
    if legacy and not is_placeholder_or_email_name(legacy, owner_email=owner_email):
        return {
            "name": legacy,
            "display_name": legacy,
            "name_source": source or NAME_SOURCE_LEGACY,
            **diagnostics,
            "custom_name": None,
        }
    placeholder = placeholder_name(workspace)
    return {
        "name": placeholder,
        "display_name": placeholder,
        "name_source": NAME_SOURCE_PLACEHOLDER,
        **diagnostics,
        "custom_name": None,
    }


def apply_official_name(
    workspace,
    official_name: str | None,
    *,
    owner_email: str | None = None,
    synced_at=None,
    payload_source: str | None = None,
    last_error: str | None = None,
) -> bool:
    cleaned = str(official_name or "").strip() or None
    if cleaned and is_placeholder_or_email_name(cleaned, owner_email=owner_email):
        cleaned = None
    changed = False
    if cleaned and cleaned != (getattr(workspace, "official_name", None) or None):
        workspace.official_name = cleaned
        changed = True
    if cleaned and synced_at is not None:
        workspace.official_name_synced_at = synced_at
        changed = True
    if payload_source is not None and hasattr(workspace, "official_name_payload_source"):
        workspace.official_name_payload_source = payload_source or None
        changed = True
    if hasattr(workspace, "official_name_last_error"):
        workspace.official_name_last_error = (str(last_error).strip()[:500] or None) if last_error else None
    custom = str(getattr(workspace, "custom_name", None) or "").strip()
    if custom:
        workspace.name_source = NAME_SOURCE_CUSTOM
        workspace.name = custom
        return changed
    if cleaned:
        workspace.name_source = NAME_SOURCE_OFFICIAL
        if workspace.name != cleaned:
            workspace.name = cleaned
            changed = True
        return changed
    # Keep existing non-email name; otherwise use placeholder.
    current = str(getattr(workspace, "name", None) or "").strip()
    if is_placeholder_or_email_name(current, owner_email=owner_email):
        placeholder = placeholder_name(workspace)
        if workspace.name != placeholder:
            workspace.name = placeholder
            changed = True
        workspace.name_source = NAME_SOURCE_PLACEHOLDER
    elif not getattr(workspace, "name_source", None):
        workspace.name_source = NAME_SOURCE_LEGACY
    return changed


def apply_custom_name(workspace, custom_name: str | None) -> dict[str, Any]:
    cleaned = str(custom_name or "").strip() or None
    if cleaned and looks_like_email(cleaned):
        raise ValueError("显示名称不能是邮箱")
    workspace.custom_name = cleaned
    if cleaned:
        workspace.name = cleaned
        workspace.name_source = NAME_SOURCE_CUSTOM
    else:
        # Fall back through official / placeholder.
        apply_official_name(workspace, getattr(workspace, "official_name", None))
    return resolve_display_name(workspace)


def extract_official_title(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("title", "name", "workspace_name", "organization_name", "team_name"):
        value = payload.get(key)
        text = str(value or "").strip()
        if text and not looks_like_email(text):
            return text
    for nested_key in ("organization", "account", "workspace", "team"):
        nested = payload.get(nested_key)
        found = extract_official_title(nested) if isinstance(nested, dict) else None
        if found:
            return found
    return None
