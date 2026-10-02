// Server mode: Team48 starts Chromix with this extension and a per-run private-config.mjs
// that exports RUNNER. The worker then takes its task from the server, reports progress and
// hands the OAuth callback back. Protocol: docs/contracts/extension-runner.md.
// Without a valid RUNNER export nothing in this file runs and the extension behaves as before.
import {STAGES, AUTO_RESUME_REASONS, autoRetryAllowed, diagnosticReport, oauthCallback, pauseReason} from './shared.mjs';

export const RUNNER_ALARM = 'runner-watchdog';
const KEY = 'runner';
const RUN_ID = /^[a-f0-9]{32}$/;
const PROBE_NAME = /^[a-z0-9_-]{1,40}$/;
const AUTH_ORIGIN = 'https://auth.openai.com';
const REQUEST_TIMEOUT = 15000;
const HEARTBEAT = 30000, AUTHORIZE_POLL = 5000;
const JOB_RETRIES = 3, JOB_RETRY_DELAY = 3000;
const PROBE_RETRIES = 3, PROBE_RETRY_DELAY = 3000;
const CALLBACK_RETRIES = 12, CALLBACK_RETRY_DELAY = 5000;
const SCREENSHOT_WAIT = 25000;
// The server accepts 64KB per event and 4MB per screenshot.
const EVENT_LIMIT = 60 * 1024, SCREENSHOT_LIMIT = 4 * 1024 * 1024;
const CODEX_RESULTS = new Set(['unknown', 'no_phone', 'phone_required', 'failed']);
// Nobody resolves these on a server: report them as final and stay where the page stopped.
export const RUNNER_MANUAL_REASONS = new Set(['phone', 'captcha', 'rate_limit', 'manual_step', 'email_mismatch',
  'email_unverified', 'session_error', 'debugger_detached',
  'phone_pool_empty', 'phone_limit', 'phone_back_missing', 'phone_relay_error']);
const CALLBACK_ERRORS = {
  callback_invalid: '服务器认为授权回调无效。',
  callback_already_received: '服务器已收到另一条授权回调。',
  not_authorizing: '服务器尚未下发授权链接。',
};

// Valid only for a loopback http base URL, a 32-hex run id and a token of 24+ characters.
export function runnerConfig(value) {
  try {
    const url = new URL(value.baseUrl);
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || url.username || url.password) return null;
    if (typeof value.runId !== 'string' || !RUN_ID.test(value.runId)) return null;
    if (typeof value.token !== 'string' || value.token.length < 24) return null;
    return Object.freeze({base: `${url.origin}/api/ext/runner/${value.runId}`, token: value.token});
  } catch { return null; }
}

// true: the extension will not continue this pause by itself.
export function runnerPauseFinal(job) {
  if (job?.status !== 'paused') return false;
  const reason = pauseReason(job.pauseReason);
  if (RUNNER_MANUAL_REASONS.has(reason)) return true;
  return !(autoRetryAllowed(job) || AUTO_RESUME_REASONS.has(reason));
}

// Status text only: never an address, password, code or URL.
function cleanMessage(text, job) {
  let value = String(text || '');
  for (const secret of [job?.email, job?.password]) if (secret) value = value.split(secret).join('***');
  return value.replace(/https?:\/\/\S+/g, '[链接]').replace(/[^\s@]+@[^\s@]+/g, '***')
    .replace(/T48![A-Za-z0-9]+/g, '***').replace(/(?<!\d)\d{6}(?!\d)/g, '******').slice(0, 200);
}

const validProbe = item => {
  try {
    return !!item && PROBE_NAME.test(String(item.name)) && new URL(item.url).protocol === 'https:';
  } catch { return false; }
};

/**
 * host: {ready, loadJob, startJob(task), stopJob(), authorize(url|null), callbackResult(ok, message), pickTab(), version()}
 * All requests to the server are sent from here, i.e. from the service worker.
 */
export function createRunner(config, host) {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  // Long waits are cut into slices that touch extension storage so the worker is not idle-killed.
  const idle = async ms => {
    for (const end = Date.now() + ms; Date.now() < end;) {
      await sleep(Math.min(5000, Math.max(0, end - Date.now())));
      await chrome.storage.session.get(KEY);
    }
  };
  const withTimeout = (promise, ms) => Promise.race([promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms))]);

  // ---- Persistent run state (survives service worker restarts) ----
  let stateQueue = Promise.resolve();
  const readState = async () => (await chrome.storage.session.get(KEY))[KEY] || null;
  const updateState = change => {
    const next = stateQueue.then(async () => {
      const state = (await readState()) || {};
      change(state);
      await chrome.storage.session.set({[KEY]: state});
      return state;
    });
    stateQueue = next.catch(() => {});
    return next;
  };
  const halted = state => !state || state.stopping || state.stopped;

  async function request(path, body) {
    let response;
    try {
      response = await fetch(config.base + path, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error',
        signal: AbortSignal.timeout(REQUEST_TIMEOUT),
        headers: {Authorization: `Bearer ${config.token}`, Accept: 'application/json',
          ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch { return {retry: true}; }
    if (response.status === 404) return {gone: true};
    if (response.status >= 500) return {retry: true};
    if (!response.ok) return {rejected: true};
    try { return {ok: true, data: await response.json()}; } catch { return {retry: true}; }
  }

  // 404: the run is over. Stop locally and never contact the server again.
  async function gone() {
    const state = await updateState(s => {
      s.stopped = true;
      if (s.selfcheck?.status === 'running') s.selfcheck.status = 'stopped';
    });
    await chrome.alarms.clear(RUNNER_ALARM).catch(() => {});
    if (state.kind !== 'selfcheck') await host.stopJob().catch(() => {});
    wake();
  }

  // command "stop" (or an unusable authorize link): same as the popup's Stop, one final report.
  async function stopCommand() {
    let first = false;
    const state = await updateState(s => {
      first = !s.stopping && !s.stopped;
      if (!first) return;
      s.stopping = true;
      if (s.selfcheck?.status === 'running') { s.selfcheck.status = 'stopped'; s.selfcheck.message = '服务器已停止自检。'; }
    });
    if (!first) return;
    if (state.kind !== 'selfcheck') await host.stopJob().catch(() => {});
    const report = await currentReport(state);
    // A finished task (callback handed over, self-check done) has nothing left to stop or report.
    if (!(report.body.status === 'done' && report.body.phase !== 'signup')) {
      const {seq} = await updateState(s => { s.seq = (s.seq || 0) + 1; });
      await request('/event', sized({seq, ...report.body, status: 'stopped', pauseReason: null, pauseFinal: false,
        diagnostics: report.diagnostics}));
    }
    await updateState(s => { s.stopped = true; });
    await chrome.alarms.clear(RUNNER_ALARM).catch(() => {});
  }

  // ---- Event reports: immediately on change, every 30 s otherwise, every 5 s while awaiting authorization ----
  async function currentReport(state) {
    if (state.kind === 'selfcheck') {
      const check = state.selfcheck || {};
      const status = ['running', 'done', 'stopped'].includes(check.status) ? check.status : 'running';
      return {key: `selfcheck:${status}`, interval: HEARTBEAT, terminal: status !== 'running',
        body: {status, phase: 'selfcheck', stage: 'unknown', pauseReason: null, pauseFinal: false,
          message: cleanMessage(check.message || '正在自检浏览器环境'), codexResult: 'unknown'},
        diagnostics: {schema: 1, version: host.version(), kind: 'selfcheck', probes: check.results || {}}};
    }
    const job = await host.loadJob();
    if (!job?.runner) {
      const status = state.startError ? 'stopped' : 'running';
      return {key: `nojob:${status}`, interval: HEARTBEAT, terminal: status === 'stopped',
        body: {status, phase: 'signup', stage: 'unknown', pauseReason: null, pauseFinal: false,
          message: cleanMessage(state.startError || '正在准备注册任务'), codexResult: 'unknown'},
        diagnostics: null};
    }
    // The job is "done" as soon as the callback is captured; it is reported done only after
    // the server has accepted the callback.
    const submitting = job.handoff?.status === 'completing';
    const status = submitting ? 'running' : ['running', 'paused', 'stopped', 'done'].includes(job.status) ? job.status : 'running';
    const phase = job.phase === 'oauth' ? 'oauth' : 'signup';
    const stage = STAGES.has(job.stage) ? job.stage : 'unknown';
    const reason = status === 'paused' ? pauseReason(job.pauseReason) : null;
    const final = status === 'paused' && runnerPauseFinal(job);
    const codexResult = CODEX_RESULTS.has(job.codexResult) ? job.codexResult : 'unknown';
    return {key: JSON.stringify([status, phase, stage, reason, final, codexResult, job.handoff?.status || '']),
      interval: status === 'done' && phase === 'signup' ? AUTHORIZE_POLL : HEARTBEAT,
      terminal: status === 'done' || status === 'stopped' || final,
      body: {status, phase, stage, pauseReason: reason, pauseFinal: final, message: cleanMessage(job.message, job), codexResult},
      diagnostics: diagnosticReport(job, host.version())};
  }
  function sized(body) {
    if (body.diagnostics && JSON.stringify(body).length > EVENT_LIMIT) {
      body.diagnostics = {...body.diagnostics, truncated: true, events: (body.diagnostics.events || []).slice(-40)};
    }
    if (JSON.stringify(body).length > EVENT_LIMIT) body.diagnostics = null;
    return body;
  }
  async function sendEvent(report, state) {
    const {seq} = await updateState(s => { s.seq = (s.seq || 0) + 1; });
    // Diagnostics only with the first report of a finished / stopped / final-paused state.
    const result = await request('/event', sized({seq, ...report.body,
      diagnostics: report.terminal && report.key !== state.sentKey ? report.diagnostics : null}));
    if (result.gone) { await gone(); return; }
    // 413/422: do not resend the same body; network/5xx: wait for the next regular report.
    const accepted = !!(result.ok || result.rejected);
    await updateState(s => {
      s.nextAt = Date.now() + report.interval; s.lastOk = accepted;
      if (accepted) s.sentKey = report.key;
    });
    if (result.ok) await command(result.data);
  }
  async function command(data) {
    const state = await readState();
    if (halted(state)) return;
    if (data?.command === 'stop') { await stopCommand(); return; }
    if (data?.command !== 'authorize' || state.kind !== 'signup') return;
    let url = null;
    try { url = new URL(data.authorizeUrl); } catch { /* invalid link */ }
    if (url?.origin !== AUTH_ORIGIN) { await stopCommand(); return; }
    startAuthorize(url.href);
  }
  let authorizing = null;
  function startAuthorize(url) {
    // Repeated "authorize" replies are ignored by host.authorize once the job has a handoff.
    if (!authorizing) authorizing = host.authorize(url).catch(() => {}).finally(() => { authorizing = null; });
  }

  let loopTask = null, wakeUp = null;
  function wake() { const resolve = wakeUp; wakeUp = null; resolve?.(); }
  const nap = ms => new Promise(resolve => {
    const timer = setTimeout(() => { wakeUp = null; resolve(); }, ms);
    wakeUp = () => { clearTimeout(timer); resolve(); };
  });
  function ensureLoop() {
    if (!loopTask) loopTask = reportLoop().catch(() => {}).finally(() => { loopTask = null; });
  }
  // One request in flight at a time; changes made meanwhile go into the next report.
  async function reportLoop() {
    for (;;) {
      const state = await readState();
      if (!state?.started || halted(state)) return;
      const report = await currentReport(state);
      const now = Date.now(), nextAt = state.nextAt || 0;
      const due = report.key !== state.sentKey ? state.lastOk !== false || now >= nextAt : now >= nextAt;
      if (due) await sendEvent(report, state);
      // Short naps that read storage also keep the worker alive between reports.
      else await nap(Math.min(5000, Math.max(200, nextAt - now)));
    }
  }

  // ---- OAuth callback ----
  let callbackTask = null;
  function submitCallback() {
    if (!callbackTask) callbackTask = sendCallback().catch(() => {}).finally(() => { callbackTask = null; });
  }
  async function sendCallback() {
    for (let attempt = 0; attempt <= CALLBACK_RETRIES; attempt++) {
      if (attempt) await idle(CALLBACK_RETRY_DELAY);
      if (halted(await readState())) return;
      const job = await host.loadJob();
      if (!job?.runner || job.handoff?.status !== 'completing') return;
      const url = job.handoff.callbackUrl;
      if (!oauthCallback(url)) { await host.callbackResult(false, '授权回调已丢失。'); return; }
      const result = await request('/callback', {callbackUrl: url});
      if (result.gone) { await gone(); return; }
      if (result.ok && result.data?.ok === true) { await host.callbackResult(true); return; }
      if (result.ok || result.rejected) {
        await host.callbackResult(false, CALLBACK_ERRORS[result.data?.error_code] || '服务器拒绝了授权回调。');
        return;
      }
    }
    await host.callbackResult(false, '无法把授权回调提交给服务器。');
  }

  // ---- Self-check: signals, exit IP, screenshots; no login, no debugger ----
  let selfcheckTask = null;
  function ensureSelfcheck() {
    if (!selfcheckTask) selfcheckTask = runSelfcheck().catch(() => {}).finally(() => { selfcheckTask = null; });
  }
  const selfcheckAlive = async () => {
    const state = await readState();
    return !halted(state) && state.selfcheck?.status === 'running';
  };
  async function postProbe(name, kind, data) {
    for (let attempt = 0; attempt <= PROBE_RETRIES; attempt++) {
      if (attempt) await idle(PROBE_RETRY_DELAY);
      if (!(await selfcheckAlive())) return 'stopped';
      const result = await request('/probe', {name, kind, data});
      if (result.gone) { await gone(); return 'gone'; }
      if (result.ok) return 'ok';
      if (result.rejected) return 'rejected';
    }
    return 'failed';
  }
  async function pageLoaded(tabId, limit = 45000) {
    await idle(1500);
    for (const end = Date.now() + limit; Date.now() < end;) {
      if ((await chrome.tabs.get(tabId)).status === 'complete') break;
      await sleep(500);
    }
    await idle(2000);
  }
  async function probeSignals() {
    const tab = await host.pickTab();
    await chrome.tabs.update(tab.id, {url: 'https://chatgpt.com/', active: true});
    await pageLoaded(tab.id);
    let data = null;
    for (let attempt = 0; attempt < 6 && !data; attempt++) {
      if (attempt) await idle(3000);
      try {
        const reply = await withTimeout(chrome.tabs.sendMessage(tab.id, {type: 'selfcheck-signals'}), 20000);
        if (reply?.ok && reply.data && typeof reply.data === 'object') data = reply.data;
      } catch { /* content script not injected yet */ }
    }
    return data ? postProbe('signals', 'signals', data) : 'no_signals';
  }
  async function probeExit() {
    let data;
    try {
      const response = await fetch('https://ipinfo.io/json', {credentials: 'omit', cache: 'no-store',
        signal: AbortSignal.timeout(REQUEST_TIMEOUT), headers: {Accept: 'application/json'}});
      let body = null;
      try { body = await response.json(); } catch { /* not JSON */ }
      data = body && typeof body === 'object' && !Array.isArray(body) ? {status: response.status, body} :
        {error: `HTTP ${response.status}，响应不是 JSON`};
    } catch (error) { data = {error: error?.name === 'TimeoutError' ? '请求超时' : '请求失败'}; }
    return postProbe('exit', 'exit', data);
  }
  async function probeScreenshot(probe) {
    const tab = await host.pickTab();
    await chrome.tabs.update(tab.id, {url: probe.url, active: true});
    await idle(SCREENSHOT_WAIT);
    if (!(await selfcheckAlive())) return 'stopped';
    const {windowId} = await chrome.tabs.get(tab.id);
    const image = await chrome.tabs.captureVisibleTab(windowId, {format: 'png'});
    if (typeof image !== 'string' || !image.startsWith('data:image/png;base64,')) return 'failed';
    if (image.length > SCREENSHOT_LIMIT) return 'too_large';
    return postProbe(probe.name, 'screenshot', image);
  }
  async function runSelfcheck() {
    const {selfcheck} = await readState();
    const probes = selfcheck.probes || [];
    const steps = ['signals', 'exit', ...probes.map(probe => `screenshot:${probe.name}`)];
    // A restarted worker repeats the step it was in and continues from there.
    for (let step = selfcheck.step || 0; step < steps.length; step++) {
      if (!(await selfcheckAlive())) return;
      const probe = probes[step - 2];
      await updateState(s => {
        s.selfcheck.step = step;
        s.selfcheck.message = step === 0 ? '正在采集浏览器信号' : step === 1 ? '正在查询出口 IP' : `正在截图：${probe.name}`;
      });
      let outcome;
      try { outcome = step === 0 ? await probeSignals() : step === 1 ? await probeExit() : await probeScreenshot(probe); }
      catch { outcome = 'failed'; }
      if (outcome === 'gone' || outcome === 'stopped') return;
      await updateState(s => { s.selfcheck.results[steps[step]] = outcome; s.selfcheck.step = step + 1; });
    }
    if (!(await selfcheckAlive())) return;
    await updateState(s => { s.selfcheck.status = 'done'; s.selfcheck.message = '自检完成'; });
    wake();
  }

  // ---- Start / resume ----
  async function fetchJob() {
    for (let attempt = 0; attempt <= JOB_RETRIES; attempt++) {
      if (attempt) await idle(JOB_RETRY_DELAY);
      const result = await request('/job');
      if (result.gone) { await gone(); return null; }
      if (result.ok) return result.data;
      if (result.rejected) break;
    }
    // Nothing to report without a task; the server ends the run when no heartbeat arrives.
    await updateState(s => { s.stopped = true; });
    await chrome.alarms.clear(RUNNER_ALARM).catch(() => {});
    return null;
  }
  async function begin(task) {
    if (task?.kind === 'selfcheck') {
      const probes = (Array.isArray(task.probeUrls) ? task.probeUrls : []).filter(validProbe).slice(0, 10)
        .map(item => ({name: String(item.name), url: new URL(item.url).href}));
      return updateState(s => Object.assign(s, {started: true, kind: 'selfcheck',
        selfcheck: {status: 'running', step: 0, probes, results: {}, message: '正在自检浏览器环境'}}));
    }
    let startError = '';
    if (task?.kind !== 'signup') startError = '服务器下发的任务类型无效。';
    else if (!(await host.loadJob())?.runner) {
      // (An existing runner job means the worker restarted right after starting it.)
      try {
        if (typeof task.password !== 'string' || !task.password || task.password.length > 256) {
          throw new Error('任务缺少有效密码。');
        }
        const workspaceNames = (Array.isArray(task.workspaceNames) ? task.workspaceNames : [])
          .map(name => String(name || '').trim()).filter(Boolean).slice(0, 5);
        await host.startJob({email: task.email, password: task.password, profile: task.profile, workspaceNames,
          phonePool: task.phonePool === true});
      } catch (error) { startError = `无法开始注册：${error?.message || '未知错误'}`; }
    }
    return updateState(s => Object.assign(s, {started: true, kind: 'signup', ...(startError ? {startError} : {})}));
  }
  async function start() {
    await host.ready;
    let state = await readState();
    if (state?.stopped) return;
    if (state?.stopping) {
      // The worker stopped in the middle of a stop: finish it without more requests.
      await updateState(s => { s.stopped = true; });
      await chrome.alarms.clear(RUNNER_ALARM).catch(() => {});
      return;
    }
    // Restarts a suspended worker: resumes reports, self-check and callback submission.
    if (!(await chrome.alarms.get(RUNNER_ALARM))) await chrome.alarms.create(RUNNER_ALARM, {periodInMinutes: 0.5});
    if (!state?.started) {
      const task = await fetchJob();
      if (!task) return;
      state = await begin(task);
    }
    ensureLoop();
    if (state.kind === 'selfcheck') { ensureSelfcheck(); return; }
    const job = await host.loadJob();
    if (job?.runner && job.handoff?.status === 'completing') submitCallback();
    if (job?.runner && job.handoff?.status === 'opening') startAuthorize(null);
  }
  let booting = null;
  function boot() {
    if (!booting) booting = start().catch(() => {}).finally(() => { booting = null; });
    return booting;
  }

  // ---- Phone relay (docs/contracts/phone-relay.md): one request, the caller owns retries. ----
  // Must not be awaited inside background.js's job queue: a 404 stops the job through it.
  async function phone(body) {
    const state = await readState();
    if (halted(state) || state?.kind !== 'signup') return {ok: false, error_code: 'run_gone', message: '运行已结束。'};
    const result = await request('/phone', body);
    if (result.gone) { await gone(); return {ok: false, error_code: 'run_gone', message: '运行已结束。'}; }
    if (result.ok && result.data && typeof result.data === 'object') return result.data;
    if (result.rejected) return {ok: false, error_code: 'invalid_request', message: '服务器拒绝了接码请求。'};
    return {ok: false, network: true, message: '无法连接服务器接码接口。'};
  }

  return {
    boot,
    // Called after every save of a server-mode job; the report loop decides whether it changed.
    jobSaved: wake,
    submitCallback,
    phone,
  };
}
