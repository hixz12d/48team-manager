const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../app/web/static/js/app.js'), 'utf8');
function declaration(name) {
  const start = source.search(new RegExp(`^  (?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1, name);
  const rest = source.slice(start + 1);
  const next = rest.search(/^  (?:async )?function /m);
  return source.slice(start, next < 0 ? undefined : start + 1 + next);
}

function environment() {
  const calls = [];
  const document = {createElement(tag) {
    return {tag, children: [], append(...children) { this.children.push(...children); }};
  }};
  const context = vm.createContext({
    document,
    Option: function(label, value) { this.label = label; this.value = value; },
    setButtonBusy() {},
    postAction: async (key, url, body) => { calls.push({key, url, body}); return {ok: true}; },
    handleActionResult: async () => {},
    reloadTeamDetails: async () => {},
    fetch: () => { throw new Error('Network forbidden'); },
  });
  for (const name of ['inviteSeatControl', 'replenishTeam', 'inviteTeamMember']) {
    vm.runInContext(declaration(name), context);
  }
  return {context, calls};
}

test('seat control defaults to workspace default and never submits on construction', () => {
  const {context, calls} = environment();
  const select = context.inviteSeatControl().children[0];
  assert.equal(select.name, 'seat_intent');
  assert.equal(select.value, 'workspace_default');
  assert.deepEqual(select.children.map(option => option.value), ['workspace_default', 'standard', 'premium']);
  assert.equal(calls.length, 0);
});

test('invite and replenish send the selected intent, defaulting without Premium', async () => {
  for (const seat of [undefined, 'workspace_default', 'standard', 'premium']) {
    const {context, calls} = environment();
    await context.inviteTeamMember({id: 7}, {email_line: 'fixture@example.test', role: 'member', seat_intent: seat}, {});
    await context.replenishTeam({id: 7}, {}, {seat_intent: seat});
    assert.equal(calls.length, 2);
    for (const call of calls) assert.equal(call.body.seat_intent, seat || 'workspace_default');
    assert.equal(calls[0].body.role, 'member');
  }
});

test('all manual invitation forms expose the same seat control', () => {
  for (const name of ['renderTeamInviteControls', 'renderTeamReplenishControls', 'openReinviteForm']) {
    assert.match(declaration(name), /inviteSeatControl\(\)/);
  }
  assert.match(declaration('openReinviteForm'), /seat_intent: seatControl\.querySelector\("select"\)\.value/);
});
