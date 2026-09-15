"""Jupiter 1: verify real seat change wire via PATCH users / invites raw bodies."""
from __future__ import annotations

import asyncio
import json
import re
import sys

sys.path.insert(0, "/app")
from app.main import app  # noqa: F401
from sqlalchemy import select

from app.application.resources import hme as hme_service
from app.application.workspaces import workspace_service
from app.application.settings import upsert_setting
from app.core.config import load_settings
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.settings import SystemSetting

WS_ID = 11


def redact(obj):
    text = json.dumps(obj, ensure_ascii=False, default=str)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"eyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}", "<jwt>", text)
    return json.loads(text)


async def raw_request(method: str, url: str, headers: dict, json_data=None):
    """Bypass client wrapper to keep full JSON body."""
    client = getattr(chatgpt_client, "_client", None) or getattr(chatgpt_client, "client", None)
    # chatgpt_client uses httpx via session helper; fall back to _make_request then also try httpx directly
    import httpx

    # Reuse proxy/session if possible by calling internal request then printing data.
    # Prefer direct httpx with same headers.
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        resp = await http.request(method, url, headers=headers, json=json_data)
        try:
            body = resp.json()
        except Exception:
            body = {"_text": resp.text[:1000]}
        return {"status_code": resp.status_code, "body": body, "headers": dict(resp.headers)}


async def main():
    settings = load_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    async with factory() as db:
        ws = await db.get(Workspace, WS_ID)
        owner = await db.get(Account, ws.owner_account_id)
        access = await workspace_service.ensure_access_token(db, ws)
        account_id = workspace_service._workspace_account_id(ws)
        base = chatgpt_client.BASE_URL
        headers = {
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        def members_view(live):
            items = live.get("members") or live.get("items") or []
            return [
                {
                    "email": m.get("email"),
                    "role": m.get("role"),
                    "seat_type": m.get("seat_type"),
                    "pending_seat_type": m.get("pending_seat_type"),
                    "id": m.get("id"),
                }
                for m in items
            ]

        live = await chatgpt_client.get_members(access, account_id, db, identifier=owner.email)
        print("MEMBERS_BEFORE", json.dumps(redact(members_view(live)), ensure_ascii=False, indent=2))
        owner_user = (live.get("members") or [{}])[0].get("id")
        print("OWNER_USER", owner_user)

        # 1) Controlled user seat PATCH candidates one at a time; check members after each.
        # Prefer values that previously returned non-422.
        user_candidates = [
            {"seat_type": "default"},
            {"pending_seat_type": "premium"},
            {"seat_type": "premium"},
            {"seatType": "premium"},
            {"pending_seat_type": "default"},
            {"seat_type": "standard"},
            {"pending_seat_type": "standard"},
        ]
        user_results = []
        if owner_user:
            for body in user_candidates:
                url = f"{base}/accounts/{account_id}/users/{owner_user}"
                # Use client path first for proxy, then inspect full data
                resp = await chatgpt_client._make_request(
                    "PATCH",
                    url,
                    headers,
                    db_session=db,
                    identifier=owner.email,
                    json_data=body,
                )
                # Also raw if client stripped
                raw = None
                try:
                    raw = await raw_request("PATCH", url, headers, body)
                except Exception as exc:  # noqa: BLE001
                    raw = {"error": str(exc)}
                live2 = await chatgpt_client.get_members(access, account_id, db, identifier=owner.email)
                entry = {
                    "body": body,
                    "client_status": resp.get("status_code"),
                    "client_error": str(resp.get("error") or "")[:240],
                    "client_data": redact(resp.get("data")) if resp.get("data") is not None else None,
                    "raw_status": (raw or {}).get("status_code"),
                    "raw_body": redact((raw or {}).get("body")),
                    "members_after": redact(members_view(live2)),
                }
                user_results.append(entry)
                print("USER_PATCH", json.dumps(entry, ensure_ascii=False)[:2500])
                # stop if we observe non-default seat on owner
                seat = (live2.get("members") or [{}])[0].get("seat_type")
                pending = (live2.get("members") or [{}])[0].get("pending_seat_type")
                if seat and str(seat).lower() != "default":
                    print("OBSERVED_NON_DEFAULT_SEAT", seat, pending)
                    break
                if pending:
                    print("OBSERVED_PENDING", pending)
                    break

        # 2) Invite create default then PATCH invite with various fields; GET invites
        claimed = await hme_service.claim_next_alias(
            db, job_id="jupiter-seat-probe3", purpose="invite_seat_probe", workspace_id=ws.id
        )
        email = getattr(claimed, "email", None)
        print("EMAIL", bool(email))
        invite_results = []
        if email:
            create = await chatgpt_client._make_request(
                "POST",
                f"{base}/accounts/{account_id}/invites",
                headers,
                db_session=db,
                identifier=owner.email,
                json_data={
                    "email_addresses": [email],
                    "role": "standard-user",
                    "resend_emails": True,
                    "seat_type": "default",
                },
            )
            inv = ((create.get("data") or {}).get("account_invites") or [{}])[0]
            invite_id = inv.get("id")
            print("INVITE_CREATED", invite_id, inv.get("seat_type"))
            if invite_id:
                invite_bodies = [
                    {"seat_type": "premium"},
                    {"seatType": "premium"},
                    {"pending_seat_type": "premium"},
                    {"seat_type": "default"},
                    {"pending_seat_type": "default"},
                    {"seat_type": "standard"},
                ]
                for body in invite_bodies:
                    resp = await chatgpt_client._make_request(
                        "PATCH",
                        f"{base}/accounts/{account_id}/invites/{invite_id}",
                        headers,
                        db_session=db,
                        identifier=owner.email,
                        json_data=body,
                    )
                    try:
                        raw = await raw_request(
                            "PATCH",
                            f"{base}/accounts/{account_id}/invites/{invite_id}",
                            headers,
                            body,
                        )
                    except Exception as exc:  # noqa: BLE001
                        raw = {"error": str(exc)}
                    inv_list = await chatgpt_client.get_invites(access, account_id, db, identifier=owner.email)
                    items = inv_list.get("items") or inv_list.get("invites") or []
                    match = next((i for i in items if i.get("id") == invite_id or i.get("email_address") == email or i.get("email") == email), None)
                    entry = {
                        "body": body,
                        "client_status": resp.get("status_code"),
                        "client_error": str(resp.get("error") or "")[:240],
                        "client_data": redact(resp.get("data")) if resp.get("data") is not None else None,
                        "raw_status": (raw or {}).get("status_code"),
                        "raw_body": redact((raw or {}).get("body")),
                        "invite_after": redact(match) if match else None,
                    }
                    invite_results.append(entry)
                    print("INVITE_PATCH", json.dumps(entry, ensure_ascii=False)[:2500])
                    if match and match.get("seat_type") and str(match.get("seat_type")).lower() != "default":
                        print("INVITE_NON_DEFAULT", match.get("seat_type"))
                        break
                    if match and match.get("pending_seat_type"):
                        print("INVITE_PENDING", match.get("pending_seat_type"))
                        break
                await chatgpt_client.delete_invite(access, account_id, email, db, identifier=owner.email)

        # Restore owner seat to default if changed
        live3 = await chatgpt_client.get_members(access, account_id, db, identifier=owner.email)
        print("MEMBERS_FINAL", json.dumps(redact(members_view(live3)), ensure_ascii=False, indent=2))
        m0 = (live3.get("members") or [{}])[0]
        if owner_user and (m0.get("seat_type") not in (None, "default") or m0.get("pending_seat_type")):
            for body in (
                {"seat_type": "default"},
                {"pending_seat_type": "default"},
                {"pending_seat_type": None},
                {"seatType": "default"},
            ):
                resp = await chatgpt_client._make_request(
                    "PATCH",
                    f"{base}/accounts/{account_id}/users/{owner_user}",
                    headers,
                    db_session=db,
                    identifier=owner.email,
                    json_data=body,
                )
                print("RESTORE", body, resp.get("status_code"), str(resp.get("error") or "")[:160])
            live4 = await chatgpt_client.get_members(access, account_id, db, identifier=owner.email)
            print("MEMBERS_RESTORED", json.dumps(redact(members_view(live4)), ensure_ascii=False, indent=2))

        # Derive premium wire if any observed non-default
        premium_wire = None
        evidence = None
        for entry in invite_results:
            inv = entry.get("invite_after") or {}
            if inv.get("seat_type") and str(inv.get("seat_type")).lower() != "default":
                premium_wire = inv.get("seat_type")
                evidence = {"kind": "invite_after", "request": entry.get("body"), "observed": inv}
                break
            if inv.get("pending_seat_type"):
                premium_wire = inv.get("pending_seat_type")
                evidence = {"kind": "invite_pending", "request": entry.get("body"), "observed": inv}
                break
        if premium_wire is None:
            for entry in user_results:
                for m in entry.get("members_after") or []:
                    if m.get("seat_type") and str(m.get("seat_type")).lower() != "default":
                        premium_wire = m.get("seat_type")
                        evidence = {"kind": "user_after", "request": entry.get("body"), "observed": m}
                        break
                    if m.get("pending_seat_type"):
                        premium_wire = m.get("pending_seat_type")
                        evidence = {"kind": "user_pending", "request": entry.get("body"), "observed": m}
                        break
                if premium_wire:
                    break

        await upsert_setting(db, "invite_seat_wire_standard", "default", description="verified")
        if premium_wire:
            await upsert_setting(
                db,
                "invite_seat_wire_premium",
                str(premium_wire),
                description=f"Jupiter evidence {json.dumps(evidence, ensure_ascii=False)[:180]}",
            )
        Lease = hme_service.HmeAliasLease
        leases = list((await db.execute(select(Lease).where(Lease.job_id.like("%seat-probe%")))).scalars())
        for row in leases:
            await db.delete(row)
        await db.commit()
        settings_rows = list(
            (await db.execute(select(SystemSetting).where(SystemSetting.key.like("invite_seat%")))).scalars()
        )
        print(
            "FINAL",
            json.dumps(
                {
                    "premium_wire": premium_wire,
                    "evidence": evidence,
                    "settings": [(r.key, r.value) for r in settings_rows],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
