export const AUTH_HOSTS = new Set(['chatgpt.com', 'auth.openai.com', 'auth0.openai.com']);
export const ACTIVE = new Set(['running', 'paused']);

export function defaultProfile(now = new Date()) {
  const pick = values => values[crypto.getRandomValues(new Uint32Array(1))[0] % values.length];
  const integer = (min, max) => min + crypto.getRandomValues(new Uint32Array(1))[0] % (max - min + 1);
  const first = pick(['James', 'Oliver', 'Henry', 'Noah', 'Ethan', 'Lucas', 'Daniel', 'Benjamin',
    'Jack', 'Leo', 'William', 'Samuel', 'Liam', 'Owen', 'Nathan', 'Ryan', 'Emma', 'Olivia', 'Amelia',
    'Charlotte', 'Sophia', 'Isabella', 'Mia', 'Grace', 'Lily', 'Chloe', 'Emily', 'Sophie', 'Hannah',
    'Sarah', 'Anna', 'Nora']);
  const middle = pick(['', '', '', '', '', '', '', '', 'A.', 'J.', 'M.', 'Lee', 'Ann', 'Marie']);
  const last = pick(['Smith', 'Taylor', 'Wilson', 'Brown', 'Martin', 'Clark', 'Walker', 'Hall',
    'Allen', 'Young', 'King', 'Wright', 'Scott', 'Green', 'Adams', 'Baker', 'Nelson', 'Carter',
    'Mitchell', 'Turner', 'Parker', 'Evans', 'Collins', 'Bennett', 'Reed', 'Murphy', 'Cook', 'Bailey']);
  const age = pick([24, 25, 26, 26, 27, 27, 28, 28, 28, 29, 29, 30, 30, 31, 31, 32, 32, 33, 34, 35,
    22, 23, 36, 38, 40, 42]);
  const month = integer(1, 12);
  let year = now.getFullYear() - age - (month > now.getMonth() + 1 ? 1 : 0);
  const day = integer(1, Math.min(new Date(year, month, 0).getDate(), new Date(year - 1, month, 0).getDate()));
  if (month === now.getMonth() + 1 && day > now.getDate()) {
    year--;
  }
  return {name: [first, middle, last].filter(Boolean).join(' '), birthday: `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`};
}

export function newPassword() {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  return `T48!${Array.from(bytes, value => alphabet[value % alphabet.length]).join('')}`;
}

export function contentAllowed(sender, job) {
  try {
    const url = new URL(sender.url);
    return sender.frameId === 0 && sender.tab?.incognito === true && sender.tab.id === job?.tabId &&
      url.protocol === 'https:' && AUTH_HOSTS.has(url.hostname);
  } catch { return false; }
}

export function publicHandoff(handoff) {
  if (!handoff) return null;
  const step = value => value && typeof value === 'object' ?
    {ok: typeof value.ok === 'boolean' ? value.ok : null, message: String(value.message || '').slice(0, 200)} : null;
  return {status: handoff.status, workspaceId: handoff.workspaceId, workspaceName: handoff.workspaceName || '',
    message: handoff.message || '', followups: {sub2api: step(handoff.followups?.sub2api), switchCount: step(handoff.followups?.switch_count)}};
}

export function publicJob(job) {
  if (!job) return null;
  return {id: job.id, email: job.email, status: job.status, stage: job.stage, message: job.message,
    mode: job.mode || 'auto', codexResult: job.codexResult || 'unknown', password: job.password, tabId: job.tabId, expiresAt: job.expiresAt,
    phase: job.phase === 'oauth' ? 'oauth' : 'signup', handoff: publicHandoff(job.handoff), formSeen: !!job.entryFormSeen,
    autoHandoff: job.autoHandoff ? {workspaceId: job.autoHandoff.workspaceId, workspaceName: job.autoHandoff.workspaceName || ''} : null};
}

// Only a real Codex CLI loopback callback is ever forwarded to Team48.
export function oauthCallback(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname) && url.port === '1455' &&
      url.pathname === '/auth/callback' && url.searchParams.has('state') &&
      (url.searchParams.has('code') || url.searchParams.has('error'));
  } catch { return false; }
}


export const MODES = new Set(['auto', 'review', 'submit', 'manual']);
export const STAGES = new Set(['signup', 'email', 'password', 'otp', 'profile', 'consent', 'home', 'unknown']);
const EVENTS = new Set(['started', 'page', 'filled', 'submit_attempt', 'form_submit', 'manual_submit',
  'waiting_manual', 'code_received', 'paused', 'resumed', 'stopped', 'expired', 'session_check',
  'session_error', 'email_mismatch', 'email_unverified', 'completed', 'captcha', 'phone', 'rate_limit', 'entry_fallback', 'click_response', 'continue_retry',
  'oauth_started', 'oauth_callback', 'session_anonymous', 'manual_complete']);
const PAGES = new Set(['chatgpt', 'signup', 'password', 'email_verification', 'profile', 'phone', 'consent', 'auth_other', 'unknown']);
const CLICK_OUTCOMES = new Set(['submitted', 'loading', 'advanced', 'validation', 'timeout']);

export function checkedProfile(value) {
  const name = String(value?.name || '').trim();
  const birthday = String(value?.birthday || '');
  if (!name && !birthday) return null;
  if (!name || name.length > 80 || /[\r\n\t]/.test(name) || !/^\d{4}-\d{2}-\d{2}$/.test(birthday)) {
    throw new Error('请同时填写姓名和有效生日，或将两项都留空。');
  }
  const [year, month, day] = birthday.split('-').map(Number);
  const date = new Date(year, month - 1, day), now = new Date();
  if (date.getFullYear() !== year || date.getMonth() + 1 !== month || date.getDate() !== day ||
      date > now || now.getFullYear() - year > 120) throw new Error('生日无效，请检查日期。');
  return {name, birthday};
}

export function pageKind(value) {
  try {
    const {hostname, pathname} = new URL(value);
    if (hostname === 'chatgpt.com') return 'chatgpt';
    if (!AUTH_HOSTS.has(hostname)) return 'unknown';
    if (/add-phone|phone-verification|verify-phone/.test(pathname)) return 'phone';
    if (/consent/.test(pathname)) return 'consent';
    if (/about-you/.test(pathname)) return 'profile';
    if (/email-verification/.test(pathname)) return 'email_verification';
    if (/password/.test(pathname)) return 'password';
    if (/create-account|sign-up/.test(pathname)) return 'signup';
    return 'auth_other';
  } catch { return 'unknown'; }
}

// Strict allowlists: never retain arbitrary URLs, page text, field values or error bodies.
export function recordEvent(job, event, {page, stage, verified, outcome} = {}) {
  if (!EVENTS.has(event)) return false;
  const entry = {ms: Math.max(0, Date.now() - (job.startedAt || Date.now())), event};
  if (PAGES.has(page)) entry.page = page;
  if (STAGES.has(stage)) entry.stage = stage;
  if (typeof verified === 'boolean') entry.verified = verified;
  if (event === 'click_response' && CLICK_OUTCOMES.has(outcome)) entry.outcome = outcome;
  job.events ||= [];
  const previous = job.events.at(-1);
  if (previous && event === 'page' && previous.event === event && previous.page === entry.page && previous.stage === entry.stage) return false;
  job.events.push(entry);
  if (job.events.length > 200) { job.events.shift(); job.eventsTruncated = true; }
  if (event === 'page' && stage === 'otp') job.otpSeen = true;
  return true;
}

export function diagnosticReport(job, version) {
  return {
    schema: 1, version, mode: MODES.has(job.mode) ? job.mode : 'auto',
    status: ['running', 'paused', 'stopped', 'done'].includes(job.status) ? job.status : 'unknown',
    profileSource: job.profileSource === 'custom' ? 'custom' : job.profileSource === 'manual' ? 'manual' : 'generated',
    input: job.inputMode === 'cdp' ? 'cdp' : job.inputMode === 'synthetic' ? 'synthetic' : 'unknown',
    userReportedCodexResult: ['no_phone', 'phone_required', 'failed'].includes(job.codexResult) ? job.codexResult : 'unknown',
    summary: {otpPageSeen: !!job.otpSeen, emailMatched: job.status === 'done',
      emailVerified: typeof job.emailVerified === 'boolean' ? job.emailVerified : 'unknown'},
    attempts: Object.fromEntries([...STAGES].filter(stage => Number.isInteger(job.attempts?.[stage]))
      .map(stage => [stage, job.attempts[stage]])),
    truncated: !!job.eventsTruncated,
    events: (job.events || []).filter(entry => EVENTS.has(entry.event)).map(entry => ({
      ms: Number.isFinite(entry.ms) ? entry.ms : 0, event: entry.event,
      ...(PAGES.has(entry.page) ? {page: entry.page} : {}),
      ...(STAGES.has(entry.stage) ? {stage: entry.stage} : {}),
      ...(typeof entry.verified === 'boolean' ? {verified: entry.verified} : {}),
      ...(entry.event === 'click_response' && CLICK_OUTCOMES.has(entry.outcome) ? {outcome: entry.outcome} : {}),
    })),
  };
}
