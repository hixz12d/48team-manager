"""Control the approved Lava1 trial through the installed extension UI."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import websockets
from scripts.hubstudio_probe import CDP, read_endpoint

ROOT = Path(__file__).resolve().parents[1]
POINTER = ROOT / 'dist/lava1-local-trial.json'
EMAIL = 'auroral.oeuvre-5s@icloud.com'


async def click(cdp, selector):
    point = await cdp.evaluate("""(()=>{
      const e=document.querySelector(%s);if(!e||e.disabled)return null;
      const r=e.getBoundingClientRect();return r.width&&r.height?{x:r.x+r.width/2,y:r.y+r.height/2}:null;
    })()""" % json.dumps(selector))
    if not point:
        raise RuntimeError('Requested original UI control is not available')
    await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', **point})
    for kind in ['mousePressed', 'mouseReleased']:
        await cdp.call('Input.dispatchMouseEvent', {'type': kind, 'button': 'left', 'clickCount': 1, **point})


async def run(action, seconds):
    state = json.loads(POINTER.read_text())
    assert state['email'] == EMAIL and state['workspace_id'] == 13
    port = state['port']
    targets = read_endpoint(port, 'list')
    popup = next(t for t in targets if t['id'] == state['popup_target'])
    async with websockets.connect(popup['webSocketDebuggerUrl']) as ws:
        cdp = CDP(ws)
        identity = await cdp.evaluate("""(async()=>{
          const t=await chrome.tabs.getCurrent(),m=chrome.runtime.getManifest();
          return {incognito:chrome.extension.inIncognitoContext,windowId:t.windowId,version:m.version};
        })()""")
        assert identity['incognito'] and identity['windowId'] == state['window']['id'] and identity['version'] == '0.3.6'
        if action == 'start':
            with (ROOT / 'dist/lava1-local-started.lock').open('x') as marker:
                marker.write(str(time.time()))
            job_present = await cdp.evaluate("(async()=>!!(await chrome.storage.session.get('job')).job)()")
            assert not job_present
            await cdp.call('Page.bringToFront')
            await cdp.evaluate("document.getElementById('email').focus()")
            await cdp.call('Input.insertText', {'text': EMAIL})
            valid = await cdp.evaluate("document.getElementById('email').value==="+json.dumps(EMAIL)+"&&document.getElementById('workflow').value==='auto'&&!document.getElementById('start').disabled")
            assert valid
            await click(cdp, '#start')
            state['start_clicked_at'] = time.time()
            POINTER.write_text(json.dumps(state))
        deadline = time.monotonic() + seconds
        while True:
            job = await cdp.evaluate("""(async()=>{
              const j=(await chrome.storage.session.get('job')).job;if(!j)return null;
              const {diagnosticReport}=await import(chrome.runtime.getURL('shared.mjs'));
              return {email:j.email,tabId:j.tabId,status:j.status,stage:j.stage,diagnostics:diagnosticReport(j,chrome.runtime.getManifest().version)};
            })()""")
            if job:
                assert job['email'] == EMAIL
                state['job'] = job
                tabs = (await cdp.call('Target.getTargets'))['targetInfos']
                context_id = next(t['browserContextId'] for t in tabs if t['targetId'] == state['popup_target'])
                # The native extension-created registration tab is the only OpenAI page in this fresh context.
                infos = []
                for tab in tabs:
                    u = urlsplit(tab.get('url', ''))
                    if tab['type'] == 'page' and tab.get('browserContextId') == context_id and u.hostname in {'chatgpt.com', 'auth.openai.com', 'auth0.openai.com'}:
                        infos.append({'target_id': tab['targetId'], 'host': u.hostname, 'path': u.path})
                state['openai_pages'] = infos
                POINTER.write_text(json.dumps(state))
            if time.monotonic() >= deadline or (job and job['status'] in {'paused', 'stopped', 'done'}):
                break
            await asyncio.sleep(1)
        print(json.dumps({'action': action, 'job': job, 'openai_pages': state.get('openai_pages', [])}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'status'])
    parser.add_argument('--seconds', type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run(args.action, min(30, max(0, args.seconds))))
