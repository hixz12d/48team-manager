"""One-shot Jupiter 1 Premium seat wire probe. Run inside team48-manager."""
from __future__ import annotations

import asyncio
import json
import re
import sys

sys.path.insert(0, "/app")
from app.main import app  # noqa: F401
from sqlalchemy import func, select

from app.application.resources import hme as hme_service
from app.application.settings import upsert_setting
from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account, Workspace, WorkspaceOfficialMemberSnapshot
from app.persistence.models.settings import SystemSetting

CANDIDATES = [
    None,
    "default",
    "standard",
    "premium",
    "business_premium",
    "business-premium",
    "premium_seat",
    "premium-seat",
    "seat_premium",
    "seat-premium",
    "chatgpt_premium",
    "chatgpt-premium",
    "oai_premium",
    "team_premium",
    "workspace_premium",
    "advanced",
    "pro",
    "plus",
    "codex",
    "enterprise_premium",
    "biz_premium",
    "premium_user",
    "premium-user",
    "PREMIUM",
    "Premium",
    "DEFAULT",
    "standard_seat",
    "standard-seat",
    "seat_standard",
]


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
        owners = list((await db.execute(select(Account).where(Account.email.like("%hixz2611%")))).scalars())
        print("OWNERS", [(o.id, o.email, o.auth_state) for o in owners])
        wss = []
        for ws in (await db.execute(select(Workspace).where(Workspace.status == "active"))).scalars():
            name = (ws.name or "") + " " + (getattr(ws, "display_name", None) or "")
            owner = await db.get(Account, ws.owner_account_id) if ws.owner_account_id else None
            hit = "jupiter" in name.lower() or (owner and "hixz2611" in (owner.email or "").lower())
            if not hit:
                continue
            snap_n = await db.scalar(
                select(func.count())
                .select_from(WorkspaceOfficialMemberSnapshot)
                .where(WorkspaceOfficialMemberSnapshot.workspace_id == ws.id)
            )
            wss.append(
                {
                    "id": ws.id,
                    "name": ws.name,
                    "official_workspace_id": ws.official_workspace_id,
                    "owner": owner.email if owner else None,
                    "owner_id": owner.id if owner else None,
                    "auth": getattr(owner, "auth_state", None),
                    "snap_n": int(snap_n or 0),
                    "subscription_plan": getattr(ws, "subscription_plan", None),
                }
            )
        print("WS_CANDIDATES", json.dumps(wss, ensure_ascii=False, indent=2))
        if not wss:
            rows = []
            for ws in (
                await db.execute(select(Workspace).where(Workspace.status == "active").order_by(Workspace.id.desc()).limit(50))
            ).scalars():
                owner = await db.get(Account, ws.owner_account_id) if ws.owner_account_id else None
                rows.append({"id": ws.id, "name": ws.name, "owner": owner.email if owner else None})
            print("RECENT", json.dumps(rows, ensure_ascii=False, indent=2))
            return

        target = next((w for w in wss if w.get("name") and "jupiter" in w["name"].lower()), wss[0])
        ws = await db.get(Workspace, target["id"])
        owner = await db.get(Account, ws.owner_account_id)
        print("TARGET", json.dumps(target, ensure_ascii=False))
        access = await workspace_service.ensure_access_token(db, ws)
        account_id = workspace_service._workspace_account_id(ws)
        print("AUTH", {"has_access": bool(access), "account_id": account_id})
        if not access or not account_id:
            return

        live = await chatgpt_client.get_members(access, account_id, db, identifier=owner.email)
        invites = await chatgpt_client.get_invites(access, account_id, db, identifier=owner.email)
        print(
            "BEFORE",
            json.dumps(
                redact(
                    {
                        "members_ok": live.get("success"),
                        "member_total": live.get("total"),
                        "invite_total": invites.get("total"),
                        "members_sample": (live.get("members") or live.get("items") or [])[:5],
                        "raw_keys": sorted(live.keys()),
                    }
                ),
                ensure_ascii=False,
            )[:4000],
        )

        # Probe account-level endpoints for seat enum hints.
        base = chatgpt_client.BASE_URL
        for path in (
            f"/accounts/{account_id}",
            f"/accounts/{account_id}/seats",
            f"/accounts/{account_id}/subscriptions",
            f"/accounts/{account_id}/settings",
        ):
            try:
                headers = {
                    "Authorization": f"Bearer {access}",
                    "chatgpt-account-id": account_id,
                }
                resp = await chatgpt_client._make_request(
                    "GET", f"{base}{path}", headers, db_session=db, identifier=owner.email
                )
                print("PATH", path, resp.get("status_code"), json.dumps(redact(resp.get("data") or resp), ensure_ascii=False)[:2000])
            except Exception as exc:  # noqa: BLE001
                print("PATH_ERR", path, type(exc).__name__, str(exc)[:200])

        claimed = await hme_service.claim_next_alias(
            db, job_id="jupiter-seat-probe", purpose="invite_seat_probe", workspace_id=ws.id
        )
        email = getattr(claimed, "email", None)
        print("CLAIMED", bool(email))
        if not email:
            return

        results = []
        premium_hit = None
        default_hit = None
        for seat in CANDIDATES:
            payload = {"email_addresses": [email], "role": "standard-user", "resend_emails": True}
            label = "omit" if seat is None else str(seat)
            if seat is not None:
                payload["seat_type"] = seat
            url = f"{base}/accounts/{account_id}/invites"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access}",
                "chatgpt-account-id": account_id,
            }
            resp = await chatgpt_client._make_request(
                "POST", url, headers, db_session=db, identifier=owner.email, json_data=payload
            )
            entry = {
                "candidate": label,
                "success": bool(resp.get("success")),
                "status_code": resp.get("status_code"),
                "error": str(resp.get("error") or "")[:320],
            }
            if resp.get("success"):
                data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                inv = (data.get("account_invites") or [{}])[0]
                observed = inv.get("seat_type")
                entry["observed_seat_type"] = observed
                entry["response"] = redact(data)
                results.append(entry)
                print("SUCCESS", json.dumps({k: entry[k] for k in entry if k != "response"}, ensure_ascii=False))
                rev = await chatgpt_client.delete_invite(access, account_id, email, db, identifier=owner.email)
                print("REVOKE", rev.get("success"), rev.get("status_code"))
                if observed and str(observed).lower() != "default":
                    premium_hit = entry
                    break
                default_hit = entry
                continue
            results.append(entry)
            print("TRY", json.dumps(entry, ensure_ascii=False))
            await chatgpt_client.delete_invite(access, account_id, email, db, identifier=owner.email)

        await upsert_setting(db, "invite_seat_wire_standard", "default", description="verified Dual World + Jupiter")
        premium_wire = None
        if premium_hit:
            # Prefer explicit request wire; omit means observed only.
            premium_wire = (
                premium_hit["observed_seat_type"]
                if premium_hit["candidate"] == "omit"
                else premium_hit["candidate"]
            )
            await upsert_setting(
                db,
                "invite_seat_wire_premium",
                premium_wire,
                description=(
                    f"Jupiter 1 capture request={premium_hit['candidate']} "
                    f"observed={premium_hit.get('observed_seat_type')}"
                ),
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
                    "workspace": target,
                    "premium_wire": premium_wire,
                    "default_hit": bool(default_hit),
                    "settings": [(r.key, r.value) for r in settings_rows],
                    "successes": [r for r in results if r.get("success")],
                    "failures": [r for r in results if not r.get("success")],
                    "attempts_count": len(results),
                },
                ensure_ascii=False,
                indent=2,
            )[:20000],
        )


if __name__ == "__main__":
    asyncio.run(main())
