import {ACTIVE, BATCH_ACTIVE, MAX_BATCH_EMAILS, parseBatchEmails} from './shared.mjs';
const $ = id => document.getElementById(id);
const isTabView = new URLSearchParams(location.search).get('tab') === '1';
if (isTabView) document.body.classList.add('tab-view');

let currentJob;
let currentBatch = null;
let busy = false;
let incognito = false;
let incognitoAllowed = true;
let team48 = false, workspaces = null, workspaceError = '', workspacesRetryAt = 0;
let activeTab = 'batch';
let userPickedTab = false;
let lastBatchItemsKey = '';
const BEEP_STORAGE_KEY = 'team48:last-beep-at';
let lastBeepAttentionAt = (() => {
  try { return sessionStorage.getItem(BEEP_STORAGE_KEY) || ''; } catch { return ''; }
})();

const HANDOFF_BUSY = new Set(['resolving', 'syncing', 'waiting_join', 'opening', 'authorizing', 'completing']);
const HANDOFF_TITLES = {resolving: '正在识别团队', syncing: '正在接入 Team48', waiting_join: '等待接受邀请', opening: '正在打开授权页', authorizing: '正在授权',
  completing: '正在提交 Team48', done: '已接入 Team48', failed: '接入未完成', callback_retry: '回调待重新提交'};
const BATCH_STATE_LABELS = {
  running: '正在批量注册',
  cooldown: '冷却等待下一个窗口…',
  waiting_human: '需要你处理当前网页',
  paused: '已暂停',
  finished: '批量已完成',
  stopped: '已停止',
};
const BATCH_ITEM_LABELS = {
  pending: '等待中',
  running: '进行中',
  waiting_human: '待人工',
  paused: '已暂停',
  succeeded: '成功',
  failed: '失败',
  skipped: '已跳过',
  stopped: '已停止',
};
const STAGE_LABELS = {
  signup: '填邮箱',
  email: '邮箱',
  password: '设密码',
  otp: '验证码',
  profile: '填资料',
  phone: '手机验证',
  phone_otp: '短信码',
  consent: '授权确认',
  home: '接入团队',
};
const ATTENTION_TITLES = {
  captcha: '遇到真人验证，请到无痕窗口处理',
  phone: '需要人工处理手机验证',
  phone_pool_empty: '手机号池已空，需人工处理或补号',
  phone_limit: '连续换号达上限，需人工处理',
  phone_back_missing: '手机验证页面需要人工操作',
  phone_relay_error: '手机接码服务异常，需人工处理',
  rate_limit: '触发网站限流，请查看无痕窗口',
  user: '任务已手动暂停',
  user_edit: '检测到人工修改页面，已暂停',
  debugger_detached: '页面调试连接已断开，请查看窗口',
};
const LAST_WORKSPACE = 'team48:last-handoff-v2';
const RS_DIRECT = 'rs';
const AUTO_TEAM = 'auto';
const HINT_RS_DIRECT = '默认不查 Team48 团队：网页遇到工作空间选择时只有 1 个 Team 自动点选，有 2 个及以上立即停止；注册完成后直接在本页授权并导入 codex-rs（已存在则覆盖更新凭据并保留原设置），遇手机验证暂停交人工。';
const HINT_TEAM_AUTO = '开始注册就在后台查询哪个团队邀请了这个邮箱，注册完成后自动：同步团队 → 接入本地 → 在本页授权 → 推送（按 Team48 设置推到 Sub2API 或 codex-rs）→ 今日切换 +1。邀请需已发出。';
const HINT_TEAM_FIXED = '注册完成后自动：同步所选团队 → 接入本地 → 在本页授权 → 推送（按 Team48 设置推到 Sub2API 或 codex-rs）→ 今日切换 +1。';
const HINT_NONE = '只完成 ChatGPT 注册，不自动发起授权或接入。';

function renderAutoHints() {
  const hintFor = value => value === RS_DIRECT ? HINT_RS_DIRECT : value === AUTO_TEAM ? HINT_TEAM_AUTO : value ? HINT_TEAM_FIXED : HINT_NONE;
  if ($('batch-auto-hint')) $('batch-auto-hint').textContent = hintFor($('batch-auto-workspace').value);
  if ($('single-auto-hint')) $('single-auto-hint').textContent = hintFor($('auto-workspace').value);
}

function fillWorkspaceSelect(select, empty, {withAuto = false, withRs = false} = {}) {
  const saved = localStorage.getItem(LAST_WORKSPACE);
  let wanted;
  if (select.dataset.ready === '1') wanted = select.value;
  else if (saved === 'none') wanted = '';
  else if (saved) wanted = saved;
  else wanted = withRs ? RS_DIRECT : withAuto ? AUTO_TEAM : '';
  select.replaceChildren(new Option(empty, ''));
  if (withRs) select.add(new Option('直接授权接入 rs（不查团队，推荐）', RS_DIRECT));
  if (withAuto && !workspaceError) select.add(new Option('查 Team48 团队并接入', AUTO_TEAM));
  for (const item of workspaces || []) select.add(new Option(item.name, String(item.id)));
  select.value = [...select.options].some(option => option.value === wanted) ? wanted : (withRs && saved !== 'none' ? RS_DIRECT : '');
  select.dataset.ready = '1';
}
function fillAllSelects() {
  fillWorkspaceSelect($('auto-workspace'), '不自动接入', {withAuto: true, withRs: true});
  fillWorkspaceSelect($('batch-auto-workspace'), '不自动接入', {withAuto: true, withRs: true});
  fillWorkspaceSelect($('handoff-workspace'), workspaceError || '选择接入方式…', {withRs: true});
  renderAutoHints();
}
async function loadWorkspaces() {
  if (!team48) return;
  if (!$('auto-workspace').dataset.ready) fillAllSelects();
  if (workspaces || Date.now() < workspacesRetryAt) return;
  workspaces = [];
  try { workspaces = (await request('workspaces')).items; workspaceError = ''; }
  catch (error) { workspaces = null; workspaceError = error.message; workspacesRetryAt = Date.now() + 15000; }
  fillAllSelects();
}
const chosenWorkspace = select => {
  const item = (workspaces || []).find(entry => String(entry.id) === select.value);
  return item ? {workspaceId: item.id, workspaceName: item.name} : null;
};
const selectedHandoff = select => {
  if (!team48) return null;
  if (select.value === RS_DIRECT) return {rs: true};
  if (select.value === AUTO_TEAM) return {auto: true};
  return chosenWorkspace(select);
};
function rememberHandoffChoice(val) {
  if (!team48) return;
  localStorage.setItem(LAST_WORKSPACE, val || 'none');
}
function syncWorkspaceChoice(source, target) {
  if (!team48) return;
  const val = source.value;
  rememberHandoffChoice(val);
  if ([...target.options].some(option => option.value === val)) target.value = val;
  renderAutoHints();
}
function syncHidePanel(checked) {
  localStorage.setItem('hidePanel', String(checked));
  $('hide-panel').checked = checked;
  $('batch-hide-panel').checked = checked;
}

function showFeedback(text, isError = false) {
  $('feedback').classList.toggle('error', isError);
  $('feedback').textContent = text;
}
async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
    showFeedback(successMessage, false);
  } catch (error) {
    showFeedback(error?.message || '复制失败，请检查剪贴板权限。', true);
  }
}

function playAttentionBeep() {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    void ctx.resume?.().catch(() => {});
    const now = ctx.currentTime;
    for (const [offset, freq] of [[0, 880], [0.22, 1174.66]]) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, now + offset);
      gain.gain.setValueAtTime(0.001, now + offset);
      gain.gain.exponentialRampToValueAtTime(0.18, now + offset + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, now + offset + 0.16);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + offset);
      osc.stop(now + offset + 0.17);
    }
    setTimeout(() => { try { void ctx.close(); } catch {} }, 600);
  } catch {}
}
function checkAttentionBeep(batch) {
  if (!batch || batch.status !== 'waiting_human' || !batch.attentionAt) return;
  const key = String(batch.attentionAt);
  if (key === lastBeepAttentionAt) return;
  lastBeepAttentionAt = key;
  try { sessionStorage.setItem(BEEP_STORAGE_KEY, key); } catch {}
  playAttentionBeep();
}

async function openProgressTab({activate = false} = {}) {
  const baseUrl = chrome.runtime.getURL('popup.html');
  const tabUrl = `${baseUrl}?tab=1`;
  try {
    const tabs = await chrome.tabs.query({url: `${baseUrl}*`});
    const existing = tabs.find(tab => !tab.incognito);
    if (existing) {
      await chrome.tabs.update(existing.id, {active: true});
      if (activate && Number.isInteger(existing.windowId)) {
        await chrome.windows.update(existing.windowId, {focused: true});
      }
      return;
    }
    const windows = await chrome.windows.getAll({windowTypes: ['normal']});
    const normalWindow = windows.find(win => !win.incognito);
    const options = {url: tabUrl, active: activate};
    if (normalWindow) options.windowId = normalWindow.id;
    await chrome.tabs.create(options);
    if (activate && normalWindow) await chrome.windows.update(normalWindow.id, {focused: true});
  } catch {}
}

function renderHandoff(job) {
  const box = $('handoff');
  const handoff = job?.handoff;
  const eligible = team48 && job && (job.status === 'done' || job.phase === 'oauth' || handoff);
  box.hidden = !eligible;
  if (!eligible) return;
  const status = handoff?.status || 'idle';
  const isRs = handoff?.rs === true;
  box.dataset.status = status;
  const rsTitles = {syncing: '正在准备 rs 授权', opening: '正在打开授权页', authorizing: '正在授权',
    completing: '正在导入 codex-rs', done: '已接入 codex-rs', failed: '接入未完成', callback_retry: '回调待重新提交'};
  $('handoff-title').textContent = (isRs ? rsTitles[status] : HANDOFF_TITLES[status]) || '授权与接入';
  $('handoff-team').textContent = handoff?.workspaceName || '';
  $('handoff-message').textContent = handoff?.message || '默认选「直接授权接入 rs（不查团队）」即可在本页完成授权并导入 codex-rs；也可选择具体 Team48 团队接入。';
  const steps = $('handoff-steps');
  const rows = status === 'done' ? [['授权', {ok: true, message: handoff.message}], ['Sub2API', handoff.followups?.sub2api],
    ['codex-rs', handoff.followups?.codexRs], ['切换次数', handoff.followups?.switchCount]].filter(([, step]) => step) : [];
  steps.hidden = !rows.length;
  steps.replaceChildren(...rows.map(([name, step]) => {
    const li = document.createElement('li');
    li.className = step.ok === true ? 'ok' : step.ok === false ? 'fail' : 'skip';
    li.textContent = `${name}：${step.message}`;
    return li;
  }));
  const working = HANDOFF_BUSY.has(status);
  $('handoff-form').hidden = working || status === 'done' || status === 'callback_retry';
  $('handoff-start').disabled = busy || !$('handoff-workspace').value;
  $('handoff-start').textContent = status === 'failed' ? '重新接入' : '接入并授权';
  $('handoff-retry').hidden = status !== 'callback_retry';
  if (!working && status !== 'done') void loadWorkspaces();
}

const modes = {auto: '自动填写并提交', review: '自动填写，手动提交', submit: '手工填写，确认后由插件提交', manual: '全程手工，仅记录流程'};
const hints = {
  auto: '沿用一键注册。手工修改字段时会暂停，避免覆盖你的输入。',
  review: '填好后请核对并点击网页的继续按钮；验证码/资料页点击后无响应时，最多自动补点两次。网站也可能自行提交验证码。',
  submit: '你填写网页和验证码，再点击进度卡片的“已填好，提交本步”。',
  manual: '插件不填写、不点击、不读取邮箱；仅记录步骤并确认最终登录状态。',
};
function renderOptions() {
  const mode = $('workflow').value;
  $('workflow-hint').textContent = hints[mode];
  const manual = ['submit', 'manual'].includes(mode);
  $('profile-options').hidden = manual;
  $('profile-name').disabled = manual;
  $('profile-birthday').disabled = manual;
  $('hide-panel').disabled = manual;
}

function updateBatchEmailHint() {
  const raw = $('batch-emails').value;
  const hint = $('batch-emails-hint');
  const batchRunning = !!(currentBatch && BATCH_ACTIVE.has(currentBatch.status));
  if (!raw.trim()) {
    hint.className = 'hint';
    hint.textContent = `每行一个邮箱，自动去重，单批最多 ${MAX_BATCH_EMAILS} 个；开始前请关闭其他无痕窗口。`;
    $('batch-start').disabled = true;
    return null;
  }
  const parsed = parseBatchEmails(raw);
  if (parsed.ok) {
    hint.className = 'hint ok';
    hint.textContent = `已识别 ${parsed.emails.length} 个有效邮箱${parsed.duplicateCount ? `（已自动去重 ${parsed.duplicateCount} 个）` : ''}。`;
  } else {
    hint.className = 'hint error';
    const sample = parsed.invalid.length ? ` 示例：${parsed.invalid.slice(0, 3).join('、')}` : '';
    const dup = !parsed.invalid.length && parsed.duplicateCount ? `（已去重 ${parsed.duplicateCount} 个）` : '';
    hint.textContent = `${parsed.message}${dup}${sample}`;
  }
  $('batch-start').disabled = busy || !incognitoAllowed || batchRunning || !parsed.ok;
  return parsed;
}

const request = async (type, payload = {}) => {
  const result = await chrome.runtime.sendMessage({type, ...payload});
  if (!result?.ok) throw new Error(result?.error || '插件未响应，请重新打开。');
  return result;
};

function renderTabs() {
  const isBatch = activeTab === 'batch';
  $('tab-batch').setAttribute('aria-selected', String(isBatch));
  $('tab-single').setAttribute('aria-selected', String(!isBatch));
  $('batch-view').hidden = !isBatch;
  $('single-view').hidden = isBatch;
  const dot = $('batch-tab-dot');
  const activeBatch = !!(currentBatch && BATCH_ACTIVE.has(currentBatch.status));
  dot.hidden = !activeBatch;
  dot.classList.toggle('warn', currentBatch?.status === 'waiting_human');
  $('footer-text').textContent = isBatch ? '批量无痕轮换 · 自动模式从 iCloud 接码' : '单次注册 · 自动模式从 iCloud 接码';
}

function renderHeaderStatus() {
  $('incognito-warning').hidden = incognitoAllowed;
  const badge = $('window-badge');
  badge.classList.remove('ready', 'warn');
  if (!incognitoAllowed) {
    badge.textContent = '需开无痕权限';
    badge.classList.add('warn');
  } else if (incognito) {
    badge.textContent = '无痕窗口';
    badge.classList.add('ready');
  } else {
    badge.textContent = '批量就绪';
    badge.classList.add('ready');
  }

  if (activeTab === 'batch') {
    const activeBatch = !!(currentBatch && BATCH_ACTIVE.has(currentBatch.status));
    $('mode').textContent = !incognitoAllowed ? '请先开启「允许在无痕模式下运行」后使用批量注册' :
      activeBatch ? '批量队列进行中，插件会自动逐个新建并关闭无痕窗口' :
      currentBatch ? '本批记录已结束，可复制结果或清空后开始新批次' :
      '粘贴多行邮箱，一键轮换无痕窗口批量注册';
  } else {
    const activeSingle = !!(currentJob && ACTIVE.has(currentJob.status));
    $('mode').textContent = !incognito ? '请在无痕窗口中打开插件开始注册' :
      activeSingle ? '本次注册进行中，可在下方暂停、继续或停止' : '填写邮箱，选择本次操作方式';
  }

  if (isTabView) {
    if (currentBatch?.status === 'waiting_human') {
      document.title = '【需处理】批量注册 · Team48';
    } else if (currentBatch && BATCH_ACTIVE.has(currentBatch.status)) {
      const c = currentBatch.counts;
      const done = c.succeeded + c.failed + c.skipped;
      document.title = `批量注册 (${done}/${c.total}) · Team48`;
    } else {
      document.title = 'ChatGPT 批量注册 · Team48';
    }
  }
}

function render(job) {
  currentJob = job;
  $('progress').hidden = !job;
  $('register').hidden = !!(job && ACTIVE.has(job.status));
  $('start').disabled = busy || !incognito || !incognitoAllowed || !!(job && ACTIVE.has(job.status));
  $('start').textContent = busy ? '正在处理…' : '开始注册 ↗';
  if (!job) return;
  $('state').textContent = (job.phase === 'oauth' ? {running: '正在授权', paused: '需要手动处理', done: '授权回调已提交', stopped: '已停止'} :
    {running: '正在注册', paused: '需要手动处理', done: '已进入 ChatGPT', stopped: '已停止'})[job.status];
  $('progress').dataset.status = job.status;
  $('account').textContent = job.email;
  $('message').textContent = job.message;
  // While signing up, show what the automatic team lookup found.
  const resolved = job.autoHandoff?.auto && !job.handoff ? job.autoHandoff.resolve : null;
  $('auto-team').hidden = !resolved?.message;
  $('auto-team').textContent = resolved?.message ? `自动接入：${resolved.message}` : '';
  $('run-mode').textContent = modes[job.mode] || modes.auto;
  const manual = ['submit', 'manual'].includes(job.mode);
  $('generated-credentials').hidden = manual;
  $('copy').textContent = manual ? '复制邮箱' : '复制账号密码';
  $('credential-hint').textContent = manual ? '请保存你在网页实际设置的密码；插件不读取手工密码。' : '密码仅在网页要求设置时使用，请在关闭浏览器前保存。';
  $('codex-result').disabled = busy || job.status !== 'done';
  $('codex-result').value = job.codexResult || 'unknown';
  $('password').value = job.password;
  $('pause').hidden = job.status !== 'running';
  $('resume').hidden = job.status !== 'paused';
  $('stop').hidden = !ACTIVE.has(job.status);
  // Refreshing only helps a stuck page in the automatic modes; manual modes keep plain Continue.
  $('reload-resume').hidden = job.status !== 'paused' || !['auto', 'review'].includes(job.mode);
  $('reload-resume').disabled = busy || job.reloading;
  // Fallback when the logged-in page is not recognized; not offered before any signup form.
  $('mark-complete').hidden = !ACTIVE.has(job.status) || job.phase === 'oauth' || !job.formSeen;
  $('mark-complete').disabled = busy;
  if ($('mark-complete').hidden) confirmArmed = false;
  $('mark-complete').textContent = confirmArmed ? '再点一次确认：网页已是本邮箱登录后的 ChatGPT' : '网页已登录本邮箱，确认已注册完成';
  $('clear').disabled = ACTIVE.has(job.status) || HANDOFF_BUSY.has(job.handoff?.status);
  renderHandoff(job);
}

function batchFailItems(batch) {
  if (!batch) return [];
  return batch.items.filter(item =>
    ['failed', 'skipped', 'stopped'].includes(item.status) ||
    (batch.status === 'stopped' && item.status === 'pending'));
}

function renderBatchItems(batch) {
  const list = $('batch-items');
  const key = JSON.stringify([batch.currentIndex, batch.status, batch.items]);
  if (key === lastBatchItemsKey) return;
  lastBatchItemsKey = key;
  const scrollTop = list.scrollTop;
  const active = BATCH_ACTIVE.has(batch.status);

  const nodes = batch.items.map(item => {
    const li = document.createElement('li');
    li.className = 'batch-item' + (active && item.index === batch.currentIndex ? ' is-current' : '');
    li.dataset.status = item.status;

    const main = document.createElement('div');
    main.className = 'item-main';
    const idx = document.createElement('span');
    idx.className = 'item-index';
    idx.textContent = String(item.index + 1);
    const email = document.createElement('span');
    email.className = 'item-email';
    email.textContent = item.email;
    email.title = item.email;
    const status = document.createElement('span');
    status.className = 'item-status';
    const stageText = item.status === 'running' && STAGE_LABELS[item.stage] ? ` · ${STAGE_LABELS[item.stage]}` : '';
    status.textContent = (BATCH_ITEM_LABELS[item.status] || item.status) + stageText;
    main.append(idx, email, status);
    li.append(main);

    const hasSub = !!(item.workspaceName || item.message || item.password);
    if (hasSub) {
      const sub = document.createElement('div');
      sub.className = 'item-sub';
      if (item.workspaceName) {
        const team = document.createElement('span');
        team.className = 'item-team';
        team.textContent = item.workspaceName;
        team.title = item.workspaceName;
        sub.append(team);
      }
      if (item.message) {
        const msg = document.createElement('span');
        msg.className = 'item-msg';
        msg.textContent = item.message;
        msg.title = item.message;
        sub.append(msg);
      }
      if (item.password) {
        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'text-button item-copy';
        copyBtn.dataset.copyIndex = String(item.index);
        copyBtn.textContent = '复制账密';
        sub.append(copyBtn);
      }
      li.append(sub);
    }
    return li;
  });

  list.replaceChildren(...nodes);
  list.scrollTop = scrollTop;
}

function renderBatch(batch) {
  currentBatch = batch;
  $('batch-progress').hidden = !batch;
  $('batch-form').hidden = !!batch;
  $('batch-start').textContent = busy ? '正在启动…' : '开始批量注册 ↗';
  updateBatchEmailHint();
  if (!batch) {
    lastBatchItemsKey = '';
    return;
  }

  checkAttentionBeep(batch);
  const section = $('batch-progress');
  section.dataset.status = batch.status;
  $('open-tab-view').hidden = isTabView;

  let stateText = BATCH_STATE_LABELS[batch.status] || batch.status;
  if (batch.status === 'cooldown' && batch.cooldownUntil) {
    const remain = Math.max(1, Math.ceil((batch.cooldownUntil - Date.now()) / 1000));
    stateText = `冷却等待下一个窗口（${remain} 秒）…`;
  }
  $('batch-state').textContent = stateText;

  const c = batch.counts;
  const done = c.succeeded + c.failed + c.skipped;
  $('batch-counts').textContent = `已完成 ${done} / 总计 ${c.total} · 成功 ${c.succeeded} · 失败 ${c.failed} · 跳过 ${c.skipped}${c.stopped ? ` · 已停 ${c.stopped}` : ''}`;

  const handoffText = batch.handoffMode === 'rs' ? '自动接入：直接授权接入 rs（不查团队）' :
    batch.handoffMode === 'auto' ? '自动接入：查 Team48 团队并接入' :
    batch.handoffMode === 'workspace' ? `自动接入：${batch.workspaceName || '指定团队'}` : '';
  $('batch-meta').hidden = !handoffText;
  $('batch-meta').textContent = handoffText;
  $('batch-message').textContent = batch.message || '';

  const needsHuman = batch.status === 'waiting_human';
  $('batch-attention').hidden = !needsHuman;
  if (needsHuman) {
    const currentItem = (batch.currentIndex >= 0 ? batch.items[batch.currentIndex] : null) ||
      batch.items.find(item => item.status === 'waiting_human');
    $('batch-attention-title').textContent = ATTENTION_TITLES[batch.attentionReason] || '需要你处理当前网页';
    $('batch-attention-email').textContent = currentItem ? `当前邮箱：${currentItem.email}` : '';
    $('batch-attention-reason').textContent = currentItem?.message || batch.message || '请切换到无痕窗口完成操作后，点击下方「我已处理，继续」。';
  }

  const active = BATCH_ACTIVE.has(batch.status);
  const runningLike = batch.status === 'running' || batch.status === 'cooldown';
  const pausedLike = batch.status === 'waiting_human' || batch.status === 'paused';
  $('batch-focus').hidden = !active;
  $('batch-focus').disabled = busy || !batch.windowId;
  $('batch-pause').hidden = !runningLike;
  $('batch-pause').disabled = busy;
  $('batch-resume').hidden = !pausedLike;
  $('batch-resume').disabled = busy;
  $('batch-skip').hidden = !active;
  $('batch-skip').disabled = busy;
  $('batch-skip').textContent = pausedLike ? '跳过此号，跑下一个' : '跳过此号';
  $('batch-stop').hidden = !active;
  $('batch-stop').disabled = busy;
  $('batch-clear').hidden = active;
  $('batch-clear').disabled = busy;

  const okItems = batch.items.filter(item => item.status === 'succeeded');
  const failItems = batchFailItems(batch);
  $('batch-copy-creds').disabled = busy || !okItems.length;
  $('batch-copy-creds').textContent = okItems.length ? `复制成功账号密码 (${okItems.length})` : '复制成功账号密码';
  $('batch-copy-ok-emails').disabled = busy || !okItems.length;
  $('batch-copy-ok-emails').textContent = okItems.length ? `复制成功邮箱 (${okItems.length})` : '复制成功邮箱';
  $('batch-copy-fail-emails').disabled = busy || !failItems.length;
  $('batch-copy-fail-emails').textContent = failItems.length ? `复制失败/未跑邮箱 (${failItems.length})` : '复制失败/未跑邮箱';

  renderBatchItems(batch);
}

async function refresh() {
  const view = await request('view');
  incognito = view.incognito === true;
  incognitoAllowed = view.incognitoAllowed !== false;
  team48 = view.team48 === true;
  $('team48-auto').hidden = !team48;
  $('batch-team48-auto').hidden = !team48;
  if (team48) void loadWorkspaces();

  if (!userPickedTab) {
    activeTab = view.batch ? 'batch' : (!incognito ? 'batch' : 'single');
  }
  render(view.job);
  if (!view.job) renderHandoff(null);
  renderBatch(view.batch || null);
  renderTabs();
  renderHeaderStatus();
  document.querySelector('.brand').textContent = `TEAM48 · ${view.version}`;
}

async function act(task) {
  if (busy) return;
  busy = true;
  showFeedback('', false);
  render(currentJob);
  renderBatch(currentBatch);
  try { await task(); await refresh(); }
  catch (error) { showFeedback(error.message, true); }
  finally {
    busy = false;
    render(currentJob);
    renderBatch(currentBatch);
    renderTabs();
    renderHeaderStatus();
  }
}

$('tab-batch').addEventListener('click', () => {
  userPickedTab = true;
  activeTab = 'batch';
  renderTabs();
  renderHeaderStatus();
});
$('tab-single').addEventListener('click', () => {
  userPickedTab = true;
  activeTab = 'single';
  renderTabs();
  renderHeaderStatus();
});

$('batch-emails').addEventListener('input', updateBatchEmailHint);
$('batch-form').addEventListener('submit', event => {
  event.preventDefault();
  const parsed = updateBatchEmailHint();
  if (!parsed?.ok) {
    showFeedback(parsed?.message || '请先输入有效邮箱地址。', true);
    return;
  }
  if (!incognitoAllowed) {
    showFeedback('请先在 chrome://extensions 开启「允许在无痕模式下运行」。', true);
    return;
  }
  const handoff = selectedHandoff($('batch-auto-workspace'));
  rememberHandoffChoice($('batch-auto-workspace').value);
  const emailsText = $('batch-emails').value;
  const hidePanel = $('batch-hide-panel').checked;
  act(async () => {
    await request('batch-start', {emailsText, hidePanel, handoff});
    $('batch-emails').value = '';
    if (!isTabView) await openProgressTab({activate: false});
  });
});

$('open-tab-view').addEventListener('click', () => void openProgressTab({activate: true}));
for (const action of ['batch-pause', 'batch-resume', 'batch-skip', 'batch-stop', 'batch-clear', 'batch-focus']) {
  $(action).addEventListener('click', () => act(() => request(action)));
}

$('batch-copy-creds').addEventListener('click', () => {
  if (!currentBatch) return;
  const rows = currentBatch.items.filter(item => item.status === 'succeeded').map(item => `${item.email}----${item.password}`);
  if (!rows.length) return;
  void copyText(rows.join('\n'), `已复制 ${rows.length} 条成功账号密码。`);
});
$('batch-copy-ok-emails').addEventListener('click', () => {
  if (!currentBatch) return;
  const rows = currentBatch.items.filter(item => item.status === 'succeeded').map(item => item.email);
  if (!rows.length) return;
  void copyText(rows.join('\n'), `已复制 ${rows.length} 个成功邮箱。`);
});
$('batch-copy-fail-emails').addEventListener('click', () => {
  if (!currentBatch) return;
  const rows = batchFailItems(currentBatch).map(item => item.email);
  if (!rows.length) return;
  void copyText(rows.join('\n'), `已复制 ${rows.length} 个失败/未跑邮箱。`);
});
$('batch-items').addEventListener('click', event => {
  const button = event.target.closest('button[data-copy-index]');
  if (!button || !currentBatch) return;
  const item = currentBatch.items[Number(button.dataset.copyIndex)];
  if (!item?.password) return;
  void copyText(`${item.email}----${item.password}`, `已复制 ${item.email} 的账号密码。`);
});

$('register').addEventListener('submit', event => {
  event.preventDefault();
  const mode = $('workflow').value;
  const handoff = selectedHandoff($('auto-workspace'));
  rememberHandoffChoice($('auto-workspace').value);
  act(() => request('start', {email: $('email').value, mode, hidePanel: !$('hide-panel').disabled && $('hide-panel').checked,
    profile: ['auto', 'review'].includes(mode) ? {name: $('profile-name').value, birthday: $('profile-birthday').value} : undefined,
    handoff}));
});
$('auto-workspace').addEventListener('change', () => syncWorkspaceChoice($('auto-workspace'), $('batch-auto-workspace')));
$('batch-auto-workspace').addEventListener('change', () => syncWorkspaceChoice($('batch-auto-workspace'), $('auto-workspace')));
$('handoff-workspace').addEventListener('change', () => renderHandoff(currentJob));
$('handoff-start').addEventListener('click', () => {
  const val = $('handoff-workspace').value;
  if (val === RS_DIRECT) {
    rememberHandoffChoice(RS_DIRECT);
    act(() => request('handoff-start', {rs: true}));
    return;
  }
  const choice = chosenWorkspace($('handoff-workspace'));
  if (!choice) return;
  rememberHandoffChoice(String(choice.workspaceId));
  act(() => request('handoff-start', choice));
});
$('handoff-retry').addEventListener('click', () => act(() => request('handoff-retry')));
for (const type of ['pause', 'stop', 'resume', 'clear', 'reload-resume']) $(type).addEventListener('click', () => act(() => request(type)));
let confirmArmed = false;
$('mark-complete').addEventListener('click', () => {
  if (!confirmArmed) { confirmArmed = true; render(currentJob); return; }
  confirmArmed = false;
  act(() => request('mark-complete'));
});
$('focus').addEventListener('click', () => act(async () => {
  const tab = await chrome.tabs.update(currentJob.tabId, {active: true});
  await chrome.windows.update(tab.windowId, {focused: true});
}));
$('copy').addEventListener('click', () => act(async () => {
  const manual = ['submit', 'manual'].includes(currentJob.mode);
  await navigator.clipboard.writeText(manual ? currentJob.email : `${currentJob.email}\n${currentJob.password}`);
  showFeedback(manual ? '邮箱已复制。' : '邮箱和本次生成的密码已复制。', false);
}));
$('reveal').addEventListener('click', () => {
  const show = $('password').type === 'password';
  $('password').type = show ? 'text' : 'password';
  $('reveal').textContent = show ? '隐藏' : '显示';
  $('reveal').setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
  $('reveal').setAttribute('aria-pressed', String(show));
});
$('workflow').addEventListener('change', renderOptions);
$('codex-result').addEventListener('change', () => {
  const result = $('codex-result').value;
  act(() => request('set-result', {result}));
});
$('diagnostics').addEventListener('click', () => act(async () => {
  const {report} = await request('diagnostics');
  await navigator.clipboard.writeText(JSON.stringify(report, null, 2));
  showFeedback('脱敏诊断记录已复制，可以保存或发来排查。', false);
}));
const savedHidePanel = localStorage.getItem('hidePanel') === 'true';
syncHidePanel(savedHidePanel);
$('hide-panel').addEventListener('change', () => syncHidePanel($('hide-panel').checked));
$('batch-hide-panel').addEventListener('change', () => syncHidePanel($('batch-hide-panel').checked));

renderOptions();
renderTabs();
updateBatchEmailHint();
refresh().catch(error => { showFeedback(error.message, true); });
setInterval(() => { if (!busy) refresh().catch(error => { showFeedback(error.message, true); }); }, 1500);
