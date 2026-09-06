const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { formatCost, usageWindow } = require('../app/web/static/js/formatters.js');

const cases = [[null, '—'], [undefined, '—'], ['', '—'], ['bad', '—'], [false, '—'],
  ['0.0000000000', '$0.00'], ['-0.000', '$0.00'], ['12.3450000000', '$12.35'],
  ['0.0048312000', '<$0.01'], ['-0.001', '>−$0.01'], ['1234567.8901', '$1,234,567.89'],
  ['999.995', '$1,000.00'], ['1e-9', '<$0.01'], ['1.2345e2', '$123.45'],
  ['9007199254740993.12', '$9,007,199,254,740,993.12'], ['1e1001', '—']];
for (const [input, expected] of cases) test(`display money ${String(input)}`, () => assert.equal(formatCost(input), expected));

test('billing fallback picks a successful window and labels it honestly', () => {
  const usage = {windows: {seven_day: {}, today: {last_success_at:'2026-09-01', user_cost:'4.00001'}}};
  assert.equal(usageWindow(usage).label, '今日');
  assert.equal(usageWindow(usage).user_cost, '4.00001');
  assert.equal(usageWindow({}), null);
});

const window = { Team48Format: {formatCost, usageWindow} };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/web/static/js/accounts-view.js'), 'utf8'), {
  window, localStorage: {getItem: () => null}, URLSearchParams, Set,
});
const {matches, viewName} = window.Team48Accounts;
test('old links select the corresponding unified view', () => {
  assert.equal(viewName('portfolio'), 'teams'); assert.equal(viewName('flat'), 'all');
});
test('member search preserves group context without matching unrelated members', () => {
  const group = {name:'North', official_workspace_id:'ws-123'};
  assert.equal(matches({email:'member@example.com'}, group, new URLSearchParams('q=member')), true);
  assert.equal(matches({email:'owner@example.com'}, group, new URLSearchParams('q=member')), false);
  assert.equal(matches({email:'owner@example.com'}, group, new URLSearchParams('q=ws-123')), true);
});
test('only actual current-credential 401 matches HTTP filter', () => {
  const params = new URLSearchParams('health=401');
  assert.equal(matches({latest_check:{http_status:null,error_code:'token_revoked'}}, null, params), false);
  assert.equal(matches({latest_check:{http_status:401,current_credential:false}}, null, params), false);
  assert.equal(matches({latest_check:{http_status:401,current_credential:true}}, null, params), true);
});
test('official owner role does not change local child filtering', () => {
  const a = {purpose:'child', official_role:'owner'};
  assert.equal(matches(a, null, new URLSearchParams('purpose=child')), true);
  assert.equal(matches(a, null, new URLSearchParams('purpose=mother')), false);
});
