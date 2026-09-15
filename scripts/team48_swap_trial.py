"""Approved one-time replacement, guarded by exact workspace/member identities."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from sqlalchemy import select
from app.application.onboard import onboard_service
from app.application.jobs import browser as browser_slot
from app.application.member_lifecycle import record_confirmed_departure, has_other_active_context
from app.application.operations import operation_store
from app.application.reauth import load_cf_config
from app.application.resources import hme
from app.application.tokens import decrypt_secret
from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.core.time import utcnow
from app.domain.onboard import KICK_COOLDOWN_SECONDS
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account, Workspace

OWNER = "hixz2611@gmail.com"
OLD = "adagio.funding1i@icloud.com"
OLD_ID = "user-kgIMhV8aoYLlhvXOSNxzWU3Z"
OFFICIAL_ID = "617649a2-2463-4e4f-bab8-ab145aa8a1bf"
STATE = {"state": "starting", "workspace_id": 11, "owner": OWNER, "old_child": OLD, "removed": False}


def progress(**fields):
    STATE.update(fields, updated_at=utcnow().isoformat())
    temporary = ROOT / "trial-state.next.json"
    temporary.write_text(json.dumps(STATE, ensure_ascii=True), encoding="utf-8")
    temporary.replace(ROOT / "trial-state.json")


async def main():
    engine = create_engine(load_settings())
    factory = create_session_factory(engine)
    heartbeat = None
    operation = None
    final = {"success": False, "error_code": "trial_interrupted"}
    try:
        async with factory() as db:
            workspace = await workspace_service.load_workspace(db, 11)
            if workspace is None or workspace.status != "active" or workspace.official_workspace_id != OFFICIAL_ID:
                raise RuntimeError("workspace_identity_changed")
            owner = await db.get(Account, workspace.owner_account_id)
            if owner is None or owner.email != OWNER:
                raise RuntimeError("mother_identity_changed")
            cf = await load_cf_config(db)
            cfg = await hme.load_config(db)
            if not (cf["admin_password"] and cfg.configured and owner.proxy):
                raise RuntimeError("required_configuration_missing")
            if await operation_store.browser_busy(db):
                raise RuntimeError("browser_busy")
            aliases = hme.hme_client.list_aliases(cfg, str(hme.resolve_account(hme.hme_client.list_accounts(cfg), cfg.account_id)["id"]))
            unavailable = await hme.active_leased_emails(db) | await hme.occupied_account_emails(db)
            if hme.pick_next_unoccupied(aliases, unavailable) is None:
                raise RuntimeError("hme_empty")

            async def official():
                access = decrypt_secret(owner.access_token_encrypted)
                members = await chatgpt_client.get_members(access, OFFICIAL_ID, db, identifier=OWNER)
                invites = await chatgpt_client.get_invites(access, OFFICIAL_ID, db, identifier=OWNER)
                if not members.get("success") or not invites.get("success"):
                    raise RuntimeError("official_read_failed")
                return members.get("members") or [], invites.get("items") or []

            operation, blocker = await operation_store.create_workspace_locked(
                db, op_type="onboard", workspace_id=11, email=OLD,
                input_payload={"mode": "approved_single_replacement", "old_email": OLD,
                               "role": "owner", "seat_intent": "premium", "oauth_signup": True},
                lease_seconds=1200,
            )
            if blocker:
                raise RuntimeError("workspace_busy")
            await db.commit()
            progress(state="preflight", operation_id=operation.public_id)

            async def keep_alive():
                while True:
                    await asyncio.sleep(20)
                    async with factory() as lease_db:
                        await operation_store.heartbeat_active(lease_db, operation.public_id, lease_seconds=1200)
                        await lease_db.commit()
            heartbeat = asyncio.create_task(keep_alive())
            members, invites = await official()
            if len(members) != 2 or invites or {m.get("email") for m in members} != {OWNER, OLD}:
                raise RuntimeError("official_roster_changed")
            old = next(m for m in members if m.get("email") == OLD)
            mother = next(m for m in members if m.get("email") == OWNER)
            if old.get("id") != OLD_ID or old.get("role") != "account-owner" or old.get("seat_type") != "prolite" or mother.get("role") != "account-owner":
                raise RuntimeError("official_identity_or_seat_changed")
            (ROOT / "before-roster.json").write_text(json.dumps(members), encoding="utf-8")
            progress(state="removing_old_child")
            removal = await workspace_service.delete_member(db, 11, OLD_ID, email=OLD)
            if not removal.get("success"):
                final = {"success": False, "error_code": removal.get("error_code") or "remove_failed"}
                return
            confirmed = False
            for _ in range(5):
                members, invites = await official()
                if len(members) == 1 and members[0].get("email") == OWNER and not invites:
                    confirmed = True
                    break
                await asyncio.sleep(3)
            if not confirmed:
                raise RuntimeError("removal_not_confirmed_do_not_repeat")
            await record_confirmed_departure(db, workspace, OLD)
            old_account = await db.scalar(select(Account).where(Account.email == OLD))
            if old_account is not None and not await has_other_active_context(db, old_account, 11):
                await workspace_service.mark_standby(db, old_account,
                    next_eligible_at=utcnow() + timedelta(seconds=KICK_COOLDOWN_SECONDS), workspace_id=11)
            await db.commit()
            progress(state="registering", removed=True)

            original_browser = browser_slot.run_reauth_isolated
            async def observed_browser(**kwargs):
                def on_stage(stage, message):
                    progress(state="registering", browser_stage=stage)
                return await original_browser(**kwargs, on_stage=on_stage)
            browser_slot.run_reauth_isolated = observed_browser
            try:
                final = await onboard_service.invite_and_onboard(
                    db, workspace_id=11, email_line="", role="owner", seat_intent="premium",
                    oauth_signup=True, browser_executable=str(ROOT / "browser/chromix/chrome"),
                    job_id=operation.public_id,
                )
            finally:
                browser_slot.run_reauth_isolated = original_browser
            await db.commit()
            members, invites = await official()
            safe = [{k: m.get(k) for k in ("email", "role", "seat_type", "id")} for m in members]
            pending = [{k: m.get(k) for k in ("email", "role", "seat_type", "id")} for m in invites]
            progress(final_members=safe, final_invites=pending)
            if final.get("success"):
                email = (final.get("child") or {}).get("email")
                new = [m for m in members if m.get("email") == email]
                if len(members) != 2 or not any(m.get("email") == OWNER for m in members) or len(new) != 1 or new[0].get("role") != "account-owner" or new[0].get("seat_type") != "prolite":
                    final = {**final, "success": False, "error_code": "final_role_or_seat_mismatch"}
    except asyncio.CancelledError:
        final = {"success": False, "error_code": "trial_timeout"}
        raise
    except Exception as exc:
        safe_codes = {"workspace_identity_changed", "mother_identity_changed", "required_configuration_missing", "browser_busy", "hme_empty", "official_read_failed", "workspace_busy", "official_roster_changed", "official_identity_or_seat_changed", "removal_not_confirmed_do_not_repeat"}
        code = str(exc) if str(exc) in safe_codes else type(exc).__name__
        final = {"success": False, "error_code": code}
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if operation is not None:
            async with factory() as finish_db:
                row = await operation_store.get_by_public_id(finish_db, operation.public_id)
                if row is not None:
                    await operation_store.finish(finish_db, row, final)
                    await finish_db.commit()
        progress(state="completed" if final.get("success") else "stopped",
                 success=bool(final.get("success")), error_code=final.get("error_code"),
                 new_child=(final.get("child") or {}).get("email"),
                 new_account_id=(final.get("child") or {}).get("id"), pushed=False)
        await engine.dispose()


async def bounded_run():
    await asyncio.wait_for(main(), timeout=1200)


if __name__ == "__main__":
    if "--confirm-once" not in sys.argv:
        raise SystemExit("Explicit one-time confirmation required")
    marker = ROOT / "trial-started.lock"
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(utcnow().isoformat())
    try:
        asyncio.run(bounded_run())
    except TimeoutError:
        pass
