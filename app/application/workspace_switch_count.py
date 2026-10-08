"""Manual per-team counters, scoped to the current Beijing calendar day."""
from __future__ import annotations

from sqlalchemy import case, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_utc, isoformat, utcnow, zone
from app.persistence.models.identity import Workspace

SWITCH_TIMEZONE = "Asia/Shanghai"
# Hint only: teams last switched at least this long ago are preferred; nothing is blocked.
SWITCH_GAP_HOURS = 8


def switch_count_record(workspace: Workspace) -> dict:
    today = utcnow().astimezone(zone(SWITCH_TIMEZONE)).date()
    return {
        "date": today.isoformat(),
        "count": (workspace.manual_switch_count or 0) if workspace.manual_switch_date == today else 0,
        "timezone": SWITCH_TIMEZONE,
        "last_switched_at": isoformat(workspace.last_switched_at),
    }


def switch_gap_met(workspace: Workspace) -> bool:
    """True when the team has no recorded switch or the last one is at least SWITCH_GAP_HOURS old."""
    last = as_utc(workspace.last_switched_at)
    return last is None or (utcnow() - last).total_seconds() >= SWITCH_GAP_HOURS * 3600


async def increment_workspace_switch_count(db: AsyncSession, workspace_id: int, *, commit: bool = True) -> dict:
    """commit=False lets a caller commit the increment together with its own dedupe marker."""
    now = utcnow()
    today = now.astimezone(zone(SWITCH_TIMEZONE)).date()
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
            last_switched_at=now,
        )
        .returning(Workspace.manual_switch_count)
        .execution_options(synchronize_session=False)
    )
    count = result.scalar_one_or_none()
    if count is None:
        return {"ok": False, "error_code": "not_found", "error": "团队不存在"}
    if commit:
        await db.commit()
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "switch_count": {"date": today.isoformat(), "count": count, "timezone": SWITCH_TIMEZONE,
                         "last_switched_at": isoformat(now)},
    }
