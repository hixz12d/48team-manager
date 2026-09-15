"""Read-only inventory for the explicitly selected Team48 workspace."""
from __future__ import annotations

import base64
from pathlib import Path
import re
import shlex

import paramiko


REMOTE = r'''
import asyncio, inspect, json, os, shutil
from sqlalchemy import select
from app.application.onboard import OnboardService
from app.core.config import load_settings
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account, Workspace
from app.application.tokens import decrypt_secret
from app.integrations.openai.chatgpt import chatgpt_client

async def main():
    settings = load_settings()
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            owner = await db.scalar(select(Account).where(Account.email == 'hixz2611@gmail.com'))
            if owner is None:
                print(json.dumps({'error': 'owner_not_found'})); return
            workspaces = list((await db.execute(select(Workspace).where(Workspace.owner_account_id == owner.id))).scalars())
            print(json.dumps({'owner_id': owner.id, 'owner_email': owner.email,
                'proxy_configured': bool(owner.proxy),
                'oauth_signup_supported': 'oauth_signup' in inspect.signature(OnboardService.invite_and_onboard).parameters,
                'browser_headless': settings.browser_headless, 'browser_channel': settings.browser_channel,
                'browsers': {name: shutil.which(name) for name in ('chromium', 'chromium-browser', 'google-chrome', 'chromix')}}, default=str))
            access = decrypt_secret(owner.access_token_encrypted)
            if not access:
                print(json.dumps({'error': 'owner_access_missing_no_refresh_attempted'})); return
            for ws in workspaces:
                print(json.dumps({'workspace_id': ws.id, 'name': ws.name, 'status': ws.status,
                    'official_workspace_id': ws.official_workspace_id, 'seat_limit': ws.seat_limit}))
                if ws.status != 'active': continue
                members = await chatgpt_client.get_members(access, ws.official_workspace_id, db, identifier=owner.email)
                invites = await chatgpt_client.get_invites(access, ws.official_workspace_id, db, identifier=owner.email)
                for kind, result, key in [('members', members, 'members'), ('invites', invites, 'items')]:
                    items = result.get(key) or []
                    safe = [{k: row.get(k) for k in ('id', 'email', 'email_address', 'role', 'seat_type', 'status', 'user_id')} for row in items if isinstance(row, dict)]
                    print(json.dumps({'kind': kind, 'workspace_id': ws.id, 'success': result.get('success'),
                        'status_code': result.get('status_code'), 'error_code': result.get('error_code'),
                        'total': result.get('total'), 'items': safe}))
            await db.rollback()
    finally:
        await engine.dispose()
asyncio.run(main())
'''


def connect():
    path = Path(__file__).resolve().parents[1] / "VPS.local.md"
    text = path.read_text(encoding="utf-8-sig")
    matches = re.findall(r"^\s*-?\s*(?:SSH\s+)?(?:password|\u5bc6\u7801|\u53e3\u4ee4)\s*[:\uff1a=]\s*`?([^`\r\n]+)", text, re.I | re.M)
    if len(matches) != 1:
        raise RuntimeError("Expected one labeled SSH password in VPS.local.md; no credential displayed")
    password = matches[0].strip()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.connect("156.238.254.8", username="root", password=password,
                   look_for_keys=False, allow_agent=False, timeout=15, auth_timeout=15)
    return client


def main():
    payload = base64.b64encode(REMOTE.encode()).decode()
    command = "docker exec -w /app team48-manager python -c " + shlex.quote(
        "import base64; exec(base64.b64decode(" + repr(payload) + "))")
    with connect() as client:
        _, stdout, stderr = client.exec_command(command, timeout=150)
        print(stdout.read().decode("utf-8", errors="replace"))
        error = stderr.read().decode("utf-8", errors="replace")
        if error:
            # Only expose error types, never remote request URLs or token strings.
            lines = [line for line in error.splitlines() if re.match(r"^[A-Za-z]+Error:", line)]
            print("remote_errors:", [line if line.startswith(("ImportError:", "ModuleNotFoundError:")) else line.split(":", 1)[0] for line in lines])
        print("exit_status:", stdout.channel.recv_exit_status())


if __name__ == "__main__":
    main()
