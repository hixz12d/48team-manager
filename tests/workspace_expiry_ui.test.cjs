const test = require('node:test');
const assert = require('node:assert/strict');
const {today, addMonths, summary} = require('../app/web/static/js/workspace-expiry.js');

const now = new Date('2026-09-12T04:00:00Z');
for (const [date, days, label, tone] of [
  [null, null, '尚未填写', 'muted'],
  ['2026-09-11', -1, '已过期 1 天', 'error'],
  ['2026-09-12', 0, '今天到期', 'warning'],
  ['2026-09-13', 1, '剩余 1 天', 'warning'],
  ['2026-09-19', 7, '剩余 7 天', 'warning'],
  ['2026-09-20', 8, '剩余 8 天', 'neutral'],
]) test(`manual reminder ${date}`, () => {
  const value = summary({date}, now);
  assert.equal(value.days, days);
  assert.equal(value.label, label);
  assert.equal(value.tone, tone);
});

test('countdown follows Beijing calendar days at UTC midnight boundary', () => {
  assert.equal(today(new Date('2026-09-12T15:59:59Z')), '2026-09-12');
  assert.equal(today(new Date('2026-09-12T16:00:00Z')), '2026-09-13');
  assert.equal(summary({date:'2026-09-12'}, new Date('2026-09-12T16:00:00Z')).days, -1);
});

test('shortcuts clamp month ends and leap days rather than overflowing', () => {
  assert.equal(addMonths('2026-01-31', 1), '2026-02-28');
  assert.equal(addMonths('2028-01-31', 1), '2028-02-29');
  assert.equal(addMonths('2028-02-29', 12), '2029-02-28');
  assert.equal(addMonths('2026-12-31', 1), '2027-01-31');
  assert.equal(addMonths('9999-12-31', 1), '');
});

test('invalid dates never produce a plausible expiry countdown', () => {
  for (const date of ['2026-02-29', '2026-04-31', '0000-01-01', 'bad', '2026-9-1', 1789200000]) {
    assert.equal(summary({date}, now).days, null);
    assert.equal(addMonths(date, 1), '');
  }
});
