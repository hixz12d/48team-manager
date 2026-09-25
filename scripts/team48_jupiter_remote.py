"""Transport for the approved Jupiter 1 isolated replacement trial."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tarfile

from scripts.team48_target_probe import connect
from scripts import team48_bybit2_remote as transport

PROJECT = Path(__file__).resolve().parents[1]
POINTER = PROJECT / 'dist/jupiter-experiment.json'
transport.POINTER = POINTER
remote = transport.remote


def stage(client):
    if POINTER.exists():
        raise SystemExit('Existing Jupiter experiment; inspect it instead of starting another')
    name = 'managed-signup-jupiter-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    root = '/app/data/experiments/' + name
    host = '/opt/team48/data/experiments/' + name
    paths = sorted(p for p in (PROJECT / 'app').rglob('*') if p.is_file() and p.suffix in {'.py', '.js'} and '__pycache__' not in p.parts)
    paths += [PROJECT / p for p in ('extensions/chatgpt-signup/content.js', 'extensions/chatgpt-signup/manifest.json', 'scripts/team48_jupiter_trial.py')]
    archive = PROJECT / 'dist/jupiter-code.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        for path in paths:
            output.add(path, arcname=path.relative_to(PROJECT).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    remote(client, f"from pathlib import Path\nimport os\nos.umask(0o077)\nPath({root!r}).mkdir(mode=0o700,exist_ok=False)")
    # Persist the pointer immediately so an interrupted upload cannot allocate a second trial.
    POINTER.write_text(json.dumps({'container_root': root, 'host_root': host, 'code_sha256': digest}), encoding='utf-8')
    with client.open_sftp() as sftp:
        sftp.put(str(archive), host + '/code.tar.gz')
        sftp.chmod(host + '/code.tar.gz', 0o600)
    remote(client, f'''
import hashlib,json,os,tarfile,sqlite3,zipfile,py_compile
from pathlib import Path
os.umask(0o077)
root=Path({root!r})
archive=root/'code.tar.gz'
assert hashlib.sha256(archive.read_bytes()).hexdigest()=={digest!r}
with tarfile.open(archive) as bundle:
 for item in bundle.getmembers():
  assert item.isfile() and not Path(item.name).is_absolute() and '..' not in Path(item.name).parts
 bundle.extractall(root,filter='data')
old=Path('/app/data/experiments/oauth-signup-hixz2611-20260910')
with (old/'chromix-linux-x64.zip').open('rb') as stream:
 assert hashlib.file_digest(stream,'sha256').hexdigest()=='85593ab5bedbdceee2196f2d1e9c603a09cf18f5dfb9bcc949c4232e7b35295f'
with zipfile.ZipFile(old/'chromix-linux-x64.zip') as bundle:
 executable_hash=hashlib.sha256(bundle.read('chromix/chrome')).hexdigest()
with Path({transport.BROWSER!r}).open('rb') as stream:
 assert hashlib.file_digest(stream,'sha256').hexdigest()==executable_hash
for relative in ['scripts/team48_jupiter_trial.py','app/integrations/openai/browser/signup.py','app/integrations/openai/browser/signup_readiness.py']:
 py_compile.compile(str(root/relative),doraise=True)
with sqlite3.connect('file:/app/data/team48.db?mode=ro',uri=True) as live,sqlite3.connect(root/'before-team48.db') as backup:
 live.backup(backup)
print(json.dumps({{'code_verified':True,'browser_verified':True,'backup_created':True,'file_count':{len(paths)},'root':str(root)}}))
''')
    archive.unlink()


SMOKE = r'''
import asyncio,json,logging
from pathlib import Path
from unittest.mock import patch
logging.disable(logging.CRITICAL)
from app.application.onboard import OnboardService
from sqlalchemy import select
from app.core.config import load_settings
from app.persistence.database import create_engine,create_session_factory
from app.persistence.models.identity import Account
from app.application.reauth import load_cf_config
from app.integrations.mail.otp import list_mailbox_codes
from app.integrations.openai.browser.onboard import chromium_context_kwargs
from app.integrations.openai.browser.signup import run_managed_signup
from app.integrations.proxy.socks_bridge import chrome_proxy_launch
from playwright.sync_api import sync_playwright
async def config():
 engine=create_engine(load_settings())
 try:
  async with create_session_factory(engine)() as db:
   owner=await db.scalar(select(Account).where(Account.email=='hixz2611@gmail.com'))
   cf=await load_cf_config(db)
   assert owner and owner.proxy and all(cf.values())
   proxy=owner.proxy
   await db.rollback()
   return proxy,cf
 finally:await engine.dispose()
proxy,cf=asyncio.run(config())
list_mailbox_codes(email='muckier_oak7w@icloud.com',proxy=proxy,cf_base_url=cf['base_url'],cf_address=cf['address'],cf_admin_password=cf['admin_password'])
with chrome_proxy_launch(proxy) as proxy_config,sync_playwright() as pw:
 options=chromium_context_kwargs(Path.cwd()/'smoke-profile',proxy_config)
 browser=pw.chromium.launch_persistent_context(**options)
 try:
  page=browser.pages[0] if browser.pages else browser.new_page()
  def route(request):
   if request.request.url.endswith('/api/auth/session'):
    request.fulfill(json={'accessToken':'fixture-token','user':{'email':'fixture@example.invalid'}})
   else:request.fulfill(content_type='text/html',body='<div id="prompt-textarea" contenteditable="true"></div>')
  browser.route('**/*',route)
  with patch('app.integrations.mail.otp.list_mailbox_codes',return_value=[]):
   result=run_managed_signup(browser=browser,page=page,email='fixture@example.invalid',password='FixturePassword123!',profile_dir=Path.cwd()/'smoke-profile',start_url='https://chatgpt.com/',invite_entry=False,team_name='',mail_kwargs={},report=lambda *_:None)
  assert result['ok'],result.get('error_code')
  assert result['signup_diagnostics']['readiness']['session_confirmations']>=2
  assert result['signup_diagnostics']['readiness']['stable_ms']>=2000
  assert page.locator('#team48-signup-progress').count()==0
  browser.unroute('**/*')
  response=page.goto('https://auth.openai.com/log-in',wait_until='domcontentloaded',timeout=45000)
  assert response and response.status<400
  proof={'ok':True,'mailbox_readable':True,'auth_http_status':response.status,'headless':options['headless'],'readiness':result['signup_diagnostics']}
  (Path.cwd()/'smoke-passed.json').write_text(json.dumps(proof))
  print(json.dumps(proof),flush=True)
 finally:browser.close()
'''


def launch(client, *, smoke=False):
    source = repr(SMOKE) if smoke else "None"
    remote(client, f'''
import json,os,subprocess
from pathlib import Path
root=Path.cwd()
os.umask(0o077)
smoke={smoke!r}
if not smoke:
 assert json.loads((root/'smoke-passed.json').read_text())['ok']
 assert not (root/'trial-started.lock').exists()
name='smoke' if smoke else 'trial'
args=['python','-c',{source}] if smoke else ['python',str(root/'scripts/team48_jupiter_trial.py'),'--confirm-once']
with (root/(name+'.log')).open('xb') as log:
 worker=subprocess.Popen(args,cwd=root,env=os.environ.copy(),stdout=log,stderr=log,start_new_session=True)
(root/(name+'.pid')).write_text(str(worker.pid))
print(json.dumps({{'started':name,'pid':worker.pid}}))
''', staged=True)


def status(client):
    remote(client, r'''
import json
from pathlib import Path
root=Path.cwd()
state=root/'trial-state.json'
print(state.read_text() if state.exists() else json.dumps({'state':'not_started'}))
pid=root/'trial.pid'
process=Path('/proc')/pid.read_text().strip() if pid.exists() else None
print(json.dumps({'worker_alive':bool(process and process.exists() and (process/'stat').read_text().split()[2]!='Z')}))
''', staged=True)


if __name__ == '__main__':
    with connect() as client:
        action = sys.argv[1]
        if action == 'stage': stage(client)
        elif action == 'smoke': launch(client, smoke=True)
        elif action == 'start': launch(client)
        elif action == 'status': status(client)
        else: raise SystemExit('Expected stage|smoke|start|status')
