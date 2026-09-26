/* Shared stage presentation. Only explicit step evidence is marked complete. */
(() => {
  const labels = {pending: '提交中', queued: '排队中', running: '进行中', waiting: '等待中', manual_required: '需人工',
    success: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消'};
  const kinds = {onboard: '邀请入组', replenish: '补充团队', rotate: '受控轮转', kick_member: '移出成员'};
  const stepStates = {done: '已完成', skipped: '已跳过', visited: '已记录', current: '当前阶段', failed: '未完成', pending: '待确认'};
  const mounts = new Map();
  let api = {}, announcement = '';
  const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; };
  const active = item => ['pending', 'queued', 'running', 'waiting'].includes(item?.state);
  const needsHelp = item => ['failed', 'partial', 'manual_required'].includes(item?.state);
  const friendly = value => api.friendlyError ? api.friendlyError(value) : String(value || '');
  function duration(item, now = Date.now()) {
    const start = Date.parse(item.started_at || item.started);
    const end = Date.parse(item.finished_at || item.finished);
    const seconds = Number.isFinite(start) ? Math.max(0, Math.floor(((Number.isFinite(end) ? end : now) - start) / 1000)) : item.elapsed_seconds;
    if (seconds == null) return '用时未知';
    return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
  function model(item) {
    const logs = item.log || [], steps = item.steps || [];
    const observed = [...(item.observed_stages || []), ...logs.map(row => row.stage)].filter(Boolean);
    let code = item.stage_code || item.current_step;
    const plan = item.stage_plan?.length ? item.stage_plan : steps.map(step => ({code: step.step_name, label: step.step_label || '任务步骤', stages: [step.step_name]}));
    let index = plan.findIndex(stage => (stage.stages || [stage.code]).includes(code));
    // OAuth reuses browser substages after the join gate; keep it in authorization.
    const authorization = plan.findIndex(stage => stage.code === 'authorizing');
    if (index >= 0 && plan[index].code === 'browser' && authorization >= 0 && observed.includes('authorizing')) index = authorization;
    if (index < 0 && !active(item)) {
      for (const value of [...observed].reverse()) {
        index = plan.findIndex(stage => (stage.stages || [stage.code]).includes(value));
        if (index >= 0) { code = value; break; }
      }
    }
    const rows = plan.map((stage, i) => {
      const codes = stage.stages || [stage.code];
      const evidence = steps.filter(step => codes.includes(step.step_name));
      const explicit = evidence.at(-1);
      let state = explicit?.result?.skipped || codes.includes('skip_invite') && observed.includes('skip_invite') ? 'skipped'
        : explicit?.state === 'success' ? 'done' : ['failed', 'partial', 'manual_required'].includes(explicit?.state) ? 'failed'
          : observed.some(value => codes.includes(value)) ? 'visited' : 'pending';
      if (i === index && state !== 'skipped' && state !== 'done') state = needsHelp(item) ? 'failed' : active(item) ? 'current' : state;
      return {...stage, state, error: explicit?.error_message ? friendly(explicit.error_message) : '',
        sublabel: i === index && item.stage_label && item.stage_label !== stage.label ? item.stage_label : ''};
    });
    return {rows, index, ordinal: index >= 0 ? `第 ${index + 1}/${plan.length} 步` : '阶段待确认',
      label: index >= 0 ? plan[index].label : item.stage_label || item.business_step || '等待后台状态'};
  }
  function summarizeOperationResult(kind, payload = {}) {
    const result = payload.result && typeof payload.result === 'object' ? payload.result : payload;
    const email = result.child?.email || result.email || payload.email;
    const parts = [];
    if (kind === 'kick_member') {
      if (result.vacancy?.confirmed || result.child || result.success) parts.push(`已移出${email ? ` ${email}` : '成员'}`);
      if (result.paused_sub2api === true) parts.push('Sub2API 调度已暂停（未解绑）');
      else if (result.pause_result?.ok === false) parts.push('Sub2API 暂停未确认，请核对远端调度');
    } else if (['onboard', 'replenish'].includes(kind)) {
      if (result.joined === true || result.joined !== false && (result.status === 'active' || result.success)) parts.push(`${email || '成员'} 已入组`);
      if (result.authorized === true) parts.push('授权已完成');
      else if (result.authorized === false) parts.push('授权未完成，请使用同一邮箱继续授权');
      if (result.pushed === true) parts.push('已推送 Sub2API');
      else if (result.pushed === false) parts.push('本轮未推送 Sub2API');
    } else if (kind === 'rotate') {
      const oldEmail = result.kick?.child?.email, newEmail = result.invite?.child?.email;
      if (oldEmail && newEmail && result.success) parts.push(`${oldEmail} → ${newEmail} 轮转完成`);
      else if (result.rotated && !result.success) parts.push('旧号已移出，补位未完成');
      if (result.rotation_count != null && result.daily_limit != null) parts.push(`今日 ${result.rotation_count}/${result.daily_limit} 次`);
    } else return null;
    // Billing vacancy is not a live occupied-seat count. Never infer capacity from it.
    if (result.occupied_seats != null && result.seat_limit != null) parts.push(`席位 ${result.occupied_seats}/${result.seat_limit}`);
    if (result.error) parts.push(friendly(result.error));
    if (!parts.length) return result.message ? friendly(result.message) : null;
    return parts.join(' · ');
  }
  function button(text, action, cls = 'button compact') {
    const node = el('button', cls, text); node.type = 'button';
    node.addEventListener('click', () => action(node)); return node;
  }
  function render(host, item, density, stale) {
    host.hidden = !item;
    if (!item) { host.replaceChildren(); host._taskSignature = null; return; }
    const view = model(item), summary = !active(item) ? summarizeOperationResult(item.kind || item.operation, item) : '';
    if (density === 'badge') {
      const text = `${needsHelp(item) ? '任务中断' : kinds[item.kind] || item.operation_label || '处理中'}${view.index >= 0 ? ` · ${view.index + 1}/${view.rows.length}` : ''}`;
      if (host._taskSignature !== text + item.id) {
        host._taskSignature = text + item.id;
        host.className = 'task-progress-badge';
        const action = button(text, b => api.open?.(item.id, b), `management-badge tone-${needsHelp(item) ? 'warning' : 'info'}`);
        action.title = `${view.label} · ${item.stage_label || ''}`;
        host.replaceChildren(action);
      }
      return;
    }
    const signature = JSON.stringify([item.id, item.state, view, summary, item.can_cancel, item.cancel_requested, item.can_retry, item.account_id, item.wait_reason, item.safe_error_message, item.error, item.target_label, stale]);
    if (host._taskSignature !== signature) {
      host._taskSignature = signature; host.replaceChildren();
      host.className = `task-progress task-progress-${density}`;
      host.dataset.state = item.state; host.dataset.operation = item.id;
      const title = el('div', 'task-progress-title');
      title.append(el('strong', '', `${item.operation_label || kinds[item.kind] || '后台任务'} · ${labels[item.state] || '状态待确认'}`), el('span', 'task-elapsed'));
      const substage = item.stage_label && item.stage_label !== view.label ? ` · ${item.stage_label}` : '';
      host.append(title, el('p', 'task-progress-stage', `${view.ordinal} · ${view.label}${substage}`));
      if (item.target_label && density === 'compact' && !mounts.get(host)?.workspaceId) host.append(el('p', 'muted', item.target_label));
      if (stale) host.append(el('p', 'text-warning', '状态更新中断，以下为上次记录；稍后自动重试'));
      if (view.rows.length) {
        const list = el('ol', density === 'compact' ? 'task-segments' : 'task-steps');
        list.setAttribute('aria-label', '流程阶段（非完成百分比）');
        view.rows.forEach(row => {
          const li = el('li', `task-step is-${row.state}`);
          li.setAttribute('aria-label', `${row.label}：${stepStates[row.state]}`);
          if (row.state === 'current') li.setAttribute('aria-current', 'step');
          const mark = el('span', 'task-step-mark', row.state === 'failed' ? '!' : ''); mark.setAttribute('aria-hidden', 'true');
          li.append(mark);
          if (density !== 'compact') {
            const copy = el('span', 'task-step-copy'); copy.append(el('strong', '', row.label), el('small', '', [stepStates[row.state], row.sublabel, row.error].filter(Boolean).join(' · ')));
            li.append(copy);
          }
          list.append(li);
        });
        host.append(list);
      }
      if (density !== 'compact' && view.rows.length) host.append(el('p', 'hint', '“已记录”表示到达过该阶段；未记录的步骤不推定成功或跳过。'));
      if (item.wait_reason) host.append(el('p', 'hint', item.wait_reason));
      if (item.cancel_requested) host.append(el('p', 'text-warning', '取消已请求，等待安全停止；已产生的变更不会自动撤销。'));
      if (summary) host.append(el('p', 'task-result', summary));
      else if (needsHelp(item)) host.append(el('p', 'task-result', friendly(item.safe_error_message || item.error || '任务未完成，请查看详情并核对团队状态。')));
      const actions = el('div', 'task-progress-actions');
      if (!item.localPending) {
        actions.append(button('查看详情', b => api.open?.(item.id, b)));
        if (density !== 'compact' && item.can_cancel && !stale) actions.append(button('取消任务', b => api.cancel?.(item, b)));
        if (density !== 'compact' && item.can_retry && !stale) actions.append(button('重试任务', b => api.retry?.(item, b)));
        if (density !== 'compact' && needsHelp(item) && item.account_id) {
          const link = el('a', 'button compact', '查看账号'); link.href = `/accounts?account=${encodeURIComponent(item.account_id)}`; actions.append(link);
        }
      } else {
        const link = el('a', 'button compact', '查看任务记录'); link.href = '/operations'; actions.append(link);
      }
      if (density !== 'compact' && !active(item) && mounts.get(host)?.workspaceId) actions.append(button('收起', () => { mounts.get(host).dismissed = item.id; update(); }));
      host.append(actions);
    }
    const timer = host.querySelector('.task-elapsed'); if (timer) timer.textContent = `用时 ${duration(item)}`;
  }
  function pick(options) {
    const runtime = window.Team48Runtime;
    if (options.id) return runtime.get(options.id) || options.item;
    if (options.account) {
      const account = options.account;
      return runtime.all().find(item => kinds[item.kind] && item.account_id != null && String(item.account_id) === String(account.id)
        && (!account.workspace_id || String(item.workspace_id) === String(account.workspace_id))
        && (active(item) || needsHelp(item) && Date.parse(item.updated_at || item.finished_at) > (Date.parse(account.latest_check?.checked_at) || 0)));
    }
    const items = runtime.all().filter(item => String(item.workspace_id) === String(options.workspaceId) && kinds[item.kind]);
    const item = items.find(active) || items[0];
    if (!item || options.dismissed === item.id) return null;
    if (options.density === 'compact' && !active(item) && !needsHelp(item) && Date.now() - Date.parse(item.finished_at) > 10000) return null;
    return item;
  }
  function mount(host, options) { host.tabIndex = -1; mounts.set(host, options); render(host, pick(options), options.density || 'standard', window.Team48Runtime.isStale()); return host; }
  function update() {
    for (const [host, options] of mounts) {
      if (!host.isConnected) { mounts.delete(host); continue; }
      render(host, pick(options), options.density || 'standard', window.Team48Runtime.isStale());
      if (options.metadata) options.metadata.hidden = !host.hidden;
    }
    const runtime = window.Team48Runtime, data = runtime.snapshot();
    const top = document.getElementById('task-center-open');
    const counts = data?.counts || {}, pendingCount = runtime.all().filter(item => item.localPending && active(item)).length;
    const running = (counts.running || 0) + (counts.queued || 0) + (counts.waiting || 0) + pendingCount;
    if (top) { top.textContent = runtime.isStale() ? '任务 · 状态更新中断' : counts.waiting_user ? `${counts.waiting_user} 个需处理 · ${running} 个进行中` : running ? `${running} 个进行中` : '任务'; top.dataset.attention = String(Boolean(counts.waiting_user)); }
    const runner = document.getElementById('global-runner-status');
    if (runner) runner.textContent = runtime.isStale() ? '状态更新中断' : ({healthy: '调度心跳正常', unknown: '尚无调度心跳', stale: '调度心跳过期', unavailable: '调度未就绪'}[data?.runner?.state] || '正在读取调度状态');
    const center = document.getElementById('task-center-list');
    if (center && !document.getElementById('task-center').hidden) {
      const items = [...runtime.all().filter(item => active(item) || item.state === 'manual_required'), ...(data?.recent_operations || []).map(item => runtime.get(item.id) || item)];
      const unique = [...new Map(items.map(item => [item.id, item])).values()];
      const old = new Map([...center.children].map(node => [node.dataset.operation, node]));
      unique.forEach((item, index) => { const host = old.get(item.id) || el('section'); old.delete(item.id); render(host, item, 'compact', runtime.isStale()); if (center.children[index] !== host) center.insertBefore(host, center.children[index] || null); });
      old.forEach(node => node.remove());
      document.getElementById('task-center-empty').hidden = Boolean(unique.length);
      document.getElementById('task-center-empty').textContent = data ? '没有进行中的任务或最近记录' : '正在读取任务…';
      const error = document.getElementById('task-center-error'); error.hidden = !runtime.isStale();
    }
    const changed = (data?.active_operations || []).map(item => `${item.id}:${item.stage_code}:${item.state}`).join('|');
    if (changed !== announcement) {
      announcement = changed;
      const live = document.getElementById('task-stage-announcement');
      if (live) live.textContent = (data?.active_operations || []).map(item => `${item.target_label}，${item.operation_label}，${item.stage_label}`).join('；');
    }
  }
  window.Team48TaskProgress = {model, duration, summarizeOperationResult, mount, render, update, configure: actions => {api = actions;}};
  window.Team48Runtime.subscribe(update);
})();
