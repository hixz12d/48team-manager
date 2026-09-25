import {ACTIVE, MODES, STAGES, defaultProfile, checkedProfile, newPassword, contentAllowed, publicJob, pageKind, recordEvent, diagnosticReport, oauthCallback} from './shared.mjs';
import {MAILBOX} from './private-config.mjs';
import * as PRIVATE_CONFIG from './private-config.mjs';
import {RUN_TTL, startMailbox, pollMailbox} from './cloudflare.mjs';

// Optional Team48 handoff: sync the team, link the member, authorize, push and count.
const TEAM48 = (() => {
  try {
    const config = PRIVATE_CONFIG.TEAM48;
    const url = new URL(config.baseUrl);
    const local = url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname);
    return (url.protocol === 'https:' || local) && typeof config.token === 'string' && config.token.length >= 24 ?
      {origin: url.origin, token: config.token} : null;
  } catch { return null; }
})();
async function team48(path, body) {
  if (!TEAM48) throw new Error('插件未配置 Team48 地址和令牌，请用 --team48-token 重新打包。');
  let response;
  try {
    response = await fetch(TEAM48.origin + path, {
      method: body ? 'POST' : 'GET', credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(90000),
      headers: {Authorization: `Bearer ${TEAM48.token}`, Accept: 'application/json', ...(body ? {'Content-Type': 'application/json'} : {})},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch { throw new Error('无法连接 Team48，请检查网络后重试。'); }
  if (response.status === 401) throw new Error('Team48 拒绝了插件令牌，请核对 EXTENSION_API_TOKEN 后重新打包。');
  if (response.status === 404) throw new Error('Team48 未启用插件接口，请在服务器配置 EXTENSION_API_TOKEN 并更新版本。');
  if (!response.ok) throw new Error(`Team48 返回 HTTP ${response.status}，请稍后重试。`);
  return response.json();
}

const ready = Promise.all([
  chrome.storage.local.remove('config'),
  chrome.storage.session.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'}),
]);
let queue = Promise.resolve();
const serial = task => {
  const next = queue.then(task);
  queue = next.catch(() => {});
  return next;
};
const saveJob = async job => {
  await chrome.storage.session.set({job});
  if (['done', 'stopped'].includes(job.status)) void releaseDebugger(job.tabId);
  const text = {running: '…', paused: '!', done: '✓', stopped: ''}[job.status] || '';
  try {
    await chrome.action.setBadgeText({tabId: job.tabId, text});
    await chrome.action.setBadgeBackgroundColor({tabId: job.tabId, color: job.status === 'paused' ? '#b36b19' : '#166a59'});
    await chrome.action.setTitle({tabId: job.tabId, title: `注册助手 · ${job.message}`});
  } catch { /* tab may have closed */ }
  return job;
};

// Trusted input: the browser itself generates mouse/keyboard events through CDP,
// so the page sees isTrusted=true and a normal keydown/keypress/input/keyup chain.
const debuggerTabs = new Set();
const pointers = new Map();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const between = (min, max) => min + Math.floor(Math.random() * (max - min + 1));
const cdp = async (tabId, method, params = {}) => {
  // Pause/stop can arrive while a pointer path or key hold is waiting. Only release
  // already-pressed inputs afterward; never start another move, press or insertion.
  const releasing = params.type === 'keyUp' || params.type === 'mouseReleased';
  if (method.startsWith('Input.') && !releasing) {
    const job = await loadJob();
    if (!job || job.tabId !== tabId || job.status !== 'running' || Date.now() >= job.expiresAt) throw new Error('任务已暂停、停止或超时。');
  }
  return chrome.debugger.sendCommand({tabId}, method, params);
};
async function attachDebugger(tabId, refreshFocus = false) {
  if (!chrome.debugger) throw new Error('浏览器未授予调试权限。');
  let fresh = false;
  if (!debuggerTabs.has(tabId)) {
    try { await chrome.debugger.attach({tabId}, '1.3'); }
    catch (error) {
      // A restarted worker loses its memory of an attachment it still owns.
      if (!/already attached/i.test(error.message)) throw error;
    }
    debuggerTabs.add(tabId); fresh = true;
  }
  // Pages in a background window still believe they have focus. Set once per attach and
  // once per page load (input-ready), not before every key.
  if (fresh || refreshFocus) await cdp(tabId, 'Emulation.setFocusEmulationEnabled', {enabled: true});
}
async function releaseDebugger(tabId) {
  if (!debuggerTabs.delete(tabId)) return;
  pointers.delete(tabId);
  try { await chrome.debugger.detach({tabId}); } catch { /* tab closed or already detached */ }
}
const KEYS = {' ': ['Space', 32], '-': ['Minus', 189], '=': ['Equal', 187], '[': ['BracketLeft', 219], ']': ['BracketRight', 221],
  '\\': ['Backslash', 220], ';': ['Semicolon', 186], "'": ['Quote', 222], ',': ['Comma', 188], '.': ['Period', 190], '/': ['Slash', 191], '`': ['Backquote', 192]};
const SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
  '_': '-', '+': '=', '{': '[', '}': ']', '|': '\\', ':': ';', '"': "'", '<': ',', '>': '.', '?': '/', '~': '`'};
const SPECIAL_KEYS = {Backspace: 8, Tab: 9, Enter: 13, Escape: 27, Home: 36, End: 35, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40};
function keyFor(character) {
  const base = SHIFTED[character] || character.toLowerCase();
  const shift = !!SHIFTED[character] || /[A-Z]/.test(character);
  if (/^[a-z]$/.test(base)) return {code: `Key${base.toUpperCase()}`, keyCode: base.toUpperCase().charCodeAt(0), shift};
  if (/^[0-9]$/.test(base)) return {code: `Digit${base}`, keyCode: base.charCodeAt(0), shift};
  return KEYS[base] ? {code: KEYS[base][0], keyCode: KEYS[base][1], shift} : null;
}
async function typeKey(tabId, key) {
  const special = SPECIAL_KEYS[key];
  if (special) {
    const event = {key, code: key, windowsVirtualKeyCode: special, nativeVirtualKeyCode: special};
    // Enter needs its text so the page performs implicit form submission.
    await cdp(tabId, 'Input.dispatchKeyEvent', key === 'Enter' ? {type: 'keyDown', text: '\r', unmodifiedText: '\r', ...event} : {type: 'rawKeyDown', ...event});
    await sleep(between(30, 70));
    await cdp(tabId, 'Input.dispatchKeyEvent', {type: 'keyUp', ...event});
    return;
  }
  const info = keyFor(key);
  if (!info) { await cdp(tabId, 'Input.insertText', {text: key}); return; }
  const shift = {key: 'Shift', code: 'ShiftLeft', windowsVirtualKeyCode: 16, nativeVirtualKeyCode: 16, location: 1};
  const modifiers = info.shift ? 8 : 0;
  let shiftPressed = false;
  try {
    if (info.shift) {
      await cdp(tabId, 'Input.dispatchKeyEvent', {type: 'rawKeyDown', modifiers, ...shift});
      shiftPressed = true;
      await sleep(between(25, 60));
    }
    const event = {key, code: info.code, windowsVirtualKeyCode: info.keyCode, nativeVirtualKeyCode: info.keyCode, modifiers};
    await cdp(tabId, 'Input.dispatchKeyEvent', {type: 'keyDown', text: key, unmodifiedText: key, ...event});
    try { await sleep(between(35, 95)); }
    finally { await cdp(tabId, 'Input.dispatchKeyEvent', {type: 'keyUp', ...event}); }
  } finally {
    if (shiftPressed) await cdp(tabId, 'Input.dispatchKeyEvent', {type: 'keyUp', ...shift, modifiers: 0});
  }
}
async function moveMouse(tabId, x, y) {
  // An eased, slightly curved path from the last position instead of a jump.
  const from = pointers.get(tabId) || {x: x + between(-160, 160), y: y + between(80, 220)};
  const distance = Math.hypot(x - from.x, y - from.y);
  if (distance < 1) { pointers.set(tabId, {x, y}); return; }
  const steps = Math.max(4, Math.min(28, Math.round(distance / 22)));
  const bend = between(-1, 1) * Math.min(40, distance / 6);
  for (let step = 1; step <= steps; step++) {
    const t = step / steps, eased = t * t * (3 - 2 * t), arc = Math.sin(Math.PI * t) * bend;
    const px = from.x + (x - from.x) * eased - (y - from.y) / (distance || 1) * arc;
    const py = from.y + (y - from.y) * eased + (x - from.x) / (distance || 1) * arc;
    await cdp(tabId, 'Input.dispatchMouseEvent', {type: 'mouseMoved', x: step === steps ? x : px, y: step === steps ? y : py, pointerType: 'mouse'});
    await sleep(between(6, 18));
  }
  pointers.set(tabId, {x, y});
}
async function inputMessage(message, sender) {
  const job = await loadJob();
  if (!job || !contentAllowed(sender, job) || message.jobId !== job.id || job.status !== 'running' ||
      message.version !== chrome.runtime.getManifest().version || Date.now() >= job.expiresAt ||
      job.mode === 'manual') return {active: false};
  if (['input-text', 'input-key'].includes(message.type) && job.mode === 'submit' && message.key !== 'Enter') return {active: false};
  const tabId = sender.tab.id;
  if (message.type === 'input-ready') {
    try { await attachDebugger(tabId, true); }
    catch { job.inputMode = 'synthetic'; await chrome.storage.session.set({job}); return {trusted: false}; }
    job.inputMode = 'cdp'; await chrome.storage.session.set({job});
    return {trusted: true};
  }
  await attachDebugger(tabId);
  const x = Number(message.x), y = Number(message.y);
  const point = Number.isFinite(x) && Number.isFinite(y) && x >= 0 && y >= 0 && x < 20000 && y < 20000;
  if (message.type === 'input-date-layout' && Number.isInteger(message.index) && message.index >= 0) {
    const {root} = await cdp(tabId, 'DOM.getDocument', {depth: 0});
    const {nodeIds} = await cdp(tabId, 'DOM.querySelectorAll', {nodeId: root.nodeId, selector: 'input[type="date"]'});
    if (!nodeIds[message.index]) throw new Error('日期控件已变化。');
    const {node} = await cdp(tabId, 'DOM.describeNode', {nodeId: nodeIds[message.index], depth: -1, pierce: true});
    const parts = [];
    const visit = item => {
      const attrs = item.attributes || [];
      const at = attrs.indexOf('pseudo');
      const match = at >= 0 && /^-webkit-datetime-edit-(year|month|day)-field$/.exec(attrs[at + 1]);
      if (match) parts.push(match[1]);
      for (const child of [...(item.children || []), ...(item.shadowRoots || [])]) visit(child);
    };
    visit(node);
    if (parts.length !== 3 || new Set(parts).size !== 3) throw new Error('无法识别日期分段，请手动填写后继续。');
    return {parts};
  }
  if (message.type === 'input-move' && point) { await moveMouse(tabId, x, y); return {}; }
  if (message.type === 'input-click' && point) {
    await moveMouse(tabId, x, y);
    const button = {x, y, button: 'left', clickCount: 1, pointerType: 'mouse'};
    await cdp(tabId, 'Input.dispatchMouseEvent', {type: 'mousePressed', buttons: 1, ...button});
    await sleep(between(50, 110));
    await cdp(tabId, 'Input.dispatchMouseEvent', {type: 'mouseReleased', buttons: 0, ...button});
    return {sent: true};
  }
  if (message.type === 'input-key' && typeof message.key === 'string' && (Object.hasOwn(SPECIAL_KEYS, message.key) || [...message.key].length === 1)) {
    await typeKey(tabId, message.key);
    return {sent: true};
  }
  if (message.type === 'input-text' && typeof message.text === 'string' && message.text.length > 0 && message.text.length <= 256) {
    // One insertion, like a password manager or paste, instead of hand-typing a long random secret.
    await cdp(tabId, 'Input.insertText', {text: message.text});
    return {sent: true};
  }
  if (message.type === 'input-wheel' && point && [message.deltaY, message.deltaX ?? 0].every(value => Number.isFinite(Number(value)) && Math.abs(Number(value)) <= 600)) {
    await cdp(tabId, 'Input.dispatchMouseEvent', {type: 'mouseWheel', x, y, deltaX: Number(message.deltaX || 0), deltaY: Number(message.deltaY), pointerType: 'mouse'});
    pointers.set(tabId, {x, y});
    return {};
  }
  if (message.type === 'input-drift') {
    const width = Number(message.width), height = Number(message.height);
    if (!(width >= 80 && height >= 80 && width < 20000 && height < 20000)) throw new Error('输入指令无效。');
    // A small idle movement from wherever the pointer rests.
    const from = pointers.get(tabId) || {x: width * (0.3 + Math.random() * 0.4), y: height * (0.3 + Math.random() * 0.4)};
    const clamp = (value, max) => Math.min(max - 20, Math.max(20, value));
    await moveMouse(tabId, clamp(from.x + between(-140, 140), width), clamp(from.y + between(-90, 90), height));
    return {};
  }
  throw new Error('输入指令无效。');
}
const loadJob = async () => (await chrome.storage.session.get('job')).job;
async function expire(job) {
  if (job && ACTIVE.has(job.status) && Date.now() >= job.expiresAt) {
    job.status = 'stopped'; job.message = job.phase === 'oauth' ? '本次授权已超时，可重新接入。' : '本次注册已超时，请重新开始。';
    recordEvent(job, 'expired');
    delete job.baseline; delete job.pendingCode;
    if (job.handoff?.status === 'authorizing') failHandoff(job, '授权超时，可选择团队后重新接入。');
    await saveJob(job);
  }
  return job;
}

// ---- Team48 handoff: sync -> link -> OAuth in this tab -> callback -> push + count ----
// Handoff statuses: syncing -> (waiting_join ->) opening -> authorizing -> completing -> done,
// or failed / callback_retry. Everything except the page steps runs here in the worker.
const HANDOFF_ACTIVE = new Set(['syncing', 'waiting_join', 'opening', 'completing']);
const JOIN_WAIT = 3 * 60 * 1000, JOIN_RETRY = 10000, SYNC_LIMIT = 4 * 60 * 1000;
const patchJob = (id, change) => serial(async () => {
  const job = await loadJob();
  if (!job || job.id !== id) return null;
  change(job);
  await saveJob(job);
  return job;
});
function failHandoff(job, message, code) {
  if (!job.handoff) return;
  job.handoff.status = 'failed';
  job.handoff.message = String(message || '接入失败').slice(0, 300);
  if (code) job.handoff.errorCode = String(code).slice(0, 60);
  delete job.handoff.ticket; delete job.handoff.authorizeUrl; delete job.handoff.callbackUrl;
}
function beginHandoff(job, workspaceId, workspaceName) {
  job.handoff = {status: 'syncing', workspaceId, workspaceName: String(workspaceName || '').slice(0, 120),
    startedAt: Date.now(), message: '正在同步团队成员…'};
}
let handoffTask = null;
function ensureHandoff() {
  // incognito: split shares session storage with the regular profile's worker, which cannot
  // see incognito tabs. Only the incognito worker may drive the handoff.
  if (!chrome.extension.inIncognitoContext) return null;
  if (!handoffTask) {
    // Service workers can be suspended; the alarm resumes an interrupted handoff.
    void chrome.alarms.create('handoff-watchdog', {periodInMinutes: 0.5});
    handoffTask = runHandoff().catch(() => {}).finally(() => { handoffTask = null; });
  }
  return handoffTask;
}
function applyHandoff(job, result) {
  const handoff = job.handoff;
  if (handoff?.status !== 'syncing') return;
  if (result?.ok && result.state === 'syncing' && result.operation_id) {
    handoff.syncOperationId = String(result.operation_id); handoff.message = '正在同步团队成员…';
    return;
  }
  if (result?.state === 'not_joined') {
    // Give the user time to accept the invitation in the page; the check repeats.
    handoff.joinDeadline ||= Date.now() + JOIN_WAIT;
    if (Date.now() < handoff.joinDeadline) {
      Object.assign(handoff, {status: 'waiting_join', retryAt: Date.now() + JOIN_RETRY,
        message: '团队中仍是「已邀请」：请在网页接受邀请，插件每 10 秒重新同步检查（最多 3 分钟）。'});
      delete handoff.syncOperationId;
      return;
    }
  }
  if (result?.ok && result.state === 'authorize' && result.ticket && result.account_id) {
    let url;
    try { url = new URL(result.authorize_url); } catch { url = null; }
    if (url?.origin !== 'https://auth.openai.com') return failHandoff(job, 'Team48 返回的授权链接无效。');
    Object.assign(handoff, {status: 'opening', accountId: Number(result.account_id), ticket: String(result.ticket),
      authorizeUrl: url.href, message: '已接入本地，正在打开授权页面',
      workspaceNames: (result.workspace?.names || [handoff.workspaceName]).map(String).filter(Boolean).slice(0, 5)});
    if (result.workspace?.name) handoff.workspaceName = String(result.workspace.name).slice(0, 120);
    return;
  }
  failHandoff(job, result?.message || 'Team48 未返回授权链接。', result?.error_code);
}
async function runHandoff() {
  for (let guard = 0; guard < 400; guard++) {
    const job = await serial(loadJob);
    const handoff = job?.handoff;
    if (!handoff || !HANDOFF_ACTIVE.has(handoff.status)) { await chrome.alarms.clear('handoff-watchdog'); return; }
    if (handoff.status === 'opening') { await openAuthorization(job.id); continue; }
    if (handoff.status === 'completing') { await completeAuthorization(job.id); continue; }
    if (handoff.status === 'waiting_join') {
      if (Date.now() < handoff.retryAt) { await sleep(Math.min(2000, handoff.retryAt - Date.now())); continue; }
      await patchJob(job.id, current => {
        if (current.handoff?.status === 'waiting_join') { current.handoff.status = 'syncing'; current.handoff.message = '正在重新同步团队成员…'; }
      });
      continue;
    }
    if (Date.now() - handoff.startedAt > SYNC_LIMIT + JOIN_WAIT) {
      await patchJob(job.id, current => failHandoff(current, '团队同步超时，请到控制台检查母号授权后重试。'));
      continue;
    }
    let result;
    try {
      result = await team48('/api/ext/handoff', {email: job.email, workspace_id: handoff.workspaceId,
        ...(handoff.syncOperationId ? {sync_operation_id: handoff.syncOperationId} : {})});
    } catch (error) {
      await patchJob(job.id, current => { if (current.handoff?.status === 'syncing') failHandoff(current, error.message); });
      continue;
    }
    await patchJob(job.id, current => applyHandoff(current, result));
    if (result?.state === 'syncing') await sleep(2000);
  }
}
async function openAuthorization(id) {
  const job = await serial(loadJob);
  if (job?.id !== id || job.handoff?.status !== 'opening') return;
  const fail = message => patchJob(id, current => { if (current.handoff?.status === 'opening') failHandoff(current, message); });
  let baseline = null;
  if (['auto', 'review'].includes(job.mode)) {
    // Snapshot the inbox first so only a fresh login code is ever used.
    try { baseline = await startMailbox(MAILBOX, job.email); }
    catch (error) { await fail(`读取邮箱失败：${error.message}`); return; }
  }
  let tabId = job.tabId;
  try { await chrome.tabs.get(tabId); }
  catch {
    try {
      const window = await chrome.windows.getLastFocused();
      if (!window.incognito) throw new Error('not incognito');
      tabId = (await chrome.tabs.create({windowId: window.id, url: 'about:blank', active: true})).id;
    } catch { await fail('注册标签页已关闭，请在无痕窗口中打开插件后重新接入。'); return; }
  }
  const ready = await patchJob(id, current => {
    if (current.handoff?.status !== 'opening') return;
    // A fresh page run: the signup submission records must not limit authorization steps.
    Object.assign(current, {tabId, phase: 'oauth', status: 'running', stage: 'signup', message: '正在打开授权页面',
      claims: {}, attempts: {}, continueClicks: {}, continueRetries: {}, clickReservations: {}, reviewSteps: {},
      baseline, lastPoll: 0, ignoredCodes: current.ignoredCodes || [], entryFormSeen: true, entryFallbackUsed: true,
      expiresAt: Date.now() + (current.mode === 'auto' ? RUN_TTL : 30 * 60 * 1000)});
    delete current.retryClaim; delete current.pendingCode;
    current.handoff.status = 'authorizing';
    current.handoff.message = '正在授权；拿到回调后自动提交 Team48';
    recordEvent(current, 'oauth_started');
  });
  if (ready?.handoff?.status !== 'authorizing') return;
  await chrome.alarms.create('signup-expiry', {when: ready.expiresAt});
  try { await chrome.tabs.update(tabId, {url: ready.handoff.authorizeUrl, active: true}); }
  catch {
    await patchJob(id, current => {
      current.status = 'stopped'; current.message = '授权标签页已关闭。';
      failHandoff(current, '授权标签页已关闭，可重新接入。');
    });
  }
}
async function completeAuthorization(id) {
  const job = await serial(loadJob);
  const handoff = job?.handoff;
  if (job?.id !== id || handoff?.status !== 'completing') return;
  if (!handoff.callbackUrl || !handoff.ticket) {
    await patchJob(id, current => failHandoff(current, '授权回调已丢失，请重新接入。'));
    return;
  }
  let result;
  try {
    result = await team48('/api/ext/handoff/complete', {account_id: handoff.accountId, workspace_id: handoff.workspaceId,
      ticket: handoff.ticket, callback_url: handoff.callbackUrl, push_sub2api: true, count_switch: true});
  } catch (error) {
    await patchJob(id, current => {
      if (current.handoff?.status !== 'completing') return;
      current.handoff.status = 'callback_retry';
      current.handoff.message = `${error.message} 回调已保留，可点击「重新提交回调」。`;
    });
    return;
  }
  await patchJob(id, current => {
    if (current.handoff?.status !== 'completing') return;
    if (!result?.ok) {
      const consumed = result?.error_code === 'callback_consumed';
      failHandoff(current, consumed ? '回调已提交过一次；请到控制台确认该账号的授权状态。' : result?.message || '授权失败。', result?.error_code);
      return;
    }
    Object.assign(current.handoff, {status: 'done', message: String(result.message || '授权完成'), followups: result.followups || {}});
    delete current.handoff.ticket; delete current.handoff.authorizeUrl; delete current.handoff.callbackUrl;
    if (current.codexResult !== 'phone_required') current.codexResult = 'no_phone';
    current.message = '已完成授权并提交 Team48。';
  });
}
function captureCallback(tabId, url) {
  if (!oauthCallback(url)) return;
  serial(async () => {
    const job = await loadJob();
    if (!job || job.tabId !== tabId || job.phase !== 'oauth' || job.handoff?.status !== 'authorizing') return false;
    Object.assign(job.handoff, {status: 'completing', callbackUrl: url, message: '已拿到授权回调，正在提交 Team48'});
    job.status = 'done'; job.stage = 'consent'; job.message = '已拿到授权回调，正在提交 Team48';
    delete job.baseline; delete job.pendingCode;
    recordEvent(job, 'oauth_callback');
    await saveJob(job);
    return true;
  }).then(captured => {
    if (!captured) return;
    ensureHandoff();
    // Replace the unreachable localhost page with the progress view.
    chrome.tabs.update(tabId, {url: chrome.runtime.getURL('popup.html')}).catch(() => {});
  }).catch(() => {});
}

async function resumeJob(job) {
  if (job.status !== 'paused') throw new Error('当前任务无法继续，请重新开始。');
  // Only a timed-out submission may be retried, and the total attempt limit remains in force.
  if (job.retryClaim) {
    delete job.claims[job.retryClaim];
    // If a page unloaded before acknowledging a click, its outcome is unknown.
    // Keep the attempt counted and don't reuse a code it may have submitted.
    const abandoned = job.clickReservations?.[job.retryClaim];
    if (abandoned?.code && job.pendingCode === abandoned.code) {
      job.ignoredCodes.push(job.pendingCode); delete job.pendingCode;
    }
    if (job.clickReservations) delete job.clickReservations[job.retryClaim];
    delete job.retryClaim;
  }
  job.status = 'running'; job.message = '继续本次注册';
  recordEvent(job, 'resumed');
  job.resumeCount = (job.resumeCount || 0) + 1;
  await saveJob(job);
  return {};
}

async function popupMessage(message) {
  const job = await expire(await loadJob());
  if (message.type === 'view') {
    if (HANDOFF_ACTIVE.has(job?.handoff?.status)) ensureHandoff();
    return {job: publicJob(job), incognito: chrome.extension.inIncognitoContext, version: chrome.runtime.getManifest().version, team48: !!TEAM48};
  }
  if (message.type === 'start') {
    if (!chrome.extension.inIncognitoContext) throw new Error('请在无痕窗口里打开插件并开始注册。');
    if (job && ACTIVE.has(job.status)) throw new Error('已有任务，请先停止当前注册。');
    if (HANDOFF_ACTIVE.has(job?.handoff?.status)) throw new Error('上一个账号仍在接入 Team48，请等待完成。');
    if (job?.handoff?.status === 'callback_retry') throw new Error('上一个账号的授权回调尚未提交，请先重新提交或清除。');
    const autoHandoff = TEAM48 && Number.isInteger(Number(message.handoff?.workspaceId)) && Number(message.handoff.workspaceId) > 0 ?
      {workspaceId: Number(message.handoff.workspaceId), workspaceName: String(message.handoff.workspaceName || '').slice(0, 120)} : null;
    const email = String(message.email || '').trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 254) throw new Error('邮箱格式不正确。');
    const mode = message.mode || 'auto';
    if (!MODES.has(mode)) throw new Error('注册模式无效。');
    const profile = checkedProfile(message.profile);
    const window = await chrome.windows.getCurrent();
    if (!window.incognito) throw new Error('当前窗口不是无痕窗口。');
    const automaticFill = mode === 'auto' || mode === 'review';
    const baseline = automaticFill ? await startMailbox(MAILBOX, email) : null;
    const tab = await chrome.tabs.create({windowId: window.id, url: 'about:blank', active: true});
    const current = {
      id: crypto.randomUUID(), tabId: tab.id, email, password: newPassword(), profile: profile || defaultProfile(), baseline,
      hidePanel: automaticFill && message.hidePanel === true,
      mode, profileSource: automaticFill ? (profile ? 'custom' : 'generated') : 'manual', startedAt: Date.now(), events: [],
      status: 'running', stage: 'signup', message: '正在打开 ChatGPT 注册页面',
      expiresAt: Date.now() + (mode === 'auto' ? RUN_TTL : 30 * 60 * 1000),
      claims: {}, attempts: {}, ignoredCodes: [], lastPoll: 0, autoHandoff,
    };
    recordEvent(current, 'started');
    await saveJob(current);
    await chrome.alarms.create('signup-expiry', {when: current.expiresAt});
    try { await chrome.tabs.update(tab.id, {url: 'https://chatgpt.com/'}); }
    catch { current.status = 'stopped'; current.message = '注册标签页已关闭，请重新开始。'; delete current.baseline; await saveJob(current); }
    return {job: publicJob(current)};
  }
  if (message.type === 'clear') {
    if (job && ACTIVE.has(job.status)) throw new Error('请先停止当前注册。');
    if (HANDOFF_ACTIVE.has(job?.handoff?.status)) throw new Error('正在接入 Team48，请等待完成。');
    await chrome.storage.session.remove('job');
    return {};
  }
  if (!job) throw new Error('没有正在进行的注册。');
  if (message.type === 'handoff-start') {
    if (!TEAM48) throw new Error('插件未配置 Team48 地址和令牌。');
    if (HANDOFF_ACTIVE.has(job.handoff?.status) || ACTIVE.has(job.status)) throw new Error('授权进行中，请等待完成或先停止。');
    if (job.status !== 'done' && job.phase !== 'oauth') throw new Error('请在注册完成后再接入 Team48。');
    const workspaceId = Number(message.workspaceId);
    if (!Number.isInteger(workspaceId) || workspaceId <= 0) throw new Error('请选择团队。');
    beginHandoff(job, workspaceId, message.workspaceName);
    await saveJob(job);
    ensureHandoff();
    return {};
  }
  if (message.type === 'handoff-retry') {
    if (job.handoff?.status !== 'callback_retry') throw new Error('当前没有可重新提交的回调。');
    job.handoff.status = 'completing'; job.handoff.message = '正在重新提交回调';
    await saveJob(job);
    ensureHandoff();
    return {};
  }
  if (message.type === 'diagnostics') return {report: diagnosticReport(job, chrome.runtime.getManifest().version)};
  if (message.type === 'set-result') {
    if (job.status !== 'done' || !['unknown', 'no_phone', 'phone_required', 'failed'].includes(message.result)) throw new Error('请在注册完成后选择授权结果。');
    job.codexResult = message.result;
    await saveJob(job);
    return {};
  }
  if (message.type === 'pause') {
    if (job.status !== 'running') return {};
    recordEvent(job, 'paused', {stage: job.stage});
    job.status = 'paused'; job.message = '已手动暂停，点击继续即可恢复。';
    await saveJob(job);
    return {};
  }
  if (message.type === 'stop') {
    recordEvent(job, 'stopped');
    job.status = 'stopped'; job.message = '已停止。可手动继续网页操作。';
    delete job.baseline; delete job.pendingCode;
    if (HANDOFF_ACTIVE.has(job.handoff?.status) || job.handoff?.status === 'authorizing') failHandoff(job, '已停止接入。');
    await saveJob(job);
    return {};
  }
  if (message.type === 'resume') {
    return resumeJob(job);
  }
  throw new Error('未知操作。');
}

async function contentMessage(message, sender) {
  const job = await expire(await loadJob());
  if (!contentAllowed(sender, job) || !job) return {active: false};
  if (message.type !== 'state' && message.jobId !== job.id) return {active: false};
  if (message.version !== chrome.runtime.getManifest().version) {
    return {active: false, id: job.id, email: job.email, status: 'paused', stage: job.stage,
      message: '页面仍在使用旧版注册脚本，请刷新本页后继续。'};
  }
  if (message.type === 'state') {
    const state = {active: job.status === 'running', id: job.id, email: job.email,
      status: job.status, message: job.message, stage: job.stage, mode: job.mode || 'auto', resumeCount: job.resumeCount || 0,
      hidePanel: job.hidePanel === true, entryFallbackUsed: !!job.entryFallbackUsed, entryFormSeen: !!job.entryFormSeen,
      phase: job.phase === 'oauth' ? 'oauth' : 'signup', passwordSet: !!job.passwordSet,
      workspaceNames: job.phase === 'oauth' ? job.handoff?.workspaceNames || [] : []};
    return state.active ? {...state, password: job.password, profile: job.profile, reviewSteps: job.reviewSteps || {}} : state;
  }
  if (message.type === 'resume' && job.status === 'paused') {
    return resumeJob(job);
  }
  if (message.type === 'finish-click') {
    const url = new URL(sender.url), key = `${message.stage}:${url.pathname}`;
    const reserved = job.clickReservations?.[key];
    if (!reserved || reserved.token !== message.token || reserved.origin !== url.origin ||
        typeof message.sent !== 'boolean') return {accepted: false};
    delete job.clickReservations[key];
    if (!message.sent) {
      // Only release a reservation whose click was definitely never dispatched.
      delete job.claims[key];
      job.attempts[message.stage] = Math.max(0, (job.attempts[message.stage] || 0) - 1);
      if (reserved.retry) {
        if (reserved.previousClaim) job.claims[key] = reserved.previousClaim;
        job.continueRetries[message.stage] = Math.max(0, job.continueRetries[message.stage] - 1);
      }
    } else {
      if (reserved.code && job.pendingCode === reserved.code) {
        job.ignoredCodes.push(job.pendingCode); delete job.pendingCode;
      }
      recordEvent(job, 'submit_attempt', {page: pageKind(sender.url), stage: message.stage});
      if (['otp', 'profile'].includes(message.stage)) {
        job.continueClicks ||= {};
        job.continueClicks[key] = {token: reserved.token, origin: reserved.origin, at: Date.now(),
          blocked: job.continueClicks[key]?.blocked || false};
        if (reserved.retry) recordEvent(job, 'continue_retry', {page: pageKind(sender.url), stage: message.stage});
      }
    }
    await saveJob(job);
    return {accepted: true};
  }
  if (job.status !== 'running') return {active: false};
  if (message.type === 'continue-observed') {
    if (job.mode === 'manual' || !['otp', 'profile'].includes(message.stage)) return {granted: false};
    const url = new URL(sender.url), key = `${message.stage}:${url.pathname}`;
    if (job.clickReservations?.[key]) return {granted: false};
    job.continueClicks ||= {};
    const token = crypto.randomUUID();
    job.continueClicks[key] = {token, origin: url.origin, at: Date.now(), blocked: false};
    recordEvent(job, 'manual_submit', {page: pageKind(sender.url), stage: message.stage});
    await saveJob(job);
    return {granted: true, token};
  }
  if (message.type === 'retry-continue') {
    const url = new URL(sender.url), key = `${message.stage}:${url.pathname}`;
    const previous = job.continueClicks?.[key];
    if (job.mode === 'manual' || !['otp', 'profile'].includes(message.stage) || !previous || previous.blocked ||
        previous.origin !== url.origin || previous.token !== message.token ||
        job.clickReservations?.[key] || (job.continueRetries?.[message.stage] || 0) >= 2 ||
        (job.attempts[message.stage] || 0) >= 3) return {granted: false};
    if (Date.now() - previous.at < 2000) return {granted: false, wait: true};
    // This is another click on the unchanged form, never a request for another OTP.
    const token = crypto.randomUUID();
    job.clickReservations ||= {};
    job.clickReservations[key] = {token, origin: url.origin, retry: true, previousClaim: job.claims[key],
      code: message.stage === 'otp' ? job.pendingCode : undefined};
    job.claims[key] = Date.now();
    job.attempts[message.stage] = (job.attempts[message.stage] || 0) + 1;
    job.continueRetries ||= {};
    job.continueRetries[message.stage] = (job.continueRetries[message.stage] || 0) + 1;
    await saveJob(job);
    return {granted: true, token};
  }
  if (message.type === 'entry-fallback') {
    const url = new URL(sender.url);
    if (url.origin !== 'https://chatgpt.com' || url.pathname !== '/' ||
        !['auto', 'review'].includes(job.mode || 'auto') || job.entryFallbackUsed || job.entryFormSeen ||
        Date.now() - job.startedAt < 15000 ||
        Object.keys(job.claims).some(key => !key.startsWith('signup:'))) return {granted: false};
    // Persist before navigating; reloads and worker restarts must not create a redirect loop.
    job.entryFallbackUsed = true;
    recordEvent(job, 'entry_fallback', {page: 'chatgpt', stage: 'signup'});
    job.message = '主页注册入口未响应，正在尝试备用入口。';
    await saveJob(job);
    return {granted: true, url: 'https://chatgpt.com/auth/login'};
  }
  if (message.type === 'review-ready' && job.mode === 'review' && STAGES.has(message.stage)) {
    // The user submits in review mode; the password we typed is still the account's password.
    if (message.stage === 'password' && job.phase !== 'oauth') job.passwordSet = true;
    job.reviewSteps ||= {};
    job.reviewSteps[`${message.stage}:${new URL(sender.url).pathname}`] = true;
    recordEvent(job, 'waiting_manual', {page: pageKind(sender.url), stage: message.stage});
    await saveJob(job);
    return {};
  }
  if (message.type === 'event') {
    if (['page', 'filled', 'form_submit', 'manual_submit', 'waiting_manual', 'session_check', 'session_error', 'captcha', 'phone', 'rate_limit', 'click_response'].includes(message.event) &&
        recordEvent(job, message.event, {page: pageKind(sender.url), stage: message.stage, verified: message.verified, outcome: message.outcome})) {
      if (message.event === 'page' && ['email', 'password', 'otp', 'profile'].includes(message.stage)) job.entryFormSeen = true;
      if (message.event === 'phone' && job.phase === 'oauth') job.codexResult = 'phone_required';
      const key = `${message.stage}:${new URL(sender.url).pathname}`;
      if (message.event === 'click_response' && ['submitted', 'loading', 'advanced', 'validation'].includes(message.outcome) && job.continueClicks?.[key]) {
        job.continueClicks[key].blocked = true;
      }
      if (message.event === 'page') {
        for (const [receiptKey, receipt] of Object.entries(job.continueClicks || {})) {
          if (receiptKey !== key || receipt.origin !== new URL(sender.url).origin) receipt.blocked = true;
        }
      }
      await saveJob(job);
    }
    return {};
  }
  if (message.type === 'pause') {
    recordEvent(job, 'paused', {page: pageKind(sender.url), stage: STAGES.has(message.stage) ? message.stage : job.stage});
    job.status = 'paused'; job.message = String(message.reason || '请手动处理页面后点击继续。').slice(0, 200);
    await saveJob(job);
    return {active: false};
  }
  if (message.type === 'complete') {
    if (new URL(sender.url).hostname !== 'chatgpt.com' || job.phase === 'oauth') return {active: false};
    if (String(message.email || '').toLowerCase() !== job.email) {
      recordEvent(job, 'email_mismatch');
      job.status = 'paused'; job.message = '当前登录邮箱不匹配，请关闭全部无痕窗口后重新开始。';
    } else if (message.verified === false) {
      job.emailVerified = false; recordEvent(job, 'email_unverified');
      job.status = 'paused'; job.message = '已登录，但会话报告邮箱尚未验证，请在网页完成验证后继续。';
    } else {
      job.emailVerified = typeof message.verified === 'boolean' ? message.verified : null;
      recordEvent(job, 'completed', {verified: job.emailVerified});
      job.status = 'done'; job.message = '已确认目标邮箱登录 ChatGPT；后续 Codex 验证要求需在授权时确认。';
      delete job.baseline; delete job.pendingCode;
      if (job.autoHandoff && TEAM48 && !job.handoff) beginHandoff(job, job.autoHandoff.workspaceId, job.autoHandoff.workspaceName);
    }
    await saveJob(job);
    if (job.handoff?.status === 'syncing') ensureHandoff();
    return {};
  }
  if (message.type === 'claim') {
    const limits = {signup: 3, email: 2, password: 2, profile: 2, otp: 2, consent: 4};
    if (!Object.hasOwn(limits, message.stage)) throw new Error('不支持的注册步骤。');
    if (job.mode === 'manual' || (job.mode === 'review' && !message.prepare && message.stage !== 'signup')) return {granted: false};
    const key = `${message.stage}:${new URL(sender.url).pathname}`;
    if (job.claims[key]) {
      if (Date.now() - job.claims[key] > 45000) {
        job.status = 'paused'; job.message = '提交后页面没有前进，请检查网页提示后点击继续重试。';
        job.retryClaim = key;
        await saveJob(job);
      }
      return {granted: false};
    }
    if ((job.attempts[message.stage] || 0) >= limits[message.stage]) {
      job.status = 'paused'; job.message = '已达到自动提交次数，请检查页面并手动继续。';
      await saveJob(job); return {granted: false};
    }
    if (message.prepare) return {granted: true};
    if (!message.reserve && message.stage === 'otp' && job.pendingCode) {
      job.ignoredCodes.push(job.pendingCode);
      delete job.pendingCode;
    }
    job.stage = message.stage;
    if (message.stage === 'password' && job.phase !== 'oauth') job.passwordSet = true;
    if (!message.reserve) recordEvent(job, 'submit_attempt', {page: pageKind(sender.url), stage: message.stage});
    job.claims[key] = Date.now();
    job.attempts[message.stage] = (job.attempts[message.stage] || 0) + 1;
    job.message = job.phase === 'oauth' ?
      {email: '提交登录邮箱', password: '输入登录密码', profile: '填写个人资料', otp: '提交登录验证码', consent: '确认授权'}[message.stage] || '正在授权' :
      {signup: '打开邮箱注册', email: '提交邮箱', password: '设置注册密码', profile: '填写个人资料', otp: '提交邮箱验证码'}[message.stage];
    const token = message.reserve ? crypto.randomUUID() : undefined;
    if (message.reserve) {
      job.clickReservations ||= {};
      job.clickReservations[key] = {token, origin: new URL(sender.url).origin,
        code: message.stage === 'otp' ? job.pendingCode : undefined};
    }
    await saveJob(job); return {granted: true, ...(token ? {token} : {})};
  }
  if (message.type === 'code') {
    if (job.mode === 'manual' || job.mode === 'submit' || !job.baseline) return {code: null};
    const submitted = job.claims[`otp:${new URL(sender.url).pathname}`];
    if (submitted) {
      if (Date.now() - submitted > 45000) {
        job.status = 'paused'; job.message = '验证码提交后页面没有前进，请检查页面，必要时重发验证码后继续。';
        job.retryClaim = `otp:${new URL(sender.url).pathname}`;
        await saveJob(job);
      }
      return {code: null};
    }
    if (job.pendingCode) return {code: job.pendingCode};
    if (Date.now() - job.lastPoll < 4000) return {code: null};
    job.stage = 'otp';
    job.lastPoll = Date.now(); job.message = '等待 Cloudflare 收到新的邮箱验证码';
    await saveJob(job);
    const code = await pollMailbox(MAILBOX, job.email, job.baseline, job.ignoredCodes);
    if (code) {
      if (!/^\d{6}$/.test(code)) throw new Error('邮箱验证码格式异常，请手动检查。');
      job.pendingCode = code;
      recordEvent(job, 'code_received', {stage: 'otp'});
      job.message = '已收到验证码，正在逐位输入';
      await saveJob(job);
    }
    return {code};
  }
  return {active: false};
}

let inputQueue = Promise.resolve();
const inputSerial = task => {
  const next = inputQueue.then(task);
  inputQueue = next.catch(() => {});
  return next;
};
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  const fromPopup = sender.url === chrome.runtime.getURL('popup.html');
  // Key/mouse commands only read the job; keep them off the queue a mailbox poll may hold.
  const input = !fromPopup && /^input-(move|click|key|text|wheel|drift|date-layout)$/.test(message?.type);
  if (fromPopup && message?.type === 'workspaces') {
    // A slow Team48 request must not hold the job queue.
    (async () => {
      if (sender.id !== chrome.runtime.id) throw new Error('来源无效。');
      const payload = await team48('/api/ext/workspaces');
      return {items: (payload.items || []).map(item => ({id: Number(item.id), name: String(item.name || `团队 ${item.id}`)}))};
    })().then(data => respond({ok: true, ...data}), error => respond({ok: false, error: error.message}));
    return true;
  }
  (input ? inputSerial : serial)(async () => {
    await ready;
    if (sender.id !== chrome.runtime.id) throw new Error('来源无效。');
    if (fromPopup) return popupMessage(message);
    return input || message?.type === 'input-ready' ? inputMessage(message, sender) : contentMessage(message, sender);
  }).then(data => respond({ok: true, ...data}), error => respond({ok: false, error: error.message}));
  return true;
});
chrome.debugger?.onDetach.addListener(({tabId}, reason) => {
  debuggerTabs.delete(tabId); pointers.delete(tabId);
  if (reason !== 'canceled_by_user') return;
  serial(async () => {
    const job = await loadJob();
    if (job?.tabId === tabId && job.status === 'running') {
      recordEvent(job, 'paused', {stage: job.stage});
      job.status = 'paused'; job.message = '调试连接被取消，已暂停；点击继续会重新连接。';
      await saveJob(job);
    }
  });
});
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'signup-expiry') serial(async () => expire(await loadJob()));
  if (alarm.name === 'handoff-watchdog') ensureHandoff();
});
chrome.tabs.onRemoved.addListener(tabId => {
  serial(async () => {
    const job = await loadJob();
    if (job?.tabId === tabId && ACTIVE.has(job.status)) {
      recordEvent(job, 'stopped');
      job.status = 'stopped'; job.message = job.phase === 'oauth' ? '授权标签页已关闭。' : '注册标签页已关闭。';
      delete job.baseline; delete job.pendingCode;
      if (job.handoff?.status === 'authorizing') failHandoff(job, '授权标签页已关闭，可重新接入。');
      await saveJob(job);
    }
  });
});
// The OAuth redirect goes to the Codex CLI loopback address, where nothing needs to listen.
const CALLBACK_FILTER = {url: [{hostEquals: 'localhost', ports: [1455], pathEquals: '/auth/callback'},
  {hostEquals: '127.0.0.1', ports: [1455], pathEquals: '/auth/callback'}]};
for (const name of ['onBeforeNavigate', 'onCommitted', 'onErrorOccurred']) {
  chrome.webNavigation?.[name]?.addListener(details => { if (details.frameId === 0) captureCallback(details.tabId, details.url); }, CALLBACK_FILTER);
}
chrome.tabs.onUpdated?.addListener((tabId, change) => { if (change.url) captureCallback(tabId, change.url); });
