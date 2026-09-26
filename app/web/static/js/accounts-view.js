/* Unified management views. Actions, network ownership and overlays stay in app.js. */
(() => {
  const fmt = window.Team48Format;
  const purposeLabels = { mother: "母号", child: "子号", standby: "待命", free: "空闲", disabled: "停用" };
  const kindLabels = { invited: "待接受邀请", unmanaged: "官方已加入 · 未接入", history: "历史成员", unassigned: "未分配" };
  const retryCodes = new Set(["rate_limited", "temporary_failure", "parse_error"]);
  let api, payload, bound = false, deepLinked = false, poller, remotePoller;
  let lastUpdated = null, syncErrors = "";
  function reportSyncErrors(items = []) {
    syncErrors = items.filter(item => !item.ok).map(item => `团队 #${item.workspace_id}：${item.error_code === "credentials_missing" ? "缺少母号凭据" : "暂时无法同步"}`).join("；");
    const node = document.getElementById("management-sync-status");
    if (node) { node.hidden = !syncErrors; node.textContent = syncErrors ? `上次批量提交：${syncErrors}` : ""; }
  }
  let collapsed = new Set();
  let selected = new Set();
  try { collapsed = new Set(JSON.parse(localStorage.getItem("team48:collapsed-teams") || "[]")); } catch (_) {}
  const canDelete = account => Boolean(account?.can_delete_local);
  const canSelect = account => account.managed !== false && Number.isInteger(account.id) && account.id > 0;
  const selectedItems = () => (payload?.accounts || []).filter(account => selected.has(account.id) && canSelect(account));
  function updateSelectionBar() {
    const valid = new Set(selectedItems().map(account => account.id));
    selected = new Set([...selected].filter(id => valid.has(id)));
    const bar = document.getElementById("account-selection-bar");
    const count = document.getElementById("account-selection-count");
    const del = document.getElementById("account-selection-delete");
    const items = selectedItems();
    if (bar) bar.hidden = items.length === 0;
    if (count) count.textContent = `已选 ${items.length} 个（含跨页选择）${items.length > 50 ? " · 单次最多 50 个" : ""}`;
    if (del) del.disabled = items.length === 0 || items.length > 50 || items.some(account => !canDelete(account));
    const exportButton = document.getElementById("account-selection-export");
    if (exportButton) exportButton.disabled = items.length === 0 || items.length > 50;
  }
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const query = () => new URLSearchParams(location.search);
  const viewName = value => ({ portfolio: "teams", flat: "all" }[value] || (["teams", "all", "unassigned", "attention"].includes(value) ? value : "teams"));
  function setQuery(key, value) {
    if (["q", "purpose", "health", "team", "view", "remote"].includes(key)) selected.clear();
    const params = query();
    if (["q", "purpose", "health", "team", "view", "remote", "sort", "page_size"].includes(key)) params.delete("page");
    if (!value || value === "all" && ["purpose", "health", "remote"].includes(key)) params.delete(key); else params.set(key, value);
    history.replaceState(null, "", `${location.pathname}${params.size ? "?" + params : ""}`);
  }
  function button(text, action, cls = "button", key) {
    const node = el("button", cls, text); node.type = "button";
    if (key) node.dataset.focusKey = key;
    node.addEventListener("click", async event => {
      event.stopPropagation();
      try { await action(node); } catch (error) { api?.toast(api.friendlyError(error), "error"); }
    });
    return node;
  }
  const FILTER_KEYS = ["q", "purpose", "health", "team", "remote"];
  const filterSelectors = {purpose: "[data-filter='purpose']", health: "#management-health", team: "#management-workspace", remote: "#management-remote"};
  const filterNames = {purpose: "用途", health: "检测", team: "团队", remote: "Sub2API"};
  const activeFilters = params => FILTER_KEYS.filter(key => {
    const value = params.get(key);
    return Boolean(value) && (key === "q" || value !== "all");
  });
  function filterText(key, value) {
    if (key === "q") return `搜索：${value}`;
    const node = document.querySelector(filterSelectors[key] || "");
    const option = node && [...node.options].find(item => item.value === value);
    return `${filterNames[key]}：${option ? option.textContent : value}`;
  }
  function clearFilters(keys) {
    keys.forEach(key => setQuery(key, ""));
    if (keys.includes("q")) {
      const search = document.getElementById("accounts-search");
      if (search) search.value = "";
    }
    render(payload);
  }
  function renderActiveFilters(params) {
    const box = document.getElementById("accounts-active-filters");
    const clear = document.getElementById("accounts-clear-filters");
    const active = activeFilters(params);
    if (clear) clear.hidden = !active.length;
    if (!box) return;
    box.hidden = !active.length;
    box.replaceChildren();
    for (const key of active) {
      const text = filterText(key, params.get(key));
      const chip = el("span", "management-chip");
      chip.append(el("span", "", text));
      const remove = button("✕", () => clearFilters([key]), "management-chip-clear", `chip:${key}`);
      remove.setAttribute("aria-label", `清除筛选 ${text}`);
      chip.append(remove);
      box.append(chip);
    }
  }
  const severityOrder = {error: 0, warning: 1, info: 2, muted: 3, success: 4};
  const numeric = value => {
    if (value == null || value === "" || typeof value === "boolean") return -1;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : -1;
  };
  const sortValues = {
    email: account => String(account?.email || account?.name || "").toLowerCase(),
    health: account => `${severityOrder[account?.health?.severity] ?? 9}-${account?.health?.label || ""}`,
    quota: account => Math.max(...contexts(account).map(context =>
      numeric((context.last_success_quota || context.quota || {}).seven_day_used_percent))),
    cost: account => numeric(fmt.usageWindow(account?.usage)?.user_cost),
    checked: account => numeric(Date.parse(account?.latest_check?.checked_at)),
  };
  function sortAccounts(items, params) {
    const raw = (params || query()).get("sort") || "";
    const descending = raw.startsWith("-");
    const read = sortValues[descending ? raw.slice(1) : raw];
    if (!read) return items;
    const sorted = [...items].sort((left, right) => {
      const a = read(left), b = read(right);
      const order = typeof a === "number" && typeof b === "number"
        ? a - b : String(a).localeCompare(String(b), "zh-CN");
      return descending ? -order : order;
    });
    return sorted;
  }
  function status(account, compact = false) {
      const h = account.health || { label: kindLabels[account.kind] || "尚未检测", severity: "muted" };
      const actual401 = account.latest_check?.http_status === 401 && account.latest_check?.current_credential !== false;
      const short = actual401 ? "401" : ({healthy: "正常", auth_required: "需 OAuth", credential_error: "缺凭据", pending: "待验证", quota_exhausted: "额度用尽", temporary_failure: "暂时失败", rate_limited: "限流", forbidden: "访问被拒", parse_error: "解析失败", partial: "需处理"}[h.code] || (h.needs_auth ? "需授权" : h.label));
      const badge = el("span", `management-badge tone-${h.needs_auth && !actual401 ? "warning" : h.severity}`, compact ? short : h.label);
      badge.title = [h.label, account.latest_check?.message].filter(Boolean).join(" · ");
      return badge;
    }
  const contexts = account => account.contexts?.length ? account.contexts : [account];
  function matches(account, group, params) {
    const text = (params.get("q") || "").trim().toLowerCase();
    const purpose = params.get("purpose") || "all";
    const health = params.get("health") || "all";
    const workspace = params.get("team");
    if (workspace && !contexts(account).some(c => String(c.workspace_id) === workspace)) return false;
    if (text && ![account.email, account.name, group?.display_name, group?.name, group?.official_workspace_id,
      account.workspace, ...contexts(account).map(c => c.workspace_name)].join(" ").toLowerCase().includes(text)) return false;
    if (purpose === "archived") {
      if (account.kind !== "history" && account.state !== "archived") return false;
    } else if (account.kind === "history" || account.state === "archived" || account.state === "disabled") return false;
    if (purpose === "needs_auth" && !account.health?.needs_auth) return false;
    if (purpose === "quota_full" && !contexts(account).some(c => c.quota?.seven_day_used_percent === 100)) return false;
    if (purpose === "conflict" && account.state !== "conflict") return false;
    if (["mother", "child", "standby", "free"].includes(purpose) && account.purpose !== purpose) return false;
    const any = predicate => contexts(account).some(predicate);
    const remote = params.get("remote") || "all";
    if (remote !== "all" && !remoteItems(account).some(item => remote === "attention"
      ? ["error", "auth_error", "forbidden", "phone_required", "identity_mismatch", "identity_unconfirmed", "binding_review"].includes(item.state)
      : item.state === remote)) return false;
    if (health === "auth") return any(c => c.health?.needs_auth);
    if (health === "401") return any(c => c.latest_check?.http_status === 401 && c.latest_check?.current_credential !== false);
    if (health === "retry") return any(c => retryCodes.has(c.health?.code));
    if (health !== "all") return any(c => c.health?.code === health);
    return true;
  }
  function remoteItems(account) {
    return contexts(account).flatMap(c => c.remote_status?.bindings || [c.remote_status || {
      state: "unknown", label: "远端尚未核对", severity: "muted", stale: true,
    }]);
  }
  function remoteStatus(account) {
    const wrap = el("div", "management-remote-status");
    for (const state of remoteItems(account)) {
      const badge = el("span", `management-badge tone-${state.severity || "muted"}`, state.label);
      badge.dataset.remoteState = state.state;
      wrap.append(badge);
      if (state.remote_id) wrap.append(el("small", "muted", `远端 #${state.remote_id}${state.schedulable === false ? " · 不调度" : ""}`));
      if (state.last_known_label && state.checked_at) wrap.append(el("small", "text-warning", `上次：${state.last_known_label}`));
      if (state.checked_at) wrap.append(el("small", state.stale ? "text-warning" : "muted", `${state.stale ? "旧状态 · " : ""}${api.relativeTime(state.checked_at)}核对`));
      else if (state.state !== "unbound") wrap.append(el("small", "muted", state.message || "等待核对"));
    }
    return wrap;
  }
  async function refreshRemote(force = false) {
    if (!payload?.sub2api_status?.configured || !payload.sub2api_status.bindings) return {ok: true, skipped: true};
    const result = await api.postAction(`sub2api-status:${force ? "manual" : "auto"}`, `/api/sub2api/status/refresh${force ? "?force=true" : ""}`, {});
    await poller?.refresh();
    const node = document.getElementById("management-remote-status");
    if (node && !result.ok) node.textContent = result.message || "Sub2API 暂时无法核对，保留上次状态";
    return result;
  }
  function quota(account, compact = false) {
      const wrap = el("div", "management-quota");
      const data = account.last_success_quota || account.quota || {};
      if (account.contexts?.length > 1) return el("span", "muted", `${account.contexts.length} 个团队 · 分别查看`);
      const age = Date.now() - Date.parse(data.queried_at);
      const stale = Number.isFinite(age) ? age > 6 * 3600000 : Boolean(data.stale);
      for (const [label, value] of [["5h", data.five_hour_used_percent], ["7d", data.seven_day_used_percent]]) {
        const line = el("div", "management-meter"); line.append(el("span", "", label));
        const bar = el("div", `management-bar${stale ? " is-stale" : ""}`);
        if (value != null && Number.isFinite(Number(value))) {
          bar.setAttribute("role", "meter"); bar.setAttribute("aria-label", `${label} 已用额度${stale ? "，旧快照" : ""}`);
          bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100"); bar.setAttribute("aria-valuenow", String(value));
          const fill = el("span"); fill.style.width = `${Math.max(0, Math.min(100, Number(value)))}%`; bar.append(fill);
        }
        line.append(bar, el("span", "tabular", value == null ? "—" : `${value}%`)); wrap.append(line);
      }
      const reset = new Date(data.seven_day_reset_at || "");
      const resetText = Number.isFinite(reset.getTime()) ? `${new Intl.DateTimeFormat("zh-CN", {weekday: "short"}).format(reset)}重置` : "";
      const facts = compact ? [resetText, stale ? "旧快照" : ""] : [resetText, data.queried_at ? `${stale ? "旧快照" : "成功"} · ${api.relativeTime(data.queried_at)}` : "无成功快照"];
      if (facts.some(Boolean)) wrap.append(el("small", stale ? "text-warning" : "muted", facts.filter(Boolean).join(" · ")));
      wrap.title = [data.queried_at ? `快照：${new Date(data.queried_at).toLocaleString("zh-CN")}` : "无成功快照", resetText ? `7d 重置：${reset.toLocaleString("zh-CN")}` : "7d 重置时间未知"].join("；");
      return wrap;
    }
  function openAccount(account, trigger, group) {
    setQuery("account", account.id);
    if (account.workspace_id) setQuery("workspace", account.workspace_id);
    api.openSheet("account", account, trigger);
    if (group) addBack(group, trigger);
  }
  function addBack(group, trigger) {
    const body = document.getElementById("sheet-body");
    if (!body || body.querySelector(".management-back")) return;
    body.prepend(button(`← ${group.display_name || group.name || "返回团队"}`, () => {
      setQuery("account", ""); api.openWorkspaceDetails(trigger, group);
    }, "button ghost management-back"));
  }
  function accountRow(account, group) {
      const row = el("tr");
      const id = `${account.id || account.email}:${account.workspace_id || "none"}`;
      row.dataset.account = String(account.id || "");
      const selectCell = el("td", "management-select");
      if (canSelect(account)) {
        const box = el("input"); box.type = "checkbox"; box.checked = selected.has(account.id);
        box.setAttribute("aria-label", `选择 ${account.email}`);
        box.addEventListener("change", () => { if (box.checked) selected.add(account.id); else selected.delete(account.id); render(payload); });
        selectCell.append(box);
      }
      const identity = el("td"), identityText = el("div", "management-identity-line");
      const canOpen = account.managed !== false && Boolean(account.id);
      const label = canOpen ? button(account.email || account.name, b => openAccount(account, b, group), "management-email", `account:${id}`) : el("span", "management-email", account.email || account.name || "未知账号");
      label.title = [account.email, subscriptionLabel(account, group), kindLabels[account.kind], !group ? account.workspace || account.workspace_name : null].filter(Boolean).join(" · ");
      identityText.append(label, el("span", "management-purpose", purposeLabels[account.purpose] || kindLabels[account.kind] || "外部"));
      identity.append(identityText);
      const health = el("td"); const healthState = el("span"); healthState.append(status(account, true)); health.append(healthState);
      if (account.interrupted_operation_id) {
      healthState.replaceChildren(button("入组中断", b => api.openOperationById(account.interrupted_operation_id,b), "management-badge tone-warning"));
    }
    const task = el("span"); health.append(task);
      window.Team48TaskProgress?.mount(task, {account, density: "badge", metadata: healthState});
      if (account.queued) healthState.title = "检查已排队或运行中";
      else if (account.latest_check?.current_credential === false) healthState.title = "上次检查属于旧凭证";
      const quotaCell = el("td"); quotaCell.append(canOpen ? quota(account, true) : el("span", "muted", kindLabels[account.kind] || "尚未接入"));
      const money = el("td", "management-money tabular");
      const remote = remoteItems(account), first = remote[0] || {};
      const remoteLabels = {healthy:"远端正常",unbound:"未绑定",paused:"已暂停",missing:"远端不存在",unknown:"待核对",auth_error:"授权异常",error:"同步失败",rate_limited:"远端限流"};
      const badge = el("span", `management-badge tone-${first.severity || "muted"}`, remote.length > 1 ? `${remote.length} 个绑定` : remoteLabels[first.state] || first.label || "待核对");
      badge.dataset.remoteState = first.state || "unknown"; money.append(badge);
      const usage = fmt.usageWindow(account.usage);
      if (usage) money.append(el("small", "muted", fmt.formatCost(usage.user_cost)));
      money.title = [...remote.map(s => [s.label, s.remote_id ? `远端 #${s.remote_id}` : "", s.stale ? "旧状态，待核对" : "", s.message].filter(Boolean).join(" · ")), usage ? `${usage.label} · 用户计费 ${fmt.formatCost(usage.user_cost)} · 成本 ${fmt.formatCost(usage.account_cost)}${usage.stale ? " · 旧计费快照" : ""}` : "无计费快照"].join("；");
      const time = el("td", "management-check-time");
      time.append(el("span", "", account.latest_check?.checked_at ? api.relativeTime(account.latest_check.checked_at) : "未检查"));
      time.title = account.next_check_at ? `下次检查：${new Date(account.next_check_at).toLocaleString("zh-CN")}` : "尚无下次检查计划";
      const actionCell = el("td", "management-action-cell"); const actions = el("div", "management-actions");
      if (canOpen) {
        const primary = button(account.health?.needs_auth ? "授权" : "详情", b => {
          if (account.health?.needs_auth) return api.entityActions.account.find(a => a.id === "account.reauth")?.run(account, b);
          return openAccount(account, b, group);
        }, `button${account.health?.needs_auth ? " primary" : ""}`, `primary:${id}`);
        actions.append(primary);
        const pushAction = account.purpose === "child" && !account.health?.needs_auth
          ? api.entityActions.account.find(a => ["account.sub2api.push", "account.sub2api.update"].includes(a.id) && a.visible(account)) : null;
        if (pushAction) actions.append(button(pushAction.label, async b => {
          if (b.disabled) return; b.disabled = true; b.setAttribute("aria-busy", "true");
          try { await pushAction.run(account, b); } finally { b.disabled = false; b.removeAttribute("aria-busy"); }
        }, "button", `sub2api:${id}`));
        actions.append(api.menuButton("account", account));
      } else if (group) {
        if (account.kind === "unmanaged") actions.append(button("接入并授权", b => api.linkTeamMember(group, account, b), "button primary", `link:${id}`));
        else actions.append(button("管理成员", b => api.openWorkspaceDetails(b, group), "button", `remote:${id}`));
      }
      actionCell.append(actions); row.append(selectCell, identity, health, quotaCell);
      if (payload?.sub2api_status?.configured !== false) row.append(money);
      row.append(time, actionCell);
      return row;
    }
  function table(items, group) {
    const scroll = el("div", "management-table-scroll"); scroll.tabIndex = 0; scroll.setAttribute("aria-label", "账号明细");
    scroll.dataset.scrollKey = group ? `team-${group.id}` : "all";
    const table = el("table", "management-table");
    const colgroup = el("colgroup");
    const hasRemote = payload?.sub2api_status?.configured !== false;
    (hasRemote ? [36, 230, 115, 180, 125, 94, 180] : [36, 270, 125, 220, 115, 180]).forEach(width => { const col = el("col"); col.style.width = `${width}px`; colgroup.append(col); });
    const head = el("thead"); const tr = el("tr");
    const selectable = items.filter(canSelect);
    const selectHead = el("th", "management-select"); selectHead.scope = "col";
    if (selectable.length) {
      const box = el("input"); box.type = "checkbox"; box.setAttribute("aria-label", "全选本地账号");
      box.checked = selectable.every(account => selected.has(account.id));
      box.indeterminate = selectable.some(account => selected.has(account.id)) && !box.checked;
      box.addEventListener("change", () => {
        selectable.forEach(account => box.checked ? selected.add(account.id) : selected.delete(account.id));
        render(payload);
      });
      selectHead.append(box);
    }
    tr.append(selectHead);
    const sortRaw = query().get("sort") || "";
    [["账号 / 本地用途", "email"], ["授权与检测", "health"], ["官方额度 · 已用", "quota"],
      ["Sub2API", "cost"], ["最近检查", "checked"], ["操作", ""]].forEach(([text, key], i) => {
      if (key === "cost" && !hasRemote) return;
      const th = el("th", i === 5 ? "num management-action-cell" : i === 3 ? "num" : ""); th.scope = "col";
      if (!key) { th.textContent = text; tr.append(th); return; }
      const active = sortRaw === key || sortRaw === `-${key}`;
      const descending = sortRaw === `-${key}`;
      th.setAttribute("aria-sort", active ? (descending ? "descending" : "ascending") : "none");
      const trigger = button(`${text}${active ? (descending ? " ↓" : " ↑") : ""}`,
        () => { setQuery("sort", active ? (descending ? "" : `-${key}`) : key); render(payload); },
        "management-sort", `sort:${key}`);
      trigger.title = descending ? "点击取消排序" : active ? "点击改为降序" : "点击按此列升序";
      th.append(trigger); tr.append(th);
    });
    head.append(tr); const body = el("tbody"); items.forEach(a => body.append(accountRow(a, group)));
    table.append(colgroup, head, body); scroll.append(table); return scroll;
  }
  function subscriptionLabel(account, group) {
    if (!group && !account.workspace_id && account.contexts?.length > 1) return "席位按团队查看";
    const info = account.subscription || group?.subscription;
    if (!info || info.plan_family !== "business") return "";
    const verified = ["verified", "fresh", "stale"].includes(info.status) && Boolean(info.observed_at);
    const tier = verified ? ({ standard: "Standard", premium: "Premium" }[info.seat_tier] || "档位未识别") : "档位未识别";
    return `Business · ${tier}${info.status === "stale" ? " · 数据待刷新" : ""}`;
  }
  function groupNode(group, items, params) {
      const section = el("section", "management-group"); section.dataset.workspace = String(group.id);
      const header = el("header", "management-group-head");
      const closed = collapsed.has(String(group.id)) && !params.get("q");
      const toggle = button(closed ? "›" : "⌄", () => {
        const key = String(group.id); collapsed.has(key) ? collapsed.delete(key) : collapsed.add(key);
        try { localStorage.setItem("team48:collapsed-teams", JSON.stringify([...collapsed])); } catch (_) {}
        render(payload);
      }, "button ghost management-expand", `expand:${group.id}`);
      toggle.setAttribute("aria-expanded", String(!closed)); toggle.setAttribute("aria-label", `展开或收起 ${group.display_name || group.name}`);
      toggle.setAttribute("aria-controls", `management-team-${group.id}`);
      const title = el("div", "management-group-title"), top = el("div", "management-group-name");
      top.append(el("h2", "", group.display_name || group.name || "未命名团队"));
      const counts = group.counts || {};
      if (counts.health_auth) top.append(el("span", "management-badge tone-warning", `${counts.health_auth} 需授权`));
      if (counts.health_retry) top.append(el("span", "management-badge tone-warning", `${counts.health_retry} 待重试`));
      if (counts.unmanaged) top.append(el("span", "management-badge tone-warning", `${counts.unmanaged} 未接入`));
      title.append(top);
      const metadata = el("div", "workspace-manual-metadata");
      const official = group.official || {};
      const seats = official.occupied_seats != null && official.seat_limit != null ? `${official.occupied_seats}/${official.seat_limit} 席` : counts.joined_people != null ? `已加入 ${counts.joined_people} 人` : "席位未同步";
      metadata.append(el("span", "muted", seats));
      if (counts.invited) metadata.append(el("span", "muted", `${counts.invited} 待接受邀请`));
      const expiry = window.Team48Expiry.trigger(group, b => api.openWorkspaceExpiry(b, group));
      const expiryInfo = window.Team48Expiry.summary(group.expiry);
      expiry.replaceChildren(document.createTextNode(expiryInfo.date ? `到期 ${expiryInfo.date} · ${expiryInfo.label}` : "到期未填"));
      metadata.append(expiry);
      const count = window.Team48SwitchCount.widget(group, {readOnly:true});
      metadata.append(count); title.append(metadata);
      const task = el("section"); task.dataset.teamTask = String(group.id); title.append(task);
      window.Team48TaskProgress?.mount(task, {workspaceId: group.id, density: "compact", metadata});
      const op = group.sync_operation || group.last_sync_operation;
      if (op && (group.sync_operation || ["failed", "partial", "manual_required"].includes(op.state))) {
        const outcome = el("a", "group-sync-outcome text-warning", group.sync_operation ? (op.state === "queued" ? "同步已排队" : op.stage_label || "同步中") : "最近同步未完成，保留上次快照");
        outcome.href = `/operations?op=${encodeURIComponent(op.id)}`; title.append(outcome);
      }
      const controls = el("div", "management-team-controls");
      const more = api.menuButton("workspace-card", group); more.dataset.focusKey = `team-menu:${group.id}`;
      more.setAttribute("aria-label", `${group.display_name || group.name}：更多团队操作`);
      controls.append(button("管理团队", b => { setQuery("workspace", group.id); api.openWorkspaceDetails(b, group); }, "button", `team:${group.id}`), more);
      header.append(toggle, title, controls);
      const body = el("div"); body.id = `management-team-${group.id}`; body.hidden = closed; body.append(table(items, group));
      section.append(header, body); return section;
    }
  function filteredEntries(data, view, params) {
    if (view === "teams") {
      const entries = [];
      const groups = [...(data.groups || [])];
      if (["quota", "-quota"].includes(params.get("sort"))) groups.sort((a,b) => {
        const worst = group => Math.max(-1,...(group.members || []).filter(a=>matches(a,group,params)).map(sortValues.quota));
        return params.get("sort") === "-quota" ? worst(b)-worst(a) : worst(a)-worst(b);
      });
      for (const group of groups) {
        if (params.get("team") && String(group.id) !== params.get("team")) continue;
        const items = sortAccounts((group.members || []).filter(a => matches(a, group, params)), params);
        if (items.length) entries.push(...items.map(account => ({account, group})));
        else if (!(group.members || []).length && !["q", "health", "purpose", "remote"].some(key => params.get(key) && params.get(key) !== "all")) entries.push({account: null, group});
      }
      entries.push(...sortAccounts((data.unassigned || []).filter(a => matches(a, null, params)), params).map(account => ({account, group: null})));
      return entries;
    }
    let items = view === "unassigned" ? data.unassigned || [] : data.accounts || [];
    if (params.get("purpose") === "archived") items = [...(data.groups || []).flatMap(g => g.history || []), ...(data.unassigned || []).filter(a => a.state === "archived")];
    items = items.filter(a => matches(a, null, params));
    if (view === "attention") items = items.filter(a => a.needs_attention ?? (!["healthy", "disabled"].includes(a.health?.code) || remoteItems(a).some(s => ["missing", "auth_error", "error", "identity_mismatch"].includes(s.state))));
    return sortAccounts(items, params).map(account => ({account, group: null}));
  }
  function pageWindow(entries, requestedPage, requestedSize) {
    const size = [10, 20, 50, 100].includes(Number(requestedSize)) ? Number(requestedSize) : 20;
    const pages = Math.max(1, Math.ceil(entries.length / size));
    const page = Math.max(1, Math.min(pages, Math.floor(Number(requestedPage)) || 1));
    return {items: entries.slice((page - 1) * size, page * size), page, pages, size, total: entries.length};
  }
  function teamPageWindow(entries, requestedPage, requestedSize) {
    const base = pageWindow(entries, 1, requestedSize), buckets = [], chunks = [];
    for (const entry of entries) {
      const last = chunks.at(-1);
      if (entry.group && last?.[0].group?.id === entry.group.id) last.push(entry); else chunks.push([entry]);
    }
    let current = [];
    for (const chunk of chunks) {
      if (current.length && current.length + chunk.length > base.size) { buckets.push(current); current = []; }
      current.push(...chunk);
    }
    if (current.length) buckets.push(current);
    const pages = Math.max(1,buckets.length), page = Math.max(1,Math.min(pages,Math.floor(Number(requestedPage))||1));
    return {...base, pages, page, items:buckets[page-1] || []};
  }
  function renderPagination(page) {
    const pager = document.getElementById("accounts-pagination"); if (!pager) return;
    pager.replaceChildren();
    const go = number => {
      setQuery("page", String(number)); render(payload);
      document.querySelector(".management-content")?.scrollIntoView({behavior: "instant", block: "start"});
    };
    const previous = button("上一页", () => go(page.page - 1)); previous.disabled = page.page <= 1;
    const next = button("下一页", () => go(page.page + 1)); next.disabled = page.page >= page.pages;
    const label = el("label", "", "每页"); const size = el("select"); size.setAttribute("aria-label", "每页条数");
    [10, 20, 50, 100].forEach(n => size.add(new Option(`${n} 条`, String(n)))); size.value = String(page.size);
    size.addEventListener("change", () => {
      try { localStorage.setItem("team48:page-size", size.value); } catch (_) {}
      setQuery("page_size", size.value); go(1);
    });
    label.append(size);
    pager.append(label, el("span", "muted", `第 ${page.page} / ${page.pages} 页 · 共 ${page.total} 条`), previous, next);
  }
  function render(data) {
    if (!data) return;
    payload = data; const root = document.getElementById("accounts-portfolio"); if (!root) return;
    const params = query(), view = viewName(params.get("view"));
    const focusKey = document.activeElement?.dataset.focusKey;
    const scrolls = new Map([...root.querySelectorAll("[data-scroll-key]")].map(n => [n.dataset.scrollKey, n.scrollLeft]));
    const scrollY = window.scrollY;
    const previousGroups = new Map([...root.querySelectorAll(".management-group")].map(n => [n.dataset.workspace, n]));
    root.replaceChildren();
    document.querySelectorAll(".management-tabs [data-management-view]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.managementView === view)));
    for (const [key, value] of Object.entries(data.summary || {})) { const node = document.getElementById(`summary-${key}`); if (node) node.textContent = value; }
    const attentionCount = document.getElementById("accounts-attention-count"); if (attentionCount) attentionCount.textContent = data.summary?.attention ?? "";
    const breakdown = document.getElementById("accounts-attention-breakdown");
    if (breakdown) { breakdown.hidden = view !== "attention"; breakdown.textContent = Object.entries(data.attention_breakdown || {}).filter(([,n])=>n).map(([key,n])=>`${({auth:"授权",onboarding:"入组中断",quota:"额度用尽",check:"检测",remote:"远端",identity:"身份",sync:"同步"})[key]} ${n}`).join(" · ") + "（同一账号可能有多项原因）"; }
    const runtime = data.probe_runtime || {};
    const runtimeText = runtime.effective_enabled ? `目标每 ${runtime.interval_minutes || 60} 分钟 · ${runtime.queued_count || 0} 个排队${runtime.max_overdue_seconds > 60 ? ` · 延后 ${Math.ceil(runtime.max_overdue_seconds / 60)} 分钟` : ""}` : `${runtime.disabled_reason || "定时检测未开启"} · ${runtime.queued_count || 0} 个手动任务`;
    document.getElementById("probe-runtime").textContent = runtimeText;
    const wsSelect = document.getElementById("management-workspace");
    const optionsKey = (data.groups || []).map(g => `${g.id}:${g.display_name || g.name}`).join("|");
    if (wsSelect.dataset.optionsKey !== optionsKey) {
      wsSelect.replaceChildren(new Option("全部团队", ""));
      (data.groups || []).forEach(g => wsSelect.add(new Option(g.display_name || g.name || `团队 ${g.id}`, g.id)));
      wsSelect.dataset.optionsKey = optionsKey;
    }
    wsSelect.value = params.get("team") || "";
    document.getElementById("management-health").value = params.get("health") || "all";
    const remoteFilter = document.getElementById("management-remote");
    if (remoteFilter) remoteFilter.value = params.get("remote") || "all";
    const remoteInfo = data.sub2api_status;
    const remoteLine = document.getElementById("management-remote-status");
    const remoteSetup = document.getElementById("management-remote-setup");
    if (remoteSetup) remoteSetup.hidden = remoteInfo?.configured !== false;
    const moreCount = ["team", "remote"].filter(key => params.get(key) && params.get(key) !== "all").length;
    const moreLabel = document.getElementById("accounts-more-label");
    if (moreLabel) moreLabel.textContent = `更多筛选${moreCount ? `（${moreCount}）` : ""}`;
    if (remoteLine && remoteInfo) remoteLine.textContent = !remoteInfo.configured ? "未配置 Sub2API，暂时无法核对"
      : `远端状态每 15 秒核对 · ${remoteInfo.bindings} 个绑定${remoteInfo.missing ? ` · ${remoteInfo.missing} 个远端已不存在` : ""}${remoteInfo.stale ? " · 有状态待更新" : ""}`;
    const remoteRefresh = document.getElementById("refresh-remote-status");
    if (remoteRefresh) remoteRefresh.disabled = !remoteInfo?.configured || !remoteInfo.bindings;
    if (remoteLine && remoteInfo?.configured && !remoteInfo.bindings) remoteLine.textContent = "尚无远端绑定，暂时无需核对";
    document.querySelector("[data-filter='purpose']").value = params.get("purpose") || "all";
    renderActiveFilters(params);
    let savedSize;
    try { savedSize = localStorage.getItem("team48:page-size"); } catch (_) {}
    const entries = filteredEntries(data, view, params);
    const page = view === "teams" ? teamPageWindow(entries, params.get("page"), params.get("page_size") || savedSize) : pageWindow(entries, params.get("page"), params.get("page_size") || savedSize);
    if (params.has("page") && params.get("page") !== String(page.page)) setQuery("page", String(page.page));
    if (view === "teams") {
      const groups = new Map();
      for (const entry of page.items) {
        const key = entry.group?.id || "unassigned";
        if (!groups.has(key)) groups.set(key, {group: entry.group, items: []});
        if (entry.account) groups.get(key).items.push(entry.account);
      }
      for (const {group, items} of groups.values()) {
        if (!group) { root.append(el("h2", "management-section-title", `未分配 · 本页 ${items.length}`), table(items)); continue; }
        const renderKey = JSON.stringify([group, items, params.toString(), collapsed.has(String(group.id)), [...selected], window.Team48Expiry.today(), data.sub2api_status?.configured]);
        const old = previousGroups.get(String(group.id));
        const node = old?._renderKey === renderKey ? old : groupNode(group, items, params);
        node._renderKey = renderKey; root.append(node);
      }
    } else if (page.items.length) root.append(table(page.items.map(entry => entry.account)));
    renderPagination(page);
    if (!root.children.length) {
      const empty = el("div", "management-empty");
      empty.append(el("h2", "", "没有匹配的记录"), button("清除筛选", () => clearFilters(FILTER_KEYS)));
      root.append(empty);
    }
    document.getElementById("accounts-count").textContent = `共 ${entries.length} 条${view === "teams" ? "关系记录" : "账号记录"} · 本页 ${page.items.length} 条`;
    for (const node of root.querySelectorAll("[data-scroll-key]")) node.scrollLeft = scrolls.get(node.dataset.scrollKey) || 0;
    if (focusKey) [...root.querySelectorAll("[data-focus-key]")].find(n => n.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
    window.scrollTo({ top: scrollY });
    updateSelectionBar();
    refreshDetails();
  }
  function healthDetails(account) {
    const fragment = el("div", "management-health-details"); fragment.append(status(account));
    if (account.latest_check?.current_credential === false) fragment.append(el("p", "text-warning", "上次检查属于旧凭证，新凭证尚待验证"));
    fragment.append(quota(account));
    const dl = el("dl", "kv");
    for (const [label, value] of [["最近检查", account.latest_check?.checked_at], ["最近成功", account.last_success_quota?.queried_at],
      ["HTTP 状态", account.latest_check?.http_status], ["检查来源", account.latest_check?.source],
      ["最近结果", account.latest_check?.message], ["凭证版本", account.credential_revision],
      ["下次检查", account.next_check_at], ["任务", account.queued ? "排队 / 运行中" : "无待执行任务"]]) {
      dl.append(el("dt", "", label), el("dd", "", value ?? "—"));
    }
    fragment.append(dl, el("h3", "", "Sub2API 远端状态"), remoteStatus(account)); return fragment;
  }
  function refreshDetails() {
    const box = document.getElementById("account-health-content"); if (!box || !payload) return;
    const account = (payload.accounts || []).find(a => String(a.id) === box.dataset.account);
    if (!account) return;
    const current = contexts(account).find(c => String(c.workspace_id || "") === box.dataset.workspace) || account;
    box.replaceChildren(healthDetails(current));
  }
  function decorateDetails(account, body, trigger) {
    const advanced = el("details", "management-advanced"); advanced.append(el("summary", "", "身份、凭证与 Sub2API 详情"));
    while (body.firstChild) advanced.append(body.firstChild);
    if (account.subscription) {
      const section = el("section", "sheet-section"); section.append(el("h3", "", "订阅与席位"));
      const info = account.subscription;
      const facts = el("dl", "kv");
      for (const [label, value] of [["档位", subscriptionLabel(account) || "未识别"], ["来源", info.source || "未知"], ["观察时间", info.observed_at ? api.relativeTime(info.observed_at) : "尚未验证"], ["Sub2API 计划", account.plan_sync?.status === "success" ? "已同步" : "未验证，未写入"]]) {
        facts.append(el("dt", "", label), el("dd", "", value));
      }
      section.append(facts); body.append(section);
    }
    const block = el("section"); block.id = "account-health-content";
    block.dataset.account = account.id; block.dataset.workspace = account.workspace_id || ""; block.append(healthDetails(account));
    body.append(block);
    if (account.interrupted_operation_id) {
      const recovery = el("section", "sheet-section");
      recovery.append(el("h3", "", "入组中断"), el("p", "hint", "先核对任务与官方成员状态。继续时沿用此邮箱；不会自动领取新号或撤回已发出的邀请。"));
      recovery.append(button("查看中断任务", b => api.openOperationById(account.interrupted_operation_id,b)));
      const workspace = payload?.groups?.find(g=>String(g.id)===String(account.workspace_id));
      if (workspace) recovery.append(button("继续此邮箱", b => {
        api.openWorkspaceDetails(b,workspace);
        const form = document.querySelector('[data-team-action-forms] form textarea[name="email_line"]')?.closest('form');
        if (!form) return;
        form._toggle.click(); form.elements.email_line.value=account.email;
        if (["owner","member"].includes(account.official_role)) form.elements.role.value=account.official_role;
      }, "button primary"));
      body.append(recovery);
    }
    if (account.contexts?.length > 1) {
      const section = el("section", "sheet-section"); section.append(el("h3", "", "工作区检测状态"));
      account.contexts.forEach(c => section.append(button(`${c.workspace_name || c.workspace_id} · ${c.health.label}`, b => openAccount(c, b), "button management-context")));
      body.append(section);
    }
    const usage = fmt.usageWindow(account.usage);
    if (usage) {
      const section = el("section", "sheet-section"); section.append(el("h3", "", `Sub2API · ${usage.label}`));
      const dl = el("dl", "kv");
      for (const [label, key] of [["用户计费", "user_cost"], ["账号成本", "account_cost"], ["标准计费", "standard_cost"], ["计费毛差", "billing_margin"]]) {
        dl.append(el("dt", "", label), el("dd", "tabular", `${fmt.formatCost(usage[key])} · 原值 ${usage[key] ?? "未知"}`));
      }
      section.append(dl); body.append(section);
    }
    const actions = el("div", "management-detail-actions");
    for (const id of ["account.reauth", "account.quota"]) {
      const action = api.entityActions.account.find(a => a.id === id);
      if (action && (!action.visible || action.visible(account))) actions.append(button(action.label, b => action.run(account, b), id === "account.reauth" ? "button primary" : "button"));
    }
    actions.append(api.menuButton("account", account)); body.append(actions, advanced);
  }
  function decorateTeam(workspace, body) {
      // Managed accounts are integrated with the official member rows by renderTeamDetails.
    }
  function openSwitchCount(workspace, trigger) {
    const body = document.getElementById("sheet-body");
    document.getElementById("sheet-title").textContent = "校正今日切换次数";
    document.getElementById("sheet-subtitle").textContent = workspace.display_name || workspace.name;
    body.replaceChildren(el("p", "hint", "授权闭环可自动计数。这里只补记漏记的次数，不会执行轮转，也不会修改自动轮转日限。当前仅支持增加。"));
    body.append(window.Team48SwitchCount.widget(workspace, {
      increment: async () => {
        const result = await api.postAction(`switch-count:${workspace.id}`, `/api/workspaces/${workspace.id}/switch-count/increment`);
        if (!result?.ok) throw new Error(result?.error || "次数保存失败");
        const current = payload?.groups?.find(item => item.id === workspace.id);
        if (current) current.switch_count = result.switch_count;
        render(payload); return result.switch_count;
      },
      onError: error => api.toast(`计数未确认：${api.friendlyError(error)}，请刷新核对后再操作`, "error"),
    }));
    api.openOverlay("entity", {returnFocus:trigger, context:{kind:"switch-count"}, initialFocus:".workspace-switch-counter button"});
  }
  function registerLocal(trigger) {
    const body = document.getElementById("sheet-body"); body.replaceChildren();
    document.getElementById("sheet-title").textContent = "登记账号";
    document.getElementById("sheet-subtitle").textContent = "";
    const form = el("form", "stack");
    const emailLabel = el("label", "", "邮箱");
    const email = el("input"); email.name = "email"; email.type = "email"; email.required = true; email.maxLength = 255;
    emailLabel.append(email);
    const purposeLabel = el("label", "", "本地用途");
    const purpose = el("select"); purpose.name = "purpose"; purpose.append(new Option("待命", "standby"), new Option("子号", "child"));
    purposeLabel.append(purpose);
    const submit = el("button", "button primary", "登记账号"); submit.type = "submit";
    const errorBox = el("p", "text-warning"); errorBox.setAttribute("role", "alert");
    form.append(emailLabel, purposeLabel, errorBox, submit);
    form.addEventListener("submit", async event => {
      event.preventDefault(); submit.disabled = true;
      try {
        const result = await api.postAction(`register-account:${email.value.trim().toLowerCase()}`, "/api/accounts", {email: email.value, purpose: purpose.value});
        await boot(api); openAccount(result.account, trigger);
      } catch (error) { errorBox.textContent = api.friendlyError(error); } finally { submit.disabled = false; }
    });
    body.append(form);
    api.openOverlay("entity", {returnFocus: trigger, context: {kind: "registration"}, initialFocus: "input[name='email']"});
  }
  async function boot(bridge) {
    api = bridge;
    if (!bound) {
      bound = true;
      api.entityActions["workspace-card"] = [
        {...api.entityActions.workspace.find(action => action.id === "workspace.sync"), disabled: item => Boolean(item.sync_operation)},
        {id:"workspace.proxy", label:"切换代理", visible:item => Boolean(item.owner_account_id && item.owner_purpose === "mother"), run:(item,b) => api.openWorkspaceProxy(b,item)},
        {id:"workspace.expiry", label:"修改到期日期", run:(item,b) => api.openWorkspaceExpiry(b,item)},
        {id:"workspace.count", label:"校正今日切换次数", run:(item,b) => openSwitchCount(item,b)},
        {id:"workspace.delete-local", label:"移除本地记录", danger:true, run:(item,b) => api.deleteLocalTeam(item,b)},
      ];
      api.entityActions.registration = [
        {id: "registration.account", label: "登记账号", run: (_item, trigger) => registerLocal(trigger)},
        {id: "registration.team", label: "登记团队", run: (_item, trigger) => api.openRegister(trigger)},
      ];
      const register = api.menuButton("registration", {});
      register.className = "button primary"; register.textContent = "＋ 登记账号 / 团队";
      register.setAttribute("aria-label", "登记账号或团队");
      document.querySelector('.topbar-actions [data-open-register]')?.replaceWith(register);
      document.getElementById("accounts-search").value = query().get("q") || "";
      document.getElementById("accounts-search").addEventListener("input", event => { setQuery("q", event.target.value); render(payload); });
      document.querySelector("[data-filter='purpose']").addEventListener("change", event => { setQuery("purpose", event.target.value); render(payload); });
      document.getElementById("management-health").addEventListener("change", event => { setQuery("health", event.target.value); render(payload); });
      document.getElementById("management-remote")?.addEventListener("change", event => { setQuery("remote", event.target.value); render(payload); });
      document.getElementById("refresh-remote-status")?.addEventListener("click", async event => {
        const b = event.currentTarget; b.disabled = true;
        try {
          const result = await refreshRemote(true);
          api.toast(result.message || "Sub2API 状态已更新", result.ok ? "success" : "warning");
        } catch (error) { api.toast(api.friendlyError(error), "error"); }
        finally { b.disabled = false; }
      });
      document.getElementById("management-workspace").addEventListener("change", event => { setQuery("team", event.target.value); render(payload); });
      document.getElementById("accounts-clear-filters")?.addEventListener("click", () => clearFilters(FILTER_KEYS));
      document.querySelectorAll("[data-management-view]").forEach(b => b.addEventListener("click", () => { setQuery("view", b.dataset.managementView); render(payload); }));
      document.querySelectorAll("[data-management-health]").forEach(b => b.addEventListener("click", () => { clearFilters(FILTER_KEYS); setQuery("view", "attention"); setQuery("health", b.dataset.managementHealth); render(payload); }));
      document.getElementById("check-all-accounts").addEventListener("click", async event => {
        const b = event.currentTarget; b.disabled = true;
        try {
          const result = await api.postAction("quota:all", "/api/accounts/probe-all");
          for (const id of result.operation_ids || []) api.startCurrentOperation(`quota:${id}`, { operation_id: id, status: "queued" });
          api.toast(`已排队 ${result.queued} 个检查任务`, "success"); await boot(api);
        } catch (error) { api.toast(api.friendlyError(error), "error"); } finally { b.disabled = false; }
      });
      document.getElementById("account-selection-export")?.addEventListener("click", async event => {
        const b = event.currentTarget, items = selectedItems();
        if (!items.length || b.disabled) return;
        b.disabled = true;
        try {
          if (!await api.openConfirm({
            title: "导出 Codex 凭据", subtitle: `已选 ${items.length} 个账号`,
            message: "文件含敏感 AT 和 ID token，不含 RT。刷新仍由 Team Manager 负责；AT 到期后需重新导出或推送。",
            hint: "最多 50 个账号；不会自动刷新凭据。", items: items.map(a => a.email), confirmLabel: "确认导出",
          }, b)) return;
          const response = await fetch("/api/accounts/codex/export", {
            method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"},
            cache: "no-store", body: JSON.stringify({confirm: true, account_ids: items.map(a => a.id)}),
          });
          if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail?.message || "导出失败，请检查账号状态与选择数量");
          }
          const blob = await response.blob(), url = URL.createObjectURL(blob);
          const link = el("a"); link.href = url; link.download = "team48-codex-at-only.json";
          document.body.append(link); link.click(); link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          api.toast("已导出 Codex 凭据（不含 RT）", "success");
        } catch (error) { api.toast(api.friendlyError(error), "error"); }
        finally { b.disabled = false; }
      });
      document.getElementById("account-selection-clear")?.addEventListener("click", () => { selected.clear(); render(payload); });
      document.getElementById("account-selection-delete")?.addEventListener("click", async event => {
        const items = selectedItems();
        if (!items.length) return;
        const ok = await api.openConfirm({
          title: "永久删除本地档案",
          subtitle: `已选 ${items.length} 个账号`,
          message: "保存的凭据、额度快照和本地绑定将被删除，无法撤销。不会删除 ChatGPT、Sub2API 远端账号或 iCloud 别名。",
          hint: "只删 Team48 本地档案。一次最多 50 个。",
          items: items.map(account => account.email),
          confirmLabel: `确认删除 ${items.length} 个`,
        }, event.currentTarget);
        if (!ok) return;
        const result = await api.postAction("account-delete-local-batch", "/api/accounts/delete-local", {
          confirm: true, account_ids: items.map(account => account.id),
        });
        const deleted = new Set((result.deleted || []).map(item => item.account_id));
        deleted.forEach(id => selected.delete(id));
        const failed = result.failed || [];
        api.toast(result.message || (failed.length ? "部分账号未删除" : "已删除"), failed.length ? "warning" : "success");
        if (failed.length) api.toast(failed.map(item => item.message).join("；"), "error");
        await boot(api);
      });
    }
    if (!poller) poller = window.Team48Polling.createPoller({
      read: async signal => {
        const response = await fetch("/api/accounts/portfolio", { headers: { Accept: "application/json" }, signal, cache: "no-store" });
        if (!response.ok) throw new Error("账号状态暂时无法刷新");
        return response.json();
      },
      onData: acceptData,
      onError: error => {
        const status = document.getElementById("management-sync-status");
        status.hidden = false;
        status.textContent = `${api.friendlyError(error)}${lastUpdated ? `，上次更新于 ${lastUpdated}` : ""}`;
      },
      delay: data => data.groups.some(g => g.sync_operation) ? 2000 : 15000,
    });
    if (!remotePoller) remotePoller = window.Team48Polling.createPoller({
      read: () => refreshRemote(),
      onData: () => {},
      onError: () => {
        const node = document.getElementById("management-remote-status");
        if (node) node.textContent = "Sub2API 核对失败，将自动重试；显示的是上次状态";
      },
      delay: result => result.refreshing ? 2000 : 15000,
    });
    await poller.refresh();
    void remotePoller.refresh();
  }
  function acceptData(data) {
    lastUpdated = new Date().toLocaleTimeString("zh-CN");
    const status = document.getElementById("management-sync-status");
    status.hidden = !syncErrors;
    status.textContent = syncErrors ? `上次批量提交：${syncErrors}` : "";
    api.cache.portfolio = data; api.cache.items = data.accounts || []; api.cache.kind = "account";
    render(data);
    if (!deepLinked) {
      deepLinked = true; const params = query();
      const group = data.groups.find(g => String(g.id) === params.get("workspace"));
      const account = data.accounts.find(a => String(a.id) === params.get("account"));
      if (account) openAccount(group?.members.find(a => a.id === account.id) || account, document.getElementById("accounts-search"), group);
      else if (group) api.openWorkspaceDetails(document.getElementById("accounts-search"), group);
    }
  }
  const closeSelection = () => { setQuery("account", ""); setQuery("workspace", ""); };
  function updateExpiry(id, expiry) {
    const group = payload?.groups?.find(item => item.id === id);
    if (group) { group.expiry = expiry; render(payload); }
  }
  window.Team48Accounts = { boot, render, decorateDetails, decorateTeam, quota, memberAccount: (workspace, row) => (payload?.groups?.find(g => g.id === workspace.id)?.members || []).find(a => a.email?.toLowerCase() === row.email?.toLowerCase()), matches, viewName, filteredEntries, pageWindow, teamPageWindow, sortAccounts, activeFilters, closeSelection, subscriptionLabel, reportSyncErrors, updateExpiry, refreshRemote, refresh: () => poller?.refresh() };
})();
