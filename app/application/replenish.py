"""One-seat replenishment: resume/reuse, invite registration, then conditional-SMS OAuth."""
from __future__ import annotations

from typing import Any

from app.application.onboard import onboard_service
from app.application.operations import operation_store
from app.domain.automation import WORKSPACE_LOCK_ACTIONS


class ReplenishService:
    def __init__(self, *, onboard=None, reauth=None):
        self.onboard = onboard or onboard_service

    async def run(
        self, db, *, workspace_id: int, job_id: str | None = None,
        role: str = "owner", seat_intent: str = "workspace_default",
        phone_line: str = "", in_test: bool = False,
    ) -> dict[str, Any]:
        busy = await operation_store.active_for_workspace(
            db, workspace_id, actions=WORKSPACE_LOCK_ACTIONS, exclude_public_id=job_id,
        )
        if busy:
            return {"success": False, "error_code": "operation_conflict", "operation_id": busy.public_id}
        browser_busy = await operation_store.browser_busy(db)
        if browser_busy and browser_busy.public_id != job_id:
            return {"success": False, "error_code": "browser_busy", "operation_id": browser_busy.public_id}
        result = await self.onboard.invite_and_onboard(
            db, workspace_id=workspace_id, email_line="", phone_line=phone_line,
            reuse_existing=True, job_id=job_id, in_test=in_test, role=role,
            seat_intent=seat_intent, oauth_signup=True,
        )
        child = result.get("child") or {}
        if job_id and child:
            op = await operation_store.get_by_public_id(db, job_id)
            if op:
                op.email = child.get("email") or op.email
                op.account_id = child.get("id") or op.account_id
                await db.flush()
        return {**result, "account_id": child.get("id"), "email": child.get("email"), "pushed": False}


replenish_service = ReplenishService()
