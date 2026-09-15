"""Read Jupiter 1 pending invites to capture real Premium seat_type wire."""
from __future__ import annotations

import asyncio
import json
import re
import sys

sys.path.insert(0, "/app")
from app.main import app  # noqa: F401
from sqlalchemy import select

from app.application.settings import upsert_setting
from app.application.workspaces import workspace_service
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
            "Accept": "application/json",
        }

        # Members + invites via client
        live = await chatgpt_client.get_members(access, account_id, db, identifier=owner.email)
        invites = await chatgpt_client.get_invites(access, account_id, db, identifier=owner.email)
        print(
            "MEMBERS",
            json.dumps(
                redact(
                    {
                        "total": live.get("total"),
                        "items": live.get("members") or live.get("items"),
                        "raw_keys": sorted(live.keys()),
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )[:6000],
        )
        print(
            "INVITES_CLIENT",
            json.dumps(
                redact(
                    {
                        "success": invites.get("success"),
                        "total": invites.get("total"),
                        "items": invites.get("items") or invites.get("invites"),
                        "raw": {k: invites.get(k) for k in invites if k not in ("items", "invites")},
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )[:8000],
        )

        # Raw list endpoints / alternate query shapes
        for path in (
            f"/accounts/{account_id}/invites",
            f"/accounts/{account_id}/invites?offset=0&limit=50",
            f"/accounts/{account_id}/invites?status=pending",
        ):
            resp = await chatgpt_client._make_request(
                "GET", f"{base}{path}", headers, db_session=db, identifier=owner.email
            )
            print(
                "RAW_INVITES",
                path,
                resp.get("status_code"),
                json.dumps(redact(resp.get("data") or {"error": resp.get("error"), "full": resp}), ensure_ascii=False)[:5000],
            )

        items = invites.get("items") or invites.get("invites") or []
        if not items and isinstance(invites.get("data"), dict):
            items = invites["data"].get("items") or invites["data"].get("account_invites") or []
        # also from raw data
        data = invites.get("data") if isinstance(invites.get("data"), dict) else None

        premium_vals = []
        for it in items:
            st = it.get("seat_type") or it.get("seatType") or it.get("seat")
            role = it.get("role")
            print("INVITE_ROW", json.dumps(redact({"role": role, "seat_type": st, "keys": sorted(it.keys()), "row": it}), ensure_ascii=False)[:2000])
            if st and str(st).lower() != "default":
                premium_vals.append(str(st))

        # If client parser dropped fields, inspect first raw path body more carefully already printed.

        premium_wire = premium_vals[0] if premium_vals else None
        if premium_wire:
            await upsert_setting(
                db,
                "invite_seat_wire_premium",
                premium_wire,
                description="Jupiter 1 UI-created pending invite observed seat_type",
            )
            await upsert_setting(db, "invite_seat_wire_standard", "default", description="verified")
            await db.commit()

        settings_rows = list(
            (await db.execute(select(SystemSetting).where(SystemSetting.key.like("invite_seat%")))).scalars()
        )
        print(
            "FINAL",
            json.dumps(
                {
                    "premium_wire": premium_wire,
                    "premium_candidates": premium_vals,
                    "settings": [(r.key, r.value) for r in settings_rows],
                    "invite_count": len(items),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
