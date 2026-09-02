"""Workspace official-name extraction. Match by official_workspace_id only."""

from __future__ import annotations

from typing import Any

from app.domain.identity.ids import workspace_official_id
from app.domain.workspaces.names import is_placeholder_or_email_name, looks_like_email

TITLE_KEYS = (
    "title",
    "name",
    "workspace_name",
    "organization_name",
    "team_name",
    "display_name",
    "account_name",
)
ID_KEYS = (
    "id",
    "account_id",
    "chatgpt_account_id",
    "workspace_id",
    "organization_id",
    "official_workspace_id",
)
NESTED_OBJECT_KEYS = (
    "organization",
    "account",
    "workspace",
    "team",
    "current_account",
    "default_account",
    "profile",
    "structure",
)
EMAIL_KEYS = {"email", "username"}
REDACT_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "authorization",
    "cookie",
    "cookies",
    "password",
    "proxy",
}


def normalize_workspace_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ID_KEYS:
            found = workspace_official_id(str(value.get(key) or "") or None)
            if found:
                return found
        return None
    return workspace_official_id(str(value or "") or None)


def _clean_title(value: Any, *, owner_email: str | None = None) -> str | None:
    text = str(value or "").strip() or None
    if not text:
        return None
    if looks_like_email(text) or is_placeholder_or_email_name(text, owner_email=owner_email):
        return None
    return text


def title_from_mapping(payload: dict[str, Any], *, owner_email: str | None = None) -> str | None:
    for key in TITLE_KEYS:
        found = _clean_title(payload.get(key), owner_email=owner_email)
        if found:
            return found
    return None


def _title_from_matched_node(node: dict[str, Any], *, owner_email: str | None = None, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    title = title_from_mapping(node, owner_email=owner_email)
    if title:
        return title
    node_id = normalize_workspace_id(node)
    for nested_key in NESTED_OBJECT_KEYS:
        nested = node.get(nested_key)
        if not isinstance(nested, dict):
            continue
        nested_id = normalize_workspace_id(nested)
        if nested_id and node_id and nested_id != node_id:
            continue
        found = _title_from_matched_node(nested, owner_email=owner_email, depth=depth + 1)
        if found:
            return found
    return None


def _walk(node: Any, *, depth: int = 0):
    if depth > 8 or node is None:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value, depth=depth + 1)
    elif isinstance(node, list):
        for item in node[:80]:
            yield from _walk(item, depth=depth + 1)


def extract_title_for_workspace(
    payload: Any,
    official_workspace_id: str | None,
    *,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Return a title only when the payload can be tied to the target Workspace."""
    target = normalize_workspace_id(official_workspace_id)
    if not target:
        return {"title": None, "matched": False, "reason": "missing_workspace_id"}
    if payload is None:
        return {"title": None, "matched": False, "reason": "empty_payload"}

    if isinstance(payload, dict):
        keyed = payload.get("accounts") if isinstance(payload.get("accounts"), dict) else None
        if keyed:
            for key, value in keyed.items():
                if normalize_workspace_id(key) == target:
                    title = title_from_mapping(value, owner_email=owner_email) if isinstance(value, dict) else None
                    if title is None and isinstance(value, dict):
                        nested = value.get("account") if isinstance(value.get("account"), dict) else value
                        title = title_from_mapping(nested, owner_email=owner_email) if isinstance(nested, dict) else None
                    if title:
                        return {"title": title, "matched": True, "reason": "accounts_map"}
        self_id = normalize_workspace_id(payload)
        if self_id == target:
            title = title_from_mapping(payload, owner_email=owner_email)
            if title:
                return {"title": title, "matched": True, "reason": "self"}

    matched_nodes = 0
    for node in _walk(payload):
        node_id = normalize_workspace_id(node)
        if node_id != target:
            continue
        matched_nodes += 1
        title = _title_from_matched_node(node, owner_email=owner_email)
        if title:
            return {"title": title, "matched": True, "reason": "id_match"}
    if matched_nodes:
        return {"title": None, "matched": True, "reason": "id_match_without_title"}
    return {"title": None, "matched": False, "reason": "no_id_match"}


def resolve_official_title(
    payloads: list[tuple[str, Any]],
    official_workspace_id: str | None,
    *,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Try sources in order. Never pick the first array item just because it has a name."""
    attempts: list[dict[str, Any]] = []
    for source, payload in payloads:
        extracted = extract_title_for_workspace(
            payload,
            official_workspace_id,
            owner_email=owner_email,
        )
        attempts.append({"source": source, **extracted})
        if extracted.get("title"):
            return {
                "title": extracted["title"],
                "payload_source": source,
                "matched": True,
                "attempts": attempts,
                "error": None,
            }
    sources = ", ".join(source for source, _ in payloads) or "(none)"
    return {
        "title": None,
        "payload_source": None,
        "matched": any(item.get("matched") for item in attempts),
        "attempts": attempts,
        "error": f"未从这些来源取到名称：{sources}",
    }


def redact_payload(payload: Any, *, depth: int = 0) -> Any:
    """Keep field names/types for fixtures. Never keep tokens, emails, or secrets."""
    if depth > 8:
        return {"_truncated": True}
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in list(payload.items())[:40]:
            lowered = str(key).lower()
            scalar = None if isinstance(value, (dict, list)) else str(value) if value is not None else None
            if lowered in EMAIL_KEYS or looks_like_email(scalar):
                redacted[key] = "<redacted_email>"
            elif lowered in REDACT_KEYS or "token" in lowered or "secret" in lowered or "password" in lowered:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_payload(value, depth=depth + 1)
        return redacted
    if isinstance(payload, list):
        return [redact_payload(item, depth=depth + 1) for item in payload[:20]]
    if isinstance(payload, str) and looks_like_email(payload):
        return "<redacted_email>"
    if isinstance(payload, (str, int, float, bool)) or payload is None:
        return payload
    return type(payload).__name__
