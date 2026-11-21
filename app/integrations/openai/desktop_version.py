"""codex_app_version for the Codex Desktop OAuth entry, kept in step with codex-rs.

codex-rs tracks the official Codex Desktop release and reports it in
/api/admin/dashboard/summary (wireProfiles[provider=openai].release.latestVersion).
A scheduler job refreshes the in-memory value; CODEX_DESKTOP_APP_VERSION is only the
fallback before the first successful read. Failures keep the last known value.
"""
from __future__ import annotations

import logging
import re

from app.core.config import load_settings

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"\d{1,6}(?:\.\d{1,9}){1,3}")
_cached = ""


def current() -> str:
    """Last version read from codex-rs, else the configured fallback."""
    return _cached or str(load_settings().codex_desktop_app_version or "")


def _extract(summary: object) -> str:
    profiles = summary.get("wireProfiles") if isinstance(summary, dict) else None
    for profile in profiles if isinstance(profiles, list) else []:
        if not isinstance(profile, dict) or profile.get("provider") != "openai":
            continue
        release = profile.get("release")
        version = release.get("latestVersion") if isinstance(release, dict) else None
        if isinstance(version, str) and _VERSION_RE.fullmatch(version):
            return version
    return ""


async def refresh(db) -> str:
    """Read the current desktop version from codex-rs; returns "" when unavailable."""
    global _cached
    from app.integrations.codex.client import CodexClient, load_config

    try:
        config = await load_config(db)
        if not config.get("base_url") or not config.get("api_key"):
            return ""
        summary = await CodexClient(config["base_url"], config["api_key"]).request(
            "GET", "/api/admin/dashboard/summary"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("codex desktop version refresh failed: %s", type(exc).__name__)
        return ""
    version = _extract(summary)
    if not version:
        logger.warning("codex-rs dashboard did not report a desktop version")
        return ""
    if version != _cached:
        logger.info("codex desktop app version -> %s", version)
    _cached = version
    return version
