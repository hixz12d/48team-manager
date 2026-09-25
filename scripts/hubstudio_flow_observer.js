/* Observation only: no input values, page dumps, clicks, submits or fetches. */
(() => {
  const key = '__team48FlowObserverV1';
  const binding = '__team48FlowEvent';
  const allowed = new Set(['chatgpt.com', 'auth.openai.com', 'auth0.openai.com']);
  if (window !== window.top || !allowed.has(location.hostname) || window[key]) return;
  const labels = new Map([
    ['continue', 'continue'], ['next', 'continue'], ['继续', 'continue'], ['下一步', 'continue'],
    ['log in', 'login'], ['login', 'login'], ['登录', 'login'],
    ['sign up', 'signup'], ['create account', 'signup'], ['注册', 'signup'],
    ['finish creating account', 'finish_signup'], ['create your account', 'finish_signup'],
    ['remove', 'remove'], ['remove member', 'remove'], ['remove user', 'remove'], ['移除', 'remove'],
    ['移除成员', 'remove'], ['delete', 'delete'], ['删除', 'delete'],
    ['invite', 'invite'], ['invite members', 'invite'], ['invite member', 'invite'],
    ['invite team members', 'invite'], ['send invites', 'send_invites'], ['send invitations', 'send_invites'],
    ['邀请', 'invite'], ['邀请成员', 'invite'], ['发送邀请', 'send_invites'],
    ['confirm', 'confirm'], ['确认', 'confirm'], ['accept', 'accept'], ['accept invitation', 'accept'],
    ['join workspace', 'join'], ['加入工作空间', 'join'],
    ['allow', 'allow'], ['authorize', 'authorize'], ['授权', 'authorize'],
    ['verify', 'verify'], ['验证', 'verify'], ['resend code', 'resend'], ['重新发送验证码', 'resend'],
    ['owner', 'owner'], ['member', 'member'], ['所有者', 'owner'], ['成员', 'member'],
    ['premium', 'premium'], ['standard', 'standard'],
    ['skip', 'skip'], ['skip for now', 'skip'], ['maybe later', 'later'], ['暂时跳过', 'skip'],
    ['something else', 'something_else'], ['continue to workspace', 'continue_workspace'],
    ['cancel', 'cancel'], ['取消', 'cancel'], ['back', 'back'], ['返回', 'back'],
    ['start', 'start'], ['开始注册 ↗', 'start'], ['继续使用电子邮件', 'email_login'],
    ['continue with email', 'email_login'], ['use email', 'email_login'],
  ]);
  const fieldKind = e => {
    if (!e?.matches?.('input,select,textarea,[contenteditable="true"]')) return 'none';
    const type = (e.getAttribute('type') || '').toLowerCase();
    const hint = [e.getAttribute('autocomplete'), e.getAttribute('name'), e.id].join(' ').toLowerCase();
    if (type === 'password') return 'password';
    if (type === 'email' || /email/.test(hint)) return 'email';
    if (type === 'tel' || /phone/.test(hint)) return 'phone';
    if (/one-time-code|otp|verification/.test(hint)) return 'otp';
    if (/birth|birthday|age|bday/.test(hint) || type === 'date') return 'profile';
    if (/name/.test(hint)) return 'profile';
    return 'other';
  };
  const page = () => {
    const path = location.pathname;
    if (/add-phone|phone-verification|verify-phone/.test(path)) return 'phone';
    if (/consent/.test(path)) return 'consent';
    if (/email-verification/.test(path)) return 'otp';
    if (/about-you/.test(path)) return 'profile';
    if (/password/.test(path)) return 'password';
    if (/\/admin\/members/.test(path)) return 'members';
    if (/create-account|sign-up/.test(path)) return 'signup';
    if (/\/auth\/login|\/log-in/.test(path)) return 'login';
    if (location.hostname === 'chatgpt.com' && path === '/') return 'home';
    return 'other';
  };
  const send = (kind, data = {}) => {
    if (typeof window[binding] !== 'function') return;
    window[binding](JSON.stringify({kind, page: page(), ...data}));
  };
  const click = event => {
    const e = event.target?.closest?.('button,a,input,[role="button"],[role="menuitem"],[role="option"]');
    if (!e) return;
    // Match a small known vocabulary locally; never send arbitrary text or attributes.
    const label = labels.get((e.textContent || '').trim().toLowerCase()) ||
      labels.get((e.getAttribute('aria-label') || '').trim().toLowerCase()) || 'other';
    send('click', {label, field: fieldKind(e), trusted: event.isTrusted});
  };
  const focus = event => {
    const field = fieldKind(event.target);
    if (field !== 'none') send('focus', {field, trusted: event.isTrusted});
  };
  const submit = event => send('submit', {trusted: event.isTrusted});
  let last = '';
  const snapshot = () => {
    const visible = e => !!e.getClientRects().length;
    const fields = [...new Set([...document.querySelectorAll('input,select')].filter(visible).map(fieldKind))].sort();
    const data = {page: page(), fields,
      composer: !!document.querySelector('#prompt-textarea'),
      dialog: [...document.querySelectorAll('[role="dialog"]')].some(visible),
      phone: [...document.querySelectorAll('h1,h2')].some(e => /^(phone number required|验证手机号|需要手机号码)$/i.test(e.textContent.trim()))};
    const encoded = JSON.stringify(data);
    if (encoded !== last) { last = encoded; send('snapshot', data); }
  };
  document.addEventListener('click', click, true);
  document.addEventListener('focusin', focus, true);
  document.addEventListener('submit', submit, true);
  const timer = setInterval(snapshot, 1000);
  window[key] = {stop() {
    clearInterval(timer);
    document.removeEventListener('click', click, true);
    document.removeEventListener('focusin', focus, true);
    document.removeEventListener('submit', submit, true);
    delete window[key];
  }};
  snapshot();
})();
