(() => {
  const controllers = new Map();
  const palette = document.getElementById("command-palette");
  const commandInput = document.getElementById("command-input");
  const commandList = document.getElementById("command-list");
  const drawer = document.getElementById("operations-drawer");
  const sheet = document.getElementById("entity-sheet");
  const menu = document.getElementById("action-menu");
  const registerSheet = document.getElementById("register-sheet");
  const reauthSheet = document.getElementById("reauth-sheet");
  const phoneImportSheet = document.getElementById("phone-import-sheet");
  const proxyAddSheet = document.getElementById("proxy-add-sheet");
  const onboardSheet = document.getElementById("onboard-sheet");
  const rotateSheet = document.getElementById("rotate-sheet");
  const proxyEditSheet = document.getElementById("proxy-edit-sheet");
  const manageChildrenSheet = document.getElementById("manage-children-sheet");
  let focusTrapRoot = null;
  const SECRET_MASK = "••••••";
  const pageCache = { items: [], kind: "", portfolio: null };
  let settingsBaseline = "";
  let settingsDirty = false;
  let overlayReturn = null;
  let pollTimer = null;
  let searchTimer = null;

  const destinations = [
    { label: "去总览", href: "/" },
    { label: "去团队", href: "/workspaces" },
    { label: "去账号", href: "/accounts" },
    { label: "去任务", href: "/operations" },
    { label: "去手机号", href: "/resources/phones" },
    { label: "去 HME", href: "/resources/hme" },
    { label: "去代理", href: "/resources/proxies" },
    { label: "打开设置", href: "/settings" },
  ];

  const purposeLabels = { mother: "母号", child: "子号", standby: "待命", disabled: "停用", free: "空闲号" };
  const stateLabels = {
    active: "在用",
    available: "空闲",
    unused: "没用过",
    standby: "待命",
    conflict: "有冲突",
    archived: "已归档",
    unknown: "未知",
    disabled: "停用",
    free: "空闲",
  };
  const statusLabels = {
    queued: "排队",
    running: "进行中",
    waiting: "等待",
    success: "完成",
    failed: "失败",
    cancelled: "已取消",
    manual_required: "需人工",
    partial: "部分完成",
    pending: "待确认",
    verified: "已验证",
    missing: "缺失",
    unbound: "未绑定",
    conflict: "冲突",
    none: "未绑定",
    set: "已设",
    off: "关着",
    unchecked: "未检测",
    healthy: "正常",
    degraded: "降级",
    failed: "失败",
    refresh_due: "需刷新",
    refreshing: "刷新中",
    oauth_required: "需 OAuth",
    phone_required: "需手机",
    deactivated: "已停用",
    ok: "正常",
    needs_auth: "需授权",
    identity_conflict: "身份冲突",
    vacancy: "空席",
    billing: "账单不清",
    quota_probe: "额度刷新",
    reauth: "重新授权",
    onboard: "拉人",
    rotate: "轮转",
    workspace_sync: "同步官方成员",
    kick_member: "踢出成员",
    revoke_invite: "撤回邀请",
    hme_reconcile: "HME 对账",
    hme_label_retry: "HME 标签重试",
    auth_probe: "授权探测",
    reconcile: "对账",
    sub2api_sync: "Sub2API 对账",
    sub2api_reconcile: "Sub2API 对账",
    sub2api_push: "Sub2API 推送",
    remote_missing: "远端未找到",
    not_eligible: "不适用",
    not_synced: "尚未同步",
    sync_failed: "同步失败",
    needs_management: "待接入",
    membership_drift: "成员漂移",
    full: "已满席",
    snapshot_updated: "快照已更新",
    verification_failed: "复读失败",
    proxy_check: "代理检测",
    free_register: "空闲号注册",
    reregister: "重注册",
  };

  function abortEntity(key) {
    const previous = controllers.get(key);
    if (previous) previous.abort();
    const next = new AbortController();
    controllers.set(key, next);
    return next;
  }

  function friendlyError(error) {
    const text = String(error && error.message ? error.message : error || "请求失败");
    if (text.length > 180 || text.trim().startsWith("{") || text.trim().startsWith("[")) {
      return "请求失败，请重试。";
    }
    return text;
  }

  async function fetchEntity(key, url, options = {}) {
    const controller = abortEntity(key);
    const { headers, ...rest } = options;
    const response = await fetch(url, {
      ...rest,
      headers: { Accept: "application/json", ...(headers || {}) },
      signal: controller.signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = payload.detail || `请求失败: ${key}`;
      throw new Error(typeof detail === "string" ? detail : "请求失败，请重试。");
    }
    return response.json();
  }

  function labelOf(map, value) {
    if (value == null || value === "") return "—";
    return map[value] || String(value);
  }

  function toneFor(code) {
    if (["conflict", "identity_conflict", "failed", "manual_required", "deactivated", "danger"].includes(code)) return "danger";
    if (["warning", "needs_auth", "refresh_due", "oauth_required", "phone_required", "vacancy", "billing", "waiting", "pending", "quota_full"].includes(code)) return "warning";
    if (["success", "verified", "healthy", "ok", "active", "running"].includes(code)) return "success";
    if (["queued", "accent"].includes(code)) return "accent";
    return "";
  }

  function relativeTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    const future = seconds < 0;
    const abs = Math.abs(seconds);
    let text = "刚刚";
    if (abs >= 60 && abs < 3600) text = `${Math.floor(abs / 60)} 分钟${future ? "后" : "前"}`;
    else if (abs >= 3600 && abs < 86400) text = `${Math.floor(abs / 3600)} 小时${future ? "后" : "前"}`;
    else if (abs >= 86400) text = `${Math.floor(abs / 86400)} 天${future ? "后" : "前"}`;
    else if (abs >= 8 && abs < 60) text = `${abs} 秒${future ? "后" : "前"}`;
    return text;
  }

  function countdownLabel(resetAt, usedPercent) {
    if (usedPercent === 0 && !resetAt) return "现在";
    if (!resetAt) return "—";
    const date = new Date(resetAt);
    if (Number.isNaN(date.getTime())) return "—";
    const delta = date.getTime() - Date.now();
    if (delta <= 0) return "待刷新";
    const hours = Math.floor(delta / 3600000);
    const minutes = Math.floor((delta % 3600000) / 60000);
    if (hours >= 48) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
    if (hours >= 1) return `${hours}h ${minutes}m`;
    return `${Math.max(1, minutes)}m`;
  }

  function meterTone(percent) {
    if (percent == null) return "";
    if (percent >= 90) return "danger";
    if (percent >= 75) return "warning";
    return "";
  }

  function timeNode(value) {
    const span = document.createElement("span");
    span.className = "cell-sub";
    span.textContent = relativeTime(value);
    if (value) span.title = String(value);
    return span;
  }

  function statusNode(code, text) {
    const span = document.createElement("span");
    span.className = "status";
    const tone = toneFor(code);
    if (tone) span.dataset.tone = tone;
    span.textContent = text || labelOf(statusLabels, code);
    return span;
  }

  function cell(row, content, className) {
    const td = document.createElement("td");
    if (className) td.className = className;
    if (content instanceof Node) td.append(content);
    else td.textContent = content == null || content === "" ? "—" : String(content);
    row.append(td);
    return td;
  }

  function twoLine(primary, secondary) {
    const wrap = document.createElement("div");
    wrap.className = "cell-main";
    const strong = document.createElement("strong");
    strong.textContent = primary || "—";
    wrap.append(strong);
    if (secondary) {
      const sub = document.createElement("span");
      sub.className = "cell-sub";
      sub.textContent = secondary;
      wrap.append(sub);
    }
    return wrap;
  }

  function emptyState(title, detail, compact) {
    const wrap = document.createElement("div");
    wrap.className = compact ? "empty-state compact" : "empty-state";
    const strong = document.createElement("strong");
    strong.textContent = title;
    wrap.append(strong);
    if (detail) {
      const p = document.createElement("p");
      p.textContent = detail;
      wrap.append(p);
    }
    return wrap;
  }

  function statusBar(text) {
    const wrap = document.createElement("div");
    wrap.className = "status-bar";
    wrap.textContent = text;
    return wrap;
  }

  function showPageError(message) {
    const box = document.getElementById("page-feedback");
    const text = document.getElementById("page-feedback-text");
    if (!box || !text) return;
    text.textContent = message;
    box.hidden = false;
  }

  function hidePageError() {
    const box = document.getElementById("page-feedback");
    if (box) box.hidden = true;
  }

  function toast(message, tone = "muted", action) {
    const region = document.getElementById("toast-region");
    if (!region) return;
    const item = document.createElement("div");
    item.className = `toast toast-${tone || "muted"}`;
    const textNode = document.createElement("span");
    textNode.textContent = message;
    item.append(textNode);
    if (action && action.label && action.onClick) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button ghost";
      button.textContent = action.label;
      button.addEventListener("click", () => {
        item.remove();
        action.onClick();
      });
      item.append(button);
    }
    region.append(item);
    window.setTimeout(() => item.remove(), 6500);
  }

  function operationTone(result) {
    if (result?.tone) return result.tone;
    const outcome = result?.outcome || "";
    if (result?.partial || result?.status === "partial" || result?.status === "manual_required") return "warning";
    if (["remote_missing", "not_eligible", "schema_mismatch", "verification_failed", "conflict"].includes(outcome)) return "warning";
    if (result?.ok || result?.success) return outcome === "remote_missing" ? "warning" : "success";
    return "error";
  }

  function syncToastMessage(result) {
    if (result?.message) return result.message;
    if (result?.ok || result?.success) {
      return `同步完成：官方已加入 ${result.joined ?? 0}、待邀请 ${result.invited ?? 0}；本地受管 ${result.managed ?? 0}；仅官方 ${result.remote_only ?? 0}`;
    }
    return result?.error || "同步失败";
  }

  function chooseWorkspaceId(item) {
    const memberships = item.memberships || [];
    if (memberships.length > 1) {
      const options = memberships.map((row) => String(row.workspace_id) + ":" + (row.workspace || row.workspace_id)).join("\n");
      const picked = window.prompt("该账号属于多个 Workspace，请输入要操作的 Workspace ID:\n" + options, String(item.primary_workspace_id || item.workspace_id || ""));
      const chosen = Number(picked || 0);
      return chosen || null;
    }
    return item.primary_workspace_id || item.workspace_id || (memberships[0] && memberships[0].workspace_id) || null;
  }

  async function handleActionResult(result, { successMessage, refresh = true, longRunning = false } = {}) {
    const failed = !(result?.ok || result?.success) || result?.partial || ["partial", "failed", "manual_required"].includes(result?.status);
    const message = result?.message || (failed ? (result?.error || "操作失败") : (successMessage || "已完成"));
    const action = failed && result?.operation_id
      ? { label: "查看详情", onClick: () => openOperationById(result.operation_id) }
      : (longRunning && result?.operation_id ? { label: "查看任务", onClick: () => openOperationById(result.operation_id) } : null);
    toast(message, operationTone(result), action || undefined);
    if (refresh) await bootPage();
    return result;
  }

  function confirmDanger(message) {
    return window.confirm(message);
  }

  function focusableNodes(root) {
    if (!root) return [];
    return Array.from(
      root.querySelectorAll('a[href], button:not([disabled]), textarea, input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')
    ).filter((node) => !node.hasAttribute("disabled") && node.getAttribute("aria-hidden") !== "true");
  }

  function activateFocusTrap(root) {
    focusTrapRoot = root;
    const nodes = focusableNodes(root);
    (nodes[0] || root)?.focus?.();
  }

  function clearFocusTrap() {
    focusTrapRoot = null;
  }

  function handleFocusTrap(event) {
    if (!focusTrapRoot || event.key !== "Tab") return;
    const nodes = focusableNodes(focusTrapRoot);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function patchAction(key, url, body) {
    return fetchEntity(key, url, {
      method: "PATCH",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  async function postAction(key, url, body) {
    const options = {
      method: "POST",
      headers: { Accept: "application/json" },
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetchEntity(key, url, options);
  }

  async function openOperationById(operationId) {
    if (!operationId) return;
    try {
      const detail = await fetchEntity(`operation-${operationId}`, `/api/operations/${encodeURIComponent(operationId)}`);
      openSheet("operation", detail);
    } catch (error) {
      toast(friendlyError(error), "error");
    }
  }

  function renderRows(bodyId, items, columns, renderItem, empty) {
    const body = document.getElementById(bodyId);
    if (!body) return;
    body.replaceChildren();
    if (!items.length) {
      const row = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = columns;
      td.append(empty);
      row.append(td);
      body.append(row);
      return;
    }
    items.forEach((item) => body.append(renderItem(item)));
  }

  function menuButton(kind, item) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button ghost";
    button.textContent = "…";
    button.dataset.menuTrigger = "1";
    button.setAttribute("aria-label", "更多操作");
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", "action-menu");
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openMenu(button, kind, item);
    });
    return button;
  }

  function bindRow(row, kind, item) {
    row.classList.add("is-interactive");
    row.tabIndex = 0;
    row.dataset.entityId = String(item.id);
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, a, input, select")) return;
      openSheet(kind, item, row);
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") openSheet(kind, item, row);
    });
  }

  function shortId(value) {
    const text = String(value || "");
    if (text.length <= 12) return text;
    return `${text.slice(0, 8)}…`;
  }


  function membershipStatusLabel(status) {
    const map = {
      owner: "母号",
      managed: "已接入",
      remote_only: "官方已加入 · 未接入",
      local_only: "本地有记录 · 官方未找到",
      invited: "已邀请 · 等待加入",
      conflict: "身份冲突 · 需人工核对",
    };
    return map[status] || status || "—";
  }

  function workspaceOfficialSummary(item) {
    const official = item.official || {};
    if ((official.sync_state || item.last_sync == null) === "never" || (item.last_sync == null && official.joined_people_total == null && item.members == null)) {
      return { main: "尚未同步", sub: "本地 " + String(item.managed?.count ?? item.managed_count ?? 0) };
    }
    if (official.sync_state === "failed" || item.health === "sync_failed") {
      return { main: "同步失败", sub: "数据可能过期" };
    }
    const people = official.joined_people_total ?? item.members;
    const children = official.joined_member_count;
    let main = people == null ? "—" : `${people} 人`;
    if (children != null) main += ` · ${children} 子号`;
    if (official.occupied_seats != null && official.seat_limit != null) {
      main += ` · 席位 ${official.occupied_seats}/${official.seat_limit}`;
    }
    const managed = item.managed?.count ?? item.managed_count ?? 0;
    const pending = item.reconciliation?.actionable_count ?? item.reconciliation?.remote_only ?? 0;
    const sub = pending ? `本地 ${managed} · ${pending} 位待接入` : `本地 ${managed}`;
    return { main, sub };
  }

  function workspaceRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "workspace", item);
    const summary = workspaceOfficialSummary(item);
    cell(row, twoLine(item.display_name || item.name, shortId(item.official_workspace_id)));
    cell(row, item.owner_email);
    cell(row, twoLine(summary.main, summary.sub), "num");
    cell(row, statusNode(item.health || item.status, labelOf(statusLabels, item.health || item.status)));
    cell(row, timeNode(item.last_sync));
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const sync = document.createElement("button");
    sync.type = "button";
    sync.className = "button ghost";
    sync.textContent = "同步";
    sync.dataset.action = "workspace.sync";
    sync.addEventListener("click", async (event) => {
      event.stopPropagation();
      sync.disabled = true;
      try {
        const result = await postAction(`workspace-sync-${item.id}`, `/api/workspaces/${item.id}/sync`);
        await handleActionResult(result, { successMessage: syncToastMessage(result) });
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        sync.disabled = false;
      }
    });
    const manage = document.createElement("button");
    manage.type = "button";
    manage.className = "button ghost";
    manage.textContent = "管理";
    manage.dataset.action = "workspace.manage-children";
    manage.addEventListener("click", (event) => {
      event.stopPropagation();
      openManageChildren(manage, item);
    });
    actions.append(sync, manage, menuButton("workspace", item));
    cell(row, actions, "actions");
    return row;
  }

  function quotaMeter(label, percent, resetAt) {
    const row = document.createElement("div");
    const tone = meterTone(percent);
    row.className = tone ? `quota-meter is-${tone}` : "quota-meter";
    const tag = document.createElement("span");
    tag.className = "meter-window";
    tag.textContent = label;
    const bar = document.createElement("div");
    bar.className = "quota-bar";
    bar.setAttribute("role", "meter");
    bar.setAttribute("aria-label", `${label} 使用率`);
    if (percent != null) {
      bar.setAttribute("aria-valuenow", String(percent));
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      const fill = document.createElement("span");
      fill.style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
      bar.append(fill);
    }
    const pct = document.createElement("span");
    pct.className = "meter-pct tabular";
    pct.textContent = percent == null ? "—" : `${percent}%`;
    const eta = document.createElement("span");
    eta.className = "meter-ttl cell-sub tabular";
    eta.textContent = countdownLabel(resetAt, percent);
    row.append(tag, bar, pct, eta);
    return row;
  }

  function quotaCell(item) {
    const wrap = document.createElement("div");
    wrap.className = "quota-cell";
    const quota = item.quota || {};
    const usage = item.usage || {};
    const metrics = [];
    if (usage.requests != null) metrics.push(usage.requests + " req");
    if (usage.tokens != null) metrics.push(usage.tokens + " tok");
    if (usage.account_billed != null) metrics.push("A $" + usage.account_billed);
    if (usage.user_billed != null) metrics.push("U $" + usage.user_billed);
    if (metrics.length) {
      const line = document.createElement("div");
      line.className = "metric-chip-group tabular";
      metrics.forEach((m) => {
        const chip = document.createElement("span");
        chip.className = "metric-chip";
        chip.textContent = m;
        line.append(chip);
      });
      line.title = "A=账号计费口径；U=用户/API Key 计费口径。未采集时不显示。";
      wrap.append(line);
    }
    wrap.append(
      quotaMeter("5h", quota.five_hour_used_percent, quota.five_hour_reset_at),
      quotaMeter("7d", quota.seven_day_used_percent, quota.seven_day_reset_at),
    );
    const footer = document.createElement("div");
    footer.className = "quota-footer";
    const source = document.createElement("span");
    source.className = "cell-sub";
    const freshness = quota.queried_at ? relativeTime(quota.queried_at) : "无快照";
    source.textContent = (quota.source || "official") + " · " + freshness;
    if (quota.queried_at) source.title = quota.queried_at;
    footer.append(source);
    wrap.append(footer);
    return wrap;
  }

  function accountPrimaryAction(item) {
    const workspaceId = item.workspace_id || item.primary_workspace_id;
    const scoped = (path) => workspaceId ? `/api/workspaces/${workspaceId}/accounts/${item.id}${path}` : `/api/accounts/${item.id}${path}`;
    if (["oauth_required", "manual_required", "deactivated", "phone_required", "refresh_due"].includes(item.auth)) {
      return { id: "account.reauth", label: "重新授权", url: `/api/accounts/${item.id}/reauth` };
    }
    if (item.quota?.seven_day_used_percent == null || item.quota?.success === false) {
      return { id: "account.quota", label: "刷新额度", url: scoped("/quota/probe") };
    }
    const publish = item.sub2api_publish || {};
    if (publish.eligible !== false) {
      if (["missing", "unbound", "none", "pending"].includes(item.sub2api)) {
        return { id: "account.sub2api.push", label: "推送到 Sub2API", url: scoped("/sub2api/push"), body: {} };
      }
      if (item.sub2api === "verified") {
        return { id: "account.sub2api.push", label: "更新 Sub2API", url: scoped("/sub2api/push"), body: {} };
      }
      if (item.sub2api === "conflict") {
        return { id: "account.sub2api.reconcile", label: "处理冲突", url: `/api/accounts/${item.id}/sub2api/reconcile` };
      }
    }
    return { id: "account.refresh", label: "刷新状态", url: `/api/accounts/${item.id}/refresh` };
  }

  function accountRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "account", item);
    const plan = [item.official_role, item.official_plan].filter(Boolean).join(" · ");
    cell(row, twoLine(item.email, plan || null));
    cell(row, item.workspace);
    cell(row, labelOf(purposeLabels, item.purpose));
    cell(row, statusNode(item.auth, labelOf(statusLabels, item.auth)));
    cell(row, quotaCell(item));
    cell(row, statusNode(item.sub2api, labelOf(statusLabels, item.sub2api)));
    cell(row, statusNode(item.state, labelOf(stateLabels, item.state)));
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const primary = accountPrimaryAction(item);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button ghost";
    button.textContent = primary.label;
    button.dataset.action = primary.id;
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (primary.id === "account.reauth") {
        await openReauth(item, button);
        return;
      }
      button.disabled = true;
      try {
        const result = await postAction(`${primary.id}-${item.id}`, primary.url, primary.body);
        await handleActionResult(result, { successMessage: result.message || (primary.label + "已完成") });
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        button.disabled = false;
      }
    });
    actions.append(button, menuButton("account", item));
    cell(row, actions, "actions");
    return row;
  }

  function operationRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "operation", item);
    const checkWrap = document.createElement("div");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "operation-select";
    check.dataset.publicId = item.id;
    check.disabled = !["success", "partial", "failed", "cancelled", "manual_required"].includes(item.state || item.status) || Boolean(item.archived);
    check.addEventListener("click", (event) => event.stopPropagation());
    check.addEventListener("change", updateOperationsBulkButton);
    checkWrap.append(check);
    cell(row, checkWrap, "num");
    cell(row, statusNode(item.state || item.status, labelOf(statusLabels, item.state || item.status)));
    cell(row, item.operation_label || labelOf(statusLabels, item.operation));
    cell(row, twoLine(item.target_label || item.target || item.email || "—", item.outcome ? labelOf(statusLabels, item.outcome) : null));
    cell(row, item.workspace_name || item.workspace || "—");
    cell(row, item.business_step || (["success", "done"].includes(item.current_step) ? "已完成" : (item.current_step || "—")));
    cell(row, timeNode(item.updated || item.started));
    cell(row, menuButton("operation", item), "actions");
    return row;
  }

  function phoneRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "phone", item);
    cell(row, item.number);
    cell(row, statusNode(item.status, item.status));
    cell(row, `${item.used_count} / ${item.max_uses}`, "num");
    cell(row, item.risk_count ?? 0, "num");
    cell(row, item.reserved_by || "—");
    cell(row, timeNode(item.cooldown_until));
    cell(row, item.last_error_type || "—");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "button ghost";
    const enable = item.status !== "active";
    toggle.textContent = enable ? "启用" : "停用";
    toggle.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!confirmDanger(`${enable ? "启用" : "停用"}手机号 ${item.number}？`)) return;
      toggle.disabled = true;
      try {
        const result = await patchAction(`phone-status-${item.id}`, `/api/resources/phones/${item.id}`, {
          status: enable ? "active" : "disabled",
        });
        toast(result.ok ? "手机号状态已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
        await bootPage();
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        toggle.disabled = false;
      }
    });
    actions.append(toggle);
    const menuBtn = document.createElement("button");
    menuBtn.type = "button";
    menuBtn.className = "button ghost";
    menuBtn.textContent = "更多";
    menuBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      openMenu(menuBtn, "phone", item);
    });
    actions.append(menuBtn);
    cell(row, actions, "actions");
    return row;
  }

  
function hmeRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "hme", item);
    cell(row, item.email);
    cell(row, item.state);
    cell(row, item.label || "—");
    cell(row, item.pending ? "待同步" : "已同步");
    cell(row, item.job_id || "—");
    cell(row, timeNode(item.expires_at));
    const actions = document.createElement("div");
    actions.className = "row-actions";
    if (item.pending) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "button ghost";
      retry.textContent = "重试标签";
      retry.dataset.action = "hme.retry-label";
      retry.addEventListener("click", async (event) => {
        event.stopPropagation();
        retry.disabled = true;
        try {
          const result = await postAction(`hme-retry-${item.id}`, `/api/resources/hme/${item.id}/retry-label`);
          await handleActionResult(result, { successMessage: result.message || "标签已同步" });
        } catch (error) {
          toast(friendlyError(error), "error");
        } finally {
          retry.disabled = false;
        }
      });
      actions.append(retry);
    }
    actions.append(menuButton("hme", item));
    cell(row, actions, "actions");
    return row;
  }

  function proxyRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "proxy", item);
    cell(row, item.name);
    cell(row, item.region || "—");
    cell(row, item.last_exit_ip || "—");
    cell(row, statusNode(item.status, item.status === "active" ? "启用" : item.status));
    cell(row, statusNode(item.health_state || "unchecked", labelOf(statusLabels, item.health_state || "unchecked")));
    cell(row, `${item.scheme}://${item.host}:${item.port}`);
    cell(row, timeNode(item.last_checked_at));
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const probe = document.createElement("button");
    probe.type = "button";
    probe.className = "button ghost";
    probe.textContent = "检测";
    probe.dataset.action = "proxy.probe";
    probe.addEventListener("click", async (event) => {
      event.stopPropagation();
      probe.disabled = true;
      try {
        const result = await postAction(`proxy-probe-${item.id}`, `/api/resources/proxies/${item.id}/probe`);
        await handleActionResult(result, { successMessage: result.message || (result.last_exit_ip ? ("检测成功 " + result.last_exit_ip) : "检测完成") });
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        probe.disabled = false;
      }
    });
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "button ghost";
    edit.textContent = "编辑";
    edit.setAttribute("aria-label", "编辑代理");
    edit.addEventListener("click", (event) => {
      event.stopPropagation();
      openProxyProfileEdit(edit, item);
    });
    actions.append(edit, probe, menuButton("proxy", item));
    cell(row, actions, "actions");
    return row;
  }

  function matchesQuery(item, query, fields) {
    if (!query) return true;
    const hay = fields.map((key) => String(item[key] || "")).join(" ").toLowerCase();
    return hay.includes(query);
  }

  function currentQuery() {
    return new URLSearchParams(window.location.search);
  }

  function writeQuery(next) {
    const url = new URL(window.location.href);
    url.search = next.toString();
    window.history.replaceState({}, "", url);
  }

  function filterItems(kind, items) {
    const params = currentQuery();
    const q = (params.get("q") || "").trim().toLowerCase();
    if (kind === "workspace") {
      return items.filter((item) =>
        matchesQuery(item, q, ["name", "owner_email", "official_workspace_id", "id"])
      );
    }
    if (kind === "account") {
      return items.filter((item) =>
        matchesQuery(item, q, ["email", "workspace", "official_user_id", "official_account_id"])
      );
    }
    return items;
  }

  function setCount(id, shown, total) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = shown === total ? `共 ${total} 个` : `${shown} / ${total}`;
  }

  function renderOverview(payload) {
    const summary = payload.summary || {};
    const set = (key, value, alert) => {
      const node = document.querySelector(`[data-summary="${key}"]`);
      if (!node) return;
      node.textContent = value ?? 0;
      node.parentElement.classList.toggle("is-alert", Boolean(alert && value));
    };
    set("workspaces", summary.workspaces ?? payload.workspaces);
    set("accounts", summary.accounts ?? payload.accounts);
    set("attention", summary.attention ?? (payload.attention || []).length, true);
    set("running", summary.running_operations ?? (payload.running_operations || []).length);
    set("conflicts", summary.identity_conflicts, true);

    const attentionRoot = document.getElementById("overview-attention");
    const attentionPanel = document.getElementById("overview-attention-panel");
    attentionRoot.replaceChildren();
    const attention = payload.attention || [];
    if (!attention.length) {
      if (attentionPanel) {
        const header = attentionPanel.querySelector(".panel-header");
        if (header) header.hidden = true;
      }
      attentionRoot.append(statusBar("当前没有需要处理的事项"));
    } else {
      if (attentionPanel) {
        const header = attentionPanel.querySelector(".panel-header");
        if (header) header.hidden = false;
      }
      const list = document.createElement("div");
      list.className = "attention-list";
      attention.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const left = document.createElement("div");
        left.append(twoLine(item.email || item.message, item.message));
        if (item.workspace) {
          const extra = document.createElement("div");
          extra.className = "cell-sub";
          extra.textContent = item.workspace;
          left.append(extra);
        }
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button ghost";
        button.textContent = item.action || "查看";
        button.addEventListener("click", () => {
          if (item.href) {
            window.location.href = item.href;
            return;
          }
          if (item.operation_id) {
            window.location.href = `/operations?op=${encodeURIComponent(item.operation_id)}`;
            return;
          }
          if (item.account_id) {
            window.location.href = `/accounts?account=${encodeURIComponent(item.account_id)}`;
            return;
          }
          if (item.workspace_id) {
            window.location.href = `/workspaces?workspace=${encodeURIComponent(item.workspace_id)}`;
            return;
          }
          window.location.href = "/accounts?purpose=conflict";
        });
        row.append(left, button);
        list.append(row);
      });
      attentionRoot.append(list);
    }

    const runningRoot = document.getElementById("overview-running");
    const runningPanel = document.getElementById("overview-running-panel");
    const running = payload.running_operations || [];
    if (runningRoot) runningRoot.replaceChildren();
    if (runningPanel) runningPanel.hidden = running.length === 0;
    if (running.length && runningRoot) {
      const list = document.createElement("div");
      list.className = "running-list";
      running.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row is-interactive";
        row.tabIndex = 0;
        row.append(
          twoLine(
            `${labelOf(statusLabels, item.state || item.status)}  ${labelOf(statusLabels, item.operation)}  ${item.target || item.email || ""}`,
            `${item.current_step || "—"} · ${relativeTime(item.updated || item.started)}`
          )
        );
        row.addEventListener("click", () => openSheet("operation", item, row));
        list.append(row);
      });
      runningRoot.append(list);
    }

    const healthRoot = document.getElementById("overview-health");
    healthRoot.replaceChildren();
    const health = payload.workspace_health || [];
    if (!health.length) {
      healthRoot.append(emptyState("还没有工作区", "点右上角「登记团队」，用母号 OAuth 授权。", true));
    } else {
      const list = document.createElement("div");
      list.className = "health-list";
      health.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const seats = item.joined_people_total != null ? `${item.joined_people_total} 人 · ${item.joined_member_count ?? item.members ?? 0} 子成员` : (item.sync_state === "never" ? "尚未同步" : (item.members == null ? "尚未同步" : `${item.members} 子成员`));
        const sub = [item.owner_email, seats, item.last_sync ? relativeTime(item.last_sync) : null].filter(Boolean).join(" · ");
        row.append(twoLine(item.display_name || item.name, sub), statusNode(item.health, labelOf(statusLabels, item.health)));
        list.append(row);
      });
      healthRoot.append(list);
    }

    stopPolling();
    if (running.length && document.visibilityState === "visible") {
      pollTimer = window.setTimeout(() => bootPage(), 7000);
    }
  }

  function stopPolling() {
    if (pollTimer) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function kvSection(title, rows) {
    const section = document.createElement("section");
    section.className = "sheet-section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const dl = document.createElement("dl");
    dl.className = "kv";
    rows.forEach(([label, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value == null || value === "" ? "—" : String(value);
      dl.append(dt, dd);
    });
    section.append(heading, dl);
    return section;
  }

  function openSheet(kind, item, trigger) {
    if (!sheet) return;
    overlayReturn = trigger || document.activeElement;
    const title = document.getElementById("sheet-title");
    const subtitle = document.getElementById("sheet-subtitle");
    const body = document.getElementById("sheet-body");
    body.replaceChildren();
    if (kind === "account") {
      title.textContent = item.email;
      subtitle.textContent = [labelOf(purposeLabels, item.purpose), item.workspace].filter(Boolean).join(" · ");
      body.append(
        kvSection("运行摘要", [
          ["授权", labelOf(statusLabels, item.auth)],
          ["运行状态", labelOf(stateLabels, item.state)],
          ["官方计划", item.official_plan],
          ["官方角色", item.official_role],
          ["Membership", item.membership_state],
        ]),
        kvSection("官方额度", [
          ["5h", item.quota?.five_hour_used_percent == null ? "—" : `${item.quota.five_hour_used_percent}%`],
          ["7d", item.quota?.seven_day_used_percent == null ? "—" : `${item.quota.seven_day_used_percent}%`],
          ["查询时间", item.quota?.queried_at || "—"],
        ]),
        kvSection("Binding", [
          ["状态", labelOf(statusLabels, item.sub2api)],
          ["远端 ID", item.binding?.remote_id],
          ["核对邮箱", item.binding?.verified_email],
          ["最近错误", item.binding?.last_error],
        ]),
        kvSection("技术信息", [
          ["Official user ID", item.official_user_id],
          ["Official account ID", item.official_account_id],
          ["AT", item.has_access_token ? "已保存" : "未设置"],
          ["RT", item.has_refresh_token ? "已保存" : "未设置"],
          ["代理", item.proxy_url || labelOf(statusLabels, item.proxy)],
          ["身份审计", item.identity],
          ["原因", (item.reasons || []).join("；")],
        ])
      );
    } else if (kind === "workspace") {
      title.textContent = item.name;
      subtitle.textContent = item.owner_email || "";
      const officialMembers = item.official_members || item.official?.members || [];
      const managed = item.member_accounts || item.managed?.accounts || [];
      const diffs = item.reconciliation?.items || [];
      const officialLines = officialMembers.length
        ? officialMembers.map((member) => {
            const bits = [member.role, member.state, member.is_owner ? "Owner" : ""].filter(Boolean).join(" · ");
            return [member.name || member.email, bits || member.email];
          })
        : [["官方成员", item.last_sync ? "官方列表为空" : "尚未同步"]];
      const managedLines = managed.length
        ? managed.map((member) => [member.email, [labelOf(purposeLabels, member.purpose), labelOf(statusLabels, member.auth)].filter(Boolean).join(" · ")])
        : [["本地受管账号", "尚未接入"]];
      const actionable = (item.reconciliation?.actionable_items || diffs.filter((row) => ["remote_only", "local_only", "conflict"].includes(row.status)));
      const diffLines = actionable.length
        ? actionable.map((row) => [row.name || row.email, (row.status_label || membershipStatusLabel(row.status)) + (row.note ? ` · ${row.note}` : "")])
        : [];
      const official = item.official || {};
      const peopleText = official.sync_state === "never" || (item.last_sync == null && official.joined_people_total == null)
        ? "尚未同步"
        : (official.joined_people_total == null ? "—" : `已加入 ${official.joined_people_total} 人（1 母号 / ${official.joined_member_count ?? "—"} 子号）`);
      const seatText = official.occupied_seats != null && official.seat_limit != null
        ? `${official.occupied_seats} / ${official.seat_limit}`
        : "无官方席位元数据";
      const nameError = item.official_name_last_error;
      body.append(
        kvSection("概览", [
          ["Team 名称", item.display_name || item.name],
          ["健康", labelOf(statusLabels, item.health || item.status)],
          ["官方已加入", peopleText],
          ["席位", seatText],
          ["受管子号", item.managed?.count ?? item.managed_count ?? managed.length],
          ["最近同步", item.last_sync || "尚未同步"],
        ].concat(nameError ? [["名称警告", `Team 名称获取失败，已保留现有名称`]] : [])),
        kvSection("母号", [
          ["邮箱", item.owner_email],
          ["授权", labelOf(statusLabels, item.owner_auth)],
          ["代理", item.owner_proxy || (item.owner_proxy_set ? "已设" : "未绑定")],
        ]),
        kvSection(officialMembers.length ? `官方成员（${officialMembers.length}）` : "官方成员", officialLines),
        kvSection(managed.length ? `本地受管账号（${managed.length}）` : "本地受管账号", managedLines)
      );
      if (actionable.length) body.append(kvSection("需要处理", diffLines));
    } else if (kind === "operation") {
      title.textContent = labelOf(statusLabels, item.operation);
      subtitle.textContent = item.target || item.email || item.id;
      const result = item.result || {};
      const steps = item.steps || [];
      const logs = item.log || [];
      body.append(
        kvSection("任务", [
          ["状态", labelOf(statusLabels, item.state || item.status)],
          ["摘要", result.message || item.error || "—"],
          ["对象", item.target || item.email],
          ["开始", relativeTime(item.started) + (item.started ? ` · ${item.started}` : "")],
          ["更新", relativeTime(item.updated) + (item.updated ? ` · ${item.updated}` : "")],
        ])
      );
      if (result.joined != null || result.remote_only != null) {
        body.append(
          kvSection("同步结果", [
            ["官方已加入", result.joined],
            ["待邀请", result.invited],
            ["本地受管", result.managed],
            ["仅官方", result.remote_only],
            ["仅本地", result.local_only],
            ["原始条数", result.raw_item_count],
            ["解析条数", result.parsed_item_count],
          ])
        );
      }
      if (steps.length) {
        body.append(kvSection("步骤", steps.map((step) => [step.step_name, `${labelOf(statusLabels, step.state)}${step.error_message ? " · " + step.error_message : ""}`])));
      }
      if (logs.length) {
        body.append(kvSection("日志", logs.slice(-8).map((row) => [row.ts || "", row.message || row.stage || ""])));
      }
    } else if (kind === "phone") {
      title.textContent = item.number;
      body.append(
        kvSection("手机号", [
          ["状态", item.status],
          ["成功 / 上限", `${item.used_count} / ${item.max_uses}`],
          ["风险", item.risk_count],
          ["租约", item.reserved_by],
          ["最后结果", item.last_error_type],
        ])
      );
    } else if (kind === "hme") {
      title.textContent = item.email;
      body.append(
        kvSection("HME", [
          ["状态", item.state],
          ["标签", item.label],
          ["同步", item.pending ? "待同步" : "已同步"],
          ["任务", item.job_id],
          ["到期", item.expires_at],
          ["错误", item.last_error],
        ])
      );
    } else if (kind === "proxy") {
      title.textContent = item.name;
      const sections = [
        kvSection("详情", [
          ["地区", item.region],
          ["出口 IP", item.last_exit_ip],
          ["状态", item.status],
          ["健康", item.health_state],
          ["地址", `${item.scheme}://${item.host}:${item.port}`],
          ["最近检测", item.last_checked_at],
          ["绑定数", item.binding_count ?? (item.bindings || []).length],
        ]),
      ];
      const bindings = item.bindings || [];
      if (bindings.length) {
        sections.push(
          kvSection(
            "绑定账号",
            bindings.map((row) => [row.email, [row.purpose, row.auth, row.state].filter(Boolean).join(" · ")])
          )
        );
      } else if (item.binding_count === 0) {
        sections.push(kvSection("绑定账号", [["账号", "当前没有账号绑定到这份代理"]]));
      }
      body.append(...sections);
    }
    closeMenu();
    sheet.hidden = false;
    activateFocusTrap(sheet.querySelector('.sheet-panel') || sheet);
    sheet.querySelector("[data-close-sheet]")?.focus();
  }

  function closeSheet() {
    if (!sheet || sheet.hidden) return;
    sheet.hidden = true;
    clearFocusTrap();
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  const entityActions = {
    workspace: [
      {
        id: "workspace.rename",
        label: "编辑显示名称",
        run: async (item) => {
          const next = window.prompt("工作区显示名称（留空清除自定义名）", item.custom_name || item.display_name || item.name || "");
          if (next == null) return;
          const result = await patchAction(`workspace-name-${item.id}`, `/api/workspaces/${item.id}/name`, { custom_name: next });
          await handleActionResult(result, { successMessage: result.display_name ? `已更新为 ${result.display_name}` : "名称已更新" });
        },
      },

      { id: "workspace.view", label: "查看详情", run: (item, trigger) => openSheet("workspace", item, trigger) },
      {
        id: "workspace.sync",
        label: "同步官方成员",
        run: async (item) => {
          const result = await postAction(`workspace-sync-${item.id}`, `/api/workspaces/${item.id}/sync`);
          await handleActionResult(result, { successMessage: syncToastMessage(result) });
        },
      },
      {
        id: "workspace.open-owner",
        label: "打开母号账号",
        visible: (item) => Boolean(item.owner_email),
        run: (item) => {
          window.location.href = `/accounts?q=${encodeURIComponent(item.owner_email || "")}`;
        },
      },
      {
        id: "workspace.manage-children",
        label: "管理子号",
        run: (item, trigger) => openManageChildren(trigger, item),
      },
      {
        id: "workspace.onboard",
        label: "创建子号",
        run: (item, trigger) => openOnboard(trigger, item),
      },
      {
        id: "workspace.rotate",
        label: "受控轮转",
        run: (item, trigger) => openRotate(trigger, item),
      },

    ],
    account: [
      { id: "account.view", label: "查看详情", run: (item, trigger) => openSheet("account", item, trigger) },
      {
        id: "account.reauth",
        label: "重新授权",
        run: (item, trigger) => openReauth(item, trigger),
      },
      {
        id: "account.refresh",
        label: "刷新状态",
        run: async (item) => {
          const result = await postAction(`account-refresh-${item.id}`, `/api/accounts/${item.id}/refresh`);
          await handleActionResult(result, { successMessage: result.message || "刷新完成" });
        },
      },
      {
        id: "account.auth",
        label: "检查授权",
        run: async (item) => {
          const result = await postAction(`account-auth-${item.id}`, `/api/accounts/${item.id}/auth/probe`);
          await handleActionResult(result, { successMessage: result.message || "授权探测完成" });
        },
      },
      {
        id: "account.quota",
        label: "刷新额度",
        run: async (item) => {
          const result = await postAction(`account-quota-${item.id}`, `/api/accounts/${item.id}/quota/probe`);
          await handleActionResult(result, { successMessage: result.message || "额度刷新完成" });
        },
      },
      {
        id: "account.sub2api",
        label: "Sub2API",
        visible: () => false,
        run: async () => {},
      },
      {
        id: "account.sub2api.push",
        label: "推送到 Sub2API",
        visible: (item) => (item.sub2api_publish?.eligible !== false) && ["missing", "unbound", "none", "pending"].includes(item.sub2api),
        run: async (item) => {
          const result = await postAction(`account-sub2api-push-${item.id}`, `/api/accounts/${item.id}/sub2api/push`, {});
          await handleActionResult(result, { successMessage: result.message || "Sub2API 推送完成" });
        },
      },
      {
        id: "account.sub2api.update",
        label: "更新 Sub2API",
        visible: (item) => (item.sub2api_publish?.eligible !== false) && item.sub2api === "verified",
        run: async (item) => {
          const result = await postAction(`account-sub2api-push-${item.id}`, `/api/accounts/${item.id}/sub2api/push`, {});
          await handleActionResult(result, { successMessage: result.message || "Sub2API 更新完成" });
        },
      },
      {
        id: "account.sub2api.reconcile",
        label: "重新对账 Sub2API",
        visible: (item) => item.sub2api_publish?.eligible !== false,
        run: async (item) => {
          const result = await postAction(`account-sub2api-reconcile-${item.id}`, `/api/accounts/${item.id}/sub2api/reconcile`);
          await handleActionResult(result, { successMessage: result.message || "Sub2API 对账完成" });
        },
      },
      {
        id: "account.copy",
        label: "复制邮箱",
        visible: (item) => Boolean(item.email),
        run: async (item) => {
          const ok = await copyText(item.email);
          toast(ok ? "邮箱已复制" : "复制失败", ok ? "success" : "error");
        },
      },
      {
        id: "account.onboard",
        label: "创建子号",
        visible: (item) => Boolean(item.workspace_id) || item.purpose === "mother",
        run: (item, trigger) => { const workspaceId = chooseWorkspaceId(item); if (!workspaceId) { toast("请先选择 Workspace", "warning"); return; } openOnboard(trigger, { id: workspaceId, name: item.workspace }); },
      },
      {
        id: "account.rotate",
        label: "受控轮转",
        visible: (item) => item.purpose === "child" && Boolean(item.workspace_id),
        run: (item, trigger) => { const workspaceId = chooseWorkspaceId(item); if (!workspaceId) { toast("请先选择 Workspace", "warning"); return; } openRotate(trigger, { id: workspaceId, name: item.workspace }, item); },
      },
      {
        id: "account.kick",
        label: "踢出待命",
        visible: (item) => item.purpose === "child" && Boolean(item.workspace_id),
        run: async (item) => {
          const workspaceId = chooseWorkspaceId(item);
          if (!workspaceId) { toast("请先选择 Workspace", "warning"); return; }
          if (!confirmDanger(`确认把 ${item.email} 踢出并转入待命？这会改官方成员。`)) return;
          const result = await postAction(`account-kick-${item.id}`, `/api/workspaces/${workspaceId}/kick`, {
            email: item.email,
            reason: "console_kick",
          });
          await handleActionResult(result, { successMessage: result.message || "踢人完成", longRunning: true });
        },
      },
      {
        id: "account.revoke",
        label: "撤回邀请",
        visible: (item) => item.purpose === "child" && Boolean(item.workspace_id) && item.membership_state === "invited",
        run: async (item) => {
          const workspaceId = chooseWorkspaceId(item);
          if (!workspaceId) { toast("请先选择 Workspace", "warning"); return; }
          if (!confirmDanger(`确认撤回 ${item.email} 的邀请？`)) return;
          const result = await postAction(`account-revoke-${item.id}`, `/api/workspaces/${workspaceId}/revoke-invite`, {
            email: item.email,
          });
          await handleActionResult(result, { successMessage: result.message || "邀请已撤回" });
        },
      },
      {
        id: "account.proxy",
        label: "修改代理",
        visible: (item) => item.purpose === "mother",
        run: (item, trigger) => openProxyEdit(trigger, item),
      },

    ],
    operation: [
      { id: "operation.view", label: "查看详情", run: (item, trigger) => openSheet("operation", item, trigger) },
      {
        id: "operation.cancel",
        label: "请求取消",
        visible: (item) => Boolean(item.can_cancel),
        run: async (item) => {
          const result = await postAction(`operation-cancel-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/cancel`);
          toast(result.ok ? "已请求取消" : (result.error || "取消失败"), result.ok ? "success" : "error");
          await openOperationById(item.id);
          await bootPage();
        },
      },
      {
        id: "operation.retry",
        label: "安全重试",
        visible: (item) => Boolean(item.can_retry),
        run: async (item) => {
          const result = await postAction(`operation-retry-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/retry`);
          await handleActionResult(result, { successMessage: result.message || "重试完成" });
        },
      },
      {
        id: "operation.archive",
        label: "清除记录",
        visible: (item) => !item.archived && ["success", "partial", "failed", "cancelled", "manual_required"].includes(item.state || item.status),
        run: async (item) => {
          if (!confirmDanger(`确认清除任务 ${item.id}？记录会软归档，可在“已清除”恢复。`)) return;
          const result = await patchAction(`operation-archive-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/archive`, { reason: "manual_clear" });
          toast(result.ok ? "已清除记录" : (result.error || "清除失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
      {
        id: "operation.restore",
        label: "恢复记录",
        visible: (item) => Boolean(item.archived),
        run: async (item) => {
          const result = await patchAction(`operation-restore-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/restore`, {});
          toast(result.ok ? "已恢复记录" : (result.error || "恢复失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
    ],
    phone: [
      { id: "phone.view", label: "查看详情", run: (item, trigger) => openSheet("phone", item, trigger) },
      {
        id: "phone.copy",
        label: "复制号码",
        visible: (item) => Boolean(item.number),
        run: async (item) => {
          const ok = await copyText(item.number);
          toast(ok ? "号码已复制" : "复制失败", ok ? "success" : "error");
        },
      },
      {
        id: "phone.toggle",
        label: "启用/停用",
        run: async (item) => {
          const enable = item.status !== "active";
          if (!confirmDanger(`${enable ? "启用" : "停用"}手机号 ${item.number}？`)) return;
          const result = await patchAction(`phone-status-${item.id}`, `/api/resources/phones/${item.id}`, {
            status: enable ? "active" : "disabled",
          });
          toast(result.ok ? "手机号状态已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
      {
        id: "phone.reset-cooldown",
        label: "重置冷却",
        run: async (item) => {
          if (!confirmDanger(`确认重置 ${item.number} 的冷却时间？`)) return;
          const result = await postAction(`phone-reset-${item.id}`, `/api/resources/phones/${item.id}/reset-cooldown`);
          toast(result.ok ? "冷却已重置" : (result.error || "重置失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },

    ],
    hme: [
      { id: "hme.view", label: "查看详情", run: (item, trigger) => openSheet("hme", item, trigger) },
      {
        id: "hme.retry-label",
        label: "重试标签同步",
        visible: (item) => Boolean(item.pending),
        run: async (item) => {
          const result = await postAction(`hme-retry-${item.id}`, `/api/resources/hme/${item.id}/retry-label`);
          await handleActionResult(result, { successMessage: result.message || "标签已同步" });
        },
      },
      {
        id: "hme.release",
        label: "安全释放",
        visible: (item) => item.state === "reserved" && !item.pending,
        run: async (item) => {
          if (!window.confirm(`确认安全释放 ${item.email}？仅 reserved 且未进入注册的租约可释放。`)) return;
          const result = await postAction(`hme-release-${item.id}`, `/api/resources/hme/${item.id}/release`);
          toast(result.ok ? "已释放" : (result.error || "释放失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
    ],
    proxy: [
      { id: "proxy.view", label: "查看详情", run: (item, trigger) => openSheet("proxy", item, trigger) },
      {
        id: "proxy.edit",
        label: "编辑",
        run: (item, trigger) => openProxyProfileEdit(trigger, item),
      },
      {
        id: "proxy.probe",
        label: "检测",
        run: async (item) => {
          const result = await postAction(`proxy-probe-${item.id}`, `/api/resources/proxies/${item.id}/probe`);
          await handleActionResult(result, { successMessage: result.message || (result.last_exit_ip ? ("检测成功 " + result.last_exit_ip) : "检测完成") });
        },
      },
      {
        id: "proxy.copy",
        label: "复制脱敏地址",
        run: async (item) => {
          const ok = await copyText(item.url || `${item.scheme}://${item.host}:${item.port}`);
          toast(ok ? "已复制" : "复制失败", ok ? "success" : "error");
        },
      },
      {
        id: "proxy.bindings",
        label: "查看绑定账号",
        run: async (item, trigger) => {
          const payload = await fetchEntity(`proxy-bindings-${item.id}`, `/api/resources/proxies/${item.id}/bindings`);
          const bound = payload.items || [];
          openSheet("proxy", {
            ...item,
            bindings: bound,
            binding_count: payload.count ?? bound.length,
          }, trigger);
        },
      },
      {
        id: "proxy.disable",
        label: "停用代理",
        visible: (item) => item.status === "active",
        run: async (item) => {
          if (!confirmDanger(`确认停用代理 ${item.name || item.host}？不会删除档案。`)) return;
          const result = await patchAction(`proxy-status-${item.id}`, `/api/resources/proxies/${item.id}`, { status: "disabled" });
          toast(result.ok ? "代理已停用" : (result.error || "停用失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
      {
        id: "proxy.enable",
        label: "启用代理",
        visible: (item) => item.status !== "active",
        run: async (item) => {
          const result = await patchAction(`proxy-status-${item.id}`, `/api/resources/proxies/${item.id}`, { status: "active" });
          toast(result.ok ? "代理已启用" : (result.error || "启用失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },

    ],
  };

  function openMenu(button, kind, item) {
    if (!menu) return;
    overlayReturn = button;
    menu.replaceChildren();
    const add = (label, handler, className) => {
      const option = document.createElement("button");
      option.type = "button";
      option.setAttribute("role", "menuitem");
      option.textContent = label;
      if (className) option.className = className;
      option.addEventListener("click", async () => {
        closeMenu();
        try {
          await handler();
        } catch (error) {
          toast(friendlyError(error), "error");
        }
      });
      menu.append(option);
    };
    const actions = (entityActions[kind] || []).filter((action) => !action.visible || action.visible(item));
    actions.forEach((action) => add(action.label, () => action.run(item, button)));
    if (!menu.childElementCount) {
      button.hidden = true;
      return;
    }
    const rect = button.getBoundingClientRect();
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    const width = Math.max(menu.offsetWidth || 180, 180);
    const height = menu.offsetHeight || 0;
    let left = rect.left;
    let top = rect.bottom + 4;
    if (left + width > window.innerWidth - 8) left = Math.max(8, window.innerWidth - width - 8);
    if (top + height > window.innerHeight - 8) top = Math.max(8, rect.top - height - 4);
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
  }

  function closeMenu() {
    if (!menu) return;
    menu.hidden = true;
    document.querySelectorAll('[data-menu-trigger][aria-expanded="true"]').forEach((node) => node.setAttribute("aria-expanded", "false"));
  }

  function renderDrawer(items) {
    const body = document.getElementById("operations-drawer-body");
    const meta = document.getElementById("operations-drawer-meta");
    if (!body) return;
    const running = items.filter((item) => ["queued", "running", "waiting"].includes(item.state || item.status));
    const manual = items.filter((item) => (item.state || item.status) === "manual_required");
    meta.textContent = `${running.length} 个运行中 · ${manual.length} 个需人工`;
    body.replaceChildren();
    const addGroup = (title, rows) => {
      const heading = document.createElement("h3");
      heading.className = "drawer-group-title";
      heading.textContent = title;
      body.append(heading);
      if (!rows.length) {
        const p = document.createElement("p");
        p.className = "muted";
        p.textContent = "没有";
        body.append(p);
        return;
      }
      rows.slice(0, 6).forEach((item) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "button ghost drawer-item";
        row.textContent = `${labelOf(statusLabels, item.operation)} ${item.target || item.email || ""} · ${item.current_step || "—"}`;
        row.addEventListener("click", async () => {
          closeDrawer();
          if (item.id) {
            await openOperationById(item.id);
            return;
          }
          window.location.href = "/operations";
        });
        body.append(row);
      });
    };
    addGroup("进行中", running);
    addGroup("需人工", manual);
    const all = document.createElement("a");
    all.href = "/operations";
    all.className = "button drawer-footer-link";
    all.textContent = "查看全部任务";
    body.append(all);
  }

  async function openDrawer() {
    if (!drawer) return;
    overlayReturn = document.getElementById("open-operations");
    drawer.hidden = false;
    const body = document.getElementById("operations-drawer-body");
    body.textContent = "正在加载任务…";
    try {
      const payload = await fetchEntity("operation-drawer", "/api/operations");
      renderDrawer(payload.items || []);
    } catch (error) {
      body.replaceChildren(emptyState("任务加载失败", friendlyError(error)));
    }
    drawer.querySelector("[data-close-drawer]")?.focus();
  }

  function closeDrawer() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  function setRegisterStatus(text, tone) {
    const statusEl = document.getElementById("register-status");
    if (!statusEl) return;
    statusEl.hidden = !text;
    statusEl.className = tone || "muted";
    statusEl.textContent = text || "";
  }

  function resetRegisterForm() {
    const form = document.getElementById("register-form");
    const oauth = document.getElementById("register-oauth");
    const authorize = document.getElementById("register-authorize-url");
    const openLink = document.getElementById("register-open-link");
    const submit = document.getElementById("register-submit");
    if (form) form.reset();
    if (oauth) oauth.hidden = true;
    if (authorize) authorize.value = "";
    if (openLink) openLink.href = "#";
    if (submit) submit.textContent = "生成授权链接";
    setRegisterStatus("", "muted");
  }

    function syncWorkspaceField(form, workspace) {
    if (!form) return;
    const id = workspace?.id || "";
    form.workspace_id.value = id;
    if (form.workspace_id_display) form.workspace_id_display.value = id;
  }

  function manageChildrenWorkspace() {
    const form = document.getElementById("manage-children-form");
    const workspaceId = Number(form?.workspace_id?.value || 0);
    if (!workspaceId) return null;
    const fromWorkspaces = (pageCache.items || []).find((item) => item.id === workspaceId);
    if (fromWorkspaces) return fromWorkspaces;
    return (pageCache.portfolio?.groups || []).find((item) => item.id === workspaceId) || null;
  }

  function manageChildRows(workspace) {
    const rows = [];
    const seen = new Set();
    const push = (row) => {
      const email = String(row?.email || "").trim();
      if (!email || seen.has(email.toLowerCase())) return;
      seen.add(email.toLowerCase());
      rows.push(row);
    };
    (workspace?.current_children || []).forEach(push);
    (workspace?.managed?.accounts || workspace?.member_accounts || []).forEach((row) => {
      if (row?.purpose === "mother" || row?.is_owner) return;
      push({ ...row, kind: "child" });
    });
    (workspace?.reconciliation?.items || []).forEach((row) => {
      if (row?.is_owner || row?.status === "owner") return;
      if (row?.status === "remote_only") push({ ...row, kind: "unmanaged", id: row.local_account_id || row.candidate_account_id });
      else if (row?.status === "local_only") push({ ...row, kind: "local_only", id: row.local_account_id });
      else if (row?.status === "invited") push({ ...row, kind: "invited", id: row.local_account_id || row.candidate_account_id });
    });
    (workspace?.unmanaged || []).forEach((row) => push({ ...row, kind: "unmanaged" }));
    (workspace?.invited || []).forEach((row) => push({ ...row, kind: "invited" }));
    rows.sort((a, b) => String(a.email || "").localeCompare(String(b.email || "")));
    return rows;
  }

  function renderManageChildrenList(workspace) {
    const list = document.getElementById("manage-children-list");
    if (!list) return;
    list.replaceChildren();
    const rows = manageChildRows(workspace || {});
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "muted manage-children-empty";
      empty.textContent = "这个工作区还没有本地子号。";
      list.append(empty);
      return;
    }
    rows.forEach((row) => {
      const item = document.createElement("div");
      item.className = "manage-child-row";
      const kind = row.kind || (row.status === "remote_only" ? "unmanaged" : (row.status === "invited" ? "invited" : (row.status === "local_only" ? "local_only" : "child")));
      const statusText = kind === "unmanaged"
        ? "官方已加入 · 未接入"
        : (kind === "invited" ? "已邀请 · 等待加入" : (kind === "local_only" ? "仅本地" : membershipStatusLabel(row.status) || labelOf(statusLabels, row.auth) || "已接入"));
      item.append(twoLine(row.email || "—", statusText));
      const actions = document.createElement("div");
      actions.className = "row-actions";
      if (kind === "unmanaged") {
        const linkBtn = document.createElement("button");
        linkBtn.type = "button";
        linkBtn.className = "button ghost compact";
        linkBtn.textContent = "接入";
        linkBtn.addEventListener("click", () => manageChildLink(row, linkBtn));
        actions.append(linkBtn);
      } else if (row.id || row.local_account_id) {
        const accountId = row.id || row.local_account_id;
        if (["oauth_required", "manual_required", "deactivated", "phone_required", "refresh_due"].includes(row.auth) || row.needs_auth) {
          const authBtn = document.createElement("button");
          authBtn.type = "button";
          authBtn.className = "button ghost compact";
          authBtn.textContent = "授权";
          authBtn.addEventListener("click", async () => {
            await openReauth({ id: accountId, email: row.email, workspace_id: workspace?.id }, authBtn);
          });
          actions.append(authBtn);
        }
        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "button ghost compact";
        removeBtn.textContent = "删除";
        removeBtn.addEventListener("click", () => manageChildRemove(row, removeBtn));
        actions.append(removeBtn);
      }
      item.append(actions);
      list.append(item);
    });
  }

  async function reloadManageChildren() {
    const form = document.getElementById("manage-children-form");
    const workspaceId = Number(form?.workspace_id?.value || 0);
    const trigger = overlayReturn;
    const sheetOpen = manageChildrenSheet && !manageChildrenSheet.hidden;
    await bootPage();
    if (!sheetOpen || !workspaceId) return;
    const workspace = manageChildrenWorkspace();
    overlayReturn = trigger;
    manageChildrenSheet.hidden = false;
    if (workspace) {
      const subtitle = document.getElementById("manage-children-subtitle");
      if (subtitle) subtitle.textContent = workspace.display_name || workspace.name || workspace.owner_email || "";
      renderManageChildrenList(workspace);
    }
    activateFocusTrap(manageChildrenSheet.querySelector(".sheet-panel") || manageChildrenSheet);
  }

  function openManageChildren(trigger, workspace) {
    if (!manageChildrenSheet) return;
    overlayReturn = trigger || document.activeElement;
    const form = document.getElementById("manage-children-form");
    form?.reset();
    syncWorkspaceField(form, workspace);
    const title = document.getElementById("manage-children-title");
    const subtitle = document.getElementById("manage-children-subtitle");
    if (title) title.textContent = "管理子号";
    if (subtitle) subtitle.textContent = workspace?.display_name || workspace?.name || workspace?.owner_email || "";
    setFormStatus("manage-children-status", "", "muted");
    renderManageChildrenList(workspace);
    manageChildrenSheet.hidden = false;
    activateFocusTrap(manageChildrenSheet.querySelector(".sheet-panel") || manageChildrenSheet);
    form?.querySelector("[name='email']")?.focus();
  }

  function closeManageChildren() {
    if (!manageChildrenSheet || manageChildrenSheet.hidden) return;
    manageChildrenSheet.hidden = true;
    clearFocusTrap();
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  async function manageChildLink(row, button) {
    const workspace = manageChildrenWorkspace();
    const workspaceId = workspace?.id;
    const email = (row?.email || "").trim();
    if (!workspaceId || !email) {
      toast("缺少官方邮箱，无法接入", "error");
      return;
    }
    if (button) button.disabled = true;
    try {
      const body = { email };
      if (row.id || row.local_account_id || row.candidate_account_id) {
        body.account_id = row.id || row.local_account_id || row.candidate_account_id;
      }
      const result = await postAction(`workspace-link-${workspaceId}-${email}`, `/api/workspaces/${workspaceId}/members/link`, body);
      toast(result.message || "已接入", operationTone(result));
      await reloadManageChildren();
      if (result.needs_auth && result.account_id) {
        await openReauth({ id: result.account_id, email: result.email || email, workspace_id: workspaceId }, overlayReturn);
      }
    } catch (error) {
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function manageChildRemove(row, button) {
    const workspace = manageChildrenWorkspace();
    const workspaceId = workspace?.id;
    const email = (row?.email || "").trim();
    const accountId = row?.id || row?.local_account_id;
    if (!workspaceId || (!email && !accountId)) return;
    if (!confirmDanger(`确认从本地子号移除 ${email || accountId}？不会改官方成员。`)) return;
    if (button) button.disabled = true;
    try {
      const body = {};
      if (email) body.email = email;
      if (accountId) body.account_id = accountId;
      const result = await postAction(`workspace-remove-child-${workspaceId}-${email || accountId}`, `/api/workspaces/${workspaceId}/members/remove`, body);
      toast(result.message || "已从本地移除", operationTone(result));
      await reloadManageChildren();
    } catch (error) {
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function submitManageChildren(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#manage-children-submit");
    const workspaceId = Number(form.workspace_id.value || form.workspace_id_display?.value || 0);
    const email = (form.email.value || "").trim();
    if (!workspaceId || !email) {
      setFormStatus("manage-children-status", "Workspace 与邮箱都必填", "error");
      return;
    }
    if (button) button.disabled = true;
    setFormStatus("manage-children-status", "正在加入…", "muted");
    try {
      const result = await postAction(`workspace-add-child-${workspaceId}-${email}`, `/api/workspaces/${workspaceId}/members/add`, { email });
      form.email.value = "";
      setFormStatus("manage-children-status", result.message || "已加入本地子号", "muted");
      toast(result.message || "已加入本地子号", operationTone(result));
      await reloadManageChildren();
      if (result.needs_auth && result.account_id) {
        await openReauth({ id: result.account_id, email: result.email || email, workspace_id: workspaceId }, overlayReturn);
      }
    } catch (error) {
      setFormStatus("manage-children-status", friendlyError(error), "error");
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  function openOnboard(trigger, workspace) {
    if (!onboardSheet) return;
    overlayReturn = trigger || document.querySelector("[data-open-onboard]");
    const form = document.getElementById("onboard-form");
    form?.reset();
    syncWorkspaceField(form, workspace);
    setFormStatus("onboard-status", "", "muted");
    onboardSheet.hidden = false;
    activateFocusTrap(onboardSheet.querySelector(".sheet-panel") || onboardSheet);
  }

  function closeOnboard() {
    if (!onboardSheet || onboardSheet.hidden) return;
    onboardSheet.hidden = true;
    clearFocusTrap();
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  function openRotate(trigger, workspace, account) {
    if (!rotateSheet) return;
    overlayReturn = trigger || document.activeElement;
    const form = document.getElementById("rotate-form");
    form?.reset();
    syncWorkspaceField(form, workspace);
    if (form && account?.email) form.email.value = account.email;
    setFormStatus("rotate-status", "", "muted");
    rotateSheet.hidden = false;
    activateFocusTrap(rotateSheet.querySelector(".sheet-panel") || rotateSheet);
  }

  function closeRotate() {
    if (!rotateSheet || rotateSheet.hidden) return;
    rotateSheet.hidden = true;
    clearFocusTrap();
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  async function fillProxyProfileOptions(selectedId) {
    const form = document.getElementById("proxy-edit-form");
    const select = form?.querySelector("[name='proxy_profile_id']");
    if (!select) return;
    select.replaceChildren();
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "不改绑定，只填新 URL";
    select.append(blank);
    try {
      const payload = await fetchEntity("proxy-list-edit", "/api/resources/proxies");
      (payload.items || []).forEach((item) => {
        const option = document.createElement("option");
        option.value = String(item.id);
        option.textContent = `${item.name || item.host}:${item.port}`;
        if (selectedId && Number(selectedId) === Number(item.id)) option.selected = true;
        select.append(option);
      });
    } catch (error) {
      toast(friendlyError(error), "error");
    }
  }

  function setProxyEditMode(mode) {
    const form = document.getElementById("proxy-edit-form");
    if (!form) return;
    form.mode.value = mode;
    const accountBlock = form.querySelector("[data-proxy-edit-account]");
    const profileBlock = form.querySelector("[data-proxy-edit-profile]");
    if (accountBlock) accountBlock.hidden = mode !== "account";
    if (profileBlock) profileBlock.hidden = mode !== "profile";
    const title = document.getElementById("proxy-edit-title");
    const subtitle = document.getElementById("proxy-edit-subtitle");
    if (mode === "profile") {
      if (title) title.textContent = "编辑代理档案";
      if (subtitle) subtitle.textContent = "可改名称或启停。留空名称会恢复自动名。";
    } else {
      if (title) title.textContent = "修改母号代理";
      if (subtitle) subtitle.textContent = "仅母号可改。可填 URL，或绑定已有代理档案。";
    }
  }

  async function openProxyEdit(trigger, account) {
    if (!proxyEditSheet) return;
    overlayReturn = trigger || document.activeElement;
    const form = document.getElementById("proxy-edit-form");
    form?.reset();
    setProxyEditMode("account");
    if (form) {
      form.account_id.value = account.id || "";
      form.proxy_id.value = "";
      form.email.value = account.email || "";
      form.current_proxy.value = account.proxy_url || "";
      form.proxy.value = "";
      form.clear.checked = false;
    }
    await fillProxyProfileOptions(account.proxy_profile_id);
    setFormStatus("proxy-edit-status", "", "muted");
    proxyEditSheet.hidden = false;
    activateFocusTrap(proxyEditSheet.querySelector(".sheet-panel") || proxyEditSheet);
  }

  function openProxyProfileEdit(trigger, proxy) {
    if (!proxyEditSheet) return;
    overlayReturn = trigger || document.activeElement;
    const form = document.getElementById("proxy-edit-form");
    form?.reset();
    setProxyEditMode("profile");
    if (form) {
      form.proxy_id.value = proxy.id || "";
      form.account_id.value = "";
      form.name.value = proxy.name || "";
      form.status.value = proxy.status === "disabled" ? "disabled" : "active";
      form.restore_auto_name.checked = false;
    }
    setFormStatus("proxy-edit-status", "", "muted");
    proxyEditSheet.hidden = false;
    activateFocusTrap(proxyEditSheet.querySelector(".sheet-panel") || proxyEditSheet);
  }

  function closeProxyEdit() {
    if (!proxyEditSheet || proxyEditSheet.hidden) return;
    proxyEditSheet.hidden = true;
    clearFocusTrap();
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  async function submitOnboard(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#onboard-submit");
    const workspaceId = Number(form.workspace_id_display.value || form.workspace_id.value || 0);
    if (!workspaceId) {
      setFormStatus("onboard-status", "请填写 Workspace ID", "error");
      return;
    }
    if (!confirmDanger(`确认向 Workspace #${workspaceId} 创建子号？可能触发邀请与浏览器自动化。`)) return;
    if (button) button.disabled = true;
    setFormStatus("onboard-status", "正在提交…", "muted");
    try {
      const result = await postAction(`onboard-${workspaceId}`, `/api/workspaces/${workspaceId}/onboard`, {
        email_line: form.email_line.value || "",
        phone_line: form.phone_line.value || "",
        proxy: form.proxy.value || "",
        force: Boolean(form.force.checked),
        skip_invite: Boolean(form.skip_invite.checked),
      });
      closeOnboard();
      await handleActionResult(result, { successMessage: result.message || "创建子号完成", longRunning: true });
    } catch (error) {
      setFormStatus("onboard-status", friendlyError(error), "error");
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function submitRotate(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#rotate-submit");
    const workspaceId = Number(form.workspace_id_display.value || form.workspace_id.value || 0);
    const email = (form.email.value || "").trim();
    if (!workspaceId || !email) {
      setFormStatus("rotate-status", "Workspace 与邮箱都必填", "error");
      return;
    }
    if (!confirmDanger(`确认对 ${email} 执行受控轮转？会踢人/撤邀请并可能补位。`)) return;
    if (button) button.disabled = true;
    setFormStatus("rotate-status", "正在提交…", "muted");
    try {
      const result = await postAction(`rotate-${workspaceId}-${email}`, `/api/workspaces/${workspaceId}/rotate`, {
        email,
        email_line: form.email_line.value || "",
        phone_line: form.phone_line.value || "",
        proxy: form.proxy.value || "",
        force_refill: Boolean(form.force_refill.checked),
        reason: "console",
      });
      closeRotate();
      await handleActionResult(result, { successMessage: result.message || "轮转完成", longRunning: true });
    } catch (error) {
      setFormStatus("rotate-status", friendlyError(error), "error");
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function submitProxyEdit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#proxy-edit-submit");
    const mode = form.mode?.value || "account";
    if (button) button.disabled = true;
    setFormStatus("proxy-edit-status", "正在保存…", "muted");
    try {
      let result;
      if (mode === "profile") {
        const proxyId = Number(form.proxy_id.value || 0);
        if (!proxyId) return;
        result = await patchAction(`proxy-edit-${proxyId}`, `/api/resources/proxies/${proxyId}`, {
          name: form.name.value,
          restore_auto_name: Boolean(form.restore_auto_name.checked),
          status: form.status.value,
        });
        setFormStatus("proxy-edit-status", result.ok ? "已保存" : (result.error || "失败"), result.ok ? "muted" : "error");
        toast(result.ok ? "代理已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
      } else {
        const accountId = Number(form.account_id.value || 0);
        if (!accountId) return;
        const url = (form.proxy.value || "").trim();
        const body = {
          clear: Boolean(form.clear.checked),
          proxy: url || null,
          proxy_profile_id: (!url && form.proxy_profile_id.value) ? Number(form.proxy_profile_id.value) : null,
        };
        result = await patchAction(`account-proxy-${accountId}`, `/api/accounts/${accountId}/proxy`, body);
        setFormStatus("proxy-edit-status", result.ok ? "已保存" : (result.error || "失败"), result.ok ? "muted" : "error");
        toast(result.ok ? "母号代理已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
      }
      if (result?.ok) {
        closeProxyEdit();
        await bootPage();
      }
    } catch (error) {
      setFormStatus("proxy-edit-status", friendlyError(error), "error");
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

function openRegister(trigger) {
    if (!registerSheet) return;
    overlayReturn = trigger || document.querySelector("[data-open-register]");
    resetRegisterForm();
    registerSheet.hidden = false;
    document.getElementById("register-form")?.querySelector("[name='email']")?.focus();
  }

  function closeRegister() {
    if (!registerSheet || registerSheet.hidden) return;
    registerSheet.hidden = true;
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  function setReauthStatus(text, tone) {
    const statusEl = document.getElementById("reauth-status");
    if (!statusEl) return;
    statusEl.hidden = !text;
    statusEl.className = tone || "muted";
    statusEl.textContent = text || "";
  }

  function closeReauth() {
    if (!reauthSheet || reauthSheet.hidden) return;
    reauthSheet.hidden = true;
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  async function openReauth(item, trigger) {
    if (!reauthSheet || !item?.id) return;
    overlayReturn = trigger || null;
    const form = document.getElementById("reauth-form");
    const authorize = document.getElementById("reauth-authorize-url");
    const openLink = document.getElementById("reauth-open-link");
    form?.reset();
    if (form) {
      form.account_id.value = item.id;
      form.email.value = item.email || "";
      form.ticket.value = "";
    }
    if (authorize) authorize.value = "";
    if (openLink) openLink.href = "#";
    setReauthStatus("正在生成授权链接…", "muted");
    reauthSheet.hidden = false;
    activateFocusTrap(reauthSheet.querySelector(".sheet-panel") || reauthSheet);
    try {
      const started = await fetchEntity(`account-reauth-${item.id}`, `/api/accounts/${item.id}/reauth`, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      if (form) form.ticket.value = started.ticket || "";
      if (authorize) authorize.value = started.authorize_url || "";
      if (openLink) openLink.href = started.authorize_url || "#";
      setReauthStatus(started.message || "打开授权链接，登录后把回调地址贴回来。", "muted");
      form?.querySelector("[name='callback_url']")?.focus();
    } catch (error) {
      setReauthStatus(friendlyError(error), "error");
      toast(friendlyError(error), "error");
    }
  }

  async function submitReauth(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#reauth-submit");
    const accountId = String(form.account_id.value || "").trim();
    const ticket = String(form.ticket.value || "").trim();
    const callbackUrl = String(form.callback_url.value || "").trim();
    if (!accountId || !ticket) {
      setReauthStatus("还没有授权会话，请关掉后重新点重新授权。", "error");
      return;
    }
    if (!callbackUrl) {
      setReauthStatus("请把跳转到 localhost:1455 的整段回调地址贴回来。", "error");
      return;
    }
    if (button) button.disabled = true;
    setReauthStatus("正在用回调换票…", "muted");
    try {
      const result = await fetchEntity(`account-reauth-complete-${accountId}`, `/api/accounts/${accountId}/reauth/complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ ticket, callback_url: callbackUrl }),
      });
      closeReauth();
      await handleActionResult(result, { successMessage: result.message || "授权已更新" });
    } catch (error) {
      setReauthStatus(friendlyError(error), "error");
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  function setFormStatus(id, text, tone) {
    const statusEl = document.getElementById(id);
    if (!statusEl) return;
    statusEl.hidden = !text;
    statusEl.className = tone || "muted";
    statusEl.textContent = text || "";
  }

  function openPhoneImport(trigger) {
    if (!phoneImportSheet) return;
    overlayReturn = trigger || document.querySelector("[data-open-phone-import]");
    const form = document.getElementById("phone-import-form");
    if (form) form.reset();
    setFormStatus("phone-import-status", "", "muted");
    phoneImportSheet.hidden = false;
    form?.querySelector("[name='text']")?.focus();
  }

  function closePhoneImport() {
    if (!phoneImportSheet || phoneImportSheet.hidden) return;
    phoneImportSheet.hidden = true;
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  function openProxyAdd(trigger) {
    if (!proxyAddSheet) return;
    overlayReturn = trigger || document.querySelector("[data-open-proxy-add]");
    const form = document.getElementById("proxy-add-form");
    if (form) form.reset();
    setFormStatus("proxy-add-status", "", "muted");
    proxyAddSheet.hidden = false;
    form?.querySelector("[name='url']")?.focus();
  }

  function closeProxyAdd() {
    if (!proxyAddSheet || proxyAddSheet.hidden) return;
    proxyAddSheet.hidden = true;
    overlayReturn?.focus?.();
    overlayReturn = null;
  }

  async function submitPhoneImport(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#phone-import-submit");
    if (button) button.disabled = true;
    setFormStatus("phone-import-status", "正在导入…", "muted");
    try {
      const data = new FormData(form);
      const result = await fetchEntity("phone-import", "/api/resources/phones/import", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ text: String(data.get("text") || "") }),
      });
      const imported = result.imported ?? 0;
      const skipped = result.skipped ?? 0;
      const errors = result.errors ?? result.invalid ?? 0;
      closePhoneImport();
      await bootPage();
      toast(
        `已导入 ${imported} 个号码，跳过 ${skipped} 个重复项${errors ? `，${errors} 行格式错误` : ""}`,
        "success",
        { label: "查看号码", onClick: () => { window.location.href = "/resources/phones"; } }
      );
    } catch (error) {
      setFormStatus("phone-import-status", friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function submitProxyAdd(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#proxy-add-submit");
    if (button) button.disabled = true;
    setFormStatus("proxy-add-status", "正在添加…", "muted");
    try {
      const data = new FormData(form);
      await fetchEntity("proxy-add", "/api/resources/proxies", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          url: String(data.get("url") || "").trim(),
          name: String(data.get("name") || "").trim() || null,
        }),
      });
      closeProxyAdd();
      await bootPage();
      toast("代理已添加", "success");
    } catch (error) {
      setFormStatus("proxy-add-status", friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function copyText(value) {
    const text = String(value || "");
    if (!text) return false;
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  async function startRegisterOAuth(form) {
    const data = new FormData(form);
    const payload = {
      email: String(data.get("email") || "").trim(),
      proxy: String(data.get("proxy") || "").trim() || null,
    };
    const started = await fetchEntity("register-oauth-start", "/api/workspaces/oauth/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    });
    form.elements.ticket.value = started.ticket || "";
    const authorize = document.getElementById("register-authorize-url");
    const openLink = document.getElementById("register-open-link");
    const oauth = document.getElementById("register-oauth");
    const submit = document.getElementById("register-submit");
    if (authorize) authorize.value = started.authorize_url || "";
    if (openLink) openLink.href = started.authorize_url || "#";
    if (oauth) oauth.hidden = false;
    if (submit) submit.textContent = "完成授权";
    form.querySelector("[name='callback_url']")?.focus();
    setRegisterStatus("打开授权链接，登录母号后把回调地址贴回来。", "muted");
  }

  async function completeRegisterOAuth(form) {
    const data = new FormData(form);
    await fetchEntity("register-oauth-complete", "/api/workspaces/oauth/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        ticket: String(data.get("ticket") || "").trim(),
        callback_url: String(data.get("callback_url") || "").trim(),
      }),
    });
    closeRegister();
    await bootPage();
  }

  async function submitRegister(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#register-submit");
    if (button) button.disabled = true;
    try {
      if (form.elements.ticket.value) {
        setRegisterStatus("正在用回调换票并登记…", "muted");
        await completeRegisterOAuth(form);
      } else {
        setRegisterStatus("正在生成授权链接…", "muted");
        await startRegisterOAuth(form);
      }
    } catch (error) {
      setRegisterStatus(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  function secretPlaceholder(state) {
    return state === "stored" ? "已保存，留空则不修改" : "尚未设置";
  }

  function snapshotSettings(form) {
    const data = new FormData(form);
    return JSON.stringify({
      sub2api_base_url: data.get("sub2api_base_url"),
      sub2api_api_key: data.get("sub2api_api_key"),
      sub2api_admin_email: data.get("sub2api_admin_email"),
      sub2api_admin_password: data.get("sub2api_admin_password"),
      hme_base_url: data.get("hme_base_url"),
      hme_service_token: data.get("hme_service_token"),
      hme_account_id: data.get("hme_account_id"),
      cf_mail_base_url: data.get("cf_mail_base_url"),
      cf_mail_address: data.get("cf_mail_address"),
      cf_mail_admin_password: data.get("cf_mail_admin_password"),
      official_quota_probe: form.official_quota_probe.checked,
      sms_max_uses_per_phone: data.get("sms_max_uses_per_phone"),
      sms_cooldown_min: data.get("sms_cooldown_min"),
      sms_reserve_min: data.get("sms_reserve_min"),
    });
  }

  function updateDirty() {
    const form = document.getElementById("settings-form");
    const bar = document.getElementById("settings-savebar");
    const status = document.getElementById("settings-status");
    const discard = document.getElementById("settings-discard");
    if (!form || !bar) return;
    settingsDirty = snapshotSettings(form) !== settingsBaseline;
    bar.classList.toggle("is-dirty", settingsDirty);
    if (discard) discard.hidden = !settingsDirty;
    if (status && !status.dataset.locked) {
      status.className = "muted";
      status.textContent = settingsDirty ? "有未保存的修改" : "没有未保存的修改";
    }
  }

  function fillSettings(payload) {
    const form = document.getElementById("settings-form");
    if (!form) return;
    const connections = payload.connections || {};
    const automation = payload.automation || {};
    const resources = payload.resources || {};
    const secretState = payload.secret_state || {};
    const account = payload.account || {};
    form.sub2api_base_url.value = connections.sub2api_base_url || "";
    form.sub2api_admin_email.value = connections.sub2api_admin_email || "";
    form.hme_base_url.value = connections.hme_base_url || "";
    form.hme_account_id.value = connections.hme_account_id || "";
    form.cf_mail_base_url.value = connections.cf_mail_base_url || "";
    form.cf_mail_address.value = connections.cf_mail_address || "";
    form.sub2api_api_key.value = "";
    form.sub2api_admin_password.value = "";
    form.hme_service_token.value = "";
    form.cf_mail_admin_password.value = "";
    form.official_quota_probe.checked = Boolean(automation.official_quota_probe);
    form.sms_max_uses_per_phone.value = resources.sms_max_uses_per_phone ?? "";
    form.sms_cooldown_min.value = resources.sms_cooldown_sec ? Math.round(resources.sms_cooldown_sec / 60) : "";
    form.sms_reserve_min.value = resources.sms_reserve_sec ? Math.round(resources.sms_reserve_sec / 60) : "";
    form.querySelectorAll("[data-secret]").forEach((input) => {
      const key = input.dataset.secret;
      input.placeholder = secretPlaceholder(secretState[key]);
    });
    const authHint = document.getElementById("sub2api-auth-hint");
    if (authHint) {
      const mode = connections.sub2api?.auth_mode;
      authHint.textContent =
        mode === "api_key" ? "认证方式：API Key" : mode === "admin" ? "认证方式：管理员账号" : "尚未配置认证";
    }
    const configuredText = (flag) => (flag ? "已配置" : "未设置");
    const setMeta = (service, flag) => {
      const meta = document.querySelector(`[data-service-meta="${service}"]`);
      if (meta && !meta.dataset.probed) meta.textContent = `${configuredText(flag)} · 未检测`;
    };
    setMeta("sub2api", connections.sub2api?.configured);
    setMeta("hme", connections.hme?.configured);
    setMeta("mail", connections.mail?.configured);
    const accountEl = document.getElementById("settings-account");
    if (accountEl) accountEl.textContent = account.username ? `当前登录账号：${account.username}` : "登录账号会显示在这里。";
    settingsBaseline = snapshotSettings(form);
    const status = document.getElementById("settings-status");
    if (status) delete status.dataset.locked;
    updateDirty();
  }

  function numberOrNull(value) {
    const text = String(value ?? "").trim();
    if (!text) return null;
    const number = Number(text);
    return Number.isFinite(number) ? number : null;
  }

  function secretOrNull(value) {
    const text = String(value ?? "").trim();
    if (!text || text === SECRET_MASK) return null;
    return text;
  }

  function minutesToSeconds(value) {
    const minutes = numberOrNull(value);
    return minutes == null ? null : minutes * 60;
  }

  function connectionsPayload(form) {
    return {
      sub2api_base_url: form.sub2api_base_url.value,
      sub2api_admin_email: form.sub2api_admin_email.value,
      hme_base_url: form.hme_base_url.value,
      hme_account_id: form.hme_account_id.value,
      cf_mail_base_url: form.cf_mail_base_url.value,
      cf_mail_address: form.cf_mail_address.value,
      sub2api_api_key: secretOrNull(form.sub2api_api_key.value),
      sub2api_admin_password: secretOrNull(form.sub2api_admin_password.value),
      hme_service_token: secretOrNull(form.hme_service_token.value),
      cf_mail_admin_password: secretOrNull(form.cf_mail_admin_password.value),
    };
  }

  function setServiceState(service, text, summary) {
    const meta = document.querySelector(`[data-service-meta="${service}"]`);
    const sum = document.querySelector(`[data-service-summary="${service}"]`);
    if (meta) {
      meta.textContent = text;
      meta.dataset.probed = "1";
    }
    if (sum && summary) sum.textContent = summary;
  }

  function probeCopy(payload) {
    const sub = payload.sub2api || {};
    const hme = payload.hme || {};
    const mail = payload.mail || {};
    if (sub.ok) {
      setServiceState("sub2api", `连接正常 · ${relativeTime(sub.checked_at)}`, `${sub.group_count || 0} 个分组 · ${sub.account_count || 0} 个账号`);
    } else {
      setServiceState("sub2api", sub.error || "检测失败", sub.error || "连不上 Sub2API");
    }
    if (hme.ok) {
      setServiceState(
        "hme",
        `连接正常 · ${relativeTime(hme.checked_at)}`,
        `${hme.account_name || hme.account_id || "当前账号"} · ${hme.alias_count || 0} 个别名 · ${hme.unused_count || 0} 个可用`
      );
    } else {
      setServiceState("hme", hme.error || "检测失败", hme.error || "连不上 HME");
    }
    if (mail.ok) {
      setServiceState("mail", `连接正常 · ${relativeTime(mail.checked_at)}`, mail.address || "连接成功");
    } else {
      setServiceState("mail", mail.error || "检测失败", mail.error || "连不上临时邮箱");
    }
  }

  async function probeSettings(focus) {
    const form = document.getElementById("settings-form");
    if (!form) return;
    const buttons = [...document.querySelectorAll("[data-probe], #settings-probe-all")];
    buttons.forEach((button) => {
      button.disabled = true;
    });
    const target = focus || "all";
    if (target === "all") {
      ["sub2api", "hme", "mail"].forEach((service) => setServiceState(service, "检测中…"));
    } else {
      setServiceState(target, "检测中…");
    }
    try {
      const payload = await fetchEntity("settings-probe", "/api/settings/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ connections: connectionsPayload(form), target: focus || "all" }),
      });
      probeCopy(payload);
    } catch (error) {
      const message = friendlyError(error);
      (target === "all" ? ["sub2api", "hme", "mail"] : [target]).forEach((service) => setServiceState(service, message));
    } finally {
      buttons.forEach((button) => {
        button.disabled = false;
      });
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const statusEl = document.getElementById("settings-status");
    const saveButton = document.getElementById("settings-save");
    const payload = {
      connections: connectionsPayload(form),
      automation: { official_quota_probe: form.official_quota_probe.checked },
      resources: {
        sms_max_uses_per_phone: numberOrNull(form.sms_max_uses_per_phone.value),
        sms_cooldown_sec: minutesToSeconds(form.sms_cooldown_min.value),
        sms_reserve_sec: minutesToSeconds(form.sms_reserve_min.value),
      },
    };
    if (saveButton) saveButton.disabled = true;
    if (statusEl) {
      statusEl.dataset.locked = "1";
      statusEl.className = "muted";
      statusEl.textContent = "正在保存…";
    }
    try {
      const saved = await fetchEntity("settings-save", "/api/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
      fillSettings(saved);
      if (statusEl) {
        statusEl.className = "muted";
        statusEl.textContent = "已保存 · 刚刚";
      }
      toast("设置已保存", "success");
    } catch (error) {
      if (statusEl) {
        statusEl.className = "error";
        statusEl.textContent = friendlyError(error);
      }
    } finally {
      if (saveButton) saveButton.disabled = false;
      if (statusEl) window.setTimeout(() => delete statusEl.dataset.locked, 1200);
    }
  }

  async function savePassword(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const statusEl = document.getElementById("password-status");
    const button = form.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.className = "muted";
      statusEl.textContent = "正在修改密码…";
    }
    try {
      await fetchEntity("password-save", "/api/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          password: {
            old_password: form.old_password.value,
            new_password: form.new_password.value,
            confirm_password: form.confirm_password.value,
          },
        }),
      });
      form.reset();
      if (statusEl) {
        statusEl.className = "muted";
        statusEl.textContent = "密码已修改。";
      }
    } catch (error) {
      if (statusEl) {
        statusEl.className = "error";
        statusEl.textContent = friendlyError(error);
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  function bindSearch(input, kind) {
    if (!input || input.dataset.bound) return;
    input.dataset.bound = "1";
    const params = currentQuery();
    if (params.get("q")) input.value = params.get("q");
    input.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        const next = currentQuery();
        const value = input.value.trim();
        if (value) next.set("q", value);
        else next.delete("q");
        writeQuery(next);
        if (kind === "account" && (currentQuery().get("view") || "portfolio") !== "flat") renderPortfolio(pageCache.portfolio || { groups: [], unassigned: [] });
        else paintList(kind);
      }, 200);
    });
  }

  function kindBadge(kind) {
    const span = document.createElement("span");
    span.className = kind === "mother" ? "badge badge-primary" : (kind === "child" ? "badge badge-info" : (kind === "unmanaged" ? "badge badge-warning" : "badge badge-muted"));
    span.textContent = { mother: "母号", child: "子号", history: "历史", unmanaged: "未接入", invited: "待接受", unassigned: "未归属" }[kind] || kind;
    return span;
  }

  function portfolioAccountRow(account, kind) {
    const row = document.createElement("div");
    row.className = kind === "history" ? "portfolio-row is-history" : (kind === "mother" ? "portfolio-row is-mother" : "portfolio-row");
    
    const identity = document.createElement("div");
    identity.className = "portfolio-cell portfolio-identity";
    identity.append(kindBadge(kind));
    const label = twoLine(account.email || account.name || "—", account.official_role || account.note || null);
    identity.append(label);
    row.append(identity);

    const authCol = document.createElement("div");
    authCol.className = "portfolio-cell portfolio-auth";
    authCol.append(statusNode(account.auth || account.membership_state, labelOf(statusLabels, account.auth || account.membership_state)));
    row.append(authCol);

    const quotaCol = document.createElement("div");
    quotaCol.className = "portfolio-cell portfolio-quota";
    quotaCol.append(account.id && kind !== "unmanaged" && kind !== "invited" ? quotaCell(account) : document.createTextNode(kind === "unmanaged" ? "未接入，无法读取额度" : (kind === "invited" ? "待接受邀请" : "尚未获取")));
    row.append(quotaCol);

    const sub2Col = document.createElement("div");
    sub2Col.className = "portfolio-cell portfolio-sub2";
    sub2Col.append(statusNode(account.sub2api, labelOf(statusLabels, account.sub2api)));
    row.append(sub2Col);

    const actions = document.createElement("div");
    actions.className = "portfolio-cell portfolio-actions row-actions";
    if (kind === "unmanaged") {
      const linkBtn = document.createElement("button");
      linkBtn.type = "button";
      linkBtn.className = "button ghost compact";
      linkBtn.textContent = "接入";
      linkBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!account.workspace_id || !account.email) {
          toast("缺少官方邮箱，无法接入", "error");
          return;
        }
        linkBtn.disabled = true;
        try {
          const body = { email: account.email };
          if (account.id) body.account_id = account.id;
          const result = await postAction(`workspace-link-${account.workspace_id}-${account.email}`, `/api/workspaces/${account.workspace_id}/members/link`, body);
          await handleActionResult(result, { successMessage: result.message || "已接入" });
          if (result.needs_auth && result.account_id) {
            await openReauth({ id: result.account_id, email: result.email || account.email, workspace_id: account.workspace_id }, linkBtn);
          }
        } catch (error) {
          toast(friendlyError(error), "error");
        } finally {
          linkBtn.disabled = false;
        }
      });
      actions.append(linkBtn);
    } else if (account.id) {
      const primary = accountPrimaryAction(account);
      const actionBtn = document.createElement("button");
      actionBtn.type = "button";
      actionBtn.className = "button ghost compact";
      actionBtn.textContent = primary.label;
      actionBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (primary.id === "account.reauth") {
          await openReauth(account, actionBtn);
          return;
        }
        actionBtn.disabled = true;
        try {
          const result = await postAction(primary.id + "-" + account.id, primary.url, primary.body);
          await handleActionResult(result, { successMessage: result.message || (primary.label + "已完成") });
        } catch (error) {
          toast(friendlyError(error), "error");
        } finally {
          actionBtn.disabled = false;
        }
      });
      actions.append(actionBtn);
      const detailBtn = document.createElement("button");
      detailBtn.type = "button";
      detailBtn.className = "button ghost compact";
      detailBtn.textContent = "详情";
      detailBtn.addEventListener("click", () => openSheet("account", account, detailBtn));
      actions.append(detailBtn);
    }
    row.append(actions);
    return row;
  }

  function renderPortfolio(payload) {
    const root = document.getElementById("accounts-portfolio");
    const table = document.querySelector("#accounts-body")?.closest(".table-scroll");
    if (!root) return;
    pageCache.portfolio = payload || pageCache.portfolio || { groups: [], unassigned: [] };
    const data = pageCache.portfolio;
    root.hidden = false;
    if (table) table.hidden = true;
    root.replaceChildren();
    const q = (currentQuery().get("q") || "").trim().toLowerCase();
    const purpose = currentQuery().get("purpose") || "all";
    const groups = data.groups || [];
    let shown = 0;

    const matchesPurpose = (account) => {
      if (!purpose || purpose === "all") return true;
      if (!account) return false;
      if (purpose === "conflict") return account.state === "conflict";
      if (purpose === "archived") return account.state === "archived" || account.kind === "history";
      if (purpose === "needs_auth") return ["oauth_required", "manual_required", "deactivated", "phone_required", "refresh_due"].includes(account.auth);
      if (purpose === "quota_full") return (account.quota || {}).seven_day_used_percent === 100;
      return account.purpose === purpose;
    };
    const haystack = (group) => [
      group.display_name,
      group.name,
      group.owner_email,
      group.official_workspace_id,
      (group.mother || {}).email,
      ...(group.current_children || []).map((row) => row.email),
      ...(group.unmanaged || []).map((row) => row.email),
      ...(group.history || []).map((row) => row.email),
    ].join(" ").toLowerCase();

    groups.forEach((group) => {
      if (q && !haystack(group).includes(q)) return;
      const mother = group.mother && matchesPurpose(group.mother) ? group.mother : null;
      const currentChildren = (group.current_children || []).filter(matchesPurpose);
      const unmanaged = (group.unmanaged || []).filter((row) => purpose === "all" || purpose === "conflict" ? matchesPurpose(row) : false);
      const history = (group.history || []).filter(matchesPurpose);
      if (purpose && purpose !== "all" && !mother && !currentChildren.length && !unmanaged.length && !history.length) return;
      shown += 1;
      const box = document.createElement("section");
      box.className = "portfolio-group";
      const head = document.createElement("div");
      head.className = "portfolio-head";
      head.tabIndex = 0;
      head.setAttribute("role", "button");
      head.setAttribute("aria-expanded", "true");

      const toggle = document.createElement("span");
      toggle.className = "toggle-icon";
      toggle.textContent = "▾";
      toggle.setAttribute("aria-hidden", "true");

      const body = document.createElement("div");
      body.className = "portfolio-body";
      const collapse = () => {
        const closed = box.classList.toggle("is-collapsed");
        head.setAttribute("aria-expanded", closed ? "false" : "true");
      };
      head.addEventListener("click", (event) => {
        if (event.target.closest(".row-actions")) return;
        collapse();
      });
      head.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        collapse();
      });

      const counts = group.counts || {};
      const meta = document.createElement("div");
      meta.className = "portfolio-meta";
      meta.append(twoLine(group.display_name || group.name, shortId(group.official_workspace_id)));

      const ownerCol = document.createElement("div");
      ownerCol.className = "portfolio-owner";
      ownerCol.append(twoLine(group.owner_email || "无母号", `${counts.joined_people ?? ((counts.managed_children ?? counts.current_children ?? 0) + (counts.unmanaged ?? 0) + (group.mother ? 1 : 0))} 人 · ${counts.managed_children ?? counts.current_children ?? 0} 子号 · ${counts.unmanaged ?? 0} 未接入`));

      const healthCol = document.createElement("div");
      healthCol.className = "portfolio-health";
      healthCol.append(statusNode(group.health, labelOf(statusLabels, group.health)));

      const syncCol = document.createElement("div");
      syncCol.className = "portfolio-sync";
      syncCol.append(timeNode(group.last_sync));

      const actions = document.createElement("div");
      actions.className = "portfolio-actions row-actions";
      const sync = document.createElement("button");
      sync.type = "button";
      sync.className = "button ghost compact";
      sync.textContent = "同步";
      sync.addEventListener("click", async (event) => {
        event.stopPropagation();
        sync.disabled = true;
        try {
          const result = await postAction("workspace-sync-" + group.id, "/api/workspaces/" + group.id + "/sync");
          await handleActionResult(result, { successMessage: syncToastMessage(result) });
        } catch (err) {
          toast(friendlyError(err), "error");
        } finally {
          sync.disabled = false;
        }
      });
      const manage = document.createElement("button");
      manage.type = "button";
      manage.className = "button ghost compact";
      manage.textContent = "管理";
      manage.addEventListener("click", (event) => {
        event.stopPropagation();
        openManageChildren(manage, group);
      });
      actions.append(sync, manage);
      head.append(toggle, meta, ownerCol, healthCol, syncCol, actions);

      if (mother) body.append(portfolioAccountRow(mother, "mother"));
      currentChildren.forEach((row) => body.append(portfolioAccountRow(row, "child")));
      unmanaged.forEach((row) => body.append(portfolioAccountRow(row, "unmanaged")));
      if (history.length) {
        const hist = document.createElement("details");
        hist.className = "portfolio-history";
        const summary = document.createElement("summary");
        summary.className = "portfolio-history-toggle";
        summary.textContent = "历史归档成员 (" + history.length + ")";
        hist.append(summary);
        history.forEach((row) => hist.append(portfolioAccountRow(row, "history")));
        body.append(hist);
      }
      box.append(head, body);
      root.append(box);
    });
    (data.unassigned || []).forEach((row) => {
      if (q && !String(row.email || "").toLowerCase().includes(q)) return;
      if (!matchesPurpose(row)) return;
      shown += 1;
      root.append(portfolioAccountRow(row, "unassigned"));
    });
    if (!shown) root.append(emptyState("没有符合当前筛选的工作区", "清除筛选或换一个关键词。", true));
    setCount("accounts-count", shown, (data.groups || []).length + (data.unassigned || []).length);
  }

  function paintList(kind) {
    const items = filterItems(kind, pageCache.items);
    if (kind === "workspace") {
      renderRows(
        "workspaces-body",
        items,
        8,
        workspaceRow,
        items.length === 0 && pageCache.items.length
          ? emptyState("没有符合当前筛选的工作区", "清除筛选或换一个关键词。")
          : emptyState("还没有工作区", "点「登记团队」，填母号邮箱并完成 OAuth 授权。")
      );
      setCount("workspaces-count", items.length, pageCache.items.length);
    } else if (kind === "account") {
      const view = currentQuery().get("view") || "portfolio";
      const table = document.querySelector("#accounts-body")?.closest(".table-scroll");
      const portfolio = document.getElementById("accounts-portfolio");
      if (view === "flat") {
        if (table) table.hidden = false;
        if (portfolio) portfolio.hidden = true;
        renderRows(
          "accounts-body",
          items,
          8,
          accountRow,
          items.length === 0 && pageCache.items.length
            ? emptyState("没有符合当前筛选的账号", "清除筛选或换一个关键词。")
            : emptyState("还没有账号", "先登记团队母号。归档的默认不显示。")
        );
        setCount("accounts-count", items.length, pageCache.items.length);
      } else if (pageCache.portfolio) {
        renderPortfolio(pageCache.portfolio);
      }
    }
  }

  async function bootOverview() {
    renderOverview(await fetchEntity("overview", "/api/overview"));
  }

  async function bootWorkspaces() {
    bindSearch(document.getElementById("workspaces-search"), "workspace");
    const payload = await fetchEntity("workspace-list", "/api/workspaces");
    pageCache.kind = "workspace";
    pageCache.items = payload.items || [];
    paintList("workspace");
  }

  async function bootAccounts() {
    const filter = document.querySelector("[data-filter='purpose']");
    const params = currentQuery();
    if (filter && params.get("purpose")) filter.value = params.get("purpose");
    if (filter && !filter.dataset.bound) {
      filter.dataset.bound = "1";
      filter.addEventListener("change", () => {
        const next = currentQuery();
        if (filter.value && filter.value !== "all") next.set("purpose", filter.value);
        else next.delete("purpose");
        writeQuery(next);
        bootPage();
      });
    }
    document.querySelectorAll("[data-accounts-view]").forEach((button) => {
      if (button.dataset.bound) return;
      button.dataset.bound = "1";
      button.addEventListener("click", () => {
        const next = currentQuery();
        const view = button.dataset.accountsView || "portfolio";
        if (view === "portfolio") next.delete("view");
        else next.set("view", view);
        writeQuery(next);
        bootPage();
      });
    });
    const view = params.get("view") || "portfolio";
    document.querySelectorAll("[data-accounts-view]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.accountsView === view);
    });
    bindSearch(document.getElementById("accounts-search"), "account");
    if (view !== "flat") {
      const payload = await fetchEntity("account-portfolio", "/api/accounts/portfolio");
      pageCache.kind = "account";
      pageCache.items = [];
      pageCache.portfolio = payload;
      renderPortfolio(payload);
      return;
    }
    const purpose = filter?.value || params.get("purpose") || "all";
    const includeArchived = purpose === "archived";
    const payload = await fetchEntity(
      "account-list",
      `/api/accounts?purpose=${encodeURIComponent(purpose)}&include_archived=${includeArchived}`
    );
    pageCache.kind = "account";
    pageCache.items = payload.items || [];
    paintList("account");
  }

  function operationsQueryFromUI() {
    const params = currentQuery();
    const q = document.getElementById("operations-search")?.value || params.get("q") || "";
    const state = document.getElementById("operations-state")?.value || params.get("state") || "";
    const type = document.getElementById("operations-type")?.value || params.get("type") || "";
    const source = document.getElementById("operations-source")?.value || params.get("source") || "";
    const range = document.getElementById("operations-range")?.value || params.get("range") || "7d";
    const archivedOnly = document.getElementById("operations-archived-only")?.checked || params.get("archived_only") === "1";
    const page = Number(params.get("page") || 1);
    const pageSize = Number(document.getElementById("operations-page-size")?.value || params.get("page_size") || 50);
    const query = new URLSearchParams();
    if (q) query.set("q", q);
    if (state) query.set("state", state);
    if (type) query.set("type", type);
    if (source) query.set("source", source);
    if (range === "today") query.set("date_from", "today");
    else if (range === "30d") query.set("date_from", "30d");
    else if (range === "all") query.delete("date_from");
    else query.set("date_from", "7d");
    if (archivedOnly) {
      query.set("archived_only", "true");
      query.set("include_archived", "true");
    }
    query.set("page", String(page || 1));
    query.set("page_size", String(pageSize || 50));
    return query;
  }

  function syncOperationsUrl(query) {
    const next = currentQuery();
    ["q", "state", "type", "source", "date_from", "date_to", "archived_only", "include_archived", "page", "page_size", "range"].forEach((key) => next.delete(key));
    query.forEach((value, key) => next.set(key, value));
    const range = document.getElementById("operations-range")?.value;
    if (range) next.set("range", range);
    writeQuery(next);
  }

  function updateOperationsBulkButton() {
    const button = document.getElementById("operations-bulk-archive");
    if (!button) return;
    const selected = Array.from(document.querySelectorAll(".operation-select:checked"));
    button.hidden = selected.length === 0;
    button.textContent = selected.length ? `清除所选（${selected.length}）` : "清除所选";
  }

  async function bootOperations() {
    const search = document.getElementById("operations-search");
    const state = document.getElementById("operations-state");
    const type = document.getElementById("operations-type");
    const source = document.getElementById("operations-source");
    const range = document.getElementById("operations-range");
    const archivedOnly = document.getElementById("operations-archived-only");
    const pageSize = document.getElementById("operations-page-size");
    const params = currentQuery();
    if (search && params.get("q")) search.value = params.get("q");
    if (state && params.get("state")) state.value = params.get("state");
    if (type && params.get("type")) type.value = params.get("type");
    if (source && params.get("source")) source.value = params.get("source");
    if (range && params.get("range")) range.value = params.get("range");
    if (archivedOnly) archivedOnly.checked = params.get("archived_only") === "1" || params.get("archived_only") === "true";
    if (pageSize && params.get("page_size")) pageSize.value = params.get("page_size");

    if (search && !search.dataset.bound) {
      search.dataset.bound = "1";
      let timer = null;
      search.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => {
          const next = currentQuery();
          next.set("page", "1");
          writeQuery(next);
          bootOperations();
        }, 250);
      });
    }
    [state, type, source, range, archivedOnly, pageSize].forEach((node) => {
      if (!node || node.dataset.bound) return;
      node.dataset.bound = "1";
      node.addEventListener("change", () => {
        const next = currentQuery();
        next.set("page", "1");
        writeQuery(next);
        bootOperations();
      });
    });
    const clearBtn = document.getElementById("operations-clear-filters");
    if (clearBtn && !clearBtn.dataset.bound) {
      clearBtn.dataset.bound = "1";
      clearBtn.addEventListener("click", () => {
        if (search) search.value = "";
        if (state) state.value = "";
        if (type) type.value = "";
        if (source) source.value = "";
        if (range) range.value = "7d";
        if (archivedOnly) archivedOnly.checked = false;
        const next = currentQuery();
        ["q", "state", "type", "source", "date_from", "date_to", "archived_only", "include_archived", "page"].forEach((k) => next.delete(k));
        next.set("range", "7d");
        next.set("page", "1");
        writeQuery(next);
        bootOperations();
      });
    }
    const bulk = document.getElementById("operations-bulk-archive");
    if (bulk && !bulk.dataset.bound) {
      bulk.dataset.bound = "1";
      bulk.addEventListener("click", async () => {
        const ids = Array.from(document.querySelectorAll(".operation-select:checked")).map((node) => node.dataset.publicId).filter(Boolean);
        if (!ids.length) return;
        if (!confirmDanger(`确认清除当前选中的 ${ids.length} 条已完成任务？`)) return;
        const result = await postAction("operations-bulk-archive", "/api/operations/bulk-archive", { public_ids: ids, reason: "bulk_clear" });
        toast(result.ok ? `已清除 ${result.count || ids.length} 条` : (result.error || "清除失败"), result.ok ? "success" : "error");
        await bootOperations();
      });
    }
    const selectPage = document.getElementById("operations-select-page");
    if (selectPage && !selectPage.dataset.bound) {
      selectPage.dataset.bound = "1";
      selectPage.addEventListener("change", () => {
        document.querySelectorAll(".operation-select").forEach((node) => {
          if (!node.disabled) node.checked = selectPage.checked;
        });
        updateOperationsBulkButton();
      });
    }
    const prev = document.getElementById("operations-prev");
    const nextBtn = document.getElementById("operations-next");
    if (prev && !prev.dataset.bound) {
      prev.dataset.bound = "1";
      prev.addEventListener("click", () => {
        const next = currentQuery();
        const page = Math.max(1, Number(next.get("page") || 1) - 1);
        next.set("page", String(page));
        writeQuery(next);
        bootOperations();
      });
    }
    if (nextBtn && !nextBtn.dataset.bound) {
      nextBtn.dataset.bound = "1";
      nextBtn.addEventListener("click", () => {
        const next = currentQuery();
        const page = Math.max(1, Number(next.get("page") || 1) + 1);
        next.set("page", String(page));
        writeQuery(next);
        bootOperations();
      });
    }

    const query = operationsQueryFromUI();
    syncOperationsUrl(query);
    const payload = await fetchEntity("operation-list", `/api/operations?${query.toString()}`);
    renderRows(
      "operations-body",
      payload.items || [],
      8,
      operationRow,
      emptyState("没有符合筛选的任务", "调整筛选，或先创建额度刷新、同步、推送任务。")
    );
    const count = document.getElementById("operations-count");
    if (count) {
      const total = payload.total ?? (payload.items || []).length;
      count.textContent = `共 ${total} 条` + (payload.range?.defaulted_to_last_7_days ? " · 默认最近 7 天" : "");
    }
    const pageLabel = document.getElementById("operations-page-label");
    if (pageLabel) pageLabel.textContent = `第 ${payload.page || 1} 页` + (payload.has_more ? " · 还有更多" : "");
    if (prev) prev.disabled = Number(payload.page || 1) <= 1;
    if (nextBtn) nextBtn.disabled = !payload.has_more;
    if (selectPage) selectPage.checked = false;
    updateOperationsBulkButton();
    const op = currentQuery().get("op");
    if (op) await openOperationById(op);
  }

  async function bootPhones() {
    const payload = await fetchEntity("phone-list", "/api/resources/phones");
    const items = payload.items || [];
    renderRows(
      "phones-body",
      items,
      8,
      phoneRow,
      emptyState("还没有手机号", "点「导入号码」，按 号码----接码链接 批量入库。")
    );
    setCount("phones-count", items.length, items.length);
  }

  async function bootHme() {
    const payload = await fetchEntity("hme-list", "/api/resources/hme");
    const items = payload.items || [];
    renderRows(
      "hme-body",
      items,
      7,
      hmeRow,
      emptyState("还没有 HME 占用记录", "领用别名后会显示本地占用和同步状态。")
    );
    setCount("hme-count", items.length, items.length);
  }

  async function bootProxies() {
    const payload = await fetchEntity("proxy-list", "/api/resources/proxies");
    const items = payload.items || [];
    renderRows(
      "proxies-body",
      items,
      8,
      proxyRow,
      emptyState("还没有代理", "点「添加代理」，或在登记母号时填写代理自动入库。")
    );
    setCount("proxies-count", items.length, items.length);
  }

  async function bootSettings() {
    const form = document.getElementById("settings-form");
    const passwordForm = document.getElementById("password-form");
    if (form && !form.dataset.bound) {
      form.dataset.bound = "1";
      form.addEventListener("submit", saveSettings);
      form.addEventListener("input", updateDirty);
      form.addEventListener("change", updateDirty);
      document.getElementById("settings-discard")?.addEventListener("click", () => bootPage());
      document.getElementById("settings-probe-all")?.addEventListener("click", () => probeSettings("all"));
      document.querySelectorAll("[data-probe]").forEach((button) => {
        button.addEventListener("click", () => probeSettings(button.dataset.probe));
      });
      document.querySelectorAll("[data-edit-toggle]").forEach((button) => {
        button.addEventListener("click", () => {
          const panel = document.querySelector(`[data-edit-panel="${button.dataset.editToggle}"]`);
          if (!panel) return;
          panel.hidden = !panel.hidden;
          button.textContent = panel.hidden ? "编辑" : "收起";
        });
      });
    }
    if (passwordForm && !passwordForm.dataset.bound) {
      passwordForm.dataset.bound = "1";
      passwordForm.addEventListener("submit", savePassword);
    }
    fillSettings(await fetchEntity("settings", "/api/settings"));
  }

  const pageBootstraps = {
    overview: bootOverview,
    workspaces: bootWorkspaces,
    accounts: bootAccounts,
    operations: bootOperations,
    phones: bootPhones,
    hme: bootHme,
    proxies: bootProxies,
    settings: bootSettings,
  };

  async function bootPage() {
    const page = document.body.dataset.page;
    hidePageError();
    try {
      const bootstrap = pageBootstraps[page];
      if (bootstrap) await bootstrap();
    } catch (error) {
      if (error.name === "AbortError") return;
      showPageError(friendlyError(error));
    }
  }

  function renderPalette(query) {
    const q = query.trim().toLowerCase();
    commandList.replaceChildren();
    destinations
      .filter((item) => item.label.toLowerCase().includes(q) || item.label.includes(query.trim()))
      .forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item.label;
        li.addEventListener("click", () => {
          window.location.href = item.href;
        });
        commandList.append(li);
      });
  }

  document.getElementById("open-operations")?.addEventListener("click", openDrawer);
  document.querySelector("[data-close-drawer]")?.addEventListener("click", closeDrawer);
  document.querySelector("[data-close-sheet]")?.addEventListener("click", closeSheet);
  document.querySelectorAll("[data-open-register]").forEach((button) => {
    button.addEventListener("click", () => openRegister(button));
  });
  document.querySelector("[data-close-register]")?.addEventListener("click", closeRegister);
  document.getElementById("register-form")?.addEventListener("submit", submitRegister);
  document.querySelector("[data-close-reauth]")?.addEventListener("click", closeReauth);
  document.getElementById("reauth-form")?.addEventListener("submit", submitReauth);
  document.querySelectorAll("[data-open-phone-import]").forEach((button) => {
    button.addEventListener("click", () => openPhoneImport(button));
  });
  document.querySelector("[data-close-phone-import]")?.addEventListener("click", closePhoneImport);
  document.getElementById("phone-import-form")?.addEventListener("submit", submitPhoneImport);
  document.querySelectorAll("[data-open-proxy-add]").forEach((button) => {
    button.addEventListener("click", () => openProxyAdd(button));
  });
  document.querySelector("[data-close-proxy-add]")?.addEventListener("click", closeProxyAdd);
  document.getElementById("proxy-add-form")?.addEventListener("submit", submitProxyAdd);
  document.querySelectorAll("[data-open-onboard]").forEach((button) => {
    button.addEventListener("click", () => openOnboard(button));
  });
  document.querySelector("[data-close-onboard]")?.addEventListener("click", closeOnboard);
  document.getElementById("onboard-form")?.addEventListener("submit", submitOnboard);
  document.querySelector("[data-close-rotate]")?.addEventListener("click", closeRotate);
  document.getElementById("rotate-form")?.addEventListener("submit", submitRotate);
  document.querySelector("[data-close-proxy-edit]")?.addEventListener("click", closeProxyEdit);
  document.getElementById("proxy-edit-form")?.addEventListener("submit", submitProxyEdit);
  document.querySelector("[data-close-manage-children]")?.addEventListener("click", closeManageChildren);
  document.getElementById("manage-children-form")?.addEventListener("submit", submitManageChildren);


  document.getElementById("register-copy-link")?.addEventListener("click", async () => {
  document.getElementById("reauth-copy-link")?.addEventListener("click", async () => {
    const authorize = document.getElementById("reauth-authorize-url");
    const copied = await copyText(authorize?.value);
    setReauthStatus(copied ? "授权链接已复制。" : "复制失败，请手动选中链接。", copied ? "muted" : "error");
  });
    const authorize = document.getElementById("register-authorize-url");
    const copied = await copyText(authorize?.value);
    setRegisterStatus(copied ? "授权链接已复制。" : "复制失败，请手动选中链接。", copied ? "muted" : "error");
  });
  document.querySelectorAll("[data-action-page]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.actionPage;
      button.disabled = true;
      try {
        if (action === "hme-reconcile") {
          const result = await postAction("hme-reconcile", "/api/resources/hme/reconcile");
          await handleActionResult(result, { successMessage: result.message || ("HME 对账完成，差异 " + (result.conflicts ?? 0)) });
        } else if (action === "hme-retry-pending") {
          const payload = await fetchEntity("hme-list", "/api/resources/hme");
          const pending = (payload.items || []).filter((item) => item.pending);
          let ok = 0;
          for (const item of pending) {
            const result = await postAction(`hme-retry-${item.id}`, `/api/resources/hme/${item.id}/retry-label`);
            if (result.ok) ok += 1;
          }
          toast(`已重试 ${ok}/${pending.length} 个待同步标签`, ok === pending.length ? "success" : "warning");
          await bootPage();
        } else if (action === "proxy-probe-all") {
          const result = await postAction("proxy-probe-all", "/api/resources/proxies/probe-all");
          await handleActionResult(result, { successMessage: result.message || ("检测完成：健康 " + (result.healthy ?? 0) + "/" + (result.total ?? 0)) });
        } else if (action === "workspace-sync-all") {
          const payload = await fetchEntity("workspace-list", "/api/workspaces");
          const items = payload.items || [];
          let ok = 0;
          for (const item of items) {
            const result = await postAction(`workspace-sync-${item.id}`, `/api/workspaces/${item.id}/sync`);
            if (result.ok) ok += 1;
          }
          toast(`同步全部完成：${ok}/${items.length}`, ok === items.length ? "success" : "warning");
          await bootPage();
        }
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.getElementById("page-retry")?.addEventListener("click", bootPage);
  document.getElementById("sidebar-toggle")?.addEventListener("click", () => {
    document.body.classList.toggle("nav-open");
  });
  drawer?.addEventListener("click", (event) => {
    if (event.target === drawer) closeDrawer();
  });
  sheet?.addEventListener("click", (event) => {
    if (event.target === sheet) closeSheet();
  });
  registerSheet?.addEventListener("click", (event) => {
  reauthSheet?.addEventListener("click", (event) => {
    if (event.target === reauthSheet) closeReauth();
  });
    if (event.target === registerSheet) closeRegister();
  });
  phoneImportSheet?.addEventListener("click", (event) => {
    if (event.target === phoneImportSheet) closePhoneImport();
  });
  proxyAddSheet?.addEventListener("click", (event) => {
    if (event.target === proxyAddSheet) closeProxyAdd();
  });
  document.addEventListener("click", (event) => {
    if (menu && !menu.hidden && !event.target.closest("#action-menu, [data-menu-trigger], .row-actions")) closeMenu();
  });

  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      palette.showModal();
      renderPalette("");
      commandInput.focus();
    }
    if (event.key === "Escape") {
      if (!menu.hidden) closeMenu();
      else if (onboardSheet && !onboardSheet.hidden) closeOnboard();
      else if (rotateSheet && !rotateSheet.hidden) closeRotate();
      else if (proxyEditSheet && !proxyEditSheet.hidden) closeProxyEdit();
      else if (phoneImportSheet && !phoneImportSheet.hidden) closePhoneImport();
      else if (proxyAddSheet && !proxyAddSheet.hidden) closeProxyAdd();
      else if (reauthSheet && !reauthSheet.hidden) closeReauth();
      else if (registerSheet && !registerSheet.hidden) closeRegister();
      else if (sheet && !sheet.hidden) closeSheet();
      else closeDrawer();
      document.body.classList.remove("nav-open");
    }
    handleFocusTrap(event);
  });
  commandInput?.addEventListener("input", () => renderPalette(commandInput.value));
  window.addEventListener("beforeunload", (event) => {
    if (!settingsDirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") stopPolling();
  });

  window.Team48 = { abortEntity, fetchEntity, entityActions, pageBootstraps };
  bootPage();
})();
