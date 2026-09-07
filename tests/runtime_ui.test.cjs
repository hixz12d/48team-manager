const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function source(name) { return fs.readFileSync(path.join(__dirname, '../app/web/static/js', name), 'utf8'); }
function bus(extra = {}) {
  const listeners = new Map();
  return Object.assign(extra, {
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    emit(type, event = {}) { for (const fn of listeners.get(type) || []) fn(event); },
    listeners,
  });
}
function pollingEnvironment() {
  const window = bus();
  const document = bus({ visibilityState: 'visible' });
  const timers = new Map(); let serial = 0;
  vm.runInNewContext(source('polling.js'), {
    window, document, AbortController, Promise,
    setTimeout(fn, ms) { const id = ++serial; timers.set(id, {fn, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  return {window, document, timers, create: window.Team48Polling.createPoller};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('polling coalesces overlapping refreshes and keeps only one timer', async () => {
  const env = pollingEnvironment(); let resolve, reads = 0;
  const poller = env.create({read: () => { reads++; return new Promise(r => { resolve = r; }); }, onData() {}, delay: () => 2000});
  const first = poller.refresh(); await tick();
  for (let i = 0; i < 10; i++) void poller.refresh();
  assert.equal(reads, 1); assert.equal(env.timers.size, 0);
  resolve({}); await first;
  assert.equal(env.timers.size, 1);
  poller.destroy(); assert.equal(env.timers.size, 0);
});

test('visibility pauses, aborts a read, and restores immediately', async () => {
  const env = pollingEnvironment(); let reads = 0, signal;
  const poller = env.create({read: s => { reads++; signal = s; return new Promise((resolve, reject) => s.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name:'AbortError'})))); }, onData() { assert.fail('aborted result rendered'); }});
  void poller.refresh(); await tick();
  env.document.visibilityState = 'hidden'; env.document.emit('visibilitychange'); await tick();
  assert.equal(signal.aborted, true); assert.equal(env.timers.size, 0);
  env.document.visibilityState = 'visible'; env.document.emit('visibilitychange'); await tick();
  assert.equal(reads, 2);
  poller.destroy(); await tick(); assert.equal(env.timers.size, 0);
});

test('pagehide cleans timers even while document is visible, pageshow restarts', async () => {
  const env = pollingEnvironment(); let reads = 0;
  const poller = env.create({read: async () => ++reads, onData() {}});
  await poller.refresh(); assert.equal(env.timers.size, 1);
  env.window.emit('pagehide'); assert.equal(env.timers.size, 0);
  env.window.emit('pageshow', {persisted: true}); await tick();
  assert.equal(reads, 2); assert.equal(env.timers.size, 1);
  poller.destroy(); assert.equal(env.window.listeners.get('pageshow').size, 0);
});

test('failure backoff preserves last data and recovers without duplicate timers', async () => {
  const env = pollingEnvironment(); let fail = false, renders = 0, errors = 0;
  const poller = env.create({read: async () => { if (fail) throw new Error('offline'); return {}; }, onData: () => { renders++; }, onError: () => { errors++; }, delay: () => 15000});
  await poller.refresh(); fail = true;
  await poller.refresh(); assert.equal([...env.timers.values()][0].ms, 3000);
  await poller.refresh(); assert.equal([...env.timers.values()][0].ms, 6000);
  assert.equal(renders, 1); assert.equal(errors, 2); assert.equal(env.timers.size, 1);
  fail = false; await poller.refresh();
  assert.equal(renders, 2); assert.equal([...env.timers.values()][0].ms, 15000);
  poller.destroy();
});

const window = { Team48Format: {} };
vm.runInNewContext(source('accounts-view.js'), { window, localStorage: {getItem: () => null}, URLSearchParams, Set });
const {subscriptionLabel} = window.Team48Accounts;
const subscription = {plan_family:'business',seat_tier:'premium',status:'unverified',observed_at:null};
test('Business does not infer a tier from role, email, or legacy fields', () => {
  assert.equal(subscriptionLabel({email:'premium@example.com',official_role:'owner',seat_type:'premium',subscription}), 'Business · 档位未识别');
});
test('verified presentation still requires an observation timestamp', () => {
  assert.equal(subscriptionLabel({subscription:{...subscription,status:'verified'}}), 'Business · 档位未识别');
  assert.equal(subscriptionLabel({subscription:{...subscription,status:'verified',observed_at:'2026-09-01'}}), 'Business · Premium');
});
test('stale tiers are marked and multiple workspaces are not merged', () => {
  assert.equal(subscriptionLabel({subscription:{...subscription,status:'stale',seat_tier:'standard',observed_at:'2026-09-01'}}), 'Business · Standard · 数据待刷新');
  assert.equal(subscriptionLabel({contexts:[{},{}],subscription}), '席位按团队查看');
});
