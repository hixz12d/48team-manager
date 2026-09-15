"""Read-only preflight; does not claim aliases, launch a browser or change members."""
import base64
import shlex

from scripts.team48_target_probe import connect

REMOTE = r'''
import asyncio, hashlib, inspect, json
from pathlib import Path
from app.application.onboard import OnboardService
from app.application.reauth import load_cf_config
from app.application.resources import hme
from app.application.operations import operation_store
from app.core.config import load_settings
from app.persistence.database import create_engine, create_session_factory
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    executable = Path(pw.chromium.executable_path)
    print(json.dumps({'playwright_browser': str(executable), 'browser_exists': executable.is_file()}))
for path in ('app/application/onboard.py', 'app/application/console_actions.py', 'app/integrations/openai/browser/reauth.py'):
    text = Path('/app', path).read_text()
    print(json.dumps({'path': path, 'normalized_sha256': hashlib.sha256(text.encode()).hexdigest()}))

async def main():
    engine = create_engine(load_settings())
    try:
        async with create_session_factory(engine)() as db:
            cf = await load_cf_config(db)
            cfg = await hme.load_config(db)
            print(json.dumps({'cloudflare_configured': bool(cf['base_url'] and cf['address'] and cf['admin_password']), 'hme_configured': cfg.configured}))
            if cfg.configured:
                accounts = hme.hme_client.list_accounts(cfg)
                account = hme.resolve_account(accounts, cfg.account_id)
                aliases = hme.hme_client.list_aliases(cfg, str(account['id']))
                blocked = await hme.active_leased_emails(db) | await hme.occupied_account_emails(db)
                picked = hme.pick_next_unoccupied(aliases, blocked)
                print(json.dumps({'hme_inventory_readable': True, 'alias_count': len(aliases), 'unused_alias_available': picked is not None}))
            busy = await operation_store.browser_busy(db)
            print(json.dumps({'browser_busy': busy is not None, 'busy_operation': busy.public_id if busy else None}))
            await db.rollback()
    finally:
        await engine.dispose()
asyncio.run(main())
'''


def main():
    command = "docker exec -w /app team48-manager python -c " + shlex.quote(
        "import base64; exec(base64.b64decode(" + repr(base64.b64encode(REMOTE.encode()).decode()) + "))")
    with connect() as client:
        _, out, err = client.exec_command(command, timeout=150)
        print(out.read().decode("utf-8", errors="replace"))
        errors = err.read().decode("utf-8", errors="replace")
        print("stderr_present:", bool(errors))
        print("exit_status:", out.channel.recv_exit_status())


if __name__ == "__main__":
    main()
