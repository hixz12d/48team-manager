"""Read-only verification of the stopped single-account trial."""
from scripts.team48_stage_signup import run_remote
from scripts.team48_target_probe import connect

REMOTE = r'''
import asyncio, json, logging
from pathlib import Path
from urllib.parse import urlparse
from html.parser import HTMLParser
logging.disable(logging.CRITICAL)
from app.application.onboard import OnboardService
from app.application.resources import hme
from app.core.config import load_settings
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account
from app.persistence.models.oauth import OAuthSession
from app.persistence.models.resources import HmeAliasLease
from sqlalchemy import select

root = Path('/app/data/experiments/oauth-signup-hixz2611-20260910')
class Fields(HTMLParser):
    def __init__(self): super().__init__(); self.fields=[]; self.tags=[]; self.text=[]
    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'input':
            a=dict(attrs); self.fields.append({k:a.get(k) for k in ('type','name','placeholder')})
    def handle_endtag(self, tag):
        if tag in self.tags:
            self.tags = self.tags[:len(self.tags)-1-self.tags[::-1].index(tag)]
    def handle_data(self, data):
        if any(t in self.tags for t in ('script','style')): return
        if data.strip(): self.text.append(data.strip())
for meta in sorted((root / 'data/debug').glob('*/meta.txt'))[-1:]:
    lines=meta.read_text().splitlines()
    url=urlparse(lines[0]); print(json.dumps({'debug_host':url.hostname,'debug_path':url.path,'page_title':lines[1] if len(lines)>1 else ''}))
    html=meta.with_name('page.html')
    if html.is_file():
        fields=Fields(); fields.feed(html.read_text())
        matches=[s for s in fields.text if any(w in s.lower() for w in ('phone','sms','verify','verification','skip'))]
        print(json.dumps({'inputs':fields.fields,'verification_text':matches[:15]}))

async def main():
    engine=create_engine(load_settings())
    try:
        async with create_session_factory(engine)() as db:
            for email in ('hixz2611@gmail.com','adagio.funding1i@icloud.com','4-acetic.glyph@icloud.com'):
                account=await db.scalar(select(Account).where(Account.email==email))
                print(json.dumps({'email':email,'local_purpose':account.local_purpose if account else None,
                    'operational_state':account.operational_state if account else None,
                    'has_access_token':bool(account and account.access_token_encrypted),
                    'has_refresh_token':bool(account and account.refresh_token_encrypted),
                    'has_phone':bool(account and account.phone)}))
            session=await db.scalar(select(OAuthSession).where(OAuthSession.operation_id=='c32371f57801'))
            print(json.dumps({'oauth_session_status':session.status if session else None}))
            lease=await db.scalar(select(HmeAliasLease).where(HmeAliasLease.email=='4-acetic.glyph@icloud.com'))
            print(json.dumps({'lease_present':lease is not None,'label_sync_pending':bool(lease and lease.label_sync_pending)}))
            cfg=await hme.load_config(db)
            account=hme.resolve_account(hme.hme_client.list_accounts(cfg),cfg.account_id)
            page=hme.hme_client._request('GET',cfg,'/api/aliases',params={'account_id':str(account['id']),'q':'4-acetic.glyph@icloud.com'})
            aliases=page.get('aliases') or []
            match=[a for a in aliases if a.get('email')=='4-acetic.glyph@icloud.com']
            print(json.dumps({'hme_matches':[{'email':a.get('email'),'label':a.get('label'),'active':a.get('active')} for a in match]}))
    finally:
        await engine.dispose()
asyncio.run(main())
processes=[]
for p in Path('/proc').iterdir():
    if not p.name.isdigit(): continue
    try:
        command=(p/'cmdline').read_bytes()
        if str(root).encode() in command:
            processes.append({'pid':p.name,'executable':Path(command.split(b'\0')[0].decode()).name})
    except (OSError,UnicodeError): pass
print(json.dumps({'remaining_trial_processes':processes}))
'''

if __name__ == "__main__":
    with connect() as client:
        run_remote(client, REMOTE, timeout=120)
