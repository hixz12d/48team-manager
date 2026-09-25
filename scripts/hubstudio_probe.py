"""Inspect an existing HubStudio browser; optionally smoke-test its native incognito UI.

This never launches another Chromium, creates a CDP browser context, reads cookies,
starts signup or closes existing windows. Pass a currently verified loopback CDP port.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit

import websockets

EXTENSION_NAME = 'Team48 ChatGPT 注册助手'


class CDP:
    def __init__(self, socket):
        self.socket = socket
        self.sequence = 0

    async def call(self, method, params=None):
        self.sequence += 1
        await self.socket.send(json.dumps({'id': self.sequence, 'method': method, 'params': params or {}}))
        while True:
            reply = json.loads(await asyncio.wait_for(self.socket.recv(), 10))
            if reply.get('id') != self.sequence:
                continue
            if 'error' in reply:
                raise RuntimeError('CDP request failed: ' + method)
            return reply['result']

    async def evaluate(self, expression):
        result = await self.call('Runtime.evaluate', {
            'expression': expression, 'returnByValue': True, 'awaitPromise': True})
        if result.get('exceptionDetails'):
            raise RuntimeError('Browser evaluation failed; private exception text omitted')
        return result.get('result', {}).get('value')


def read_endpoint(port, endpoint):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/json/{endpoint}', timeout=4) as response:
        return json.load(response)


def loopback_ws(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'ws' or parsed.hostname not in {'127.0.0.1', 'localhost'}:
        raise RuntimeError('Only local browser debugging endpoints are allowed')
    return url


IDENTITY = """(async()=>{
    const m=chrome.runtime.getManifest();
    if(m.name!==%s)return null;
    return {name:m.name,version:m.version,extensionId:chrome.runtime.id,
      inIncognito:chrome.extension.inIncognitoContext,
      incognitoAllowed:await chrome.extension.isAllowedIncognitoAccess(),
      jobActive:['running','paused'].includes((await chrome.storage.session.get('job')).job?.status),
      windows:(await chrome.windows.getAll()).map(w=>({id:w.id,incognito:w.incognito,focused:w.focused}))};
})()""" % json.dumps(EXTENSION_NAME)


async def inspect_browser(port, smoke):
    version = read_endpoint(port, 'version')
    targets = read_endpoint(port, 'list')
    found = []
    for target in targets:
        url = urlsplit(target.get('url', ''))
        if target['type'] != 'service_worker' or url.scheme != 'chrome-extension' or url.path != '/background.js':
            continue
        async with websockets.connect(loopback_ws(target['webSocketDebuggerUrl']), open_timeout=5) as ws:
            identity = await CDP(ws).evaluate(IDENTITY)
        if identity:
            found.append((target, identity))
    report = {'browser': version.get('Browser'), 'port': port, 'extensions': [identity for _, identity in found]}
    if not smoke:
        return report
    candidates = [(target, identity) for target, identity in found
                  if identity['inIncognito'] and identity['incognitoAllowed'] and not identity['jobActive']]
    if len(candidates) != 1:
        raise RuntimeError('Need exactly one idle, native incognito registration extension worker')
    target, identity = candidates[0]
    async with websockets.connect(loopback_ws(target['webSocketDebuggerUrl']), open_timeout=5) as ws:
        cdp = CDP(ws)
        current = await cdp.evaluate(IDENTITY)
        if current['jobActive'] or not current['inIncognito']:
            raise RuntimeError('Selected extension state changed')
        created = await cdp.evaluate("""(async()=>{
          const w=await chrome.windows.create({url:'about:blank',incognito:true,focused:false});
          return {id:w.id,incognito:w.incognito,tabId:w.tabs[0].id};
        })()""")
        report['temporary_window'] = {'native_incognito': created['incognito'], 'closed': False}
        try:
            if not created['incognito']:
                raise RuntimeError('Browser did not create a native incognito window')
            popup_url = f"chrome-extension://{identity['extensionId']}/popup.html"
            await cdp.evaluate(f"chrome.tabs.update({int(created['tabId'])},{{url:{json.dumps(popup_url)}}}).then(()=>true)")
            popup_target = None
            for _ in range(30):
                matches = [t for t in read_endpoint(port, 'list') if t['type'] == 'page' and t.get('url') == popup_url]
                # Refuse to inspect an existing popup if the target isn't unambiguous.
                if len(matches) == 1:
                    popup_target = matches[0]
                    break
                await asyncio.sleep(0.1)
            if popup_target is None:
                raise RuntimeError('Could not uniquely identify the temporary extension page')
            async with websockets.connect(loopback_ws(popup_target['webSocketDebuggerUrl']), open_timeout=5) as popup_ws:
                popup = CDP(popup_ws)
                ui = None
                for _ in range(30):
                    ui = await popup.evaluate("""(()=>({
                      loaded:document.readyState==='complete',
                      inIncognito:chrome.extension.inIncognitoContext,
                      version:chrome.runtime.getManifest().version,
                      emailInput:!!document.querySelector('input[type="email"]'),
                      buttons:[...document.querySelectorAll('button')].map(b=>({text:b.textContent.trim(),disabled:b.disabled}))
                    }))()""")
                    if ui['loaded']:
                        break
                    await asyncio.sleep(0.1)
                if not ui or not ui['loaded'] or not ui['inIncognito'] or not ui['emailInput']:
                    raise RuntimeError('Original extension UI did not load in incognito')
                report['popup'] = ui
            after = await cdp.evaluate(IDENTITY)
            if after['jobActive']:
                raise RuntimeError('An unexpected active signup task appeared')
            report['signup_started'] = False
            report['smoke_passed'] = True
        finally:
            await cdp.evaluate(f"chrome.windows.remove({int(created['id'])}).then(()=>true)")
            report['temporary_window']['closed'] = True
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Invalid loopback port')
    result = asyncio.run(inspect_browser(args.port, args.smoke))
    text = json.dumps(result, ensure_ascii=True, indent=2)
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text)
