"""Manual per-team counters, scoped to the current Beijing calendar day."""
from __future__ import annotations

from sqlalchemy import case, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utcnow, zone
from app.persistence.models.identity import Workspace

SWITCH_TIMEZONE = "Asia/Shanghai"


def switch_count_record(workspace: Workspace) -> dict:
    today = utcnow().astimezone(zone(SWITCH_TIMEZONE)).date()
    return {
        "date": today.isoformat(),
        "count": (workspace.manual_switch_count or 0) if workspace.manual_switch_date == today else 0,
        "timezone": SWITCH_TIMEZONE,
    }


async def increment_workspace_switch_count(db: AsyncSession, workspace_id: int) -> dict:
    today = utcnow().astimezone(zone(SWITCH_TIMEZONE)).date()
    # One statement handles both rollover and increment, including concurrent clients.
    result = await db.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(
            manual_switch_date=today,
            manual_switch_count=case(
                (Workspace.manual_switch_date == today, Workspace.manual_switch_count + 1),
                else_=1,
            ),
        )
        .returning(Workspace.manual_switch_count)
        .execution_options(synchronize_session=False)
    )
    count = result.scalar_one_or_none()
    if count is None:
        return {"ok": False, "error_code": "not_found", "error": "团队不存在"}
    await db.commit()
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "switch_count": {"date": today.isoformat(), "count": count, "timezone": SWITCH_TIMEZONE},
    }
