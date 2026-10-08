/* Runs only in the single incognito tab explicitly started by the user. */
(() => {
  if (window !== window.top) return;
  const VERSION = '0.8.5';
  // One runner per extension world. Reloading an unpacked extension invalidates the old world.
  if (globalThis.__team48SignupRunner) return;
  globalThis.__team48SignupRunner = true;
  let disconnected = false, finished = false;
  // A runner created after a "refresh and continue" request belongs to the reloaded page.
  const runnerStarted = Date.now();
  // The project installs this transport in its own CDP isolated world only.
  const managed = globalThis.__team48ManagedSignup;
  // Server-mode self-check only: the worker asks for these read-only browser signals on
  // https://chatgpt.com/. Nothing is collected unless that message arrives.
  if (!managed && globalThis.chrome?.runtime?.onMessage) {
    chrome.runtime.onMessage.addListener((message, sender, respond) => {
      if (message?.type !== 'selfcheck-signals' || sender.id !== chrome.runtime.id || sender.tab ||
          location.origin !== 'https://chatgpt.com') return false;
      selfcheckSignals().then(data => respond({ok: true, data}), () => respond({ok: false}));
      return true;
    });
  }
  const listeners = new AbortController();
  const onDocument = (type, handler, capture) => document.addEventListener(type, handler, {capture, signal: listeners.signal});
  class RetryStep extends Error {}
  class StopStep extends Error {}
  const visible = element => !!element && element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
  const all = selector => [...document.querySelectorAll(selector)].filter(visible);
  const first = selector => all(selector)[0];
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const between = (min, max) => min + Math.floor(Math.random() * (max - min + 1));
  const rest = (min, max) => sleep(between(min, max));
  // Time the page spent hidden (minimised or fully covered, when the browser throttles timers and
  // freezes animations) does not count toward any wait below. Wall-clock time is kept only where it
  // is compared with the worker's timestamps.
  let hiddenTotal = 0, hiddenSince = document.visibilityState === 'hidden' ? Date.now() : 0, settleUntil = 0;
  const clock = () => Date.now() - hiddenTotal - (hiddenSince ? Date.now() - hiddenSince : 0);
  onDocument('visibilitychange', () => {
    if (document.visibilityState === 'hidden') { hiddenSince ||= Date.now(); return; }
    if (!hiddenSince) return;
    hiddenTotal += Date.now() - hiddenSince; hiddenSince = 0;
    // Dialogs and layout frozen while hidden catch up before the next check or click.
    settleUntil = clock() + between(1200, 2000);
  });
  let job;
  let unknownSince = clock();
  let entryWaitSince = 0, sessionAnonymous = false;
  const ENTRY_WAIT = 15000;
  // After requesting the fallback, the old page may keep ticking until navigation commits.
  const FALLBACK_NAV_WAIT = 30000, FALLBACK_PAGE_WAIT = 30000;
  const ENTRY_PAUSE = '备用注册入口仍未打开，请检查网络或手动打开注册页面后继续。';
  let fallbackAt = 0;
  const entryPage = () => location.origin === 'https://chatgpt.com' &&
    (location.pathname === '/' || (job?.entryFallbackUsed && location.pathname === '/auth/login'));
  let lastSessionCheck = 0;
  let sessionCandidate = '', sessionErrors = 0;
  // After the signup forms, the logged-in homepage must be confirmed within this time,
  // otherwise pause with a clear message instead of waiting silently.
  const HOME_CONFIRM_WAIT = 40000;
  let homeSince = 0;
  let currentStage = 'unknown', lastObservation = '';
  let manualStep, approvedStep, userEditing = false, resumeCount = 0;
  let submittedForms = new WeakMap();
  let pendingClick, clickForm;
  const intendedValues = new WeakMap();
  const CONTINUE_WAIT = 2000;
  // How long a submit button may stay disabled, covered or moving before pausing.
  const SUBMIT_BUTTON_WAIT = 15000;
  // A DOM submit alone cannot establish the server outcome. Only the existing
  // idle, unchanged, no-click/no-loading branch may release this observation guard.
  const SUBMISSION_WAIT = 3000;
  // Sites also fire DOM submit while validating our own typing. In auto mode those are
  // judged only after typing ends: ~1s idle, then a normal click. A complete OTP that
  // the site submitted itself gets slightly longer to advance without a second request.
  const FILL_SUBMIT_WAIT = 1000, AUTO_ADVANCE_WAIT = 2500;
  let fillPhase = false;
  const fillWait = record => record.complete && currentStage === 'otp' ? AUTO_ADVANCE_WAIT : FILL_SUBMIT_WAIT;
  const fillPending = form => {
    const record = form && submittedForms.get(form);
    return !!record?.fill && !record.loadingSeen && clock() - record.at < fillWait(record);
  };
  // Let the page finish its own async validation/auto-advance before judging or clicking.
  const settle = () => sleep(1000);
  const recheck = () => rest(700, 1100);
  const continueStage = stage => ['otp', 'profile'].includes(stage);
  const loadingSelector = '[aria-busy="true"],[role="progressbar"],.animate-spin,[data-loading="true"]';
  const formValues = scope => JSON.stringify([...scope.querySelectorAll('input,select,textarea')]
    .filter(element => visible(element) && element.type !== 'hidden').map(element => [element.value, element.checked]));
  const submitLabels = /^(continue|verify|create account|create your account|finish creating account|sign up|agree and continue|继续|验证|创建帐户|创建账户|完成注册|注册|同意并继续)$/i;
  const oauthPhase = () => job?.phase === 'oauth';
  const continueButton = scope => button(submitLabels, scope);
  const loadingWatches = new Set();
  const loadingNow = (scope, target) => scope.getAttribute?.('aria-busy') === 'true' ||
    [...scope.querySelectorAll(loadingSelector)].some(visible) ||
    (target?.isConnected && (target.matches(':disabled') || target.getAttribute('aria-disabled') === 'true'));
  const watchLoading = (record, target, strict = false) => {
    record.loadingSeen = !!loadingNow(record.scope, target);
    if (record.loadingSeen) record.busyAt = clock();
    const observe = records => {
      // Remember even a short spinner/disabled interval between runner ticks.
      // While filling, a button becoming enabled is ordinary validation, not loading.
      if (loadingNow(record.scope, target) || records.some(change => {
        if (change.type === 'attributes') return (
          ['aria-busy', 'data-loading'].includes(change.attributeName) && change.oldValue === 'true' ||
          !strict && change.attributeName === 'aria-disabled' && change.oldValue === 'true' ||
          !strict && change.attributeName === 'disabled' && change.oldValue !== null ||
          change.attributeName === 'class' && /\banimate-spin\b/.test(change.oldValue || ''));
        return [...change.addedNodes, ...change.removedNodes].some(node => node.nodeType === 1 &&
          (node.matches(loadingSelector) || node.querySelector(loadingSelector)));
      })) { record.loadingSeen = true; record.busyAt = clock(); }
    };
    const observer = new MutationObserver(observe);
    observer.observe(record.scope, {subtree: true, childList: true, attributes: true, attributeOldValue: true,
      attributeFilter: ['aria-busy', 'data-loading', 'disabled', 'aria-disabled', 'class']});
    record.flush = () => observe(observer.takeRecords());
    record.stop = () => { observer.disconnect(); loadingWatches.delete(record); };
    loadingWatches.add(record);
  };
  const forgetSubmission = form => { submittedForms.get(form)?.stop?.(); submittedForms.delete(form); };
  const setPendingClick = pending => { pendingClick?.stop?.(); pendingClick = pending; };
  const clickFeedback = (element, stage) => {
    const scope = element.closest('form') || element.closest('[role="dialog"],dialog[open],[aria-modal="true"]') || document;
    const record = {stage, path: location.pathname, scope, button: element, resolve: controlRef(element),
      shape: formShape(scope), values: formValues(scope), errors: validationSnapshot(scope), at: clock(), response: ''};
    watchLoading(record, element);
    return record;
  };
  const retryableClick = pending => {
    pending.flush();
    const target = pending.resolve();
    return pending === pendingClick && continueStage(pending.stage) && job.mode !== 'manual' &&
      currentStage === pending.stage && location.pathname === pending.path && pending.scope.isConnected &&
      formShape(pending.scope) === pending.shape && formValues(pending.scope) === pending.values &&
      !pending.loadingSeen && !pending.retryBlocked && !submittedForms.has(pending.scope) &&
      validationSnapshot(pending.scope).size === 0 && target && enabled(target) &&
      submitLabels.test((target.innerText || target.getAttribute('aria-label') || '').trim());
  };
  // The page reacted (spinner or submit) but then went idle on the same, unchanged, error-free
  // form. OTP/profile pages often stall like this; after a quiet interval, click Continue again.
  const STALL_WAIT = 6000;
  const stalledClick = pending => {
    pending.flush();
    const submission = submittedForms.get(pending.scope);
    submission?.flush?.();
    const target = pending.resolve();
    const quietSince = Math.max(pending.at, pending.busyAt || 0, submission?.at || 0, submission?.busyAt || 0);
    return pending === pendingClick && continueStage(pending.stage) && job.mode !== 'manual' &&
      currentStage === pending.stage && location.pathname === pending.path && pending.scope.isConnected &&
      formShape(pending.scope) === pending.shape && formValues(pending.scope) === pending.values &&
      validationSnapshot(pending.scope).size === 0 && target && enabled(target) && !loadingNow(pending.scope, target) &&
      submitLabels.test((target.innerText || target.getAttribute('aria-label') || '').trim()) &&
      clock() - quietSince >= STALL_WAIT;
  };
  // The same rule for a submit the site sent by itself (e.g. a complete OTP): after it showed a
  // spinner and then went idle, the unchanged form may be released for one normal Continue click.
  // Bounded per page step, because a site that re-submits on its own would otherwise loop.
  const STALL_RELEASES = 2;
  let stallReleases = new Map();
  const retryAllowed = pending => pending.stalled ?
    stalledClick(pending) && submittedForms.get(pending.scope) === pending.submission : retryableClick(pending);
  const autoFill = () => !['submit', 'manual'].includes(job?.mode);
  const requireForeground = () => {
    // With CDP input the page runs with emulated focus; it only has to stay visible.
    if (document.visibilityState === 'hidden' || (!trustedInput && !document.hasFocus())) {
      unknownSince = clock(); entryWaitSince = 0;
      renderProgress({...job, message: trustedInput ? '注册窗口被最小化或完全遮挡，恢复显示后会自动继续填写。' :
        '注册页面已离开前台，切回此窗口后会自动继续填写。'});
      throw new StopStep();
    }
    if (clock() < settleUntil) {
      renderProgress({...job, message: '窗口刚恢复显示，等页面稳定后继续。'});
      throw new StopStep();
    }
  };
  // null: not yet negotiated; true: the worker dispatches browser-level (trusted) input.
  let trustedInput = null, ownInput = 0;
  const own = async task => { ownInput++; try { return await task(); } finally { ownInput--; } };
  const ageField = (scope = document) => [...scope.querySelectorAll('input')].find(element => {
    if (!visible(element) || !['text', 'number', 'tel'].includes(element.type)) return false;
    const label = [element.name, element.id, element.placeholder, element.getAttribute('aria-label'),
      ...[...(element.labels || [])].map(item => item.textContent),
      ...(element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).map(id => document.getElementById(id)?.textContent)]
      .filter(Boolean).join(' ');
    return /\bage\b|年龄|年齡/i.test(label);
  });
  // SPA steps can reuse the same <form>. Identify its current controls, not its object alone.
  const formShape = form => JSON.stringify([...form.querySelectorAll('input,select,textarea')]
    .filter(element => visible(element) && element.type !== 'hidden')
    .map(element => [element.localName, element.type, element.name, element.id, element.autocomplete]));
  const trace = (event, extra = {}) => send('event', {event, stage: currentStage, ...extra});
  // Submission claims live in the worker so pause/resume and page navigation agree.
  const send = async (type, extra = {}) => {
    if (disconnected) throw new StopStep();
    let response;
    try {
      const message = {type, version: VERSION, jobId: job?.id, ...extra};
      response = await (managed ? managed.sendMessage(message) : chrome.runtime.sendMessage(message));
    }
    catch {
      disconnected = true;
      if (panel) renderProgress({...job, status: 'paused', message: '插件已更新或连接断开，请刷新本页加载新版后继续。'});
      throw new StopStep();
    }
    if (!response?.ok) throw new Error(response?.error || '插件连接已断开，请刷新页面。');
    return response;
  };
  // `code` is a fixed reason for diagnostics; the text is shown to the user only.
  const pause = async (reason, code = 'other') => {
    const state = await send('state');
    // Preserve the first diagnostic and a user's explicit pause/stop.
    if (state.active) await send('pause', {reason, code, stage: currentStage});
    renderProgress(await send('state'));
  };
  let panel;
  function renderProgress(state) {
    if (!state.status) return;
    if (state.hidePanel && ['auto', 'review'].includes(state.mode)) {
      panel?.host.remove(); panel = null;
      return;
    }
    if (!panel) {
      document.getElementById('team48-signup-progress')?.remove();
      const host = document.createElement('div');
      host.id = 'team48-signup-progress';
      host.style.cssText = 'position:fixed!important;right:20px!important;bottom:20px!important;z-index:2147483647!important;';
      panel = host.attachShadow({mode: 'closed'});
      panel.innerHTML = `<style>
        :host{all:initial}*{box-sizing:border-box}.card{width:292px;background:#fff;color:#1c302a;border:1px solid #d6e2dc;border-radius:14px;box-shadow:0 8px 32px #172b3226;font:13px/1.6 system-ui,sans-serif}
        header{display:flex;align-items:center;justify-content:space-between;padding:11px 14px;background:#edf5f1;border-radius:14px 14px 0 0}strong{font-size:12px}.body{padding:13px 14px}.email{color:#66786e;font-size:11px;overflow-wrap:anywhere}p{margin:7px 0 12px}
        button{font:inherit;cursor:pointer;border:0;border-radius:7px;background:#166a59;color:white;padding:6px 10px}header button{padding:0 5px;color:#426557;background:transparent}button:disabled{opacity:.5}.hint{color:#748178;font-size:11px;margin:9px 0 0}[hidden]{display:none}
      </style><div class="card"><header><strong>TEAM48 · 注册进行中</strong><button id="collapse" aria-label="收起或展开进度">−</button></header><div class="body"><div class="email"></div><p id="text" role="status"></p><button id="action"></button> <button id="submit-step" hidden>已填好，提交本步</button><p class="hint">账号密码可在浏览器工具栏的插件中复制</p></div></div>`;
      panel.querySelector('#collapse').addEventListener('click', () => {
        const body = panel.querySelector('.body'); body.hidden = !body.hidden;
        panel.querySelector('#collapse').textContent = body.hidden ? '+' : '−';
      });
      panel.querySelector('#action').addEventListener('click', async () => {
        const action = panel.querySelector('#action'); action.disabled = true;
        try {
          const state = await send('state');
          if (state.status === 'running') await pause('已手动暂停，点击继续即可恢复。', 'user');
          else if (state.status === 'paused') { await send('resume'); renderProgress(await send('state')); }
          else host.remove();
        } catch (error) { panel.querySelector('#text').textContent = error.message; }
        finally { action.disabled = false; }
      });
      panel.querySelector('#submit-step').addEventListener('click', () => {
        if (manualStep && job?.active) { approvedStep = manualStep; panel.querySelector('#submit-step').disabled = true; }
      });
      document.documentElement.append(host);
    }
    panel.host.dataset.version = VERSION;
    panel.querySelector('strong').textContent = `TEAM48 ${VERSION} · ` + (state.phase === 'oauth' ?
      {running: '授权进行中', paused: '需要你处理', done: '授权已提交', stopped: '已停止'} :
      {running: '注册进行中', paused: '需要你处理', done: '已进入 ChatGPT', stopped: '已停止'})[state.status];
    panel.querySelector('.email').textContent = state.email;
    panel.querySelector('#text').textContent = state.message;
    const submitStep = panel.querySelector('#submit-step');
    submitStep.hidden = !state.active || state.mode !== 'submit' || !manualStep;
    submitStep.disabled = !!approvedStep;
    panel.querySelector('#action').textContent = {running: '暂停自动填写', paused: '我已处理，继续', done: '收起提示', stopped: '收起提示'}[state.status];
  }
  const button = (pattern, scope = document) => [...scope.querySelectorAll('button,a,[role="button"]')]
    .find(element => visible(element) && pattern.test((element.innerText || element.getAttribute('aria-label') || '').trim()));
  const enabled = element => element?.isConnected && visible(element) && !element.matches(':disabled') &&
    element.getAttribute('aria-disabled') !== 'true' && !element.closest('[inert]');
  const emailField = (scope = document) => {
    const emailLabel = /\be[- ]?mail\b|邮箱|電子郵件|电子邮件/i;
    const candidates = [...scope.querySelectorAll('input')].filter(element => {
      if (!visible(element) || element.closest('[inert]') || !['text', 'email', 'tel'].includes(element.type)) return false;
      const label = [element.getAttribute('aria-label'), element.placeholder,
        ...[...(element.labels || [])].map(label => label.textContent),
        ...(element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).map(id => document.getElementById(id)?.textContent)].filter(Boolean).join(' ');
      return element.type === 'email' || /^(email|username)$/i.test(element.name) ||
        /^(email|username)$/i.test(element.id) || /\b(email|username)\b/i.test(element.autocomplete) ||
        element.inputMode === 'email' || emailLabel.test(label);
    });
    // A visible dialog's editable field takes precedence over a previous read-only account field.
    const rank = element => Number(enabled(element) && !element.readOnly) * 4 +
      Number(!!element.closest('[role="dialog"],dialog[open],[aria-modal="true"]')) * 2 + Number(document.activeElement === element);
    return candidates.sort((a, b) => rank(b) - rank(a))[0];
  };
  const showClick = point => {
    // Visual feedback only; the marker never receives clicks or moves the OS pointer.
    const marker = document.createElement('div');
    marker.dataset.team48Click = '';
    marker.setAttribute('aria-hidden', 'true');
    marker.style.cssText = `position:fixed!important;left:${point.x - 12}px!important;top:${point.y - 12}px!important;width:24px!important;height:24px!important;border:2px solid #22c6a3!important;border-radius:50%!important;background:#22c6a326!important;pointer-events:none!important;z-index:2147483647!important;box-sizing:border-box!important;`;
    document.documentElement.append(marker);
    marker.animate([{transform: 'scale(.6)', opacity: 1}, {transform: 'scale(1.5)', opacity: 0}], {duration: 600});
    setTimeout(() => marker.remove(), 600);
  };
  const hitsControl = (element, point) => {
    const hit = document.elementFromPoint(point.x, point.y);
    // Floating labels are part of the input control, not an unrelated obstruction.
    return !!hit && (hit === element || element.contains(hit) || hit.closest('label')?.control === element);
  };
  const hitPoint = element => {
    const rect = element.getBoundingClientRect();
    const sample = area => {
      const left = Math.max(0, rect.left, area.left), right = Math.min(innerWidth, rect.right, area.right);
      const top = Math.max(0, rect.top, area.top), bottom = Math.min(innerHeight, rect.bottom, area.bottom);
      if (right <= left || bottom <= top) return null;
      // Rounded controls and overlays can leave the centre covered.
      for (const [horizontal, vertical] of [[.5,.5],[.2,.5],[.8,.5],[.5,.2],[.5,.8],[.2,.2],[.8,.2],[.2,.8],[.8,.8]]) {
        const point = {x: left + (right - left) * horizontal, y: top + (bottom - top) * vertical, rect};
        if (hitsControl(element, point)) return point;
      }
      return null;
    };
    // Browser hit testing is authoritative: absolute/top-layer controls can escape
    // ancestors that our clipping estimate includes. Only use that estimate to find
    // extra points when a narrow visible strip misses the full-box samples.
    return sample(rect) || sample(viewArea(element));
  };
  const moveProgressAside = element => {
    const host = panel?.host;
    if (!host?.isConnected) return () => {};
    const target = element.getBoundingClientRect();
    const overlaps = rect => rect.left < target.right && rect.right > target.left && rect.top < target.bottom && rect.bottom > target.top;
    if (!overlaps(host.getBoundingClientRect())) return () => {};
    const previous = host.style.cssText;
    const restore = () => { host.style.cssText = previous; };
    // Move only our own UI. Site dialogs and other overlays remain subject to hit testing.
    for (const [vertical, horizontal] of [['top','right'],['top','left'],['bottom','left']]) {
      for (const edge of ['top','right','bottom','left']) host.style.setProperty(edge, 'auto', 'important');
      host.style.setProperty(vertical, '12px', 'important');
      host.style.setProperty(horizontal, '12px', 'important');
      if (!overlaps(host.getBoundingClientRect())) return restore;
    }
    host.style.setProperty('visibility', 'hidden', 'important');
    return restore;
  };
  let profileLoadingSince = 0;
  const profileLoading = () => {
    const name = first('input[name="name"],input[name="fullName"],input[autocomplete="name"]');
    const form = name?.closest('form');
    if (!form) return false;
    const shown = selector => [...form.querySelectorAll(selector)].some(visible);
    return form.getAttribute('aria-busy') === 'true' || shown('[aria-busy="true"],[role="progressbar"],.animate-spin,[data-loading="true"]') ||
      (!!name.disabled && shown('button:disabled,input[type="submit"]:disabled'));
  };
  const waitForProfile = async () => {
    if (!profileLoading()) { profileLoadingSince = 0; return false; }
    profileLoadingSince ||= clock();
    renderProgress({...job, message: '资料正在提交，等待页面跳转'});
    if (clock() - profileLoadingSince > 45000) {
      await pause('资料提交超过 45 秒仍未完成，请检查网页提示后继续。', 'profile_timeout');
    }
    return true;
  };
  let hovered;
  const controlRef = original => {
    if (original.matches('input,select,textarea')) return fieldRef(original);
    const scope = original.closest('[role="dialog"],dialog[open],[aria-modal="true"]') || original.closest('form') || document;
    const shape = formShape(scope), origin = location.origin, path = location.pathname;
    const identity = node => JSON.stringify([node.localName, node.getAttribute('type'), node.id, node.getAttribute('name'),
      node.getAttribute('href'), (node.innerText || node.getAttribute('aria-label') || node.textContent || '').trim()]);
    const signature = identity(original);
    let current = original;
    return () => {
      if (location.origin !== origin || location.pathname !== path || !scope.isConnected || formShape(scope) !== shape) return null;
      if (current?.isConnected && identity(current) === signature) return current;
      const matches = [...scope.querySelectorAll('button,a,[role="button"]')].filter(node => visible(node) && identity(node) === signature);
      current = matches.length === 1 ? matches[0] : null;
      return current;
    };
  };
  const validationSnapshot = scope => new Map([...scope.querySelectorAll('input,select,textarea,[aria-invalid="true"],[role="alert"]')]
    .filter(node => visible(node) && ((node.willValidate && !node.validity.valid) || node.getAttribute('aria-invalid') === 'true' ||
      (node.getAttribute('role') === 'alert' && node.textContent.trim())))
    .map(node => [node, node.textContent.trim() + ':' + (node.validationMessage || '')]));

  const trustedKey = async key => {
    if (!(await own(() => send('input-key', {key}))).sent) throw new StopStep();
  };
  // The area where a control can actually be seen: viewport ∩ real clipping ancestors.
  // html/body overflow (e.g. a dialog's scroll lock) applies to the viewport, not their own
  // box; display:contents has no box; ancestors above a fixed dialog do not clip it.
  const viewArea = element => {
    const area = {top: 0, bottom: innerHeight, left: 0, right: innerWidth};
    let fixed = getComputedStyle(element).position === 'fixed';
    for (let parent = element.parentElement; parent && !fixed && parent !== document.body && parent !== document.documentElement;
      parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.display === 'contents') continue;
      fixed = style.position === 'fixed';
      const box = parent.getBoundingClientRect();
      if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) { area.top = Math.max(area.top, box.top); area.bottom = Math.min(area.bottom, box.bottom); }
      if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) { area.left = Math.max(area.left, box.left); area.right = Math.min(area.right, box.right); }
    }
    return area;
  };
  const fullyShown = (rect, area) => rect.top >= area.top - 1 && rect.bottom <= area.bottom + 1 &&
    rect.left >= area.left - 1 && rect.right <= area.right + 1;
  // Enough of the control is on screen to click; hit testing in clickControl judges obstructions.
  const partlyShown = (rect, area) =>
    Math.min(rect.bottom, area.bottom) - Math.max(rect.top, area.top) >= Math.min(rect.height, 16) &&
    Math.min(rect.right, area.right) - Math.max(rect.left, area.left) >= Math.min(rect.width, 16);
  async function scrollToControl(element, deadline = clock() + 5000) {
    if (!trustedInput) { element.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'instant'}); return true; }
    let previous, stuck = 0;
    for (let attempt = 0; attempt < 32 && clock() < deadline; attempt++) {
      if (!(await send('state')).active || userEditing) throw new StopStep();
      requireForeground();
      if (!element.isConnected) throw new RetryStep('滚动时控件已更新');
      const rect = element.getBoundingClientRect(), area = viewArea(element);
      // If the browser can hit it, scrolling cannot make it more actionable. The
      // click path still checks enabled state, stability and obstruction before input.
      if (hitPoint(element) || fullyShown(rect, area)) return true;
      // Wheels that no longer move the control (clip-only wrappers, scroll-locked pages) cannot help.
      if (previous && ['top', 'left'].every(key => Math.abs(rect[key] - previous[key]) < 1) && ++stuck >= 2) break;
      if (previous && ['top', 'left'].some(key => Math.abs(rect[key] - previous[key]) >= 1)) stuck = 0;
      previous = rect;
      if (area.bottom - area.top < 2 || area.right - area.left < 2) break;
      // Aim a few pixels inside the area when there is room, never an impossible margin.
      const padY = Math.min(8, Math.max(0, (area.bottom - area.top - rect.height) / 2));
      const padX = Math.min(8, Math.max(0, (area.right - area.left - rect.width) / 2));
      const top = area.top + padY, bottom = area.bottom - padY, left = area.left + padX, right = area.right - padX;
      const deltaY = rect.top < top ? rect.top - top : rect.bottom > bottom ? rect.bottom - bottom : 0;
      const deltaX = rect.left < left ? rect.left - left : rect.right > right ? rect.right - right : 0;
      const limit = value => Math.sign(value) * Math.min(Math.abs(value), between(160, 360));
      const x = Math.min(area.right - 1, Math.max(area.left + 1, (rect.left + rect.right) / 2));
      const y = Math.min(area.bottom - 1, Math.max(area.top + 1, (rect.top + rect.bottom) / 2));
      await own(() => send('input-move', {x, y}));
      await own(() => send('input-wheel', {x, y, deltaY: limit(deltaY), deltaX: limit(deltaX)}));
      await rest(160, 250);
    }
    if (!element.isConnected) throw new RetryStep('滚动时控件已更新');
    if (hitPoint(element) || partlyShown(element.getBoundingClientRect(), viewArea(element))) return true;
    if (!(await send('state')).active || userEditing) throw new StopStep();
    requireForeground();
    if (clock() >= deadline) return false;
    // Last resort for containers the wheel cannot reach; the page itself is not modified.
    element.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    await rest(120, 200);
    if (!element.isConnected) throw new RetryStep('滚动时控件已更新');
    // A temporarily clipped control is not a fatal page error. The caller owns
    // the wait deadline and will re-resolve/recheck it before any input.
    return !!hitPoint(element) || partlyShown(element.getBoundingClientRect(), viewArea(element));
  }
  const controlSnapshot = element => {
    const rect = element.getBoundingClientRect(), area = viewArea(element);
    return {kind: element.localName, rect: [rect.left, rect.top, rect.width, rect.height],
      viewport: [innerWidth, innerHeight], clip: [area.left, area.top, area.right, area.bottom],
      enabled: !!enabled(element), hit: !!hitPoint(element)};
  };
  async function tabToControl(element) {
    const current = document.activeElement;
    if (!trustedInput || !current?.form || current.form !== element.form || Math.random() >= 0.4) return false;
    const controls = [...document.querySelectorAll('input,select,textarea,button,a[href],[tabindex]')]
      .filter(node => enabled(node) && node.tabIndex >= 0);
    if (controls.some(node => node.tabIndex > 0) || controls[controls.indexOf(current) + 1] !== element) return false;
    await trustedKey('Tab');
    await rest(80, 160);
    return document.activeElement === element;
  }
  async function clickControl(element, stage, retry = null, enterFrom = null) {
    // CDP generates browser input; the legacy path dispatches DOM events.
    if (!element?.isConnected || userEditing || !(await send('state')).active || userEditing) return false;
    requireForeground();
    if (stage === 'profile' && await waitForProfile()) return false;
    const resolve = controlRef(element);
    let restoreProgress = moveProgressAside(element), reservation, dispatched = false;
    try {
      // Submit buttons often stay disabled while the site validates; give them longer
      // before asking the user. Field focus and the signup entry keep the short wait.
      const patience = stage && stage !== 'signup' ? SUBMIT_BUTTON_WAIT : 5000;
      const waitStarted = clock(), deadline = waitStarted + patience;
      let point, previous, told = false, observed = false, nextScroll = 0;
      while (clock() < deadline) {
        if (!(await send('state')).active || userEditing) return false;
        requireForeground();
        const current = resolve();
        if (!current) throw new RetryStep('点击目标已更新');
        if (current !== element) {
          restoreProgress(); element = current; previous = null; nextScroll = 0;
          restoreProgress = moveProgressAside(element);
        }
        if (stage === 'profile' && await waitForProfile()) return false;
        point = enabled(element) ? hitPoint(element) : null;
        if (!point && !observed) {
          observed = true;
          await trace('control_wait', {stage: stage || currentStage, outcome: 'waiting', control: controlSnapshot(element)});
        }
        if (!point && clock() >= nextScroll && !fullyShown(element.getBoundingClientRect(), viewArea(element))) {
          try { await scrollToControl(element, deadline); }
          catch (error) {
            if (!(error instanceof RetryStep) || !resolve()) throw error;
            previous = null; nextScroll = 0;
            continue;
          }
          nextScroll = clock() + 500;
          // Scrolling and layout can replace the node or overlap a user pause.
          if (!(await send('state')).active || userEditing) return false;
          requireForeground();
          if (resolve() !== element) { previous = null; continue; }
          point = enabled(element) ? hitPoint(element) : null;
        }
        if (point && previous && ['left', 'top', 'width', 'height'].every(key => Math.abs(point.rect[key] - previous.rect[key]) < 1)) break;
        previous = point; point = null;
        if (stage && !told && clock() - waitStarted > 2000) {
          told = true;
          renderProgress({...job, message: '等待网页按钮可用，就绪后自动点击。'});
        }
        await sleep(100);
      }
      if (!point) {
        if (stage === 'signup' && autoFill() && entryPage() && !job.entryFormSeen) {
          renderProgress({...job, message: '注册入口尚未响应，正在等待；超时后会尝试备用入口。'});
          return false;
        }
        const control = stage === 'profile' ? '资料页的继续按钮' : element.matches('input') ? '当前输入框' : '当前按钮';
        const reason = !enabled(element) ? '仍被网页禁用或隐藏' :
          !partlyShown(element.getBoundingClientRect(), viewArea(element)) ? '仍未进入可操作区域' :
          !hitPoint(element) ? '被页面其他元素遮挡' : '位置仍在变化';
        await trace('control_wait', {stage: stage || currentStage, outcome: 'timeout', control: controlSnapshot(element)});
        await pause(`${control}等待 ${Math.round(patience / 1000)} 秒后${reason}，已暂停点击；请处理后继续。`, 'button_unavailable');
        return false;
      }
      if (observed) await trace('control_wait', {stage: stage || currentStage, outcome: 'ready', control: controlSnapshot(element)});
      const ready = () => {
        requireForeground();
        if (userEditing) throw new StopStep();
        if (enterFrom && (document.activeElement !== enterFrom || enterFrom.form !== element.form || !enabled(enterFrom))) throw new StopStep();
        if (retry && submittedForms.get(element.closest('form')) !== retry.submission) retry.retryBlocked = true;
        if (retry && !retryAllowed(retry)) throw new StopStep();
        const form = element.closest('form');
        if (stage && stage !== 'signup' && form && !form.checkValidity()) {
          throw new Error('点击前表单内容已变化或不符合要求，请按网页提示检查后继续。');
        }
        const rect = element.getBoundingClientRect();
        if (resolve() !== element || !enabled(element) || !hitsControl(element, point) ||
            !['left', 'top', 'width', 'height'].every(key => Math.abs(rect[key] - point.rect[key]) < 1)) {
          throw new RetryStep('点击前目标或位置已变化');
        }
      };
      const emit = (target, type, buttons = 0, relatedTarget = null) => {
        const pointer = type.startsWith('pointer');
        const enterLeave = /(?:enter|leave)$/.test(type);
        const options = {bubbles: !enterLeave, composed: !enterLeave, cancelable: !enterLeave,
          clientX: point.x, clientY: point.y, button: 0, buttons, relatedTarget,
          detail: /down|up|click/.test(type) ? 1 : 0,
          pointerId: 1, pointerType: 'mouse', isPrimary: true};
        return target.dispatchEvent(pointer ? new PointerEvent(type, options) : new MouseEvent(type, options));
      };
      if (trustedInput) {
        // Browser-level pointer: over/enter/move come from the real hit test along the path.
        if (!enterFrom) await own(() => send('input-move', {x: point.x, y: point.y}));
        await rest(120, 240);
        if (!(await send('state')).active || userEditing) return false;
        ready();
      } else {
        if (hovered !== element) {
          if (hovered?.isConnected) {
            for (const type of ['pointerout', 'mouseout', 'pointerleave', 'mouseleave']) emit(hovered, type, 0, element);
          }
          for (const type of ['pointerover', 'mouseover', 'pointerenter', 'mouseenter']) emit(element, type, 0, hovered || null);
          hovered = element;
        }
        emit(element, 'pointermove'); emit(element, 'mousemove');
        await rest(120, 240);
        if (!(await send('state')).active) return false;
        ready();
        clickForm = stage ? element.closest('form') : null;
        const pointerAllowed = emit(element, 'pointerdown', 1);
        if (pointerAllowed) showClick(point);
        const mouseAllowed = pointerAllowed && emit(element, 'mousedown', 1);
        if (mouseAllowed && document.activeElement !== element) element.focus({preventScroll: true});
        await rest(50, 110);
        const active = (await send('state')).active;
        emit(element, 'pointerup');
        if (pointerAllowed) emit(element, 'mouseup');
        if (!active || userEditing || !pointerAllowed) return false;
      }
      const blockedSubmission = async () => {
        if (retry && submittedForms.get(element.closest('form')) !== retry.submission) retry.retryBlocked = true;
        if (retry && retryAllowed(retry) && submittedForms.get(element.closest('form')) === retry.submission) return false;
        return waitForSubmission(element.closest('form'));
      };
      if (stage && await blockedSubmission()) return false;
      if (stage === 'profile' && await waitForProfile()) return false;
      ready();
      if (stage) {
        reservation = retry ? await send('retry-continue', {stage, token: retry.token, stalled: !!retry.stalled}) : await send('claim', {stage, reserve: true});
        if (!reservation.granted) {
          if (retry && reservation.wait) return false;
          if (retry?.stalled && !reservation.limit) {
            // The worker refused a stalled-page retry: keep waiting; the 45 s timeout still applies.
            retry.stalled = false; retry.stallDenied = true;
            return false;
          }
          if (retry) {
            setPendingClick(null);
            await pause('Continue 已达到补点次数或当前步骤已变化，请在网页手动继续。', 'retry_limit');
          }
          return false;
        }
        // The worker round-trip can overlap a redraw, pause or window switch.
        if (!(await send('state')).active) return false;
        if (await blockedSubmission()) return false;
        ready();
      }
      const feedback = stage && stage !== 'signup' ? clickFeedback(element, stage) : null;
      if (feedback) {
        feedback.token = reservation.token;
        // After a stalled-page re-click, further clicks on this form only follow the stalled rule.
        feedback.stallMode = !!(retry?.stalled || retry?.stallMode);
        setPendingClick(feedback);
      }
      if (stage) fillPhase = false;
      if (trustedInput) {
        clickForm = stage ? element.closest('form') : null;
        // Press and release happen in the browser; a completed click may submit and navigate at once.
        // If navigation loses the reply, the press may already have submitted.
        // Preserve the reservation in that uncertain case instead of authorizing a duplicate.
        dispatched = true;
        const result = await own(() => enterFrom ? send('input-key', {key: 'Enter'}) : send('input-click', {x: point.x, y: point.y}));
        if (!result.sent) {
          dispatched = false;
          if (feedback) setPendingClick(null);
          return false;
        }
      } else emit(element, 'click');
      dispatched = true;
      if (!stage && element.matches('input,select,textarea') && resolve() === element && document.activeElement !== element) {
        throw new RetryStep('点击后输入框未获得焦点');
      }
      return true;
    } finally {
      restoreProgress();
      clickForm = null;
      if (reservation?.granted) await send('finish-click', {stage, token: reservation.token, sent: dispatched});
    }
  }

  async function waitForClick(stage) {
    const pending = pendingClick;
    if (!pending) return false;
    pending.flush();
    let response = '';
    if (stage !== pending.stage || location.pathname !== pending.path || !pending.scope.isConnected ||
        formShape(pending.scope) !== pending.shape) response = 'advanced';
    else if ([...validationSnapshot(pending.scope)].some(([node, value]) => pending.errors.get(node) !== value)) response = 'validation';
    else if (pending.loadingSeen) response = 'loading';
    else if (submittedForms.has(pending.scope)) response = 'submitted';
    if (response && response !== pending.response) {
      pending.response = response;
      await trace('click_response', {stage: pending.stage, outcome: response});
    }
    if (response === 'advanced') { setPendingClick(null); return false; }
    if (response === 'validation') {
      setPendingClick(null);
      await pause('点击后网页提示输入有误，请按网页提示修正并手动提交，再点击插件继续。', 'validation');
      return true;
    }
    if (continueStage(pending.stage) && formValues(pending.scope) !== pending.values) {
      setPendingClick(null);
      await pause('Continue 等待期间表单内容发生变化，请核对后手动提交，再点击插件继续。', 'form_changed');
      return true;
    }
    if (clock() - pending.at >= CONTINUE_WAIT && pending.token && !pending.stallMode && retryableClick(pending)) {
      requireForeground();
      pending.submission = submittedForms.get(pending.scope);
      renderProgress({...job, message: 'Continue 后页面仍无变化，正在重新确认按钮并有限补点。'});
      await clickControl(pending.resolve(), pending.stage, pending);
      return true;
    }
    if (pending.token && !pending.stallDenied &&
        (pending.stallMode || pending.loadingSeen || response === 'submitted' || pending.retryBlocked) && stalledClick(pending)) {
      // The page showed a spinner or submitted, then went idle without advancing. A person would click again.
      requireForeground();
      pending.stalled = true;
      pending.submission = submittedForms.get(pending.scope);
      renderProgress({...job, message: '页面处理后停住了，正在重新点击继续（有次数上限）。'});
      await clickControl(pending.resolve(), pending.stage, pending);
      if (pendingClick === pending) pending.stalled = false;
      return true;
    }
    renderProgress({...job, message: pending.loadingSeen ? '网页正在处理，等待下一步；不会重复点击。' :
      response === 'submitted' ? '表单已触发提交，等待网页确认结果；不会重复点击。' :
      continueStage(pending.stage) ? '等待按钮响应；未触发提交且持续无变化时会有限补点。' : '已点击，正在等待网页响应；不会重复点击。'});
    if (clock() - pending.at > 45000) {
      await trace('click_response', {stage: pending.stage, outcome: 'timeout'});
      await claim(pending.stage, true);
      await pause('点击后超过 45 秒未进入下一步，请检查网络和网页提示后继续。', 'click_timeout');
      setPendingClick(null);
    }
    return true;
  }
  const fieldRefs = new WeakMap();
  const fieldRef = original => {
    const cached = fieldRefs.get(original);
    if (cached?.sameStep()) return cached;
    const scope = original.form || document;
    const origin = location.origin, path = location.pathname, shape = formShape(scope);
    const sameStep = () => location.origin === origin && location.pathname === path && formShape(scope) === shape;
    const key = ['id', 'name', 'autocomplete'].find(key => original.getAttribute(key));
    const selector = key ? `${original.localName}[${key}="${CSS.escape(original.getAttribute(key))}"]` : original.localName;
    // Attribute-less OTP boxes need a stable ordinal among the original input group.
    const candidates = () => [...scope.querySelectorAll(selector)].filter(visible);
    const ordinal = Math.max(0, candidates().indexOf(original));
    let current = original;
    const resolve = () => {
      // SPA steps may repurpose a still-connected input while an async fill is in flight.
      if (!sameStep()) return null;
      if (current?.isConnected) return current;
      if (scope !== document && !scope.isConnected) return null;
      current = candidates()[ordinal];
      return current || null;
    };
    resolve.sameStep = sameStep;
    fieldRefs.set(original, resolve);
    return resolve;
  };
  const typeValue = async (original, value) => {
    const text = String(value), resolve = fieldRef(original);
    let element = resolve();
    if (!element) throw new RetryStep('输入框已更新');
    fillPhase = true;
    const active = async () => {
      if (userEditing || !(await send('state')).active || userEditing) throw new StopStep();
      requireForeground();
      element = resolve();
      if (!element) throw new RetryStep('输入框已更新');
      // A per-character validation submit must not lock a half-filled field.
      intendedValues.set(element, text);
      // Only a visible loading state stops typing; validation submits are judged after filling.
      if (await waitForSubmission(element.form, {filling: true})) throw new StopStep();
      return enabled(element) && !element.readOnly;
    };
    const deadline = clock() + 5000;
    while (!(await active())) {
      if (clock() >= deadline) return false;
      await sleep(120);
    }
    if (element.value === text) return true;
    const nativeDate = element instanceof HTMLInputElement && element.type === 'date';
    const setter = Object.getOwnPropertyDescriptor(element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')?.set;
    if (!setter) return false;
    const nativeSelect = element instanceof HTMLSelectElement;
    // Native date/select popups are OS widgets. Focus their keyboard control without opening a popup.
    let focused = trustedInput && document.activeElement === element;
    if (trustedInput && !focused) focused = await tabToControl(element);
    if (trustedInput && (nativeDate || nativeSelect) && !focused) {
      if (!(await scrollToControl(element))) throw new RetryStep('原生控件暂时不可见');
      element.focus({preventScroll: true}); focused = document.activeElement === element;
    }
    if (!focused && !(await clickControl(element))) {
      if (!(await send('state')).active) throw new StopStep();
      if (!element.isConnected && resolve()) throw new RetryStep('聚焦时输入框已更新');
      return false;
    }
    if (!(await active())) return false;
    if (document.activeElement !== element) element.focus({preventScroll: true});
    if (document.activeElement !== element) throw new RetryStep('输入框暂时未获得焦点');
    await rest(140, 260);
    if (nativeDate || nativeSelect) {
      if (!(await active())) return false;
      if (trustedInput) {
        const key = async value => {
          if (!(await active()) || document.activeElement !== element) throw new StopStep();
          await trustedKey(value);
        };
        if (nativeDate) {
          const index = [...document.querySelectorAll('input[type="date"]')].indexOf(element);
          const {parts} = await send('input-date-layout', {index});
          if (!Array.isArray(parts) || parts.length !== 3) throw new Error('无法识别日期分段，请手动填写。');
          const [year, month, day] = text.split('-'), values = {year, month, day};
          await key('ArrowLeft'); await key('ArrowLeft');
          for (let part = 0; part < parts.length; part++) {
            for (const digit of values[parts[part]]) await key(digit);
            if (part < parts.length - 1) await key('ArrowRight');
          }
        } else {
          const options = [...element.options].filter(option => !option.disabled && !option.closest('optgroup[disabled]'));
          const target = options.findIndex(option => option.value === text);
          if (target < 0 || element.multiple || element.size > 1) return false;
          const fromEnd = target > (options.length - 1) / 2;
          await key(fromEnd ? 'End' : 'Home');
          const steps = fromEnd ? options.length - 1 - target : target;
          if (steps > 400) throw new Error('下拉选项过多，请手动选择后继续。');
          for (let i = 0; i < steps; i++) await key(fromEnd ? 'ArrowUp' : 'ArrowDown');
        }
        if (!resolve() || resolve().value !== text) throw new Error('日期或下拉选择结果不一致，请核对后手动继续。');
      } else {
        setter.call(element, text);
        element.dispatchEvent(nativeDate ? new InputEvent('input', {bubbles: true, inputType: 'insertReplacementText', data: null}) : new Event('input', {bubbles: true}));
      }
    } else {
      let lastKeyTarget = null;
      const edit = async (character, next, inputType) => {
        if (!(await active())) return false;
        // A controlled field may be replaced; recover focus only when it actually moved.
        if (document.activeElement !== element) {
          if (!(await clickControl(element))) throw new StopStep();
          element = resolve();
          if (!element) throw new RetryStep('输入框已更新');
          if (document.activeElement !== element) throw new RetryStep('输入框暂时未获得焦点');
        }
        if (trustedInput) {
          // Real keys insert at the caret; a re-rendered field may have moved it, so press End like a person would.
          // Number/email inputs hide the caret, so there only a replaced node triggers End.
          const length = element.value.length, caret = element.selectionStart;
          if (caret != null ? caret !== length || element.selectionEnd !== length : !!lastKeyTarget && lastKeyTarget !== element) {
            await own(() => send('input-key', {key: 'End'}));
            await rest(40, 120);
          }
          lastKeyTarget = element;
          // Real key events: keydown/keypress/beforeinput/input/keyup, all isTrusted.
          await own(() => send('input-key', {key: character}));
        } else {
          const data = inputType === 'insertText' ? character : null;
          // These remain synthetic editing events, not hardware keypresses.
          if (!element.dispatchEvent(new InputEvent('beforeinput', {data, inputType, bubbles: true, cancelable: true}))) return false;
          setter.call(element, next);
          element.dispatchEvent(new InputEvent('input', {data, inputType, bubbles: true}));
        }
        // Key down/up already takes ~35–95 ms with trusted input; keep the overall rhythm similar.
        const base = trustedInput ? [15, 130] : [35, 190];
        const pace = inputType !== 'insertText' ? between(40, 90) :
          character === ' ' || character === '@' || character === '.' ? between(180, 420) :
          between(1, 12) === 1 ? between(280, 620) : between(...base);
        await sleep(pace);
        element = resolve();
        if (!element && next === text) return true;
        if (!element) throw new RetryStep('输入框已更新');
        return element.value === next;
      };
      // Preserve a correct prefix on resume instead of erasing it.
      while (element.value && !text.startsWith(element.value)) {
        if (!(await edit('Backspace', Array.from(element.value).slice(0, -1).join(''), 'deleteContentBackward'))) return false;
      }
      if (trustedInput && element.type === 'password') {
        await rest(350, 750);
        if (!(await active()) || document.activeElement !== element) throw new StopStep();
        if (element.selectionStart !== element.value.length || element.selectionEnd !== element.value.length) await trustedKey('End');
        const remaining = text.slice(element.value.length);
        if (remaining && !(await own(() => send('input-text', {text: remaining}))).sent) throw new StopStep();
        await rest(100, 200);
        element = resolve();
        if (!element) return true;
        if (element.value !== text) return false;
      }
      let index = element.value.length;
      while (index < text.length) {
        const character = text[index];
        if (!(await edit(character, text.slice(0, index + 1), 'insertText'))) return false;
        index++;
      }
    }
    element = resolve();
    if (!element) return true;
    if (!(await active())) return false;
    const complete = element.value === text;
    intendedValues.delete(element);
    // A real blur fires the native change event after trusted typing.
    if (!trustedInput) element.dispatchEvent(new Event('change', {bubbles: true}));
    if (await waitForSubmission(element.form, {filling: true})) throw new StopStep();
    if (!trustedInput || nativeDate || nativeSelect || job.mode === 'review') element.blur();
    await rest(180, 350);
    element = resolve();
    return complete && (!element || element.value === text);
  };
  async function claim(stage, prepare = false) {
    return (await send('claim', {stage, prepare})).granted;
  }
  async function submit(anchor, stage, validate = () => '', preferred = null) {
    if (job.mode === 'manual' || !anchor) return;
    // Bind the step before waiting: an SPA may reuse this node for the next form.
    const resolve = fieldRefs.get(anchor) || fieldRef(anchor);
    const refresh = async () => {
      for (;;) {
        if (!(await send('state')).active || userEditing) return null;
        requireForeground();
        const current = resolve();
        if (!current) return null;
        const form = current.closest('form');
        if (!(await waitForSubmission(form))) return resolve();
        // A site submit fired during our typing: keep waiting briefly in this step
        // instead of dropping back to the 1.5s tick and filling/settling again.
        if (!fillPending(form)) return null;
        await sleep(200);
      }
    };
    // Leaving the final field commits its native change event. Some sites only
    // enable Continue after blur; Tab also lets keyboard submission use the button.
    const focused = document.activeElement;
    if (trustedInput && job.mode === 'auto' && focused?.form && focused.form === anchor.form &&
        focused.matches('input,select,textarea')) {
      if (!(await send('state')).active || userEditing || !resolve()) return;
      requireForeground();
      await trustedKey('Tab');
    }
    // Give asynchronous validation and OTP auto-advance one second before inspecting.
    await settle();
    anchor = await refresh();
    if (!anchor) return;
    let form = anchor.closest('form');
    const invalid = () => validate() || (form && [...form.elements].some(element =>
      element.willValidate && !element.validity.valid) ?
      '表单仍有未填写或格式不正确的内容，请按网页提示修正后继续。' : '');
    let reason = invalid();
    if (reason) {
      await recheck();
      anchor = await refresh();
      if (!anchor) return;
      form = anchor.closest('form');
      reason = invalid();
      if (reason) {
        await trace('waiting_manual', {stage});
        return pause(reason, 'validation');
      }
    }
    if (job.mode === 'review') {
      job.reviewSteps ||= {};
      job.reviewSteps[`${stage}:${location.pathname}`] = true;
      await send('review-ready', {stage});
      if (!(await refresh())) return;
      renderProgress({...job, message: '已填写，请核对后点击网页自己的继续按钮。'});
      return;
    }
    const scope = anchor.closest('[role="dialog"],dialog[open],[aria-modal="true"]') || form || document;
    // Phone steps only: the SMS option may itself be the submit button, or the button says "Send code".
    const phoneTarget = () => !/^phone/.test(stage) ? null : preferred?.isConnected && visible(preferred) ? preferred :
      button(PHONE_SUBMIT, scope) || (form && [...form.elements].find(node => visible(node) && node.matches('button:not([type])')));
    const target = (preferred && phoneTarget()) || continueButton(scope) ||
      [...scope.querySelectorAll('button[type="submit"]')].find(element => visible(element)) || phoneTarget();
    if (target) {
      const focused = document.activeElement;
      const defaultSubmit = form && [...form.elements].find(node => node.matches('button[type="submit"],button:not([type]),input[type="submit"]'));
      const enter = trustedInput && job.mode === 'auto' && Math.random() < 0.35 && target === defaultSubmit &&
        focused?.form === form && enabled(focused) && (focused === target || focused.matches('input[type="text"],input[type="email"],input[type="password"],input[type="number"],input[type="tel"],input:not([type])'));
      return clickControl(target, stage, null, enter ? focused : null);
    }
    else await pause('找不到可用的提交按钮，请在网页上手动继续。', 'no_submit_button');
  }
  async function fillProfile() {
    if (await waitForProfile()) return;
    const name = first('input[name="name"],input[name="fullName"],input[autocomplete="name"],input[placeholder*="Full name" i]');
    if (!name) return pause('个人资料表单发生变化，请手动填写姓名和生日后继续。', 'form_changed');
    // A submitted/loading profile must never be filled again on each tick.
    const resolveName = fieldRef(name);
    if (!(await claim('profile', true)) || !resolveName()) return;
    if (!(await typeValue(name, job.profile.name))) {
      if (await waitForProfile()) return;
      return pause('姓名输入未完成，请检查网页后继续。', 'fill_incomplete');
    }
    if (!resolveName()) return;
    let validateProfile = () => '';
    const [year, month, day] = job.profile.birthday.split('-');
    const age = ageField(resolveName().closest('form') || document);
    const date = first('input[type="date"]');
    if (age) {
      const today = new Date();
      let years = today.getFullYear() - Number(year);
      if (today.getMonth() + 1 < Number(month) || (today.getMonth() + 1 === Number(month) && today.getDate() < Number(day))) years--;
      const birth = new Date(Number(year), Number(month) - 1, Number(day));
      if (!Number.isInteger(years) || years < 0 || years > 120 || birth.getFullYear() !== Number(year) ||
          birth.getMonth() + 1 !== Number(month) || birth.getDate() !== Number(day)) {
        return pause('本次生日与年龄不一致，请重新开始注册。', 'fill_incomplete');
      }
      if (!(await typeValue(age, String(years)))) {
        if (await waitForProfile()) return;
        return pause('年龄输入未完成，请检查网页后继续。', 'fill_incomplete');
      }
      const resolveAge = fieldRef(age);
      // Recheck the same age field after submit()'s shared settle delay.
      validateProfile = () => {
        const current = resolveAge();
        return current && current.value === String(years) && current.validity.valid ? '' :
          '页面年龄与本次生日不一致，已暂停提交，请检查年龄。';
      };
    } else if (date) {
      if (!(await typeValue(date, job.profile.birthday))) return pause('生日输入未完成，请检查网页后继续。', 'fill_incomplete');
    } else {
      let parts = 0;
      for (const [part, value] of [['year', year], ['month', month], ['day', day]]) {
        const element = first(`select[name="${part}"],select[autocomplete="bday-${part}"],input[name="${part}"],input[autocomplete="bday-${part}"]`);
        if (!element) continue;
        if (element instanceof HTMLSelectElement) {
          const option = [...element.options].find(option => option.value !== '' && Number(option.value) === Number(value));
          if (option && await typeValue(element, option.value)) parts++;
        } else if (await typeValue(element, String(Number(value)))) parts++;
      }
      if (parts !== 3) return pause('已填写姓名；请手动填写网页的生日控件并提交，再点击插件中的继续。', 'fill_incomplete');
    }
    await trace('filled', {stage: 'profile'});
    if (!(await waitForProfile())) await submit(name, 'profile', validateProfile);
  }
  // A real Continue click authorizes only bounded retries of this unchanged form.
  // Review mode still requires the user to initiate the first submission.
  onDocument('click', event => {
    const target = event.target.closest?.('button,a,[role="button"]');
    if (!event.isTrusted || ownInput || !job?.active || job.mode === 'manual' || !continueStage(currentStage) || !target) return;
    const scope = target.closest('form') || target.closest('[role="dialog"],dialog[open],[aria-modal="true"]') || document;
    if (continueButton(scope) !== target || !enabled(target)) return;
    userEditing = false;
    const pending = clickFeedback(target, currentStage);
    setPendingClick(pending);
    void send('continue-observed', {stage: currentStage}).then(response => {
      if (pendingClick === pending) pending.token = response.token;
    }).catch(() => {});
  }, true);
  // A DOM submit event is not proof of a successful request: sites may dispatch it
  // while validating individual fields. Don't lock an idle, incomplete form.
  const formIncomplete = form => {
    const age = ageField(form);
    return [...form.querySelectorAll('input,select,textarea')]
      .some(element => visible(element) && enabled(element) &&
        ((element.willValidate && !element.validity.valid) ||
         (intendedValues.has(element) && element.value !== intendedValues.get(element)))) ||
      (age && enabled(age) && !age.readOnly && !age.value.trim());
  };
  const rememberSubmission = (form, event = false) => {
    const old = submittedForms.get(form);
    old?.flush?.();
    const control = clickForm === form;
    // Only a submit fired while the auto runner is typing (not after its click) is a fill-time submit.
    const fill = event && fillPhase && !control && (job?.mode || 'auto') === 'auto';
    const record = {at: clock(), scope: form, shape: formShape(form), values: formValues(form), control,
      fill, complete: fill && !formIncomplete(form)};
    watchLoading(record, fill ? null : continueButton(form), fill);
    record.loadingSeen ||= old?.loadingSeen && old.shape === record.shape;
    forgetSubmission(form);
    submittedForms.set(form, record);
  };
  onDocument('submit', event => {
    if (!job?.active || !(event.target instanceof HTMLFormElement)) return;
    rememberSubmission(event.target, true);
    // After an explicit click, even a spinner-free submission has an unknown server outcome.
    // Never turn that uncertainty into another automatic click on the same form.
    if (pendingClick?.scope === event.target) pendingClick.retryBlocked = true;
    // A submit event can also come from the site's own OTP auto-advance. Do not label it as human input.
    void trace('form_submit').catch(() => {});
  }, true);
  onDocument('input', event => {
    if (!event.isTrusted || ownInput || !job?.active || !autoFill() || !event.target.matches?.('input,select,textarea')) return;
    if (job.mode === 'review' && job.reviewSteps?.[`${currentStage}:${location.pathname}`]) return;
    userEditing = true;
    void pause('检测到你在手工修改表单，已暂停自动填写；确认后可继续或停止插件。', 'user_edit').catch(() => {});
  }, true);

  async function waitForSubmission(form, {filling = false} = {}) {
    if (!form) return false;
    const previous = submittedForms.get(form);
    // An initially disabled Continue can mean the form hasn't been filled yet.
    const loading = loadingNow(form, previous && !previous.fill ? continueButton(form) : null);
    const shape = formShape(form);
    const incomplete = formIncomplete(form);
    if (previous && (previous.shape !== shape || (!loading && incomplete))) forgetSubmission(form);
    if (loading && !submittedForms.has(form)) rememberSubmission(form);
    const submitted = submittedForms.get(form);
    if (!submitted) return false;
    submitted.flush();
    if (submitted.fill && !submitted.loadingSeen) {
      // Keep typing; the page's own validation submit is judged once the step is filled.
      if (filling) return false;
      if (clock() - submitted.at >= fillWait(submitted) && !incomplete && !pendingClick &&
          validationSnapshot(form).size === 0 && enabled(continueButton(form))) {
        requireForeground();
        forgetSubmission(form);
        return false;
      }
    }
    // Input/change validation can fire submit without any actual Continue click.
    // Let the normal mode continue once the complete form has stayed idle; review
    // will show its manual-submit prompt, never silently authorize a first click.
    if (continueStage(currentStage) && job.mode !== 'manual' && !submitted.loadingSeen && !submitted.control && !pendingClick &&
        clock() - submitted.at >= SUBMISSION_WAIT && submitted.values === formValues(form) &&
        validationSnapshot(form).size === 0 && enabled(continueButton(form))) {
      requireForeground();
      forgetSubmission(form);
      return false;
    }
    // The site's own submit showed a spinner, then the unchanged, complete, error-free form went
    // quiet: release it like a stalled click, so the normal step flow clicks Continue once
    // (reusing the same values and OTP). Auto mode only; never while typing, loading or after our own click.
    const stallKey = `${currentStage}:${location.pathname}`;
    const submitButton = continueButton(form);
    if (!filling && continueStage(currentStage) && (job.mode || 'auto') === 'auto' && submitted.loadingSeen && !submitted.control && !pendingClick &&
        (stallReleases.get(stallKey) || 0) < STALL_RELEASES && !loading && !loadingNow(form, submitButton) &&
        clock() - Math.max(submitted.at, submitted.busyAt || 0) >= STALL_WAIT &&
        submitted.values === formValues(form) && !incomplete && validationSnapshot(form).size === 0 && enabled(submitButton)) {
      requireForeground();
      stallReleases.set(stallKey, (stallReleases.get(stallKey) || 0) + 1);
      forgetSubmission(form);
      await trace('submit_stalled');
      renderProgress({...job, message: '网页提交后停住了，正在重新点击继续（有次数上限）。'});
      return false;
    }
    renderProgress({...job, message: submitted.loadingSeen ? '表单正在处理，等待网页完成。' :
      submitted.fill ? '已填写，稍等页面校验后提交。' : '表单触发了提交事件，正在确认网页是否前进。'});
    if (clock() - submitted.at > 45000) {
      if (['email', 'password', 'otp', 'profile'].includes(currentStage)) await claim(currentStage, true);
      await pause('表单提交后超过 45 秒未前进，请检查网页提示后继续。', 'submit_timeout');
    }
    return true;
  }

  async function manualStage(anchor, stage) {
    if (job.mode === 'manual') return true;
    if (job.mode === 'review' && job.reviewSteps?.[`${stage}:${location.pathname}`]) {
      renderProgress({...job, message: '已填写，请核对后点击网页自己的继续按钮。'});
      return true;
    }
    if (job.mode !== 'submit') return false;
    if (!manualStep || manualStep.stage !== stage || manualStep.path !== location.pathname || manualStep.anchor !== anchor) {
      manualStep = {stage, path: location.pathname, anchor}; approvedStep = null;
      await trace('waiting_manual', {stage});
    }
    renderProgress({...job, message: stage === 'signup' ? '点击下方按钮打开注册页面。' : '请手工填写网页，再点击下方“已填好，提交本步”。'});
    if (approvedStep !== manualStep) return true;
    approvedStep = null;
    if (!(await claim(stage, true))) return true;
    const email = emailField(anchor.closest('form') || document);
    if (email?.value && email.value.trim().toLowerCase() !== job.email) {
      await pause('网页填写的邮箱与本次任务不一致，请检查后继续。', 'email_mismatch'); return true;
    }
    if (stage === 'signup') await clickControl(anchor, stage);
    else await submit(anchor, stage);
    return true;
  }

  async function checkSession() {
    // Right after the signup forms the session usually appears within seconds; check it sooner.
    const interval = sessionCandidate ? 2000 : job.entryFormSeen ? 3000 : 10000;
    if (clock() - lastSessionCheck < interval) return;
    lastSessionCheck = clock();
    try {
      const response = await fetch('/api/auth/session', {credentials: 'include', cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error('session unavailable');
      const session = await response.json();
      // Some session payloads omit user.email; the access token's profile claim carries it.
      const tokenEmail = () => {
        try {
          const part = String(session.accessToken || '').split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
          const claims = JSON.parse(atob(part.padEnd(Math.ceil(part.length / 4) * 4, '=')));
          return claims['https://api.openai.com/profile']?.email;
        } catch { return ''; }
      };
      const rawEmail = typeof session.user?.email === 'string' && session.user.email.trim() ? session.user.email : tokenEmail();
      const email = typeof rawEmail === 'string' ? rawEmail.trim().toLowerCase() : '';
      const verified = typeof session.user?.emailVerified === 'boolean' ? session.user.emailVerified :
        typeof session.user?.email_verified === 'boolean' ? session.user.email_verified : undefined;
      await trace('session_check', {verified});
      sessionErrors = 0;
      sessionAnonymous = !email;
      if (!email) {
        sessionCandidate = '';
        if (job.entryFormSeen) await trace('session_anonymous');
        return;
      }
      if (email !== job.email || verified === false) {
        await send('complete', {email, verified}); return;
      }
      // Confirm the identity on two observations, without storing the response or any tokens.
      if (sessionCandidate === email) { await send('complete', {email, verified}); return; }
      sessionCandidate = email;
      renderProgress({...job, message: '已进入 ChatGPT，正在确认登录状态。'});
    } catch (error) {
      if (error instanceof StopStep) throw error;
      sessionCandidate = ''; sessionAnonymous = false;
      await trace('session_error');
      if (++sessionErrors >= 3) await pause('暂时无法确认 ChatGPT 登录状态，请检查网络和网页后继续。', 'session_error');
    }
  }

  const entryHasFields = () => !!(emailField() || ageField() ||
    first('input[type="password"],input[autocomplete="one-time-code"],input[name="code"],input[name="otp"],input[name="pin"],input[name="name"],input[name="fullName"],input[autocomplete="name"],input[type="date"],select[autocomplete="bday-year"]') ||
    all('input[maxlength="1"]').length === 6);

  async function recoverEntry() {
    if (!autoFill() || !entryPage() || job.entryFormSeen || entryHasFields()) {
      entryWaitSince = 0;
      return false;
    }
    requireForeground();
    entryWaitSince ||= clock();
    // The fallback page itself may load slowly; give it longer than the homepage.
    if (clock() - entryWaitSince < (location.pathname === '/auth/login' ? FALLBACK_PAGE_WAIT : ENTRY_WAIT)) return false;
    // Recheck after the worker round-trip so a late modal isn't replaced by navigation.
    const state = await send('state');
    if (!state.active || state.entryFormSeen || entryHasFields()) return false;
    if (state.entryFallbackUsed) {
      // This page requested the fallback and is still unloading: a slow navigation is not a failure.
      if (fallbackAt && clock() - fallbackAt < FALLBACK_NAV_WAIT) {
        renderProgress({...job, message: '正在打开备用注册入口，请稍候。'});
        return true;
      }
      await pause(ENTRY_PAUSE, 'entry_missing');
      return true;
    }
    const {granted, url} = await send('entry-fallback');
    if (!granted) return false;
    requireForeground();
    if (entryHasFields() || !(await send('state')).active) return false;
    requireForeground();
    if (entryHasFields()) return false;
    if (url !== 'https://chatgpt.com/auth/login') throw new Error('备用入口地址无效。');
    renderProgress({...job, message: '主页注册入口未响应，正在打开备用入口。'});
    fallbackAt = clock();
    location.assign(url);
    return true;
  }

  // ---- Codex OAuth phase: log in to the new account, choose its team, approve. ----
  const oneTimeCode = () => button(/^(log in with a one-time code|use a one-time code|email me a (login )?code|continue with (a )?one-time code|使用一次性验证码登录|使用一次性验证码|发送验证码)$/i);
  async function loginPassword(password, resolveAnchor) {
    if (!job.passwordSet) {
      // Accounts registered without a password log in with an emailed code.
      const code = oneTimeCode();
      if (code && enabled(code)) { await clickControl(code); return; }
      return pause('该账号注册时没有设置密码，请在网页选择邮箱验证码登录；之后插件会继续。', 'manual_step');
    }
    if (await manualStage(password, 'password')) return;
    if (!(await claim('password', true)) || !resolveAnchor()) return;
    if (!(await typeValue(password, job.password))) return pause('登录密码输入未完成，请检查网页后继续。', 'fill_incomplete');
    await trace('filled'); await submit(password, 'password');
  }
  const choiceText = node => (node.innerText || node.getAttribute('aria-label') || '').trim().toLowerCase();
  let pickedOn = '';
  async function oauthPage(text, path) {
    const names = (job.workspaceNames || []).map(name => String(name).trim().toLowerCase()).filter(Boolean);
    const approve = button(/^(continue|allow|authorize|accept|confirm|继续|允许|授权|确认)$/i);
    const approval = /consent|authorize|workspace|organization/.test(path) || /codex/.test(text);
    if (!approve || !approval) {
      const login = button(/^(log in|continue with password|使用密码继续|登录)$/i);
      if (autoFill() && job.mode === 'auto' && login && enabled(login) && location.hostname !== 'chatgpt.com') {
        unknownSince = clock();
        if (await claim('signup', true)) await clickControl(login, 'signup');
        return true;
      }
      return false;
    }
    unknownSince = clock();
    currentStage = 'consent';
    if (!autoFill()) {
      renderProgress({...job, message: '请在网页选择团队并确认授权；拿到回调后插件会自动提交 Team48。'});
      return true;
    }
    // A team chooser has selectable options or a workspace path; consent text alone is not one.
    const picker = /workspace|organization/.test(path) ||
      all('[role="radio"],[role="option"],[role="menuitemradio"],input[type="radio"]').length > 1;
    if (picker && names.length && pickedOn !== location.pathname) {
      // Same rule as the project's browser runner: click the team's exact name once, then approve.
      const matches = all('[role="radio"],[role="option"],[role="menuitemradio"],label,button,li,a')
        .filter(node => !node.closest('#team48-signup-progress') && names.includes(choiceText(node)));
      const target = matches.find(node => !matches.some(other => other !== node && node.contains(other)));
      if (!target) {
        await pause(`授权页没有找到团队「${job.workspaceNames[0]}」，请手动选择该团队后点击插件中的继续。`, 'manual_step');
        return true;
      }
      if (await clickControl(target)) pickedOn = location.pathname;
      await rest(600, 1000);
      return true;
    }
    if (!enabled(approve)) { renderProgress({...job, message: '等待授权按钮可用。'}); return true; }
    if (job.mode === 'review') {
      renderProgress({...job, message: '已选择团队，请核对后点击网页的继续按钮完成授权。'});
      return true;
    }
    if (await claim('consent', true)) {
      renderProgress({...job, message: '确认授权，等待回调。'});
      await clickControl(approve, 'consent');
    }
    return true;
  }

  // ---- Phone relay (auto / review only, never managed): number and SMS code come from Team48's pool. ----
  // Page-text rules ported from app/integrations/openai/browser/onboard.py (classify_phone_outcome).
  const PHONE_PATH = /add-phone|phone-verification|verify-phone/;
  const PHONE_INVALID = ['phone number is not valid', 'is not valid', 'not a valid phone', 'invalid phone number',
    'already linked', 'maximum number of accounts', 'too many accounts'];
  const PHONE_RECENT = ['recently used', 'used recently', 'try a different number', 'use a different number',
    'use a different phone', 'please use a different', '请使用其他', '换一个号码'];
  const PHONE_RISK = ["couldn't send a text", 'could not send a text', "can't send a text", 'cannot send a text', 'unable to send',
    "we couldn't send", 'continue with whatsapp', 'try whatsapp', 'sent a code to whatsapp', 'check whatsapp',
    'verify with whatsapp', '无法向该号发短信', '无法发送短信'];
  const SMS_WRONG = ['incorrect code', 'invalid code', 'wrong code', 'code is incorrect', 'code is invalid', 'code is not valid',
    'code was incorrect', 'code you entered', 'code has expired', 'code expired', '验证码错误', '验证码无效', '验证码不正确', '验证码已过期'];
  const PHONE_SUBMIT = /^(continue|next|verify|send|send code|send (a )?text( message)?|send sms|text me( a code)?|继续|下一步|验证|发送|发送验证码|发送短信)$/i;
  const PHONE_BACK = /^(change|edit|back|go back|change (phone )?number|edit (phone )?number|use (a )?different (phone )?number|try (a )?different (phone )?number|use another (phone )?number|返回|更换(手机)?号码|更改(手机)?号码|修改(手机)?号码|换个号码|使用其他号码)$/i;
  const hasAny = (text, tokens) => tokens.some(token => text.includes(token));
  // Visible page messages without control labels: "Use a different number" as a link is not an error.
  const phoneMessages = () => {
    const parts = [];
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || !node.nodeValue.trim() || parent.closest('button,a,[role="button"],label,option,select,script,style,noscript') ||
          !visible(parent)) continue;
      parts.push(node.nodeValue);
      if (parts.length > 2000) break;
    }
    return parts.join(' ').replace(/\s+/g, ' ').toLowerCase().replace(/[’‘]/g, "'");
  };
  const phoneOutcome = (text, codeTyped) => codeTyped && hasAny(text, SMS_WRONG) ? 'wrong_code' :
    hasAny(text, PHONE_RECENT) ? 'recently_used' : hasAny(text, PHONE_RISK) ? 'risk' : hasAny(text, PHONE_INVALID) ? 'invalid' : '';
  const plainText = node => (node.innerText || node.getAttribute('aria-label') || node.textContent || '')
    .replace(/[^\p{L}\p{N}+()\s]/gu, ' ').replace(/\s+/g, ' ').trim();
  // Phone fields reformat while typing ("(555) 123-…"), so success is judged on the digits only.
  // `force` clears and retypes even a field that already ends with the number.
  async function typePhone(original, value, national, force = false) {
    const resolve = fieldRef(original);
    const digits = () => String(resolve()?.value || '').replace(/\D/g, '');
    const done = () => digits().endsWith(national) && digits().length <= national.length + 1;
    if (!force && done()) return true;
    const active = async () => {
      if (userEditing || !(await send('state')).active || userEditing) throw new StopStep();
      requireForeground();
      const element = resolve();
      if (!element) throw new RetryStep('号码输入框已更新');
      return element;
    };
    let element = await active();
    if (!enabled(element) || element.readOnly) return false;
    if (document.activeElement !== element && !(await clickControl(element))) {
      if (!(await send('state')).active) throw new StopStep();
      return false;
    }
    element = await active();
    if (document.activeElement !== element) element.focus({preventScroll: true});
    if (document.activeElement !== element) throw new RetryStep('号码输入框暂时未获得焦点');
    await rest(140, 260);
    if (trustedInput) {
      if (element.value) await trustedKey('End');
      for (let guard = 0; resolve()?.value && guard < 40; guard++) {
        element = await active();
        if (document.activeElement !== element) throw new StopStep();
        await trustedKey('Backspace');
        await rest(30, 80);
      }
      for (const character of value) {
        element = await active();
        if (document.activeElement !== element) throw new StopStep();
        await trustedKey(character);
        await rest(40, 160);
      }
    } else {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      if (!setter) return false;
      setter.call(element, value);
      element.dispatchEvent(new InputEvent('input', {data: value, inputType: 'insertText', bubbles: true}));
      element.dispatchEvent(new Event('change', {bubbles: true}));
    }
    await rest(300, 500);
    return done();
  }
  // ---- Number page (stage `phone`): fill -> check -> Continue -> judge. A failed number is reported and the
  // page reloaded for a fresh form. The country is never checked: the page picks it from the area code.
  const PHONE_RESULT_WAIT = 20000, PHONE_BUSY_WAIT = 40000, PHONE_RELOAD_WAIT = 15000;
  const PHONE_FAIL_TEXT = {invalid: '不被 OpenAI 接受', recently_used: '刚被用过', risk: '被转到 WhatsApp 或发不出短信',
    cancelled: '提交后页面没有反应'};
  const phoneInput = email => all('input[autocomplete="tel"],input[name="phone"],input[type="tel"]')
    .find(element => element !== email && element.maxLength !== 1 && element.autocomplete !== 'one-time-code' && !/^(code|otp|pin)$/i.test(element.name));
  const phoneDigits = value => String(value || '').replace(/\D/g, '');
  const phoneScope = field => field?.closest('form') || document;
  // Hidden form fields: `channel` is the delivery method (sms / whatsapp), `phoneNumber` the full E.164 number.
  const phoneChannel = field => field ? phoneScope(field).querySelector('input[name="channel"]')?.value : undefined;
  const phoneShown = (field, reply) => {
    const hidden = phoneScope(field).querySelector('input[name="phoneNumber"]');
    return hidden ? phoneDigits(hidden.value) === phoneDigits(reply.number) : phoneDigits(field.value).endsWith(reply.national);
  };
  const phoneSubmitButton = field => {
    const scope = phoneScope(field);
    return [...scope.querySelectorAll('button[type="submit"]')].find(visible) || button(PHONE_SUBMIT, scope);
  };
  // Spinner, or a Continue that went disabled after the click: the page is still working on the number.
  const phoneBusy = field => {
    const form = field?.closest('form');
    if (!form) return false;
    const target = phoneSubmitButton(field);
    return loadingNow(form, target) || (!!target && !enabled(target));
  };
  // The re-rendered form keeps the number in its hidden field; give it a moment to catch up.
  async function phoneSettled(reply) {
    for (let waited = 0; ; waited += 250) {
      const field = phoneInput(emailField());
      if (field && phoneShown(field, reply)) return true;
      if (waited >= 1500) return false;
      await sleep(250);
    }
  }
  let phoneReloadAt = 0, phoneReloads = 0, phoneReloadedId = 0;
  // The page's answer to this number, or '' while undecided: an error that was not there before the click,
  // or the same error once the page moved the number off SMS (e.g. the previous number's WhatsApp notice).
  function phoneVerdict(field, phone) {
    const before = phone.snapshot?.step === 'number' ? phone.snapshot.outcome : '';
    const now = phoneOutcome(phoneMessages(), false);
    const channel = phoneChannel(field);
    return now && !phoneBusy(field) && (now !== before || (channel !== undefined && channel !== 'sms')) ? now : '';
  }
  // Report the number, then reload the page for a fresh form; the reloaded page takes the next number.
  async function phoneFail(phone, outcome) {
    await send('phone-result', {phoneId: phone.id, outcome});
    const state = await send('state');
    if (!state.active) { renderProgress(state); return; }
    if (state.phone?.id === phone.id) return pause('换号失败：插件没能放弃当前号码，请刷新页面后点继续。', 'phone_relay_error');
    renderProgress({...state, message: `尾号 ${phone.tail} ${PHONE_FAIL_TEXT[outcome] || '不可用'}，正在刷新页面换号`});
    // One reload per number.
    if (phoneReloadedId === phone.id) return;
    phoneReloadedId = phone.id; phoneReloads = 1; phoneReloadAt = clock();
    location.reload();
  }
  // Still on the old page after location.reload(): try once more, then pause.
  async function phoneReloading() {
    if (clock() - phoneReloadAt < PHONE_RELOAD_WAIT) {
      renderProgress({...job, message: '正在刷新页面换号，请稍候。'});
      return;
    }
    if (phoneReloads >= 2) {
      phoneReloadAt = 0;
      return pause('已刷新手机页两次，页面仍没有重新加载，请手动刷新后点继续。', 'unknown_page');
    }
    phoneReloads++; phoneReloadAt = clock();
    location.reload();
  }
  // Local to this page: what the page said when this number / code was submitted.
  let phoneSnapshot = null, phoneTraced = '', backSince = 0, backClickedAt = 0, phoneWaitSince = 0, codeWaitSince = 0;
  async function rememberPhoneSnapshot(snapshot) {
    phoneSnapshot = snapshot;
    await send('phone-snapshot', {phoneId: snapshot.id, step: snapshot.step, outcome: snapshot.outcome});
  }
  async function phoneRejected(stage) {
    const phone = job.phone;
    if (!phone) return false;
    const text = phoneMessages();
    let outcome = '';
    // WhatsApp instead of SMS shows as the code page itself, also after a reload.
    if (stage === 'phone_otp' && !phone.bound && hasAny(text, PHONE_RISK)) outcome = 'risk';
    else {
      // After a reload the in-page snapshot is gone: use the copy the worker kept for this number.
      if (phoneSnapshot?.id !== phone.id) phoneSnapshot = phone.snapshot ? {id: phone.id, ...phone.snapshot} : null;
      if (!phoneSnapshot) return false;
      const now = phoneOutcome(text, stage === 'phone_otp' && phoneSnapshot.step === 'code');
      // An error still shown from before this submission only counts once it went away and came back.
      if (!now) {
        if (phoneSnapshot.outcome) await rememberPhoneSnapshot({...phoneSnapshot, outcome: ''});
        return false;
      }
      if (phone.bound && now !== 'wrong_code') return false;
      // The same error as before this number (e.g. the last number's WhatsApp notice) counts once our
      // Continue was processed and the page stayed: a number the page accepted would have moved on.
      const answered = pendingClick?.stage === 'phone' && phoneSnapshot.step === 'number' && pendingClick.scope.isConnected &&
        pendingClick.path === location.pathname && (pendingClick.flush(), !loadingNow(pendingClick.scope, pendingClick.button)) &&
        clock() - (pendingClick.loadingSeen ? pendingClick.busyAt || pendingClick.at : pendingClick.at) > (pendingClick.loadingSeen ? 2000 : 8000);
      if (now === phoneSnapshot.outcome && !answered) return false;
      outcome = now;
    }
    phoneSnapshot = null;
    setPendingClick(null);
    // The rejected submission is over; the next number is a fresh submission of the same form.
    for (const form of document.querySelectorAll('form')) forgetSubmission(form);
    await send('phone-result', {phoneId: phone.id, outcome});
    renderProgress(await send('state'));
    return true;
  }
  async function phoneGoBack() {
    const back = all('button,a,[role="button"]').find(node => !node.closest('#team48-signup-progress') && PHONE_BACK.test(plainText(node)));
    if (!back || !enabled(back)) {
      backSince ||= clock();
      renderProgress({...job, message: '正在寻找"更换号码"入口，准备换号。'});
      if (clock() - backSince > 6000) await pause('换号时找不到"更换号码 / 返回"入口，请手动回到填写手机号的页面后点继续。', 'phone_back_missing');
      return;
    }
    if (backClickedAt && clock() - backClickedAt < 20000) {
      renderProgress({...job, message: '正在返回号码输入页。'});
      return;
    }
    if (backClickedAt) {
      await pause('已点"更换号码"但页面没有回到号码输入页，请手动回去后点继续。', 'phone_back_missing');
      return;
    }
    setPendingClick(null);
    if (await clickControl(back)) backClickedAt = clock();
  }
  async function phoneNumberStep(field) {
    // Submitted: judge first, the field may be gone for a moment while the page works.
    if (job.phone?.submitted && !job.phone.bound && job.mode !== 'review') return phoneJudge(field, job.phone);
    if (!field) {
      phoneWaitSince ||= clock();
      renderProgress({...job, message: '等待号码输入框出现。'});
      if (clock() - phoneWaitSince > 25000) await pause('手机页没有找到号码输入框，请手动处理后点继续。', 'unknown_page');
      return;
    }
    phoneWaitSince = 0;
    if (job.mode === 'review' && job.reviewSteps?.[`phone:${location.pathname}`] && job.phone) {
      // The person clicks Continue; a new error on this number still takes the next one.
      const verdict = !job.phone.bound && phoneVerdict(field, job.phone);
      if (verdict) return phoneFail(job.phone, verdict);
      renderProgress({...job, message: `已填写尾号 ${job.phone.tail} 的号码，请核对后点击网页的按钮发送短信。`});
      return;
    }
    const reply = await send('phone-number', {bound: false});
    if (!reply.number) { if (!reply.paused) renderProgress(await send('state')); return; }
    if (!(await claim('phone', true)) || !field.isConnected) return;
    renderProgress(await send('state'));
    // Without a country picker, a field that shows "+" takes the full E.164 number.
    const picker = phoneScope(field).querySelector('select,[aria-haspopup="listbox"],[role="combobox"]');
    const value = !picker && /^\+/.test(`${field.placeholder || ''}${field.value || ''}`.trim()) ? reply.number : reply.national;
    if (!(await typePhone(field, value, reply.national))) return pause('手机号输入未完成，请检查网页后继续。', 'fill_incomplete');
    // The area code may switch the country and re-render the form; check what the page will submit.
    if (!(await phoneSettled(reply))) {
      const again = phoneInput(emailField());
      if (!again || !(await typePhone(again, value, reply.national, true)) || !(await phoneSettled(reply))) {
        return pause('号码框内容与领取的号码不一致，请检查后继续。', 'fill_incomplete');
      }
    }
    if (!(await phoneSmsChannel())) return;
    // What the page already says before the click; only a different message, or the same one with the
    // number moved off SMS, is this number's answer. Kept by the worker across a reload.
    await rememberPhoneSnapshot({id: reply.id, step: 'number', outcome: phoneOutcome(phoneMessages(), false)});
    if (job.mode === 'review') {
      job.reviewSteps ||= {};
      job.reviewSteps[`phone:${location.pathname}`] = true;
      await send('review-ready', {stage: 'phone'});
      renderProgress({...job, message: `已填写尾号 ${reply.tail} 的号码，请核对后点击网页的按钮发送短信。`});
      return;
    }
    await trace('filled', {stage: 'phone'});
    renderProgress({...job, message: `已填好尾号 ${reply.tail}，正在点 Continue`});
    if (!(await phoneContinue(reply))) return;
    await send('phone-submitted', {phoneId: reply.id, step: 'number'});
    renderProgress({...job, message: `已提交尾号 ${reply.tail}，等待短信验证码页`});
  }
  // `channel` other than sms (e.g. WhatsApp preselected): pick Text Message once. Toggling it may fire a
  // DOM submit; the number page never reads submit events, so that is not taken as the number submitted.
  async function phoneSmsChannel() {
    let field = phoneInput(emailField());
    const channel = phoneChannel(field);
    if (channel === undefined || channel === 'sms') return true;
    const radio = phoneScope(field).querySelector('input[type="radio"][value="sms"]');
    const target = radio && ([...(radio.labels || [])].find(visible) || radio.closest('label'));
    if (target) await clickControl(target);
    await sleep(500);
    field = phoneInput(emailField());
    if (phoneChannel(field) === 'sms') return true;
    await pause('页面不允许短信方式（发送方式不是 Text Message），请检查网页后继续。', 'phone_relay_error');
    return false;
  }
  // Click Continue of the form that holds the number. The button is looked up again right before the
  // click: a form re-rendered for the area code brings a new one, which is fine while the number still checks.
  // The worker's per-page click count (claim / finish-click, limit 2) keeps this from looping.
  async function phoneContinue(reply) {
    const current = () => {
      const field = phoneInput(emailField());
      return field && phoneShown(field, reply) ? {field, target: phoneSubmitButton(field)} : null;
    };
    const deadline = clock() + 10000;
    let found, point;
    for (;;) {
      if (!(await send('state')).active || userEditing) return false;
      requireForeground();
      found = current();
      if (!found) { await pause('号码框内容与领取的号码不一致，请检查后继续。', 'fill_incomplete'); return false; }
      point = found.target && enabled(found.target) ? hitPoint(found.target) : null;
      if (point) break;
      // Off screen: scroll once per pass; a re-rendered button is simply looked up again.
      if (found.target && enabled(found.target)) {
        try { await scrollToControl(found.target, clock() + 1500); } catch (error) { if (!(error instanceof RetryStep)) throw error; }
      }
      if (clock() > deadline) {
        await pause(found.target ? '手机页的 Continue 按钮 10 秒内一直不可点，请检查网页后继续。' : '手机页找不到 Continue 按钮，请检查网页后继续。',
          found.target ? 'button_unavailable' : 'no_submit_button');
        return false;
      }
      await sleep(250);
    }
    if (trustedInput) { await own(() => send('input-move', {x: point.x, y: point.y})); await rest(120, 240); }
    const reservation = await send('claim', {stage: 'phone', reserve: true});
    if (!reservation.granted) return false;
    let sent = false;
    try {
      if (!(await send('state')).active || userEditing) return false;
      requireForeground();
      found = current();
      point = found?.target && enabled(found.target) ? hitPoint(found.target) : null;
      if (!point) return false;
      // A click that may have submitted before the page navigated keeps its reservation.
      sent = true;
      if (trustedInput) sent = !!(await own(() => send('input-click', {x: point.x, y: point.y}))).sent;
      else { showClick(point); found.target.click(); }
      return sent;
    } finally {
      await send('finish-click', {stage: 'phone', token: reservation.token, sent});
    }
  }
  // Submitted and still on the number page: wait for the SMS page, a new error, or the time limit.
  async function phoneJudge(field, phone) {
    const waited = phone.sinceSubmit || 0;
    const verdict = phoneVerdict(field, phone);
    if (verdict) return phoneFail(phone, verdict);
    if (waited < PHONE_RESULT_WAIT || (phoneBusy(field) && waited < PHONE_BUSY_WAIT)) {
      renderProgress({...job, message: `已提交尾号 ${phone.tail}，等待短信验证码页（已等 ${Math.round(waited / 1000)} 秒）`});
      return;
    }
    return phoneFail(phone, 'cancelled');
  }
  async function phoneCodeStep(anchor, boxes) {
    if (!job.phone) {
      // Only a code box and no number submitted by this job: the account already has a phone.
      const reply = await send('phone-number', {bound: true});
      if (reply.back) await phoneGoBack();
      else if (!reply.paused) renderProgress(await send('state'));
      return;
    }
    if (!anchor) {
      phoneWaitSince ||= clock();
      renderProgress({...job, message: '等待短信验证码输入框出现。'});
      if (clock() - phoneWaitSince > 25000) await pause('短信验证码页没有找到输入框，请手动处理后点继续。', 'unknown_page');
      return;
    }
    phoneWaitSince = 0;
    if (job.mode === 'review' && job.reviewSteps?.[`phone_otp:${location.pathname}`]) {
      renderProgress({...job, message: '已填写短信验证码，请核对后点击网页的继续按钮。'});
      return;
    }
    if (job.phone.codeSubmitted) {
      // Neither moved on nor showed a known error: stop instead of waiting until the job expires.
      codeWaitSince ||= clock();
      if (clock() - codeWaitSince > 45000) {
        return pause('短信验证码提交后 45 秒页面没有前进，请检查网页后点继续（会重新输入同一个验证码）。', 'manual_step');
      }
      renderProgress({...job, message: '已输入短信验证码，等待页面确认。'});
      return;
    }
    codeWaitSince = 0;
    const resolveAnchor = fieldRef(anchor);
    if (boxes.length === 6) boxes.forEach(fieldRef);
    if (!(await claim('phone_otp', true))) return;
    const reply = await send('phone-code');
    if (reply.rejected) { renderProgress(await send('state')); return; }
    if (!reply.code) { renderProgress(await send('state')); return; }
    if (!(await claim('phone_otp', true)) || !resolveAnchor()) return;
    const phoneId = job.phone.id;
    if (boxes.length === 6) {
      for (let index = 0; index < boxes.length; index++) {
        if (!(await typeValue(boxes[index], reply.code[index]))) return pause('短信验证码输入未完成，请检查网页后继续。', 'fill_incomplete');
      }
    } else if (!(await typeValue(anchor, reply.code))) return pause('短信验证码输入未完成，请检查网页后继续。', 'fill_incomplete');
    await rememberPhoneSnapshot({id: phoneId, step: 'code', outcome: phoneOutcome(phoneMessages(), true)});
    await send('phone-submitted', {phoneId, step: 'code'});
    await trace('filled', {stage: 'phone_otp'});
    await submit(anchor, 'phone_otp');
  }
  async function phoneTick(stage, path, field, anchor, boxes) {
    unknownSince = clock();
    if (phoneTraced !== path) { phoneTraced = path; await trace('phone'); }
    if (stage === 'phone') { backSince = 0; backClickedAt = 0; }
    if (stage === 'phone' && phoneReloadAt) return phoneReloading();
    if (trustedInput && clock() < readUntil) { renderProgress({...job, message: '页面已打开，稍候确认当前表单。'}); return; }
    // The number page judges its own result (phoneJudge); the rest is the SMS code page.
    if (stage === 'phone') return phoneNumberStep(field);
    if (await phoneRejected(stage)) return;
    if (await waitForClick(stage)) return;
    if (job.phoneBack) return phoneGoBack();
    const form = anchor?.closest('form');
    if (form && await waitForSubmission(form)) return;
    return phoneCodeStep(anchor, boxes);
  }

  let readUntil = 0, nextDrift = 0;
  function detectStage() {
    const path = location.pathname.toLowerCase();
    const email = emailField();
    const otp = first('input[autocomplete="one-time-code"],input[name="code"],input[name="otp"],input[name="pin"]');
    const boxes = all('input[maxlength="1"]');
    const password = first('input[type="password"]');
    const profile = first('input[name="name"],input[name="fullName"],input[autocomplete="name"],input[type="date"],select[autocomplete="bday-year"]') || ageField();
    const signup = button(/^(sign up( for free)?|create account|get started|免费注册|注册|创建账户|创建帐户)$/i);
    // Once the signup forms were seen, any form-free chatgpt.com page is checked through the
    // session API; the logged-in UI changes too often to depend on one element id.
    const home = location.hostname === 'chatgpt.com' && !signup &&
      (!!first('#prompt-textarea,[data-testid="profile-button"],[data-testid="accounts-profile-button"],[data-testid="composer-plus-btn"],[data-testid="create-new-chat-button"]') ||
       (!!job.entryFormSeen && !oauthPhase()));
    const stage = otp || boxes.length === 6 ? 'otp' : /about-you/.test(path) || profile ? 'profile' :
      // The logged-out homepage also shows the prompt box; a visible Sign up button means not logged in.
      password ? 'password' : email ? 'email' : signup ? 'signup' : home ? 'home' : 'unknown';
    return {stage, email, otp, boxes, password, profile, signup};
  }
  // While paused, keep watching the page. Continue by itself when (a) this page was reloaded by
  // "refresh and continue", or (b) a pause that only meant "the page did not move in time" is
  // followed by the page reaching a later step. The worker makes the final decision.
  const STAGE_ORDER = ['signup', 'email', 'password', 'otp', 'profile', 'home'];
  let autoResumeAsked = '';
  async function autoResume() {
    if (job.status !== 'paused' || !autoFill()) return false;
    // A hidden page only waits. Focus is not required: the popup holds it right after
    // "refresh and continue", and the resumed runner applies its usual foreground rules.
    if (document.visibilityState === 'hidden') return false;
    const {stage} = detectStage();
    const reload = !!job.reloadAt && runnerStarted > job.reloadAt && Date.now() - job.reloadAt < 60000 && stage !== 'unknown';
    const advanced = !!job.autoResume && !oauthPhase() &&
      STAGE_ORDER.indexOf(stage) > STAGE_ORDER.indexOf(job.pausedStage) && STAGE_ORDER.indexOf(job.pausedStage) >= 0;
    if (!reload && !advanced) return false;
    const ask = `${stage}:${job.pausedStage}:${job.reloadAt || 0}:${job.resumeCount || 0}`;
    if (ask === autoResumeAsked) return false;
    autoResumeAsked = ask;
    return (await send('auto-resume', {stage, reload})).resumed === true;
  }
  // A pause that only means "a control never became usable" (often while the window was covered)
  // retries the same step by itself after a short visible wait; the worker keeps the retry limit.
  const AUTO_RETRY_DELAY = 8000;
  let retryWaitKey = '', retryWaitSince = 0;
  async function autoRetry() {
    if (job.status !== 'paused' || !job.autoRetry || document.visibilityState === 'hidden') return false;
    const key = `${job.pausedStage}:${job.resumeCount || 0}`;
    if (key !== retryWaitKey) { retryWaitKey = key; retryWaitSince = clock(); }
    if (clock() - retryWaitSince < AUTO_RETRY_DELAY) return false;
    retryWaitSince = clock();
    return (await send('auto-retry')).resumed === true;
  }
  async function tick() {
    const wasActive = job?.active;
    fillPhase = false;
    job = await send('state');
    if (job.active && (!wasActive || resumeCount !== (job.resumeCount || 0))) {
      for (const record of loadingWatches) record.stop();
      submittedForms = new WeakMap(); userEditing = false; pendingClick = null; stallReleases = new Map();
      resumeCount = job.resumeCount || 0;
      entryWaitSince = 0; homeSince = 0; sessionErrors = 0;
      backSince = 0; backClickedAt = 0; phoneWaitSince = 0; codeWaitSince = 0; phoneReloadAt = 0;
    }
    renderProgress(job);
    if (job.active && job.mode !== 'manual' && trustedInput === null) {
      // Once per page load; hosts without CDP input (managed runner, fixtures) keep synthetic events.
      trustedInput = (await send('input-ready')).trusted === true;
    }
    if (!job.active) {
      // The entry pause is only about a missing form: once the form shows up, carry on by itself.
      if (job.status === 'paused' && job.message === ENTRY_PAUSE && autoFill() && entryHasFields()) {
        await send('resume');
        return;
      }
      if (await autoResume()) return;
      if (await autoRetry()) return;
      for (const record of loadingWatches) record.stop();
      unknownSince = clock(); profileLoadingSince = 0;
      entryWaitSince = 0;
      manualStep = null; approvedStep = null; sessionCandidate = '';
      if (['done', 'stopped'].includes(job.status)) {
        finished = true;
        listeners.abort();
      }
      return;
    }
    const text = (document.body?.innerText || '').toLowerCase();
    const path = location.pathname.toLowerCase();
    const {stage: pageStage, email, otp, boxes, password, profile, signup} = detectStage();
    const phone = all('input[autocomplete="tel"],input[name="phone"],input[type="tel"]')
      .find(element => element !== email && element.maxLength !== 1 && element.autocomplete !== 'one-time-code' && !/^(code|otp|pin)$/i.test(element.name));
    const phonePage = PHONE_PATH.test(path) || !!phone ||
      // A code form while this job holds a number is the SMS step, whatever its path; also when the
      // person clicked Continue on the number page, so the plugin never saw the number submitted.
      (!!job.phone && !job.phone.codeSubmitted && (!!otp || boxes.length === 6) && !/email-verification/.test(path)) ||
      ((!!otp || boxes.length === 6) && /check your phone|查看(你|您)的手机/.test(text) && !/email-verification/.test(path));
    // Phone relay: number form -> phone, SMS code form -> phone_otp (never the email code step).
    const relay = phonePage && !managed && job.phoneRelay === true;
    const stage = !relay ? pageStage : phone ? 'phone' :
      /phone-verification|verify-phone/.test(path) || otp || boxes.length === 6 ? 'phone_otp' : 'phone';
    currentStage = stage;
    if (stage !== 'home') homeSince = 0;
    const observation = `${location.hostname}:${path}:${stage}`;
    if (observation !== lastObservation) {
      lastObservation = observation; manualStep = null; approvedStep = null;
      readUntil = trustedInput ? clock() + between(1000, 3000) : 0;
      await trace('page');
    }
    if (!oauthPhase() && /\/consent(?:\/|$)/.test(path)) return pause('当前是授权确认页面，注册助手已暂停，请手工继续授权。', 'manual_step');
    if (/just a moment|verify you are human|checking your browser|确认您是真人|验证您是真人/.test(document.title.toLowerCase() + '\n' + text) ||
        all('iframe[src*="challenges.cloudflare.com"],iframe[src*="recaptcha"]').length) {
      await trace('captcha'); return pause('请先在网页完成人机验证，然后点击插件中的继续。', 'captcha');
    }
    if (/too many requests|too many attempts|try again later|尝试次数过多|请求过于频繁/.test(text)) {
      await trace('rate_limit'); return pause('网页提示操作频繁或限流，请按页面提示稍后重试。', 'rate_limit');
    }
    if (relay) return phoneTick(stage, path, phone, otp || boxes[0], boxes);
    if (phonePage) {
      await trace('phone');
      return pause(oauthPhase() ? '授权要求手机验证，请在网页手动完成；完成后插件会自动提交回调。' : '注册需要手机验证，请在网页手动完成后继续。', 'phone');
    }
    if (trustedInput && job.mode === 'auto' && clock() >= nextDrift && document.visibilityState !== 'hidden') {
      nextDrift = clock() + between(8000, 18000);
      // Idle movement is cosmetic; its failure must never pause the run.
      if (Math.random() < 0.3) await own(() => send('input-drift', {width: innerWidth, height: innerHeight})).catch(() => {});
    }
    if (trustedInput && autoFill() && clock() < readUntil && !['home', 'unknown'].includes(stage)) {
      renderProgress({...job, message: '页面已打开，稍候确认当前表单。'});
      return;
    }
    if (await waitForClick(stage)) return;
    if (oauthPhase() && ['home', 'signup', 'unknown'].includes(stage)) {
      if (await oauthPage(text, path)) return;
      if (stage === 'home') { unknownSince = clock(); return; }
      // Never start a new signup from an authorization page.
      if (clock() - unknownSince > 25000) await pause('未识别当前授权页面，请手动继续；拿到回调后插件会自动提交。', 'unknown_page');
      return;
    }
    if (managed && location.hostname === 'chatgpt.com' && ['home', 'unknown'].includes(stage)) {
      // Yield all page interaction to the host for invitation/workspace handling.
      unknownSince = clock();
      await send('managed-page', {stage});
      return;
    }
    if (stage === 'home') {
      unknownSince = clock(); homeSince ||= clock();
      await checkSession();
      if (sessionAnonymous) await recoverEntry();
      // Only after the signup forms: never while the logged-out homepage is still loading.
      if (job.entryFormSeen && clock() - homeSince > HOME_CONFIRM_WAIT) {
        await pause('已进入 ChatGPT 首页，但 40 秒内未能确认登录邮箱。若网页已登录本次邮箱，请在插件弹窗点「确认已注册完成」；否则请手动登录后点继续。', 'home_unconfirmed');
      }
      return;
    }
    if (['signup', 'unknown'].includes(stage)) {
      if (await recoverEntry()) return;
    } else entryWaitSince = 0;
    sessionCandidate = '';
    if (job.mode === 'manual') {
      renderProgress({...job, message: '手工记录模式：请自行注册，插件只记录步骤并确认登录状态。'});
      return;
    }
    const anchor = stage === 'otp' ? otp || boxes[0] : stage === 'profile' ? profile : stage === 'password' ? password : email;
    // Bind these controls before mailbox/worker awaits, not after a potential SPA transition.
    const resolveAnchor = anchor && fieldRef(anchor);
    if (stage === 'otp') boxes.forEach(fieldRef);
    if (anchor && await waitForSubmission(anchor.closest('form'))) return;
    if (stage === 'otp') {
      unknownSince = clock();
      if (await manualStage(anchor, 'otp')) return;
      if (!(await claim('otp', true))) return;
      const {code} = await send('code');
      if (!code || !(await claim('otp', true)) || !resolveAnchor()) return;
      if (boxes.length === 6) {
        for (let index = 0; index < boxes.length; index++) {
          if (!(await typeValue(boxes[index], code[index]))) return pause('验证码输入未完成，请检查网页后继续。', 'fill_incomplete');
        }
      } else if (!(await typeValue(otp, code))) return pause('验证码输入未完成，请检查网页后继续。', 'fill_incomplete');
      await trace('filled');
      await submit(anchor, 'otp'); return;
    }
    if (stage === 'profile') {
      unknownSince = clock();
      if (profile && await manualStage(profile, 'profile')) return;
      if (!autoFill()) return;
      await fillProfile(); return;
    }
    if (password) {
      unknownSince = clock();
      if (password.autocomplete === 'current-password' || /log-in\/password|login\/password/.test(path)) {
        if (!oauthPhase()) return pause('网页要求登录已有账号，请确认邮箱或手工继续。插件不会反复切换注册入口。', 'manual_step');
        return loginPassword(password, resolveAnchor);
      }
      if (await manualStage(password, 'password')) return;
      if (await claim('password', true) && resolveAnchor()) {
        const formEmail = emailField(password.form || document);
        if (formEmail && formEmail.value !== job.email) {
          if (formEmail.readOnly || (formEmail.matches(':disabled') && formEmail.value)) return pause('密码页显示的邮箱与本次注册不一致，请检查网页。', 'email_mismatch');
          if (!(await typeValue(formEmail, job.email))) return pause('邮箱输入未完成，请检查网页后继续。', 'fill_incomplete');
        }
        if (!(await typeValue(password, job.password))) return pause('密码输入未完成，请检查网页后继续。', 'fill_incomplete');
        await trace('filled'); await submit(password, 'password');
      }
      return;
    }
    if (email) {
      unknownSince = clock();
      if (await manualStage(email, 'email')) return;
      if (await claim('email', true) && resolveAnchor()) {
        if (!(await typeValue(email, job.email))) return pause('邮箱输入未完成，请检查网页后继续。', 'fill_incomplete');
        await trace('filled'); await submit(email, 'email');
      }
      return;
    }
    if (signup) {
      unknownSince = clock();
      if (await manualStage(signup, 'signup')) return;
      if (await claim('signup', true)) await clickControl(signup, 'signup');
      return;
    }
    if (job.mode === 'submit') return;
    if (clock() - unknownSince > 25000) await pause('未识别当前页面，请手动继续至注册表单或 ChatGPT 首页，再点击插件中的继续。', 'unknown_page');
  }
  let retryCount = 0, timer, running;
  managed?.onStop(async () => {
    disconnected = true;
    clearTimeout(timer);
    listeners.abort();
    for (const record of loadingWatches) record.stop();
    if (job) job.active = false;
    await running?.catch(() => {});
    panel?.host.remove();
  });
  async function loop() {
    try { await tick(); retryCount = 0; }
    catch (error) {
      try {
        if (error instanceof RetryStep) {
          if (++retryCount >= 3) await pause(`输入框持续重绘或无法聚焦（${error.message || '控件已更新'}），请检查页面后继续。`, 'redraw');
        } else if (error instanceof StopStep) retryCount = 0;
        else if (job?.active) await pause(error.message, 'error');
      } catch { /* extension reloaded */ }
    }
    if (!disconnected && !finished) timer = setTimeout(startLoop, 1500);
  }
  const startLoop = () => { running = loop(); };
  startLoop();
  // Browser signals for the server self-check (see the listener at the top). Values only;
  // no page text, cookies or storage.
  async function selfcheckSignals() {
    const safe = async read => { try { return await read(); } catch { return null; } };
    const uaData = await safe(async () => {
      const data = navigator.userAgentData;
      if (!data) return null;
      const high = await data.getHighEntropyValues(['platform', 'platformVersion', 'architecture', 'bitness']);
      return {platform: high.platform ?? data.platform ?? null, platformVersion: high.platformVersion ?? null,
        architecture: high.architecture ?? null, bitness: high.bitness ?? null, mobile: !!data.mobile,
        brands: (data.brands || []).map(item => ({brand: String(item.brand), version: String(item.version)}))};
    });
    const webgl = await safe(() => {
      const canvas = document.createElement('canvas');
      const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
      if (!gl) return null;
      const info = gl.getExtension('WEBGL_debug_renderer_info');
      return {vendor: gl.getParameter(gl.VENDOR) ?? null, renderer: gl.getParameter(gl.RENDERER) ?? null,
        unmaskedVendor: info ? gl.getParameter(info.UNMASKED_VENDOR_WEBGL) ?? null : null,
        unmaskedRenderer: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) ?? null : null};
    });
    const webrtcIps = await safe(() => new Promise(resolve => {
      const ips = new Set();
      const connection = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
      const add = candidate => {
        const text = candidate?.candidate || '';
        const address = candidate?.address || text.split(' ')[4] || '';
        if (address && !address.endsWith('.local') && /^[0-9a-f.:]+$/i.test(address)) ips.add(address);
      };
      connection.onicecandidate = event => { if (event.candidate) add(event.candidate); };
      connection.createDataChannel('probe');
      connection.createOffer().then(offer => connection.setLocalDescription(offer)).catch(() => {});
      setTimeout(() => { try { connection.close(); } catch { /* closed */ } resolve([...ips].slice(0, 20)); }, 5000);
    }));
    return {
      userAgent: navigator.userAgent ?? null,
      platform: navigator.platform ?? null,
      uaData,
      timezone: await safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone ?? null),
      utcOffsetMinutes: -new Date().getTimezoneOffset(),
      language: navigator.language ?? null,
      languages: Array.isArray(navigator.languages) ? [...navigator.languages].slice(0, 10) : null,
      screen: {width: screen.width, height: screen.height, availWidth: screen.availWidth, availHeight: screen.availHeight,
        colorDepth: screen.colorDepth, devicePixelRatio: window.devicePixelRatio},
      window: {outerWidth, outerHeight, innerWidth, innerHeight},
      hardwareConcurrency: navigator.hardwareConcurrency ?? null,
      deviceMemory: navigator.deviceMemory ?? null,
      webdriver: typeof navigator.webdriver === 'boolean' ? navigator.webdriver : null,
      webgl,
      webrtcIps,
    };
  }
})();
