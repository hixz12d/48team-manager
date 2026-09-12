"""Local calendar reminders. Never change official subscription or account state."""
from __future__ import annotations

from datetime import date

from app.core.time import isoformat, utcnow
from app.persistence.models.identity import Workspace

# A calendar date, not a token timestamp. Keep countdowns consistent across clients.
EXPIRY_TIMEZONE = "Asia/Shanghai"


def expiry_record(workspace: Workspace) -> dict:
    value = workspace.manual_expires_on
    return {
        "date": value.isoformat() if value else None,
        "source": "manual" if value else None,
        "updated_at": isoformat(workspace.manual_expiry_updated_at),
        "timezone": EXPIRY_TIMEZONE,
    }


async def update_workspace_expiry(db, workspace_id: int, expires_on: date | None) -> dict:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return {"ok": False, "error_code": "not_found", "error": "团队不存在"}
    workspace.manual_expires_on = expires_on
    workspace.manual_expiry_updated_at = utcnow()
    await db.commit()
    return {"ok": True, "workspace_id": workspace.id, "expiry": expiry_record(workspace)}
