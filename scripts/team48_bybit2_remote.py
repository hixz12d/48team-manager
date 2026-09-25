"""SSH transport for the explicitly authorized isolated Bybit2 test.

Credentials are loaded privately via team48_target_probe.connect; no production
application files or Compose services are replaced.
"""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tarfile

from scripts.team48_target_probe import connect

PROJECT = Path(__file__).resolve().parents[1]
POINTER = PROJECT / 'dist' / 'bybit2-experiment.json'
BROWSER = '/app/data/experiments/oauth-signup-hixz2611-20260910/browser/chromix/chrome'


def remote(client, source, *, staged=False, timeout=50):
    args = ['docker', 'exec']
    if staged:
        root = json.loads(POINTER.read_text())['container_root']
        args += ['-e', 'BROWSER_SIGNUP_FLOW=extension', '-e', 'BROWSER_ENGINE=chromix',
                 '-e', 'BROWSER_EXECUTABLE=' + BROWSER, '-e', 'PYTHONPATH=' + root, '-w', root]
    else:
        args += ['-w', '/app']
    args += ['team48-manager', 'python', '-c', 'import base64;exec(base64.b64decode(' + repr(base64.b64encode(source.encode()).decode()) + '))']
    _, stdout, stderr = client.exec_command(shlex.join(args), timeout=timeout)
    output = stdout.read().decode('utf-8', errors='replace')
    errors = stderr.read().decode('utf-8', errors='replace')
    code = stdout.channel.recv_exit_status()
    print(output, end='')
    if code:
        print(json.dumps({'remote_exit': code, 'error_types': [line.split(':')[0] for line in errors.splitlines() if line.split(':')[0].endswith(('Error', 'Exception'))]}))
        raise SystemExit(code)


def stage(client):
    if POINTER.exists():
        raise SystemExit('Stage pointer exists; inspect it instead of allocating a second experiment')
    name = 'managed-signup-bybit2-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    container_root = '/app/data/experiments/' + name
    host_root = '/opt/team48/data/experiments/' + name
    paths = sorted([p for p in (PROJECT / 'app').rglob('*') if p.is_file() and p.suffix in {'.py', '.js'} and '__pycache__' not in p.parts])
    paths += [PROJECT / name for name in ['extensions/chatgpt-signup/content.js', 'extensions/chatgpt-signup/manifest.json', 'scripts/team48_bybit2_trial.py']]
    archive = PROJECT / 'dist' / 'bybit2-code.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        for path in paths:
            output.add(path, arcname=path.relative_to(PROJECT).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    remote(client, f"from pathlib import Path\nimport os,json\nos.umask(0o077)\nPath({container_root!r}).mkdir(mode=0o700,exist_ok=False)\nprint(json.dumps({{'created':{container_root!r}}}))")
    with client.open_sftp() as sftp:
        sftp.put(str(archive), host_root + '/code.tar.gz')
        sftp.chmod(host_root + '/code.tar.gz', 0o600)
    source = f'''
import hashlib,json,os,tarfile,sqlite3,zipfile
from pathlib import Path
os.umask(0o077)
root=Path({container_root!r})
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
with Path({BROWSER!r}).open('rb') as stream:
    assert hashlib.file_digest(stream,'sha256').hexdigest()==executable_hash
with sqlite3.connect('file:/app/data/team48.db?mode=ro',uri=True) as live, sqlite3.connect(root/'before-team48.db') as backup:
    live.backup(backup)
print(json.dumps({{'code_verified':True,'browser_verified':True,'backup_created':True,'file_count':{len(paths)}}}))
'''
    remote(client, source)
    POINTER.write_text(json.dumps({'container_root': container_root, 'host_root': host_root, 'code_sha256': digest}), encoding='utf-8')
    archive.unlink()


SMOKE = r'''
import asyncio,json,logging,time
from pathlib import Path
logging.disable(logging.CRITICAL)
from sqlalchemy import select
from app.application.onboard import OnboardService
from app.core.config import load_settings
from app.persistence.database import create_engine,create_session_factory
from app.persistence.models.identity import Account
from app.application.reauth import load_cf_config
from app.integrations.mail.otp import list_mailbox_codes
from app.integrations.openai.browser.onboard import chromium_context_kwargs
from app.integrations.openai.browser.signup import SignupBridge,signup_assets
from app.integrations.openai.browser.signup_state import SignupState
from app.integrations.proxy.socks_bridge import chrome_proxy_launch
from playwright.sync_api import sync_playwright
async def config():
    engine=create_engine(load_settings())
    try:
        async with create_session_factory(engine)() as db:
            owner=await db.scalar(select(Account).where(Account.email=='hixz2616@gmail.com'))
            cf=await load_cf_config(db)
            assert owner and owner.proxy and all(cf.values())
            proxy = owner.proxy
            await db.rollback()
            return proxy,cf
    finally: await engine.dispose()
proxy,cf=asyncio.run(config())
list_mailbox_codes(email='are.tapirs_1u@icloud.com',proxy=proxy,cf_base_url=cf['base_url'],cf_address=cf['address'],cf_admin_password=cf['admin_password'])
with chrome_proxy_launch(proxy) as proxy_config, sync_playwright() as pw:
    options=chromium_context_kwargs(Path.cwd()/'smoke-profile',proxy_config)
    browser=pw.chromium.launch_persistent_context(**options)
    try:
        page=browser.pages[0] if browser.pages else browser.new_page()
        page.bring_to_front()
        content,version=signup_assets()
        state=SignupState(email='fixture@example.invalid',password='FixturePassword123!',profile={'name':'Test User','birthday':'1996-01-01'},version=version,read_codes=lambda:[])
        bridge=SignupBridge(page,state,content)
        page.route('**/*',lambda req:req.fulfill(content_type='text/html',body='<form onsubmit="event.preventDefault();window.submitted=true"><input type="email" name="email"><button>Continue</button></form>'))
        page.goto('https://auth.openai.com/create-account')
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and state.status=='running':
            page.wait_for_timeout(50);bridge.pump()
            if page.evaluate('!!window.submitted'):break
        assert page.evaluate('!!window.submitted'),'isolated_world_did_not_submit'
        assert page.locator('input').input_value()=='fixture@example.invalid'
        assert page.evaluate('name=>typeof globalThis[name]',bridge.binding)=='undefined'
        bridge.close()
        assert not page.locator('#team48-signup-progress').count()
        page.unroute('**/*')
        response=page.goto('https://auth.openai.com/log-in',wait_until='domcontentloaded',timeout=45000)
        print(json.dumps({'chromix_dom_bridge':True,'mailbox_readable':True,'signup_flow':load_settings().browser_signup_flow,'auth_http_status':response.status if response else None,'headless':options['headless']}),flush=True)
        assert response and response.status<400
        (Path.cwd()/'smoke-passed.json').write_text(json.dumps({'ok':True}),encoding='utf-8')
    finally:browser.close()
'''


def start(client):
    remote(client, r'''
import json,os,subprocess
from pathlib import Path
root=Path.cwd()
assert json.loads((root/'smoke-passed.json').read_text())['ok']
assert not (root/'trial-started.lock').exists()
os.umask(0o077)
with (root/'trial.log').open('xb') as log:
    worker=subprocess.Popen(['python',str(root/'scripts/team48_bybit2_trial.py'),'--confirm-once'],cwd=root,env=os.environ.copy(),stdout=log,stderr=log,start_new_session=True)
(root/'worker.pid').write_text(str(worker.pid))
print(json.dumps({'worker_started':True,'pid':worker.pid}))
''', staged=True)


def status(client):
    remote(client, r'''
import json
from pathlib import Path
root=Path.cwd()
state=root/'trial-state.json'
print(state.read_text() if state.exists() else json.dumps({'state':'not_started'}))
pid=root/'worker.pid'
print(json.dumps({'worker_alive':Path('/proc',pid.read_text().strip()).exists() if pid.exists() else False}))
''', staged=True)


if __name__ == '__main__':
    with connect() as client:
        action = sys.argv[1]
        if action == 'stage': stage(client)
        elif action == 'smoke': remote(client, SMOKE, staged=True, timeout=55)
        elif action == 'start': start(client)
        elif action == 'status': status(client)
        else: raise SystemExit('Expected stage|smoke|start|status')
