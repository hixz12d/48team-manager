const test = require('node:test');
const assert = require('node:assert/strict');
require('../app/web/static/js/workspace-expiry.js');
const {countForToday, millisecondsUntilReset} = require('../app/web/static/js/workspace-switch-count.js');

test('counts reset exactly at Beijing midnight across a year boundary', () => {
  const record = {date: '2026-12-31', count: 8};
  const before = new Date('2026-12-31T15:59:59.999Z');
  const midnight = new Date('2026-12-31T16:00:00Z');
  assert.equal(countForToday(record, before), 8);
  assert.equal(countForToday(record, midnight), 0);
  assert.equal(millisecondsUntilReset(before), 1);
  assert.equal(millisecondsUntilReset(midnight), 86400000);
  assert.equal(countForToday({date: '2027-01-01', count: 1}, midnight), 1);
});

test('empty and older records display zero, regardless of the browser timezone', () => {
  const now = new Date('2026-06-01T17:00:00Z');
  assert.equal(countForToday(undefined, now), 0);
  assert.equal(countForToday({date: '2026-06-01', count: 12}, now), 0);
  assert.equal(countForToday({date: '2026-06-02', count: 3}, now), 3);
  assert.equal(millisecondsUntilReset(now), 23 * 3600000);
});
