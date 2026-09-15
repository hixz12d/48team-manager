"""Jupiter 1: dump seat_metadata and try alternate invite/update shapes."""
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
from app.core.config import load_settings
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account, Workspace

WS_ID = 11  # hixz2611 Jupiter workspace


def redact(obj):
    text = json.dumps(obj, ensure_ascii=False, default=str)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"eyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}", "<jwt>", text)
    return json.loads(text)


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
        headers_get = {"Authorization": f"Bearer {access}", "chatgpt-account-id": account_id}
        headers_json = {
            **headers_get,
            "Content-Type": "application/json",
        }

        live = await chatgpt_client.get_members(access, account_id, db, identifier=owner.email)
        print("SEAT_METADATA", json.dumps(redact(live.get("seat_metadata")), ensure_ascii=False, indent=2)[:5000])
        print("MEMBER0", json.dumps(redact((live.get("members") or [{}])[0]), ensure_ascii=False, indent=2)[:2000])

        # More discovery paths
        for method, path in [
            ("GET", f"/accounts/{account_id}/seat_types"),
            ("GET", f"/accounts/{account_id}/seat-types"),
            ("GET", f"/accounts/{account_id}/members/seats"),
            ("GET", f"/accounts/{account_id}/billing"),
            ("GET", f"/accounts/{account_id}/billing/subscriptions"),
            ("GET", f"/accounts/{account_id}/subscription"),
            ("GET", f"/accounts/{account_id}/plan"),
            ("GET", f"/accounts/{account_id}/invites/seat_types"),
            ("OPTIONS", f"/accounts/{account_id}/invites"),
        ]:
            try:
                resp = await chatgpt_client._make_request(
                    method, f"{base}{path}", headers_get, db_session=db, identifier=owner.email
                )
                print(
                    "DISC",
                    method,
                    path,
                    resp.get("status_code"),
                    json.dumps(redact(resp.get("data") or {"error": resp.get("error")}), ensure_ascii=False)[:1200],
                )
            except Exception as exc:  # noqa: BLE001
                print("DISC_ERR", path, exc)

        claimed = await hme_service.claim_next_alias(
            db, job_id="jupiter-seat-probe2", purpose="invite_seat_probe", workspace_id=ws.id
        )
        email = getattr(claimed, "email", None)
        print("EMAIL", bool(email))
        if not email:
            return

        # Alternate body shapes for premium invite
        bodies = [
            ("seatType_camel_premium", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seatType": "premium"}),
            ("seat_premium_enum_like", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "premium"}),
            ("seats_array", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seats": ["premium"]}),
            ("seat_object", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat": {"type": "premium"}}),
            ("invite_seat_type", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "invite_seat_type": "premium"}),
            ("workspace_seat_type", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "workspace_seat_type": "premium"}),
            ("product_premium", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "product": "premium"}),
            ("seat_type_1", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": 1}),
            ("seat_type_2", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": 2}),
            ("seat_type_true", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": True}),
            # maybe only one email string form
            ("email_single_seat", {"email_address": email, "role": "standard-user", "seat_type": "premium"}),
            # owner role + default already known; try owner + nondefault
            ("owner_role_premium", {"email_addresses": [email], "role": "account-owner", "resend_emails": True, "seat_type": "premium"}),
            # hyphenated seat types from UI copy
            ("seat_type_standard_chatgpt", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "standard_chatgpt"}),
            ("seat_type_premium_chatgpt", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "premium_chatgpt"}),
            ("seat_type_chatgpt", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "chatgpt"}),
            ("seat_type_business", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "business"}),
            ("seat_type_team", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "team"}),
            ("seat_type_full", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "full"}),
            ("seat_type_basic", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "basic"}),
            ("seat_type_lite", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "lite"}),
            ("seat_type_core", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "core"}),
            ("seat_type_enhanced", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "enhanced"}),
            ("seat_type_priority", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "priority"}),
            ("seat_type_reasoner", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "reasoner"}),
            ("seat_type_o1", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "o1"}),
            ("seat_type_agent", {"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "agent"}),
        ]

        results = []
        for label, body in bodies:
            resp = await chatgpt_client._make_request(
                "POST",
                f"{base}/accounts/{account_id}/invites",
                headers_json,
                db_session=db,
                identifier=owner.email,
                json_data=body,
            )
            entry = {
                "label": label,
                "success": bool(resp.get("success")),
                "status_code": resp.get("status_code"),
                "error": str(resp.get("error") or "")[:280],
            }
            if resp.get("success"):
                data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                inv = (data.get("account_invites") or [{}])[0]
                entry["observed_seat_type"] = inv.get("seat_type")
                entry["response"] = redact(data)
                print("SUCCESS", json.dumps({k: entry[k] for k in entry if k != "response"}, ensure_ascii=False))
                await chatgpt_client.delete_invite(access, account_id, email, db, identifier=owner.email)
            else:
                print("TRY", json.dumps(entry, ensure_ascii=False))
            results.append(entry)

        # Invite with default first, then try PATCH seat on invite/member
        create = await chatgpt_client._make_request(
            "POST",
            f"{base}/accounts/{account_id}/invites",
            headers_json,
            db_session=db,
            identifier=owner.email,
            json_data={"email_addresses": [email], "role": "standard-user", "resend_emails": True, "seat_type": "default"},
        )
        print("CREATE_DEFAULT", create.get("success"), create.get("status_code"))
        invite_id = None
        if create.get("success"):
            data = create.get("data") or {}
            inv = (data.get("account_invites") or [{}])[0]
            invite_id = inv.get("id")
            print("INVITE_ID", invite_id, "seat", inv.get("seat_type"))

        if invite_id:
            patch_bodies = [
                ("patch_seat_type_premium", {"seat_type": "premium"}),
                ("patch_seatType_premium", {"seatType": "premium"}),
                ("patch_pending_seat_type", {"pending_seat_type": "premium"}),
                ("patch_seat_type_default_to_something", {"seat_type": "premium", "role": "standard-user"}),
            ]
            for label, body in patch_bodies:
                for method in ("PATCH", "PUT", "POST"):
                    path = f"/accounts/{account_id}/invites/{invite_id}"
                    resp = await chatgpt_client._make_request(
                        method,
                        f"{base}{path}",
                        headers_json,
                        db_session=db,
                        identifier=owner.email,
                        json_data=body,
                    )
                    print(
                        "PATCH_INVITE",
                        method,
                        label,
                        resp.get("status_code"),
                        str(resp.get("error") or "")[:200],
                        json.dumps(redact(resp.get("data") or {}), ensure_ascii=False)[:400],
                    )

            # also try members seat update paths with owner user id
            owner_user = (live.get("members") or [{}])[0].get("id")
            if owner_user:
                for path in [
                    f"/accounts/{account_id}/users/{owner_user}",
                    f"/accounts/{account_id}/members/{owner_user}",
                    f"/accounts/{account_id}/users/{owner_user}/seat",
                    f"/accounts/{account_id}/users/{owner_user}/seat_type",
                ]:
                    for body in ({"seat_type": "premium"}, {"seatType": "premium"}, {"pending_seat_type": "premium"}):
                        resp = await chatgpt_client._make_request(
                            "PATCH",
                            f"{base}{path}",
                            headers_json,
                            db_session=db,
                            identifier=owner.email,
                            json_data=body,
                        )
                        if resp.get("status_code") not in (404, 405):
                            print(
                                "PATCH_USER",
                                path,
                                body,
                                resp.get("status_code"),
                                str(resp.get("error") or "")[:200],
                                json.dumps(redact(resp.get("data") or {}), ensure_ascii=False)[:500],
                            )

            await chatgpt_client.delete_invite(access, account_id, email, db, identifier=owner.email)

        # cleanup leases
        Lease = hme_service.HmeAliasLease
        leases = list((await db.execute(select(Lease).where(Lease.job_id.like("%seat-probe%")))).scalars())
        for row in leases:
            await db.delete(row)
        await db.commit()
        print(
            "SUMMARY",
            json.dumps(
                {
                    "successes": [r for r in results if r.get("success")],
                    "fail_count": sum(1 for r in results if not r.get("success")),
                },
                ensure_ascii=False,
                indent=2,
            )[:5000],
        )


if __name__ == "__main__":
    asyncio.run(main())
