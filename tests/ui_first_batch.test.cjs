const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../app/web/static/js/app.js'), 'utf8');
function declaration(name) {
  const start = source.search(new RegExp(`^  (?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1, name);
  const tail = source.slice(start);
  return tail.slice(0, tail.search(/^  }/m) + 4);
}
const env = vm.createContext({});
for (const name of ['attentionReason', 'changedSettings', 'requestActionLabel']) vm.runInContext(declaration(name), env);

test('overview separates the email title from the reason, including legacy messages', () => {
  assert.equal(env.attentionReason({email: 'a.b@example.com', message: 'a.b@example.com · 需 OAuth'}), '需 OAuth');
  assert.equal(env.attentionReason({email: 'a.b@example.com', message: 'HME 标签待同步：a.b@example.com'}), 'HME 标签待同步');
  assert.equal(env.attentionReason({email: 'a.b@example.com', message: '母号尚未配置代理'}), '母号尚未配置代理');
  assert.equal(env.attentionReason({message: '团队尚未同步'}), '团队尚未同步');
  assert.equal(env.attentionReason({email: 'a.b@example.com', message: 'a.b@example.com'}), '请查看账号详情');
});

test('dirty count distinguishes actual settings and groups related proxy changes', () => {
  const baseline = {sub2api_proxy_mode: 'none', sub2api_push: {concurrency: 5, group_ids: [], proxy_id: null, proxy_group_id: null}, auto_rotate: false};
  const changes = patch => [...env.changedSettings(JSON.stringify({...baseline, ...patch}), JSON.stringify(baseline))];
  assert.deepEqual(changes({}), []);
  assert.deepEqual(changes({auto_rotate: true}), ['auto_rotate']);
  assert.deepEqual(changes({sub2api_push: {...baseline.sub2api_push, concurrency: 8, group_ids: [7]}}), ['sub2api_concurrency', 'sub2api_group_ids']);
  assert.deepEqual(changes({sub2api_proxy_mode: 'group'}), ['sub2api_proxy_mode'], 'an unselected new proxy mode must still be discardable');
  assert.deepEqual(changes({sub2api_proxy_mode: 'group', sub2api_push: {...baseline.sub2api_push, proxy_group_id: 9}}), ['sub2api_proxy_mode']);
});

test('empty HTTP errors use a Chinese action label without leaking dynamic keys', async () => {
  env.RequestError = class extends Error { constructor(message, details) { super(message); Object.assign(this, details); } };
  vm.runInContext(declaration('parseJsonResponse'), env);
  for (const [key, label] of [['settings-probe', '检测服务连接'], ['settings-save', '保存设置'], ['workspace-invite-7', '邀请团队成员'], ['rotate-7-private@example.com', '执行受控轮转']]) {
    await assert.rejects(env.parseJsonResponse({ok: false, status: 502, json: async () => { throw new Error('html'); }}, key), error => {
      assert.ok(error.message.includes(`操作没有完成（${label}）`));
      assert.ok(!error.message.includes(key));
      assert.equal(error.status, 502);
      return true;
    });
  }
  await assert.rejects(env.parseJsonResponse({ok: false, status: 409, json: async () => ({detail: {error_code: 'operation_conflict', message: '已有运行中的任务'}})}, 'workspace-invite-7'), error => error.errorCode === 'operation_conflict' && error.message === '已有运行中的任务');
});

test('probing one service preserves every other service result', () => {
  const states = [];
  const context = vm.createContext({setServiceState: (...args) => states.push(args), relativeTime: () => '刚刚'});
  vm.runInContext(declaration('probeCopy'), context);
  context.probeCopy({sub2api: {ok: true, group_count: 3}});
  assert.equal(states.length, 1);
  assert.equal(states[0][0], 'sub2api');
  context.probeCopy({hme: {ok: false, error: '连接超时'}});
  assert.equal(states.length, 2);
  assert.deepEqual(states[1], ['hme', '连接超时', '连接超时']);
});


test('account detail actions obey the registry visible predicate', () => {
  const accountsSource = fs.readFileSync(path.join(__dirname, '../app/web/static/js/accounts-view.js'), 'utf8');
  const start = accountsSource.indexOf('  function decorateDetails(');
  const tail = accountsSource.slice(start);
  const node = () => ({children: [], dataset: {}, append(...items) { this.children.push(...items); }});
  const context = vm.createContext({el: node, healthDetails: node, fmt: {usageWindow: () => null},
    button: label => ({label}), api: {menuButton: node, entityActions: {account: [
      {id: 'account.reauth', label: '手动授权', visible: account => account.allowAuth},
      {id: 'account.quota', label: '刷新额度', visible: account => account.allowQuota},
    ]}}});
  vm.runInContext(tail.slice(0, tail.search(/^  }/m) + 4), context);
  for (const [allowAuth, allowQuota, expected] of [[false, false, []], [true, false, ['手动授权']], [false, true, ['刷新额度']]]) {
    const body = node();
    context.decorateDetails({id: 1, allowAuth, allowQuota}, body);
    const actions = body.children.at(-2);
    assert.deepEqual(actions.children.filter(item => item.label).map(item => item.label), expected);
  }
});
