"""Verify invite create with seat_type=prolite on Jupiter 1, then revoke."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time

sys.path.insert(0, "/app")
from app.main import app  # noqa: F401

from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account, Workspace

WS_ID = 11


def redact(obj):
    text = json.dumps(obj, ensure_ascii=False, default=str)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
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
        headers = {
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account_id,
            "Content-Type": "application/json",
        }
        # Disposable probe address; revoke immediately. Avoid HME (token flake).
        email = f"team48.prolite.probe.{int(time.time())}@example.com"
        results = []
        for label, body in [
            (
                "member_prolite",
                {
                    "email_addresses": [email],
                    "role": "standard-user",
                    "resend_emails": True,
                    "seat_type": "prolite",
                },
            ),
            (
                "owner_prolite",
                {
                    "email_addresses": [email],
                    "role": "account-owner",
                    "resend_emails": True,
                    "seat_type": "prolite",
                },
            ),
        ]:
            await chatgpt_client.delete_invite(access, account_id, email, db, identifier=owner.email)
            resp = await chatgpt_client._make_request(
                "POST",
                f"{base}/accounts/{account_id}/invites",
                headers,
                db_session=db,
                identifier=owner.email,
                json_data=body,
            )
            entry = {
                "label": label,
                "request": {**body, "email_addresses": ["<email>"]},
                "success": bool(resp.get("success")),
                "status_code": resp.get("status_code"),
                "error": str(resp.get("error") or "")[:300],
            }
            if resp.get("success"):
                data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                inv = (data.get("account_invites") or [{}])[0]
                entry["observed_seat_type"] = inv.get("seat_type")
                entry["observed_role"] = inv.get("role")
                entry["response"] = redact(data)
            results.append(entry)
            print("TRY", json.dumps({k: entry[k] for k in entry if k != "response"}, ensure_ascii=False))
            await chatgpt_client.delete_invite(access, account_id, email, db, identifier=owner.email)

        print("FINAL", json.dumps({"results": results}, ensure_ascii=False, indent=2)[:5000])


if __name__ == "__main__":
    asyncio.run(main())
