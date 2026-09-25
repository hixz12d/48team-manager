/* Installed only in a named CDP isolated world, never in the website's world. */
({binding, hosts}) => {
  if (window !== window.top || location.protocol !== 'https:' || !hosts.includes(location.host)) return;
  const callHost = globalThis[binding];
  const waiting = new Map();
  let sequence = 0, stopped = false, stopRunner;
  // Observe readiness in the isolated world, including short loading/modal changes
  // between host polls. Never export page text, input values or a DOM dump.
  const visibility = new AbortController();
  const visible = element => !!element && element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== 'hidden' && !element.closest('[inert]');
  const any = selector => [...document.querySelectorAll(selector)].some(visible);
  const usable = selector => [...document.querySelectorAll(selector)].some(element =>
    visible(element) && !element.matches(':disabled,[aria-disabled="true"],[contenteditable="false"]') &&
    !element.closest('[aria-disabled="true"]'));
  const formSelector = 'input[type="email"],input[name="email"],input[name="username"],input[type="password"],input[autocomplete="one-time-code"],input[name="code"],input[name="otp"],input[name="pin"],input[maxlength="1"],input[name="name"],input[name="fullName"],input[name="age"],input[type="date"],input[type="tel"],select[autocomplete^="bday"],input[autocomplete^="bday"]';
  const loadingSelector = '[aria-busy="true"],[role="progressbar"],.animate-spin,[data-loading="true"]';
  let revision = 0, signature = '';
  function readiness() {
    const home = usable('#prompt-textarea,[data-testid="profile-button"],[data-testid="accounts-profile-button"]');
    const forms = any(formSelector);
    const busy = any(loadingSelector);
    const dialog = any('dialog[open],[role="dialog"],[aria-modal="true"]');
    const pending = [...document.querySelectorAll('button,a,[role="button"]')].some(element =>
      visible(element) && /^(accept invite|join workspace|join|accept|接受邀请|加入工作空间|加入|接受)$/i.test(element.textContent.trim()));
    const foreground = document.visibilityState === 'visible' && document.hasFocus();
    const loaded = document.readyState === 'complete';
    const next = JSON.stringify([location.href, home, forms, busy, dialog, pending, foreground, loaded]);
    if (next !== signature) { signature = next; revision++; }
    return {url: location.href, revision, home, forms, busy, dialog, pending, foreground,
      ready: location.hostname === 'chatgpt.com' && loaded && foreground && home && !forms && !busy && !dialog && !pending};
  }
  const observer = new MutationObserver(() => readiness());
  observer.observe(document, {subtree: true, childList: true, attributes: true,
    attributeFilter: ['class', 'style', 'hidden', 'inert', 'disabled', 'aria-disabled', 'aria-busy', 'data-loading', 'open', 'role', 'aria-modal', 'contenteditable']});
  for (const event of ['readystatechange', 'visibilitychange'])
    document.addEventListener(event, readiness, {signal: visibility.signal});
  for (const event of ['focus', 'blur', 'popstate', 'hashchange'])
    window.addEventListener(event, readiness, {signal: visibility.signal});
  globalThis.__team48SignupReadiness = readiness;
  const transport = {
    sendMessage(message) {
      if (stopped) return Promise.resolve({ok: true, active: false});
      return new Promise((resolve, reject) => {
        const id = ++sequence;
        waiting.set(id, {resolve, reject});
        callHost(JSON.stringify({id, url: location.href, message}));
      });
    },
    onStop(callback) { stopRunner = callback; },
  };
  globalThis.__team48ManagedSignup = transport;
  globalThis.__team48ManagedSignupReply = (id, response) => {
    const pending = waiting.get(id);
    if (!pending) return;
    waiting.delete(id);
    pending.resolve(response);
  };
  globalThis.__team48ManagedSignupStop = async () => {
    stopped = true;
    observer.disconnect();
    visibility.abort();
    delete globalThis.__team48SignupReadiness;
    for (const {resolve} of waiting.values()) resolve({ok: true, active: false});
    waiting.clear();
    await stopRunner?.();
    delete globalThis.__team48ManagedSignup;
  };
}
