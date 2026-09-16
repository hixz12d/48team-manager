const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const window = {Team48Format: {}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/web/static/js/accounts-view.js'), 'utf8'), {
  window, localStorage: {getItem: () => null}, URLSearchParams, Set,
});
const {matches} = window.Team48Accounts;
const filter = (account, value) => matches(account, null, new URLSearchParams(`remote=${value}`));

test('remote deletion is independent of official quota and local authorization', () => {
  const account = {health: {code:'healthy'}, quota:{seven_day_used_percent:0}, remote_status:{state:'missing'}};
  assert.equal(filter(account, 'missing'), true);
  assert.equal(filter(account, 'healthy'), false);
});
test('unreachable remote is unknown, never deleted', () => {
  assert.equal(filter({remote_status:{state:'unknown', last_known_state:'missing'}}, 'missing'), false);
  assert.equal(filter({remote_status:{state:'unknown'}}, 'unknown'), true);
});
test('flat account filters inspect every workspace binding', () => {
  const account = {contexts:[{remote_status:{state:'healthy'}}, {remote_status:{state:'missing'}}]};
  assert.equal(filter(account, 'missing'), true);
  assert.equal(filter(account, 'paused'), false);
});
test('unassigned account with several historical bindings still exposes deletion', () => {
  const account = {remote_status:{state:'multiple', bindings:[{state:'paused'}, {state:'missing'}]}};
  assert.equal(filter(account, 'missing'), true);
  assert.equal(filter(account, 'healthy'), false);
});
