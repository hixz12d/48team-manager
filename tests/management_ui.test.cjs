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

const {filteredEntries, pageWindow} = window.Team48Accounts;
test('pagination splits large teams while retaining each workspace context', () => {
  const members = Array.from({length: 25}, (_, i) => ({id:i + 1, email:`child${i}@example.com`, workspace_id:7}));
  const data = {groups:[{id:7, members}], unassigned:[{id:30, email:'free@example.com'}]};
  const entries = filteredEntries(data, 'teams', new URLSearchParams());
  const first = pageWindow(entries, 1, 20), second = pageWindow(entries, 2, 20);
  assert.equal(first.items.length, 20); assert.equal(second.items.length, 6);
  assert.equal(second.items[0].account.id, 21);
  assert.equal(second.items[0].group.id, 7);
  assert.equal(second.items[5].group, null);
});
test('filters apply before pagination and deleting the final page clamps to the last page', () => {
  const accounts = Array.from({length: 21}, (_, i) => ({id:i + 1, purpose:i < 10 ? 'child' : 'standby'}));
  const entries = filteredEntries({accounts}, 'all', new URLSearchParams('purpose=standby'));
  assert.equal(entries.length, 11);
  assert.equal(pageWindow(entries, 5, 10).page, 2);
  assert.equal(pageWindow(entries.slice(0, 10), 2, 10).page, 1);
  assert.equal(pageWindow(entries, '-3', 'bad').size, 20);
  assert.equal(pageWindow([], 100, 20).page, 1);
});
test('attention and remote filters still inspect all rows before slicing', () => {
  const accounts = [{id:1, health:{code:'healthy'}, remote_status:{state:'missing'}}, {id:2, health:{code:'healthy'}, remote_status:{state:'healthy'}}];
  assert.equal(filteredEntries({accounts}, 'attention', new URLSearchParams()).length, 1);
  assert.equal(filteredEntries({accounts}, 'all', new URLSearchParams('remote=missing'))[0].account.id, 1);
});
