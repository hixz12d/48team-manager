"""Observe a chosen HubStudio CDP browser; never operate its accounts or pages.

Run from the project Python environment. Output is deliberately lossy and contains
only allowlisted metadata. No HAR, request bodies, response bodies or credentials.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import websockets
from scripts.hubstudio_probe import loopback_ws, read_endpoint

HOSTS = {'chatgpt.com', 'auth.openai.com', 'auth0.openai.com'}
EXTENSION = 'nigimejknaadfcbpegojdnajfpomegcg'
STAGES = {'prepare', 'remove', 'invite', 'register', 'oauth', 'phone', 'done'}
PAGES = {'phone', 'consent', 'otp', 'profile', 'password', 'members', 'signup', 'login', 'home', 'other'}
FIELDS = {'none', 'email', 'password', 'phone', 'otp', 'profile', 'other'}
LABELS = set('continue login signup finish_signup remove delete invite send_invites confirm accept join allow authorize verify resend owner member premium standard skip later something_else continue_workspace cancel back start email_login other'.split())
PLUGIN_EVENTS = set('started page filled submit_attempt form_submit manual_submit waiting_manual code_received paused resumed stopped expired session_check session_error email_mismatch email_unverified completed captcha phone rate_limit entry_fallback click_response continue_retry'.split())
PLUGIN_PAGES = set('chatgpt signup password email_verification profile phone consent auth_other unknown'.split())
PLUGIN_STAGES = set('signup email password otp profile home unknown'.split())
OUTCOMES = {'submitted', 'loading', 'advanced', 'validation', 'timeout'}
BINDING = '__team48FlowEvent'
SCRIPT = Path(__file__).with_name('hubstudio_flow_observer.js').read_text(encoding='utf-8')
ROUTES = [
    (r'/backend-api/accounts/[^/]+/users/[^/]+/?', 'member'),
    (r'/backend-api/accounts/[^/]+/users/?', 'members'),
    (r'/backend-api/accounts/[^/]+/invites(?:/[^/]+)?/?', 'invites'),
    (r'/admin/members/?', 'members_page'),
    (r'/api/auth/session/?', 'web_session'),
    (r'/auth/login/?', 'login_entry'),
    (r'/(?:oauth/)?authorize/?', 'oauth_authorize'),
    (r'/oauth/token/?', 'oauth_token'),
    (r'/(?:api/accounts/)?(?:add-phone|phone-verification|verify-phone)(?:/.*)?', 'phone'),
    (r'/(?:api/accounts/)?(?:email-verification|email-otp)(?:/.*)?', 'email_verification'),
    (r'/(?:api/accounts/)?(?:create-account|sign-up|user/register)(?:/.*)?', 'signup'),
    (r'/(?:api/accounts/)?(?:about-you|password|log-in/password)(?:/.*)?', 'profile_or_password'),
    (r'/(?:api/accounts/)?(?:consent|consent/accept)(?:/.*)?', 'consent'),
    (r'/(?:api/accounts/)?(?:login|log-in|continue)(?:/.*)?', 'login'),
    (r'/', 'home'),
]


def route(value):
    """Return fixed categories, never a reconstructed URL or untrusted path."""
    try:
        u = urlsplit(value)
        if u.scheme == 'http' and u.hostname in {'localhost', '127.0.0.1'} and u.path == '/auth/callback':
            return {'host': 'loopback', 'route': 'oauth_callback'}
        if u.scheme != 'https' or u.hostname not in HOSTS:
            return None
        name = next((name for pattern, name in ROUTES if re.fullmatch(pattern, u.path)), 'other')
        return {'host': u.hostname, 'route': name}
    except (TypeError, ValueError):
        return None


def dom_event(payload):
    if not isinstance(payload, dict) or payload.get('kind') not in {'click', 'focus', 'submit', 'snapshot'}:
        return None
    result = {'kind': payload['kind'], 'page': payload.get('page') if payload.get('page') in PAGES else 'other'}
    for key, choices in [('field', FIELDS), ('label', LABELS)]:
        if payload.get(key) in choices:
            result[key] = payload[key]
    for key in ('trusted', 'composer', 'dialog', 'phone'):
        if type(payload.get(key)) is bool:
            result[key] = payload[key]
    if isinstance(payload.get('fields'), list):
        result['fields'] = sorted({v for v in payload['fields'] if isinstance(v, str) and v in FIELDS})
    return result


def plugin_event(value):
    if not isinstance(value, dict) or value.get('event') not in PLUGIN_EVENTS:
        return None
    result = {'event': value['event']}
    if type(value.get('ms')) in (float, int) and 0 <= value['ms'] <= 86400000:
        result['ms'] = int(value['ms'])
    for key, choices in [('page', PLUGIN_PAGES), ('stage', PLUGIN_STAGES), ('outcome', OUTCOMES)]:
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    if type(value.get('verified')) is bool:
        result['verified'] = value['verified']
    return result


PLUGIN_PROBE = """(async()=>{
  const m=chrome.runtime.getManifest();if(m.name!=='Team48 ChatGPT 注册助手')return null;
  const j=(await chrome.storage.session.get('job')).job;
  return {version:m.version,incognito:chrome.extension.inIncognitoContext,
    status:j?.status||'idle',stage:j?.stage||'unknown',mode:j?.mode||'unknown',
    startedAt:j?.startedAt||0,truncated:!!j?.eventsTruncated,
    events:(j?.events||[]).map(e=>({ms:e.ms,event:e.event,page:e.page,stage:e.stage,verified:e.verified,outcome:e.outcome}))};
})()"""


class ProtocolError(Exception):
    pass


class Session:
    def __init__(self, socket, handler):
        self.socket, self.handler = socket, handler
        self.pending, self.seq = {}, 0
        self.reader = asyncio.create_task(self.receive())

    async def receive(self):
        try:
            async for raw in self.socket:
                message = json.loads(raw)
                if 'id' in message:
                    future = self.pending.get(message['id'])
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(ProtocolError('CDP command rejected'))
                        else:
                            future.set_result(message.get('result', {}))
                elif 'method' in message:
                    self.handler(message['method'], message.get('params', {}))
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ProtocolError('CDP connection ended'))

    async def call(self, method, params=None):
        self.seq += 1
        number = self.seq
        future = self.pending[number] = asyncio.get_running_loop().create_future()
        try:
            await self.socket.send(json.dumps({'id': number, 'method': method, 'params': params or {}}))
            return await asyncio.wait_for(future, 6)
        finally:
            self.pending.pop(number, None)

    async def evaluate(self, expression):
        result = await self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
        if result.get('exceptionDetails'):
            raise ProtocolError('Page evaluation unavailable')
        return result.get('result', {}).get('value')

    async def close(self):
        await self.socket.close()
        await asyncio.gather(self.reader, return_exceptions=True)


class Recorder:
    def __init__(self, folder, dom=True):
        folder.mkdir(parents=True, exist_ok=False)
        self.folder, self.dom = folder, dom
        self.stream = (folder / 'timeline.jsonl').open('x', encoding='utf-8')
        self.started = time.monotonic()
        self.stage, self.contexts, self.counts = 'prepare', {}, Counter()
        self.stop = asyncio.Event()
        self.errors = 0

    def write(self, event, target='recorder', **data):
        self.counts[event] += 1
        entry = {'at': datetime.now(timezone.utc).isoformat(), 'ms': round((time.monotonic()-self.started)*1000),
                 'stage': self.stage, 'target': target, 'event': event, **data}
        self.stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
        self.stream.flush()

    async def watch(self, port, target, label):
        cdp, script_id = None, None
        current_url = target.get('url', '')
        requests = {}
        worker = target['type'] == 'service_worker'
        request_sequence = 0

        def handle(method, params):
            nonlocal current_url, request_sequence
            if method == 'Page.frameNavigated' and not params.get('frame', {}).get('parentId'):
                current_url = params['frame'].get('url', '')
                if safe := route(current_url):
                    self.write('navigation', label, **safe)
            elif method == 'Runtime.bindingCalled' and params.get('name') == BINDING and route(current_url):
                try:
                    raw = params.get('payload', '')
                    safe = dom_event(json.loads(raw)) if len(raw) < 4096 else None
                    if safe:
                        self.write('ui', label, **safe)
                except (ValueError, TypeError):
                    pass
            elif method == 'Network.requestWillBeSent':
                request = params.get('request', {})
                safe = route(request.get('url', ''))
                rid = params.get('requestId')
                previous = requests.pop(rid, None)
                if previous and params.get('redirectResponse'):
                    status = params['redirectResponse'].get('status')
                    if type(status) in (int, float):
                        self.write('redirect', label, **previous[0], status=int(status))
                # Whitelist navigation + account management/auth calls, not unrelated resources/chat.
                if safe and (safe['route'] != 'other' or params.get('type') == 'Document'):
                    verb = request.get('method')
                    request_sequence += 1
                    safe = {**safe, 'request': request_sequence,
                            'method': verb if verb in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'} else 'OTHER'}
                    requests[rid] = (safe, time.monotonic())
                    self.write('request', label, **safe)
                    if len(requests) > 2000:
                        requests.pop(next(iter(requests)))
            elif method == 'Network.responseReceived' and params.get('requestId') in requests:
                safe, start = requests[params['requestId']]
                status = params.get('response', {}).get('status')
                if type(status) in (int, float):
                    self.write('response', label, **safe, status=int(status), elapsed_ms=round((time.monotonic()-start)*1000))
            elif method in {'Network.loadingFailed', 'Network.loadingFinished'}:
                previous = requests.pop(params.get('requestId'), None)
                if previous and method.endswith('Failed'):
                    self.write('network_failed', label, **previous[0], cancelled=params.get('canceled') is True)

        try:
            socket = await websockets.connect(loopback_ws(target['webSocketDebuggerUrl']), open_timeout=5, max_size=16*1024*1024)
            cdp = Session(socket, handle)
            info = (await cdp.call('Target.getTargetInfo', {'targetId': target['id']}))['targetInfo']
            context_key = (port, info.get('browserContextId', 'default'))
            context = self.contexts.setdefault(context_key, 'context-' + str(len(self.contexts)+1))
            self.write('attached', label, port=port, context=context, kind='extension' if worker else 'page')
            await cdp.call('Runtime.enable')
            if not worker:
                await cdp.call('Network.enable', {'maxPostDataSize': 0})
                await cdp.call('Page.enable')
                frame = (await cdp.call('Page.getFrameTree'))['frameTree']['frame']
                current_url = frame.get('url', current_url)
                if safe := route(current_url):
                    self.write('navigation', label, **safe)
                if self.dom:
                    await cdp.call('Runtime.addBinding', {'name': BINDING})
                    script_id = (await cdp.call('Page.addScriptToEvaluateOnNewDocument', {'source': SCRIPT}))['identifier']
                    await cdp.evaluate(SCRIPT)
            last_plugin, seen = None, set()
            while not self.stop.is_set() and not cdp.reader.done():
                if worker:
                    value = await cdp.evaluate(PLUGIN_PROBE)
                    if value:
                        version = value.get('version', '')
                        head = {k: value[k] for k, choices in [('status', {'idle', 'running', 'paused', 'done', 'stopped'}),
                            ('stage', PLUGIN_STAGES), ('mode', {'auto', 'review', 'submit', 'manual', 'unknown'})]
                            if value.get(k) in choices}
                        if 'stage' in head:
                            head['plugin_stage'] = head.pop('stage')
                        if isinstance(version, str) and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}', version):
                            head['version'] = version
                        head['incognito'] = value.get('incognito') is True
                        head['truncated'] = value.get('truncated') is True
                        started = value.get('startedAt', 0)
                        if type(started) in (int, float) and 0 <= started < 1e14:
                            head['run_started_epoch_ms'] = int(started)
                        if head != last_plugin:
                            self.write('plugin_state', label, **head)
                            if last_plugin and head.get('run_started_epoch_ms') != last_plugin.get('run_started_epoch_ms'):
                                seen.clear()
                            last_plugin = head
                        for raw in value.get('events', [])[-200:]:
                            event = plugin_event(raw)
                            if event and (key := json.dumps(event, sort_keys=True)) not in seen:
                                seen.add(key)
                                self.write('plugin_event', label, detail=event)
                try:
                    await asyncio.wait_for(self.stop.wait(), 1)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors += 1
            # Do not print raw websocket/CDP errors: they can contain URLs or page text.
            self.write('observer_error', label, reason=type(exc).__name__ if isinstance(exc, (ProtocolError, TimeoutError)) else 'connection_or_page_unavailable')
        finally:
            if cdp:
                for method, params in ([('Page.removeScriptToEvaluateOnNewDocument', {'identifier': script_id}),
                                        ('Runtime.evaluate', {'expression': 'window.__team48FlowObserverV1?.stop()'}),
                                        ('Runtime.removeBinding', {'name': BINDING})] if script_id else []):
                    try:
                        await asyncio.wait_for(cdp.call(method, params), 2)
                    except Exception:
                        pass
                await cdp.close()
            self.write('detached', label)

    async def record(self, ports, extension, seconds):
        tasks, seen, sequence = {}, set(), 0
        self.write('started', dom=self.dom, ports=ports)
        print(f'Recording: {self.folder}', flush=True)
        try:
            while not self.stop.is_set() and time.monotonic()-self.started < seconds:
                if (self.folder / 'STOP').exists():
                    break
                mark = self.folder / 'stage.txt'
                if mark.exists():
                    value = mark.read_text(encoding='utf-8').strip()
                    if value in STAGES and value != self.stage:
                        self.stage = value
                        self.write('stage_mark', value=value)
                for port in ports:
                    targets = await asyncio.to_thread(read_endpoint, port, 'list')
                    active = {(port, t['id']) for t in targets}
                    for key, task in list(tasks.items()):
                        if key[0] == port and key not in active:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            tasks.pop(key)
                    for target in targets:
                        key = (port, target['id'])
                        url = target.get('url', '')
                        allowed = (target['type'] == 'page' and (route(url) or url in {'about:blank', 'chrome://newtab/'})) or (
                            target['type'] == 'service_worker' and url == f'chrome-extension://{extension}/background.js')
                        if not allowed or key in seen:
                            continue
                        seen.add(key)
                        sequence += 1
                        tasks[key] = asyncio.create_task(self.watch(port, target, f'target-{sequence}'))
                await asyncio.sleep(.5)
        finally:
            self.stop.set()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            self.write('stopped', observer_errors=self.errors)
            summary = {'seconds': round(time.monotonic()-self.started, 1), 'counts': dict(self.counts),
                       'observer_errors': self.errors, 'dom_observation': self.dom,
                       'note': 'Client metadata only; not proof of server-side validation or risk decisions.'}
            (self.folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
            self.stream.close()
            print(f'Stopped. Events: {sum(self.counts.values())}; observer errors: {self.errors}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    record = sub.add_parser('record')
    record.add_argument('--port', type=int, action='append', required=True)
    record.add_argument('--out', type=Path, required=True)
    record.add_argument('--seconds', type=int, default=3600)
    record.add_argument('--no-dom', action='store_true')
    record.add_argument('--extension-id', default=EXTENSION)
    mark = sub.add_parser('mark')
    mark.add_argument('--out', type=Path, required=True)
    mark.add_argument('--stage', choices=sorted(STAGES), required=True)
    stop = sub.add_parser('stop')
    stop.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.command != 'record':
        if not (args.out / 'timeline.jsonl').exists() or (args.out / 'summary.json').exists():
            parser.error('No active recording folder')
        name = 'STOP' if args.command == 'stop' else 'stage.txt'
        (args.out / name).write_text('stop' if args.command == 'stop' else args.stage, encoding='utf-8')
        return
    if not all(1 <= p <= 65535 for p in args.port) or not 1 <= args.seconds <= 14400:
        parser.error('Expected local port 1-65535 and duration 1-14400 seconds')
    if not re.fullmatch(r'[a-p]{32}', args.extension_id):
        parser.error('Invalid extension ID')
    try:
        asyncio.run(Recorder(args.out, dom=not args.no_dom).record(args.port, args.extension_id, args.seconds))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f'Recorder stopped: {type(exc).__name__}. Details omitted for privacy.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
