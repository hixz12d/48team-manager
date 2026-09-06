/* Unified management views. Actions, network ownership and overlays stay in app.js. */
(() => {
  const fmt = window.Team48Format;
  const purposeLabels = { mother: "母号", child: "子号", standby: "待命", free: "空闲", disabled: "停用" };
  const kindLabels = { invited: "待接受邀请", unmanaged: "官方已加入 · 未接入", history: "历史成员", unassigned: "未分配" };
  const retryCodes = new Set(["rate_limited", "temporary_failure", "parse_error"]);
  let api, payload, bound = false, deepLinked = false, refreshTimer;
  let collapsed = new Set();
  try { collapsed = new Set(JSON.parse(localStorage.getItem("team48:collapsed-teams") || "[]")); } catch (_) {}
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const query = () => new URLSearchParams(location.search);
  const viewName = value => ({ portfolio: "teams", flat: "all" }[value] || (["teams", "all", "unassigned", "attention"].includes(value) ? value : "teams"));
  function setQuery(key, value) {
    const params = query();
    if (!value || value === "all" && key !== "view") params.delete(key); else params.set(key, value);
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
  function status(account) {
    const h = account.health || { label: kindLabels[account.kind] || "尚未检测", severity: "muted" };
    return el("span", `management-badge tone-${h.severity}`, h.label);
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
    if (health === "auth") return any(c => c.health?.needs_auth);
    if (health === "401") return any(c => c.latest_check?.http_status === 401 && c.latest_check?.current_credential !== false);
    if (health === "retry") return any(c => retryCodes.has(c.health?.code));
    if (health !== "all") return any(c => c.health?.code === health);
    return true;
  }
  function quota(account) {
    const wrap = el("div", "management-quota");
    const data = account.last_success_quota || account.quota || {};
    if (account.contexts?.length > 1) return el("span", "muted", `${account.contexts.length} 个工作区 · 分别查看`);
    for (const [label, value] of [["5h", data.five_hour_used_percent], ["7d", data.seven_day_used_percent]]) {
      const line = el("div", "management-meter");
      line.append(el("span", "", label));
      const bar = el("div", `management-bar${data.stale ? " is-stale" : ""}`);
      if (value != null && Number.isFinite(Number(value))) {
        bar.setAttribute("role", "meter"); bar.setAttribute("aria-label", `${label} 已用额度${data.stale ? "，旧快照" : ""}`);
        bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100"); bar.setAttribute("aria-valuenow", String(value));
        const fill = el("span"); fill.style.width = `${Math.max(0, Math.min(100, Number(value)))}%`; bar.append(fill);
      }
      line.append(bar, el("span", "tabular", value == null ? "—" : `${value}%`)); wrap.append(line);
    }
    wrap.append(el("small", data.stale ? "text-warning" : "muted", data.queried_at ? `${data.stale ? "旧快照" : "成功"} · ${api.relativeTime(data.queried_at)}` : "无成功快照"));
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
    const identity = el("td");
    const identityWrap = el("div", "management-identity");
    const avatar = el("span", "management-avatar", (purposeLabels[account.purpose] || "外").slice(0, 1));
    const identityText = el("div");
    const canOpen = account.managed !== false && Boolean(account.id);
    const label = canOpen ? button(account.email || account.name, b => openAccount(account, b, group), "management-email", `account:${id}`) : el("span", "management-email", account.email || account.name || "未知账号");
    label.title = account.email || "";
    identityText.append(label, el("small", "muted", [purposeLabels[account.purpose], kindLabels[account.kind], !group ? account.workspace || account.workspace_name : null].filter(Boolean).join(" · ")));
    identityWrap.append(avatar, identityText); identity.append(identityWrap);
    const health = el("td"); health.append(status(account));
    if (account.queued) health.append(el("small", "muted", "检查已排队 / 运行中"));
    else if (account.latest_check?.current_credential === false) health.append(el("small", "muted", "上次检查属于旧凭证"));
    else if (account.latest_check?.state === "temporary_failure" && account.health?.code === "auth_required") health.append(el("small", "text-warning", "最近复查超时 / 暂时失败"));
    const quotaCell = el("td"); quotaCell.append(canOpen ? quota(account) : el("span", "muted", kindLabels[account.kind] || "尚未接入"));
    const money = el("td", "management-money tabular");
    const usage = fmt.usageWindow(account.usage);
    money.append(el("strong", "", fmt.formatCost(usage?.user_cost)), el("small", "muted", usage ? `${usage.label} · 成本 ${fmt.formatCost(usage.account_cost)}` : "无计费快照"));
    if (usage?.stale) money.append(el("small", "text-warning", "旧计费快照"));
    const time = el("td");
    time.append(el("span", "", account.latest_check?.checked_at ? api.relativeTime(account.latest_check.checked_at) : "未检查"));
    time.append(el("small", "muted", account.next_check_at ? `下次 ${api.relativeTime(account.next_check_at)}` : account.health?.needs_auth ? "等待授权" : "尚无计划"));
    const actionCell = el("td"); const actions = el("div", "management-actions");
    if (canOpen) {
      const primary = button(account.health?.needs_auth ? "授权" : "详情", b => {
        if (account.health?.needs_auth) return api.entityActions.account.find(a => a.id === "account.reauth")?.run(account, b);
        else openAccount(account, b, group);
      }, `button${account.health?.needs_auth ? " danger" : ""}`, `primary:${id}`);
      actions.append(primary, api.menuButton("account", account));
    } else if (group) actions.append(button("管理成员", b => api.openWorkspaceDetails(b, group), "button", `remote:${id}`));
    actionCell.append(actions); row.append(identity, health, quotaCell, money, time, actionCell);
    return row;
  }
  function table(items, group) {
    const scroll = el("div", "management-table-scroll"); scroll.tabIndex = 0; scroll.setAttribute("aria-label", "账号明细");
    scroll.dataset.scrollKey = group ? `team-${group.id}` : "all";
    const table = el("table", "management-table");
    const colgroup = el("colgroup"); [27, 17, 18, 14, 12, 12].forEach(width => { const col = el("col"); col.style.width = `${width}%`; colgroup.append(col); });
    const head = el("thead"); const tr = el("tr");
    ["账号 / 本地用途", "授权与检测", "官方额度 · 已用", "Sub2API 用户计费", "最近检查", "操作"].forEach((text, i) => { const th = el("th", i === 3 || i === 5 ? "num" : "", text); th.scope = "col"; tr.append(th); });
    head.append(tr); const body = el("tbody"); items.forEach(a => body.append(accountRow(a, group)));
    table.append(colgroup, head, body); scroll.append(table); return scroll;
  }
  function groupNode(group, items, params) {
    const section = el("section", "management-group");
    const header = el("header", "management-group-head");
    const closed = collapsed.has(String(group.id)) && !params.get("q");
    const toggle = button(closed ? "›" : "⌄", () => {
      const key = String(group.id); collapsed.has(key) ? collapsed.delete(key) : collapsed.add(key);
      try { localStorage.setItem("team48:collapsed-teams", JSON.stringify([...collapsed])); } catch (_) {}
      render(payload);
    }, "button ghost management-expand", `expand:${group.id}`);
    toggle.setAttribute("aria-expanded", String(!closed)); toggle.setAttribute("aria-label", `展开或收起 ${group.display_name || group.name}`);
    toggle.setAttribute("aria-controls", `management-team-${group.id}`);
    const title = el("div", "management-group-title");
    const top = el("div", "management-group-name"); top.append(el("h2", "", group.display_name || group.name || "未命名团队"));
    const counts = group.counts || {};
    if (counts.health_auth) top.append(el("span", "management-badge tone-error", `${counts.health_auth} 个需授权`));
    if (counts.health_retry) top.append(el("span", "management-badge tone-warning", `${counts.health_retry} 个待重试`));
    if (counts.invited) top.append(el("span", "management-badge tone-info", `${counts.invited} 个待邀请接受`));
    if (counts.unmanaged) top.append(el("span", "management-badge tone-warning", `${counts.unmanaged} 个未接入`));
    const seats = counts.joined_people == null ? "人数尚未同步" : `已加入 ${counts.joined_people} 人`;
    title.append(top, el("small", "muted", `${seats} · ${items.length} 条${params.get("q") ? "命中" : "记录"} · 成员同步 ${group.last_sync ? api.relativeTime(group.last_sync) : "尚未同步"}`));
    const usage = fmt.usageWindow(group.usage); const money = el("div", "management-group-money");
    if (usage) {
      money.append(el("small", "muted", `${usage.label} · Sub2API`), el("span", "tabular", `用户计费 ${fmt.formatCost(usage.user_cost)} / 成本 ${fmt.formatCost(usage.account_cost)}`));
      if (usage.stale || usage.coverage?.synced < usage.coverage?.total) money.append(el("small", "text-warning", `${usage.coverage ? `覆盖 ${usage.coverage.synced}/${usage.coverage.total}` : ""}${usage.stale ? " · 含旧快照" : ""}`));
    }
    header.append(toggle, el("span", "management-team-icon", String(group.display_name || group.name || "T").slice(0, 1)), title, money,
      button("管理团队", b => { setQuery("workspace", group.id); api.openWorkspaceDetails(b, group); }, "button", `team:${group.id}`));
    const body = el("div"); body.id = `management-team-${group.id}`; body.hidden = closed; body.append(table(items, group));
    section.append(header, body); return section;
  }
  function render(data) {
    if (!data) return;
    payload = data; const root = document.getElementById("accounts-portfolio"); if (!root) return;
    const params = query(), view = viewName(params.get("view"));
    const focusKey = document.activeElement?.dataset.focusKey;
    const scrolls = new Map([...root.querySelectorAll("[data-scroll-key]")].map(n => [n.dataset.scrollKey, n.scrollLeft]));
    const scrollY = window.scrollY;
    root.replaceChildren();
    document.querySelectorAll(".management-tabs [data-management-view]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.managementView === view)));
    for (const [key, value] of Object.entries(data.summary || {})) { const node = document.getElementById(`summary-${key}`); if (node) node.textContent = value; }
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
    document.querySelector("[data-filter='purpose']").value = params.get("purpose") || "all";
    let shown = 0;
    if (view === "teams") {
      for (const group of data.groups || []) {
        const items = (group.members || []).filter(a => matches(a, group, params));
        if (!items.length && ((group.members || []).length || params.get("q") || params.get("health"))) continue;
        if (params.get("team") && String(group.id) !== params.get("team")) continue;
        root.append(groupNode(group, items, params)); shown += items.length;
      }
      const free = (data.unassigned || []).filter(a => matches(a, null, params));
      if (free.length) { root.append(el("h2", "management-section-title", `未分配 · ${free.length}`), table(free)); shown += free.length; }
    } else {
      let items = view === "unassigned" ? data.unassigned || [] : data.accounts || [];
      if (params.get("purpose") === "archived") items = [...(data.groups || []).flatMap(g => g.history || []), ...(data.unassigned || []).filter(a => a.state === "archived")];
      items = items.filter(a => matches(a, null, params));
      if (view === "attention") items = items.filter(a => !["healthy", "disabled"].includes(a.health?.code));
      shown = items.length; if (shown) root.append(table(items));
    }
    if (!root.children.length) {
      const empty = el("div", "management-empty"); empty.append(el("h2", "", "没有匹配的记录"), button("清除筛选", () => {
        ["q", "purpose", "health", "team"].forEach(k => setQuery(k, "")); document.getElementById("accounts-search").value = ""; render(payload);
      })); root.append(empty);
    }
    document.getElementById("accounts-count").textContent = `${shown} 条${view === "teams" ? "关系记录" : "账号记录"}`;
    for (const node of root.querySelectorAll("[data-scroll-key]")) node.scrollLeft = scrolls.get(node.dataset.scrollKey) || 0;
    if (focusKey) [...root.querySelectorAll("[data-focus-key]")].find(n => n.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
    window.scrollTo({ top: scrollY });
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
    fragment.append(dl); return fragment;
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
    const block = el("section"); block.id = "account-health-content";
    block.dataset.account = account.id; block.dataset.workspace = account.workspace_id || ""; block.append(healthDetails(account));
    body.append(block);
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
      if (action && (!action.when || action.when(account))) actions.append(button(action.label, b => action.run(account, b), "button"));
    }
    actions.append(api.menuButton("account", account)); body.append(actions, advanced);
  }
  function decorateTeam(workspace, body) {
    const group = payload?.groups?.find(g => g.id === workspace.id); if (!group) return;
    const section = el("section", "sheet-section"); section.append(el("h3", "", "已管理账号"));
    for (const account of group.members || []) if (account.managed && account.kind !== "history") {
      section.append(button(`${account.email} · ${account.health?.label || "尚未检测"}`, b => openAccount(account, b, group), "button management-context"));
    }
    body.prepend(section);
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
      document.getElementById("management-workspace").addEventListener("change", event => { setQuery("team", event.target.value); render(payload); });
      document.querySelectorAll("[data-management-view]").forEach(b => b.addEventListener("click", () => { setQuery("view", b.dataset.managementView); render(payload); }));
      document.querySelectorAll("[data-management-health]").forEach(b => b.addEventListener("click", () => { setQuery("view", "all"); setQuery("health", b.dataset.managementHealth); render(payload); }));
      document.getElementById("check-all-accounts").addEventListener("click", async event => {
        const b = event.currentTarget; b.disabled = true;
        try {
          const result = await api.postAction("quota:all", "/api/accounts/probe-all");
          for (const id of result.operation_ids || []) api.startCurrentOperation(`quota:${id}`, { operation_id: id, status: "queued" });
          api.toast(`已排队 ${result.queued} 个检查任务`, "success"); await boot(api);
        } catch (error) { api.toast(api.friendlyError(error), "error"); } finally { b.disabled = false; }
      });
      refreshTimer = setInterval(() => {
        if (document.visibilityState === "visible" && document.getElementById("action-menu")?.hidden) boot(api).catch(error => api.showPageError(api.friendlyError(error)));
      }, 45000);
      window.addEventListener("pageshow", event => { if (event.persisted) boot(api).catch(error => api.showPageError(api.friendlyError(error))); });
    }
    const data = await api.fetchEntity("account-portfolio", "/api/accounts/portfolio");
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
  window.Team48Accounts = { boot, render, decorateDetails, decorateTeam, matches, viewName, closeSelection };
})();
