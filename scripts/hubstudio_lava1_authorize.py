"""Persist the approved local signup and open one OAuth session in the same tab."""
import asyncio
import base64
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import websockets
from scripts.hubstudio_probe import CDP, read_endpoint
from scripts.team48_target_probe import connect
from scripts.team48_bybit2_remote import remote

PROJECT = Path(__file__).resolve().parents[1]
POINTER = PROJECT / 'dist/lava1-local-trial.json'
EMAIL = 'auroral.oeuvre-5s@icloud.com'


def state():
    value = json.loads(POINTER.read_text())
    assert value['email'] == EMAIL and value['workspace_id'] == 13
    return value


def page_target(s):
    assert len(s['openai_pages']) == 1
    return next(t for t in read_endpoint(s['port'], 'list') if t['id'] == s['openai_pages'][0]['target_id'])


async def collect(s):
    popup = next(t for t in read_endpoint(s['port'], 'list') if t['id'] == s['popup_target'])
    async with websockets.connect(popup['webSocketDebuggerUrl']) as ws:
        credentials = await CDP(ws).evaluate("""(async()=>{
          const j=(await chrome.storage.session.get('job')).job;
          if(j.email!=='auroral.oeuvre-5s@icloud.com'||j.status!=='done')throw new Error('Mismatch');
          return {email:j.email,password:j.password};
        })()""")
    target = page_target(s)
    assert urlsplit(target['url']).hostname == 'chatgpt.com'
    async with websockets.connect(target['webSocketDebuggerUrl']) as ws:
        session = await CDP(ws).evaluate("""(async()=>{
          const r=await fetch('/api/auth/session',{credentials:'include'});const s=await r.json();
          return {email:s.user?.email,access_token:s.accessToken};
        })()""")
    assert session['email'] == credentials['email'] and session['access_token']
    return {**credentials, **session}


PREPARE = r'''
import asyncio,json,logging
from pathlib import Path
logging.disable(logging.CRITICAL)
from app.application.onboard import OnboardService
from app.application.tokens import auth_service,encrypt_secret
from app.application.identity import ensure_membership
from app.application.workspaces import workspace_service
from app.application.operations import operation_store
from app.application.reauth import reauth_service
from app.core.config import load_settings
from app.core.time import utcnow
from app.persistence.database import create_engine,create_session_factory
from app.persistence.models.identity import Account
async def main():
 root=Path(TRIAL_ROOT);state=json.loads((root/'state.json').read_text())
 assert not (root/'oauth-private.json').exists(),'OAuth already allocated; do not repeat'
 secret=root/'web-credentials.json';creds=json.loads(secret.read_text())
 engine=create_engine(load_settings())
 try:
  async with create_session_factory(engine)() as db:
   child=await db.get(Account,state['account_id']);assert child.email==creds['email']=='auroral.oeuvre-5s@icloud.com'
   ws=await workspace_service.load_workspace(db,13)
   live,item=await workspace_service.lookup_live_member(db,ws,child.email)
   assert live.get('success') and item and item['status']=='joined' and item['seat_type']=='prolite' and item['role'] in ('owner','account-owner')
   await auth_service.apply_tokens(child,{'access_token':creds['access_token']})
   child.password_encrypted=encrypt_secret(creds['password']);child.auth_state='oauth_required';child.operational_state='active'
   await ensure_membership(db,workspace_id=13,account_id=child.id,official_role='owner',membership_state='joined',local_purpose='child',joined_at=utcnow())
   await operation_store.heartbeat_active(db,state['operation_id'],lease_seconds=1800)
   await db.commit()
   auth=await reauth_service.start_manual_reauth(db,child);assert auth['success']
   (root/'oauth-private.json').write_text(json.dumps({k:auth[k] for k in ['ticket','authorize_url','redirect_uri']}));(root/'oauth-private.json').chmod(0o600)
   state.update(state='oauth_ready',registered=True,joined=True,role='owner',seat_type='prolite')
   (root/'state.json').write_text(json.dumps(state));secret.unlink()
   print(json.dumps({**state,'oauth_created':True}))
 finally:await engine.dispose()
asyncio.run(main())
'''


def prepare(s):
    credentials = asyncio.run(collect(s))
    with connect() as client:
        with client.open_sftp() as ftp:
            path = s['host_root'] + '/web-credentials.json'
            # A prior transport failure left our own empty reserved file.
            assert ftp.stat(path).st_size == 0
            ftp.chmod(path, 0o600)
            with ftp.open(path, 'w') as stream:
                stream.write(json.dumps(credentials))
        remote(client, 'TRIAL_ROOT=' + repr(s['remote_root']) + '\n' + PREPARE, timeout=40)


async def navigate(s, url):
    target = page_target(s)
    assert urlsplit(target['url']).hostname == 'chatgpt.com'
    async with websockets.connect(target['webSocketDebuggerUrl']) as ws:
        cdp = CDP(ws)
        await cdp.call('Page.bringToFront')
        await cdp.call('Page.navigate', {'url': url})
    s['oauth_opened'] = True
    POINTER.write_text(json.dumps(s))
    print(json.dumps({'oauth_opened_same_tab': True}))


def open_oauth(s):
    assert not s.get('oauth_opened')
    with connect() as client:
        with client.open_sftp() as ftp:
            with ftp.open(s['host_root'] + '/oauth-private.json', 'r') as stream:
                auth = json.loads(stream.read())
    assert urlsplit(auth['authorize_url']).hostname == 'auth.openai.com'
    asyncio.run(navigate(s, auth['authorize_url']))


async def inspect(s):
    target = page_target(s)
    u = urlsplit(target['url'])
    async with websockets.connect(target['webSocketDebuggerUrl']) as ws:
        cdp = CDP(ws)
        info = await cdp.evaluate("""(()=>({
          ready:document.readyState,
          headings:[...document.querySelectorAll('h1,h2')].map(e=>e.innerText),
          buttons:[...document.querySelectorAll('button')].filter(e=>e.getClientRects().length).map(e=>({text:e.innerText,disabled:e.disabled})),
          fields:[...document.querySelectorAll('input')].filter(e=>e.getClientRects().length).map(e=>({name:e.name,type:e.type}))
        }))()""")
        screenshot = await cdp.call('Page.captureScreenshot', {'format': 'png'})
        (PROJECT / 'dist/lava1-oauth-page.png').write_bytes(base64.b64decode(screenshot['data']))
    proof = {'host': u.hostname, 'path': u.path, **info}
    (PROJECT / 'dist/lava1-oauth-observation.json').write_text(json.dumps(proof), encoding='utf-8')
    print(json.dumps(proof))


if __name__ == '__main__':
    s = state()
    if sys.argv[1] == 'prepare': prepare(s)
    elif sys.argv[1] == 'open': open_oauth(s)
    elif sys.argv[1] == 'status': asyncio.run(inspect(s))
    else: raise SystemExit('Expected prepare|open|status')
