"""One explicit HME -> invite -> OAuth signup. No scheduler or SMS pool."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workspace-id", type=int, required=True)
    result.add_argument("--role", choices=("member", "owner"), required=True)
    result.add_argument("--seat-intent", choices=("workspace_default", "standard", "premium"), default="workspace_default")
    result.add_argument("--email-line", default="", help="Resume an invited email; empty claims one HME alias")
    result.add_argument("--browser-executable", default="", help="Optional Chromium/Chromix executable, not its .cmd launcher")
    result.add_argument("--confirm", action="store_true", help="Allow HME reservation/local labeling, official invitation and signup")
    result.add_argument("--sms-stdin", action="store_true", help="Read one authorized number----HTTPS receipt URL from stdin; use only if OAuth requests SMS")
    return result


async def run(args) -> dict:
    from app.application.console_actions import start_workspace_onboard
    from app.application.reauth import load_cf_config
    from app.application.resources.hme import load_config
    from app.core.config import load_settings
    from app.integrations.mail.otp import parse_mail_line
    from app.persistence.database import create_engine, create_session_factory, sqlite_path_from_url
    from app.persistence.models.identity import Workspace

    settings = load_settings()
    from app.integrations.sms.client import parse_optional_sms

    phone_line = sys.stdin.readline(4096).strip() if args.sms_stdin else ""
    if args.sms_stdin and not phone_line:
        raise ValueError("SMS stdin was empty")
    parse_optional_sms(phone_line)
    db_path = sqlite_path_from_url(settings.database_url)
    if db_path is not None and not db_path.is_file():
        raise ValueError("Configured database does not exist; refusing to create a new database")
    executable = ""
    if args.browser_executable:
        path = Path(args.browser_executable).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() in {".cmd", ".bat"}:
            raise ValueError("Pass the browser executable, not a directory or shell launcher")
        executable = str(path)
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            workspace = await db.get(Workspace, args.workspace_id)
            if workspace is None or workspace.status != "active":
                raise ValueError("Workspace is missing or inactive")
            cf = await load_cf_config(db)
            if not parse_mail_line(args.email_line).get("pickup_url") and not cf["admin_password"]:
                raise ValueError("Configure Cloudflare mailbox before claiming HME or inviting")
            result = await start_workspace_onboard(
                db, args.workspace_id, email_line=args.email_line,
                role=args.role, seat_intent=args.seat_intent,
                oauth_signup=True, browser_executable=executable,
                phone_line=phone_line,
            )
            return {
                "ok": bool(result.get("ok")),
                "operation_id": result.get("operation_id"),
                "status": result.get("status"),
                "error_code": result.get("error_code"),
                "account_id": (result.get("child") or {}).get("id"),
                "email": (result.get("child") or {}).get("email"),
                "pushed": False,
            }
    finally:
        await engine.dispose()


def main() -> int:
    args = parser().parse_args()
    if not args.confirm:
        print("Not executed. Add --confirm to register via invitation and authorize one account. SMS requires --sms-stdin and a phone verification page. Sub2API push is disabled.")
        return 0
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        # External errors can contain proxy credentials or OAuth callback URLs.
        print(json.dumps({"ok": False, "error_code": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
