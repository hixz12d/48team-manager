const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/web/static/js/app.js'), 'utf8');
function declaration(name) {
  const start = source.search(new RegExp(`^  (?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1, name);
  const tail = source.slice(start);
  return tail.slice(0, tail.search(/^  }/m) + 4);
}
function env(stored = []) {
  const notifications = [], timers = new Map(), requests = [], currentOperations = new Map();
  let serial = 0, storage = JSON.stringify(stored), refreshes = 0;
  const context = vm.createContext({
    currentOperations, CURRENT_OPERATION_STORAGE: 'fixture',
    ACTIVE_OPERATION_STATES: new Set(['queued', 'running', 'waiting']),
    TERMINAL_OPERATION_STATES: new Set(['success', 'failed', 'cancelled', 'partial', 'manual_required']),
    sessionStorage: {getItem: () => storage, setItem: (_, value) => { storage = value; }},
    window: {setTimeout(fn) { const id = ++serial; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); }},
    fetch: async url => { requests.push(url); return context.response; },
    response: {state: 'failed', error: 'old failure'},
    parseJsonResponse: async response => response,
    AbortController, Date,
    syncAutoReauthButtons() {},
    friendlyError: value => value,
    toast: (...args) => notifications.push(args),
    isAbortError: error => error.name === 'AbortError',
    teamDetailState: null, overlayState: {},
    bootPage: async () => { refreshes++; },
  });
  for (const name of ['persistCurrentOperations', 'cancelCurrentOperationPolling', 'operationPollDelay', 'operationTone',
    'finishCurrentOperation', 'pollCurrentOperation', 'resumeCurrentOperations', 'stopPolling']) {
    vm.runInContext(declaration(name), context);
  }
  return {context, currentOperations, notifications, timers, requests, stored: () => JSON.parse(storage), refreshes: () => refreshes};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('refresh silently reconciles previously finished operations, without a toast storm', async () => {
  const fixture = Array.from({length: 20}, (_, i) => ({stableKey: `k${i}`, operationId: `public-${i}`, state: 'queued'}));
  const e = env(fixture);
  e.context.resumeCurrentOperations(); await tick();
  assert.equal(e.requests.length, 20);
  assert.equal(e.notifications.length, 0);
  assert.equal(e.currentOperations.size, 0);
  assert.equal(e.refreshes(), 0);
  assert.deepEqual(e.stored(), []);
  e.context.resumeCurrentOperations(); await tick();
  assert.equal(e.requests.length, 20);
});

test('operation detail state success is not misreported as failure and public id is used', async () => {
  const e = env();
  e.currentOperations.set('k', {operationId: 'public-id'});
  await e.context.finishCurrentOperation('k', {state: 'success', operation_id: 17, result: {success: true, message: 'done'}});
  assert.equal(e.notifications[0][0], 'done');
  assert.equal(e.notifications[0][1], 'success');
  await e.context.finishCurrentOperation('k', {state: 'failed'});
  assert.equal(e.notifications.length, 1);
});

test('restored active operation still notifies when it later completes', async () => {
  const e = env([{stableKey: 'k', operationId: 'public-id', state: 'queued'}]);
  e.context.response = {state: 'running'};
  e.context.resumeCurrentOperations(); await tick();
  assert.equal(e.currentOperations.get('k').restored, false);
  e.context.response = {state: 'failed', error: 'new failure'};
  await e.context.pollCurrentOperation('k');
  assert.equal(e.notifications.length, 1);
  assert.equal(e.notifications[0][0], 'new failure');
});

test('hidden then visible resumes existing stopped polling without duplicate requests', async () => {
  const e = env([{stableKey: 'k', operationId: 'public-id', state: 'queued'}]);
  e.context.response = {state: 'running'};
  e.context.resumeCurrentOperations(); await tick();
  e.context.stopPolling();
  e.context.resumeCurrentOperations(); await tick();
  assert.equal(e.requests.length, 2);
  e.context.resumeCurrentOperations(); await tick();
  assert.equal(e.requests.length, 2);
});

test('late response cannot finish a replacement operation with the same stable key', async () => {
  const e = env(); let resolve;
  e.context.fetch = () => new Promise(r => { resolve = r; });
  e.currentOperations.set('k', {operationId: 'old'});
  const pending = e.context.pollCurrentOperation('k');
  e.currentOperations.set('k', {operationId: 'new'});
  resolve({state: 'failed'}); await pending;
  assert.equal(e.currentOperations.get('k').operationId, 'new');
  assert.equal(e.notifications.length, 0);
});

test('persisted terminal records are pruned without fetching', () => {
  const e = env([{stableKey: 'k', operationId: 'old', state: 'success'}]);
  e.context.resumeCurrentOperations();
  assert.equal(e.requests.length, 0);
  assert.deepEqual(e.stored(), []);
});

test('toast region never grows beyond three visible messages', () => {
  const region = {children: [], get firstElementChild() { return this.children[0]; }, append(item) { this.children.push(item); }};
  const context = vm.createContext({document: {getElementById: () => region, createElement: () => {
    const node = {append() {}, setAttribute() {}, remove() { region.children = region.children.filter(n => n !== node); }};
    return node;
  }}, window: {setTimeout() {}}});
  vm.runInContext(declaration('toast'), context);
  for (let i = 0; i < 20; i++) context.toast('error', 'error');
  assert.equal(region.children.length, 3);
});

test('local team deletion requires confirmation and sends only the local DELETE endpoint', async () => {
  let confirmed = false, confirmation, deletes = 0, refreshed = 0;
  const context = vm.createContext({
    URL, window: {location: {href: 'https://fixture.invalid/accounts?team=7'}, history: {replaceState() {}}},
    document: {body: {dataset: {page: 'accounts'}}}, teamDetailState: null,
    openConfirm: async input => { confirmation = input; return confirmed; },
    setButtonBusy() {},
    deleteAction: async (key, url) => { assert.equal(url, '/api/workspaces/7'); deletes++; return {ok: true}; },
    handleActionResult: async () => { refreshed++; },
    toast: () => assert.fail('unexpected failure'), friendlyError: error => error.message,
  });
  vm.runInContext(declaration('deleteLocalTeam'), context);
  await context.deleteLocalTeam({id: 7, name: 'Fixture'}, {});
  assert.equal(deletes, 0);
  assert.ok(confirmation.items.some(text => text.includes('凭据')));
  assert.ok(confirmation.hint.includes('不会取消官方订阅'));
  confirmed = true;
  await context.deleteLocalTeam({id: 7, name: 'Fixture'}, {});
  assert.equal(deletes, 1); assert.equal(refreshed, 1);
});
