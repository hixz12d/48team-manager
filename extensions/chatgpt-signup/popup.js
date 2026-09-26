import {ACTIVE} from './shared.mjs';
const $ = id => document.getElementById(id);
let currentJob;
let busy = false;
let incognito = false;
let team48 = false, workspaces = null, workspaceError = '', workspacesRetryAt = 0;
const HANDOFF_BUSY = new Set(['syncing', 'waiting_join', 'opening', 'authorizing', 'completing']);
const HANDOFF_TITLES = {syncing: '正在接入 Team48', waiting_join: '等待接受邀请', opening: '正在打开授权页', authorizing: '正在授权',
  completing: '正在提交 Team48', done: '已接入 Team48', failed: '接入未完成', callback_retry: '回调待重新提交'};
const LAST_WORKSPACE = 'team48:last-workspace';
function fillWorkspaceSelect(select, empty) {
  const keep = select.value || localStorage.getItem(LAST_WORKSPACE) || '';
  select.replaceChildren(new Option(workspaceError || empty, ''));
  for (const item of workspaces || []) select.add(new Option(item.name, String(item.id)));
  select.value = (workspaces || []).some(item => String(item.id) === keep) ? keep : '';
}
async function loadWorkspaces() {
  if (!team48 || workspaces || Date.now() < workspacesRetryAt) return;
  workspaces = [];
  try { workspaces = (await request('workspaces')).items; workspaceError = ''; }
  catch (error) { workspaces = null; workspaceError = error.message; workspacesRetryAt = Date.now() + 15000; }
  fillWorkspaceSelect($('auto-workspace'), '不自动接入');
  fillWorkspaceSelect($('handoff-workspace'), '选择团队…');
}
const chosenWorkspace = select => {
  const item = (workspaces || []).find(entry => String(entry.id) === select.value);
  return item ? {workspaceId: item.id, workspaceName: item.name} : null;
};
function renderHandoff(job) {
  const box = $('handoff');
  const handoff = job?.handoff;
  const eligible = team48 && job && (job.status === 'done' || job.phase === 'oauth' || handoff);
  box.hidden = !eligible;
  if (!eligible) return;
  const status = handoff?.status || 'idle';
  box.dataset.status = status;
  $('handoff-title').textContent = HANDOFF_TITLES[status] || '接入 Team48';
  $('handoff-team').textContent = handoff?.workspaceName || '';
  $('handoff-message').textContent = handoff?.message || '选择团队后自动：同步 → 接入本地 → 授权 → 推送 Sub2API → 今日切换 +1。';
  const steps = $('handoff-steps');
  const rows = status === 'done' ? [['授权', {ok: true, message: handoff.message}], ['Sub2API', handoff.followups?.sub2api],
    ['切换次数', handoff.followups?.switchCount]].filter(([, step]) => step) : [];
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
const request = async (type, payload = {}) => {
  const result = await chrome.runtime.sendMessage({type, ...payload});
  if (!result?.ok) throw new Error(result?.error || '插件未响应，请重新打开。');
  return result;
};
function render(job) {
  currentJob = job;
  $('progress').hidden = !job;
  $('register').hidden = !!(job && ACTIVE.has(job.status));
  $('start').disabled = busy || !incognito || !!(job && ACTIVE.has(job.status));
  $('start').textContent = busy ? '正在处理…' : '开始注册 ↗';
  if (!job) return;
  $('state').textContent = (job.phase === 'oauth' ? {running: '正在授权', paused: '需要手动处理', done: '授权回调已提交', stopped: '已停止'} :
    {running: '正在注册', paused: '需要手动处理', done: '已进入 ChatGPT', stopped: '已停止'})[job.status];
  $('progress').dataset.status = job.status;
  $('account').textContent = job.email;
  $('message').textContent = job.message;
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
  // Fallback when the logged-in page is not recognized; not offered before any signup form.
  $('mark-complete').hidden = !ACTIVE.has(job.status) || job.phase === 'oauth' || !job.formSeen;
  $('mark-complete').disabled = busy;
  if ($('mark-complete').hidden) confirmArmed = false;
  $('mark-complete').textContent = confirmArmed ? '再点一次确认：网页已是本邮箱登录后的 ChatGPT' : '网页已登录本邮箱，确认已注册完成';
  $('clear').disabled = ACTIVE.has(job.status) || HANDOFF_BUSY.has(job.handoff?.status);
  renderHandoff(job);
}
async function refresh() {
  const view = await request('view');
  incognito = view.incognito;
  team48 = view.team48 === true;
  $('team48-auto').hidden = !team48;
  if (team48 && !view.job) void loadWorkspaces();
  $('window-badge').textContent = incognito ? '无痕窗口' : '普通窗口';
  $('window-badge').classList.toggle('ready', incognito);
  const active = !!(view.job && ACTIVE.has(view.job.status));
  $('mode').textContent = !incognito ? '请在无痕窗口中打开插件开始注册' :
    active ? '本次注册进行中，可在下方暂停、继续或停止' : '填写邮箱，选择本次操作方式';
  render(view.job);
  if (!view.job) renderHandoff(null);
  document.querySelector('.brand').textContent = `TEAM48 · ${view.version}`;
}
async function act(task) {
  if (busy) return;
  busy = true; $('feedback').textContent = ''; $('feedback').classList.remove('error'); render(currentJob);
  try { await task(); await refresh(); }
  catch (error) { $('feedback').classList.add('error'); $('feedback').textContent = error.message; }
  finally { busy = false; render(currentJob); }
}
$('register').addEventListener('submit', event => {
  event.preventDefault();
  const mode = $('workflow').value;
  const handoff = team48 ? chosenWorkspace($('auto-workspace')) : null;
  if (handoff) localStorage.setItem(LAST_WORKSPACE, String(handoff.workspaceId));
  act(() => request('start', {email: $('email').value, mode, hidePanel: !$('hide-panel').disabled && $('hide-panel').checked,
    profile: ['auto', 'review'].includes(mode) ? {name: $('profile-name').value, birthday: $('profile-birthday').value} : undefined,
    handoff}));
});
$('handoff-workspace').addEventListener('change', () => renderHandoff(currentJob));
$('handoff-start').addEventListener('click', () => {
  const choice = chosenWorkspace($('handoff-workspace'));
  if (!choice) return;
  localStorage.setItem(LAST_WORKSPACE, String(choice.workspaceId));
  act(() => request('handoff-start', choice));
});
$('handoff-retry').addEventListener('click', () => act(() => request('handoff-retry')));
for (const type of ['pause', 'stop', 'resume', 'clear']) $(type).addEventListener('click', () => act(() => request(type)));
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
  $('feedback').textContent = manual ? '邮箱已复制。' : '邮箱和本次生成的密码已复制。';
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
  $('feedback').textContent = '脱敏诊断记录已复制，可以保存或发来排查。';
}));
$('hide-panel').checked = localStorage.getItem('hidePanel') === 'true';
$('hide-panel').addEventListener('change', () => localStorage.setItem('hidePanel', String($('hide-panel').checked)));
renderOptions();
refresh().catch(error => { $('feedback').textContent = error.message; });
setInterval(() => { if (!busy) refresh().catch(error => { $('feedback').textContent = error.message; }); }, 1500);
