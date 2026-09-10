"""Explicit, single-account OAuth signup after a successful Team invitation."""

from __future__ import annotations

from app.application.jobs import browser as browser_slot
from app.application.oauth_sessions import OAuthSessionError, oauth_session_store
from app.core.jwt import jwt_parser
from app.domain.identity.ids import normalize_email
from app.integrations.openai import oauth_sessions
from app.integrations.openai.chatgpt import chatgpt_client


async def run_invited_oauth_signup(
    db, *, child, workspace, password, pickup_url, use_cloudflare, cf_config,
    job_id=None, executable_path="", phone_line="",
):
    from app.integrations.sms.client import parse_optional_sms

    phone, sms_url = parse_optional_sms(phone_line)
    authorize = chatgpt_client.create_oauth_authorize_url(
        client_id=oauth_sessions.CLIENT_ID,
        redirect_uri=oauth_sessions.REDIRECT_URI,
        login_hint=child.email,
    )
    public = oauth_sessions.create_session(
        team_id=int(workspace.source_team_id or 0),
        email=child.email,
        authorize=authorize,
        proxy=child.proxy,
        proxy_source=child.proxy_source or "",
        sub2api_proxy_id=child.sub2api_proxy_id,
        proxy_instance_key=child.proxy_instance_key or "",
    )
    ticket = public["ticket"]
    oauth_sessions.mark_session(ticket, job_id=job_id or "")
    stored = None
    success = False
    try:
        stored = await oauth_session_store.persist(
            db, oauth_sessions.get_session(ticket), purpose="account_reauth",
            account_id=child.id, workspace_id=workspace.id,
            credential_revision=int(child.credential_revision or 1),
        )
        await db.commit()
        async def on_stage(stage, message):
            from app.application.invitation_flow import browser_progress
            await browser_progress(db, job_id, stage)

        result = await browser_slot.run_reauth_isolated(
            email=child.email,
            password=password,
            authorize_url=authorize["authorize_url"],
            proxy=child.proxy,
            pickup_url=pickup_url,
            use_cloudflare=use_cloudflare,
            cf_base_url=cf_config["base_url"],
            cf_address=cf_config["address"],
            cf_admin_password=cf_config["admin_password"],
            allow_signup=False,
            allow_sms=bool(phone and sms_url),
            phone=phone,
            sms_url=sms_url,
            max_sms_submissions=1,
            max_sms_code_submissions=1,
            team_name=str(workspace.name or ""),
            executable_path=executable_path,
            on_stage=on_stage,
        )
        if not result.get("ok"):
            # Do not expose callback URLs, tokens or browser diagnostics to the CLI.
            return {
                "ok": False,
                "error_code": result.get("error_code") or "browser_failed",
                "error": "OAuth 授权未完成，请使用同一邮箱继续；不会自动换号",
            }
        stored, callback = await oauth_session_store.begin_exchange(
            db, ticket, result.get("callback_url") or "",
            account_id=child.id, purpose="account_reauth",
        )
        await db.refresh(child)
        if stored.credential_revision != int(child.credential_revision or 1):
            return {"ok": False, "error_code": "credential_revision_conflict", "error": "Credentials changed during signup"}
        context = oauth_session_store.exchange_context(stored)
        exchanged = await chatgpt_client.exchange_oauth_code(
            code=callback["code"], client_id=context["client_id"],
            redirect_uri=context["redirect_uri"], code_verifier=context["code_verifier"],
            db_session=db, identifier=child.email,
        )
        if not exchanged.get("success") or not exchanged.get("access_token") or not exchanged.get("refresh_token"):
            return {"ok": False, "error_code": "oauth_exchange_failed", "error": "OAuth code exchange failed"}
        token_email = jwt_parser.extract_email(exchanged["access_token"])
        if not token_email or normalize_email(token_email) != normalize_email(child.email):
            return {"ok": False, "error_code": "token_identity_mismatch", "error": "OAuth token identity could not be matched to the invited email"}
        success = True
        return {**exchanged, "ok": True, "client_id": context["client_id"],
                "sms_verified": bool(result.get("sms_verified")),
                "phone": phone if result.get("sms_verified") else ""}
    except OAuthSessionError as exc:
        return {"ok": False, "error_code": exc.error_code, "error": str(exc)}
    finally:
        oauth_sessions.pop_session(ticket)
        if stored is not None:
            await oauth_session_store.finish(db, stored, success=success)
            await db.commit()
