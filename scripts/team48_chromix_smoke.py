"""Chromix launch/DOM/proxy smoke check with no account registration."""
from scripts.team48_stage_signup import CONTAINER_ROOT, run_remote
from scripts.team48_target_probe import connect

REMOTE = r'''
import asyncio, json, logging, sys
from pathlib import Path
root = Path('/app/data/experiments/oauth-signup-hixz2611-20260910')
sys.path.insert(0, str(root))
logging.disable(logging.CRITICAL)
from app.application.onboard import OnboardService
from app.core.config import load_settings
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account
from sqlalchemy import select
from app.integrations.openai.browser.onboard import chromium_context_kwargs, wait_cloudflare
from app.integrations.proxy.socks_bridge import chrome_proxy_launch
from playwright.sync_api import sync_playwright

async def get_proxy():
    engine = create_engine(load_settings())
    try:
        async with create_session_factory(engine)() as db:
            account = await db.scalar(select(Account).where(Account.email == 'hixz2611@gmail.com'))
            if account is None or not account.proxy: raise RuntimeError('Target proxy missing')
            return account.proxy
    finally:
        await engine.dispose()

proxy = asyncio.run(get_proxy())
try:
    with chrome_proxy_launch(proxy) as config, sync_playwright() as pw:
        kwargs = chromium_context_kwargs(root / 'smoke-profile', config)
        kwargs.pop('channel', None)
        kwargs['executable_path'] = str(root / 'browser/chromix/chrome')
        context = pw.chromium.launch_persistent_context(**kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content('<html><body><h1 id="smoke">Chromix smoke</h1><button onclick="this.textContent=\'clicked\'">Test</button></body></html>')
            page.get_by_role('button').click()
            assert page.get_by_role('button').inner_text() == 'clicked'
            print(json.dumps({'chromix_started': True, 'dom_interactive': True}), flush=True)
            from app.integrations.openai.chatgpt import chatgpt_client
            from app.integrations.openai.oauth_sessions import CLIENT_ID, REDIRECT_URI
            authorize = chatgpt_client.create_oauth_authorize_url(client_id=CLIENT_ID, redirect_uri=REDIRECT_URI)
            response = page.goto(authorize['authorize_url'], wait_until='domcontentloaded', timeout=60000)
            challenge_cleared = wait_cloudflare(page, timeout_sec=35)
            page.wait_for_timeout(2500)
            from urllib.parse import urlparse
            print(json.dumps({'auth_http_status': response.status if response else None,
                'auth_host': urlparse(page.url).hostname, 'challenge_cleared': challenge_cleared,
                'title': page.title(),
                'email_input_visible': page.locator('input[type="email"]').count() > 0}), flush=True)
            try:
                import os
                os.environ['PW_TEST_SCREENSHOT_NO_FONTS_READY'] = '1'
                page.screenshot(path=str(root / 'smoke-auth.png'), timeout=10000)
                print(json.dumps({'screenshot_saved': True}), flush=True)
            except Exception as exc:
                print(json.dumps({'screenshot_error': type(exc).__name__}), flush=True)
        finally:
            context.close()
except Exception as exc:
    print(json.dumps({'smoke_failed': type(exc).__name__, 'detail': str(exc).splitlines()[0][:160]}), flush=True)
    raise SystemExit(1)
'''

if __name__ == "__main__":
    with connect() as client:
        run_remote(client, REMOTE, timeout=150)
