"""Resume OAuth for the approved existing child; SMS is used only if requested by OpenAI."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from sqlalchemy import select
from app.application.reauth import ReauthService, load_cf_config
from app.application.oauth_sessions import oauth_session_store
from app.application.jobs import browser as browser_slot
from app.application.operations import operation_store
from app.application.identity import ensure_membership
from app.application.tokens import auth_service, decrypt_secret
from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.core.crypto import token_cipher
from app.core.jwt import jwt_parser
from app.core.time import utcnow
from app.integrations.openai import oauth_sessions
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account
from app.persistence.models.oauth import OAuthSession

STATE = {"state": "starting", "email": "4-acetic.glyph@icloud.com", "sms_used": False}


def report(**fields):
    STATE.update(fields, updated_at=utcnow().isoformat())
    temporary = ROOT / "sms-resume.next.json"
    temporary.write_text(json.dumps(STATE), encoding="utf-8")
    temporary.replace(ROOT / "sms-resume-state.json")


async def main():
    secret_file = ROOT / "sms-once.enc"
    secret = json.loads(token_cipher().decrypt(secret_file.read_text(encoding="utf-8")))
    secret_file.unlink()
    engine = create_engine(load_settings())
    factory = create_session_factory(engine)
    stored = operation = None
    heartbeat = None
    success = False
    code = "interrupted"
    try:
        async with factory() as db:
            child = await db.get(Account, 44)
            workspace = await workspace_service.load_workspace(db, 11)
            owner = await db.get(Account, workspace.owner_account_id) if workspace else None
            if child is None or child.email != STATE["email"] or owner is None or owner.email != "hixz2611@gmail.com":
                raise RuntimeError("identity_mismatch")
            if await operation_store.browser_busy(db):
                raise RuntimeError("browser_busy")
            members = await workspace_service.get_members(db, workspace)
            rows = members.get("members") or []
            current = [m for m in rows if m.get("email") == child.email]
            if not members.get("success") or len(current) != 1 or current[0].get("role") not in ("owner", "account-owner") or current[0].get("seat_type") != "prolite":
                raise RuntimeError("owner_premium_membership_unconfirmed")
            operation, blocker = await operation_store.create_workspace_locked(
                db, op_type="reauth", workspace_id=11, account_id=44, email=child.email,
                input_payload={"mode":"single_manual_sms", "sms_policy":"only_if_required"}, lease_seconds=1200,
            )
            if blocker:
                raise RuntimeError("workspace_busy")
            await db.commit()
            report(state="authorizing", operation_id=operation.public_id)

            async def keep_alive():
                while True:
                    await asyncio.sleep(20)
                    async with factory() as lease_db:
                        await operation_store.heartbeat_active(lease_db, operation.public_id, lease_seconds=1200)
                        await lease_db.commit()
            heartbeat = asyncio.create_task(keep_alive())
            session = await ReauthService().start_manual_reauth(db, child)
            stored = await db.scalar(select(OAuthSession).where(OAuthSession.public_id == session["ticket"]))
            stored.operation_id = operation.public_id
            await db.commit()
            cf = await load_cf_config(db)
            def on_stage(stage, message):
                report(browser_stage=stage, sms_used=STATE["sms_used"] or stage in {"add_phone", "sms_otp"})
            browser = await browser_slot.run_reauth_isolated(
                email=child.email, password="", authorize_url=session["authorize_url"],
                proxy=child.proxy or owner.proxy, allow_signup=False, allow_sms=True,
                phone=secret["phone"], sms_url=secret["sms_url"],
                use_cloudflare=True, cf_base_url=cf["base_url"], cf_address=cf["address"],
                cf_admin_password=cf["admin_password"], team_name=workspace.name,
                executable_path=str(ROOT / "browser/chromix/chrome"), on_stage=on_stage,
            )
            secret.clear()
            if not browser.get("ok"):
                code = browser.get("error_code") or "browser_failed"
                return
            stored, parsed = await oauth_session_store.begin_exchange(db, session["ticket"], browser["callback_url"], account_id=44, purpose="account_reauth")
            await db.refresh(child)
            if stored.credential_revision != int(child.credential_revision or 1):
                code = "credential_revision_conflict"
                return
            context = oauth_session_store.exchange_context(stored)
            tokens = await chatgpt_client.exchange_oauth_code(
                code=parsed["code"], client_id=context["client_id"], redirect_uri=context["redirect_uri"],
                code_verifier=context["code_verifier"], db_session=db, identifier=child.email,
            )
            if not tokens.get("success") or not tokens.get("access_token") or not tokens.get("refresh_token"):
                code = "oauth_exchange_failed"
                return
            if jwt_parser.extract_email(tokens["access_token"]) != child.email:
                code = "token_identity_mismatch"
                return
            await auth_service.apply_tokens(child, {**tokens, "client_id":context["client_id"]})
            await ReauthService().mark_outcome(db, child, success=True)
            await ensure_membership(db, workspace_id=11, account_id=44, official_role="owner", membership_state="joined", local_purpose="child", joined_at=utcnow())
            child.operational_state = "active"
            if STATE["sms_used"]:
                child.phone = browser.get("phone") or child.phone
            await db.commit()
            success = True
            code = ""
            report(has_access_token=True, has_refresh_token=True, membership_state="joined", role="owner", seat_type="prolite")
    except asyncio.CancelledError:
        code = "trial_timeout"
        raise
    except Exception as exc:
        safe = {"identity_mismatch", "browser_busy", "workspace_busy", "owner_premium_membership_unconfirmed"}
        code = str(exc) if str(exc) in safe else type(exc).__name__
    finally:
        secret.clear()
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        async with factory() as db:
            if stored is not None:
                row = await db.get(OAuthSession, stored.id)
                await oauth_session_store.finish(db, row, success=success)
                oauth_sessions.pop_session(stored.public_id)
            if operation is not None:
                op = await operation_store.get_by_public_id(db, operation.public_id)
                await operation_store.finish(db, op, {"success":success, "error_code":code, "pushed":False})
            await db.commit()
        report(state="completed" if success else "stopped", success=success, error_code=code, pushed=False)
        await engine.dispose()


async def bounded():
    await asyncio.wait_for(main(), timeout=1200)


if __name__ == "__main__":
    with (ROOT / "sms-resume-started.lock").open("x") as marker:
        marker.write(utcnow().isoformat())
    try:
        asyncio.run(bounded())
    except TimeoutError:
        pass
