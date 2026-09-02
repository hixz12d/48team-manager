(() => {
  const controllers = new Map();
  const palette = document.getElementById("command-palette");
  const commandInput = document.getElementById("command-input");
  const commandList = document.getElementById("command-list");
  const drawer = document.getElementById("operations-drawer");
  const sheet = document.getElementById("entity-sheet");
  const menu = document.getElementById("action-menu");
  const registerSheet = document.getElementById("register-sheet");
  const phoneImportSheet = document.getElementById("phone-import-sheet");
  const proxyAddSheet = document.getElementById("proxy-add-sheet");
  const onboardSheet = document.getElementById("onboard-sheet");
  const rotateSheet = document.getElementById("rotate-sheet");
  const proxyEditSheet = document.getElementById("proxy-edit-sheet");
  let focusTrapRoot = null;
  const SECRET_MASK = "••••••";
  const pageCache = { items: [], kind: "" };
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
    auth_probe: "授权探测",
    reconcile: "对账",
    sub2api_sync: "Sub2API 同步",
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

  function emptyState(title, detail) {
    const wrap = document.createElement("div");
    wrap.className = "empty-state";
    const strong = document.createElement("strong");
    strong.textContent = title;
    const p = document.createElement("p");
    p.textContent = detail;
    wrap.append(strong, p);
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
    button.setAttribute("aria-label", "更多操作");
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

  function workspaceRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "workspace", item);
    const seats = item.seat_limit ? `${item.members} / ${item.seat_limit}` : String(item.members ?? "—");
    cell(row, twoLine(item.name, shortId(item.official_workspace_id)));
    cell(row, item.owner_email);
    cell(row, seats, "num");
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
        toast(result.ok ? "官方成员同步已完成" : (result.error || "同步失败"), result.ok ? "success" : "error");
        if (result.operation_id) await openOperationById(result.operation_id);
        await bootPage();
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        sync.disabled = false;
      }
    });
    actions.append(sync, menuButton("workspace", item));
    cell(row, actions, "actions");
    return row;
  }

  function quotaCell(item) {
    const wrap = document.createElement("div");
    wrap.className = "cell-main";
    const quota = item.quota || {};
    const five = quota.five_hour_used_percent;
    const seven = quota.seven_day_used_percent;
    const line1 = document.createElement("span");
    line1.className = "tabular";
    line1.textContent = five == null ? "5h  —" : `5h  ${five}%`;
    const line2 = document.createElement("span");
    line2.className = "cell-sub tabular";
    line2.textContent = seven == null ? "7d  —" : `7d  ${seven}% · ${relativeTime(quota.queried_at)}`;
    if (quota.queried_at) line2.title = quota.queried_at;
    wrap.append(line1, line2);
    return wrap;
  }

  function accountPrimaryAction(item) {
    if (["oauth_required", "manual_required", "deactivated", "phone_required", "refresh_due"].includes(item.auth)) {
      return { id: "account.reauth", label: "重新授权", url: `/api/accounts/${item.id}/reauth` };
    }
    if (item.quota?.seven_day_used_percent == null || item.quota?.success === false) {
      return { id: "account.quota", label: "刷新额度", url: `/api/accounts/${item.id}/quota/probe` };
    }
    if (["missing", "unbound", "none"].includes(item.sub2api)) {
      return { id: "account.sub2api", label: "同步 Sub2API", url: `/api/accounts/${item.id}/sub2api/sync` };
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
      button.disabled = true;
      try {
        const result = await postAction(`${primary.id}-${item.id}`, primary.url);
        toast(result.ok || result.success ? `${primary.label}已提交` : (result.error || "操作失败"), result.ok || result.success ? "success" : "error");
        if (result.operation_id) await openOperationById(result.operation_id);
        await bootPage();
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
    cell(row, statusNode(item.state || item.status, labelOf(statusLabels, item.state || item.status)));
    cell(row, labelOf(statusLabels, item.operation));
    cell(row, item.target || item.email);
    cell(row, item.workspace || "—");
    cell(row, item.current_step || "—");
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
          toast(result.ok ? "标签重试已提交" : (result.error || "重试失败"), result.ok ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
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
        toast(result.ok ? `检测成功 ${result.last_exit_ip || ""}`.trim() : (result.error || "检测失败"), result.ok ? "success" : "error");
        if (result.operation_id) await openOperationById(result.operation_id);
        await bootPage();
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        probe.disabled = false;
      }
    });
    actions.append(probe, menuButton("proxy", item));
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
    attentionRoot.replaceChildren();
    const attention = payload.attention || [];
    if (!attention.length) {
      attentionRoot.append(
        emptyState("当前没有需要人工处理的事项", "摘要条、运行任务和工作区健康会继续显示。")
      );
    } else {
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
    runningRoot.replaceChildren();
    const running = payload.running_operations || [];
    if (!running.length) {
      runningRoot.append(emptyState("没有正在运行的任务", "有 active Operation 时会在这里显示最近 4 条。"));
    } else {
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
      healthRoot.append(emptyState("还没有工作区", "点右上角「登记团队」，用母号 OAuth 授权。"));
    } else {
      const list = document.createElement("div");
      list.className = "health-list";
      health.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const seats = item.seat_limit ? `${item.members}/${item.seat_limit} 席` : `${item.members} 席`;
        row.append(twoLine(item.name, seats), statusNode(item.health, labelOf(statusLabels, item.health)));
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
      const members = item.member_accounts || [];
      const memberLines = members.length
        ? members.map((member) => {
            const role = member.official_role || labelOf(purposeLabels, member.purpose) || "member";
            const state = labelOf(stateLabels, member.state) || member.state || "";
            const auth = labelOf(statusLabels, member.auth) || member.auth || "";
            return [member.email, [role, state, auth].filter(Boolean).join(" · ")];
          })
        : [["子号", "还没有子号。拉人或同步 membership 后会出现在这里。"]];
      body.append(
        kvSection("运行摘要", [
          ["健康", labelOf(statusLabels, item.health || item.status)],
          ["席位", item.seat_limit ? `${item.members} / ${item.seat_limit}` : item.members],
          ["官方 Workspace ID", item.official_workspace_id],
          ["最近同步", item.last_sync || "未同步"],
          ["自动化", item.automation_available ? labelOf(statusLabels, item.automation) : "未接入"],
          ["官方 7d", item.quota_available ? (item.quota || "—") : "未接入"],
        ]),
        kvSection("母号", [
          ["邮箱", item.owner_email],
          ["用途", labelOf(purposeLabels, item.owner_purpose)],
          ["授权", labelOf(statusLabels, item.owner_auth)],
          ["代理", item.owner_proxy || (item.owner_proxy_set ? "已设" : "未绑定")],
          ["代理档案", item.proxy_profile_id || "—"],
        ]),
        kvSection(members.length ? `子号（${members.length}）` : "子号", memberLines)
      );
    } else if (kind === "operation") {
      title.textContent = labelOf(statusLabels, item.operation);
      subtitle.textContent = item.target || item.email || item.id;
      body.append(
        kvSection("任务", [
          ["状态", labelOf(statusLabels, item.state || item.status)],
          ["当前步骤", item.current_step],
          ["对象", item.target || item.email],
          ["错误码", item.error_code],
          ["说明", item.error],
          ["开始", item.started],
          ["更新", item.updated],
          ["Operation ID", item.id],
        ])
      );
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
      { id: "workspace.view", label: "查看详情", run: (item, trigger) => openSheet("workspace", item, trigger) },
      {
        id: "workspace.sync",
        label: "同步官方成员",
        run: async (item) => {
          const result = await postAction(`workspace-sync-${item.id}`, `/api/workspaces/${item.id}/sync`);
          toast(result.ok ? "官方成员同步已完成" : (result.error || "同步失败"), result.ok ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
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
        id: "account.refresh",
        label: "刷新状态",
        run: async (item) => {
          const result = await postAction(`account-refresh-${item.id}`, `/api/accounts/${item.id}/refresh`);
          toast(result.ok || result.success ? "刷新已提交" : (result.error || "刷新失败"), result.ok || result.success ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
        },
      },
      {
        id: "account.auth",
        label: "检查授权",
        run: async (item) => {
          const result = await postAction(`account-auth-${item.id}`, `/api/accounts/${item.id}/auth/probe`);
          toast(result.ok || result.success ? "授权探测已提交" : (result.error || "探测失败"), result.ok || result.success ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
        },
      },
      {
        id: "account.quota",
        label: "刷新额度",
        run: async (item) => {
          const result = await postAction(`account-quota-${item.id}`, `/api/accounts/${item.id}/quota/probe`);
          toast(result.ok || result.success ? "额度刷新已提交" : (result.error || "刷新失败"), result.ok || result.success ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
        },
      },
      {
        id: "account.sub2api",
        label: "同步 Sub2API",
        run: async (item) => {
          const result = await postAction(`account-sub2api-${item.id}`, `/api/accounts/${item.id}/sub2api/sync`);
          toast(result.ok || result.success ? "Sub2API 同步已提交" : (result.error || "同步失败"), result.ok || result.success ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
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
        run: (item, trigger) => openOnboard(trigger, { id: item.workspace_id, name: item.workspace }),
      },
      {
        id: "account.rotate",
        label: "受控轮转",
        visible: (item) => item.purpose === "child" && Boolean(item.workspace_id),
        run: (item, trigger) => openRotate(trigger, { id: item.workspace_id, name: item.workspace }, item),
      },
      {
        id: "account.kick",
        label: "踢出待命",
        visible: (item) => item.purpose === "child" && Boolean(item.workspace_id),
        run: async (item) => {
          if (!confirmDanger(`确认把 ${item.email} 踢出并转入待命？这会改官方成员。`)) return;
          const result = await postAction(`account-kick-${item.id}`, `/api/workspaces/${item.workspace_id}/kick`, {
            email: item.email,
            reason: "console_kick",
          });
          toast(result.ok || result.success ? "踢人任务已提交" : (result.error || "踢人失败"), result.ok || result.success ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
        },
      },
      {
        id: "account.revoke",
        label: "撤回邀请",
        visible: (item) => item.purpose === "child" && Boolean(item.workspace_id) && item.membership_state === "invited",
        run: async (item) => {
          if (!confirmDanger(`确认撤回 ${item.email} 的邀请？`)) return;
          const result = await postAction(`account-revoke-${item.id}`, `/api/workspaces/${item.workspace_id}/revoke-invite`, {
            email: item.email,
          });
          toast(result.ok || result.success ? "撤回已提交" : (result.error || "撤回失败"), result.ok || result.success ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
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
        visible: (item) => ["queued", "running", "waiting"].includes(item.state || item.status) && !item.cancel_requested,
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
        visible: (item) => ["failed", "cancelled"].includes(item.state || item.status),
        run: async (item) => {
          const result = await postAction(`operation-retry-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/retry`);
          toast(result.ok ? "已创建重试任务" : (result.error || "不可重试"), result.ok ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
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
          toast(result.ok ? "标签重试已提交" : (result.error || "重试失败"), result.ok ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
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
        id: "proxy.probe",
        label: "检测",
        run: async (item) => {
          const result = await postAction(`proxy-probe-${item.id}`, `/api/resources/proxies/${item.id}/probe`);
          toast(result.ok ? `检测成功 ${result.last_exit_ip || ""}`.trim() : (result.error || "检测失败"), result.ok ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
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
    const actions = entityActions[kind] || [];
    actions.forEach((action) => {
      if (action.visible && !action.visible(item)) return;
      add(action.label, () => action.run(item, button));
    });
    if (!actions.length) add("查看详情", () => openSheet(kind, item, button));
    const rect = button.getBoundingClientRect();
    menu.hidden = false;
    menu.style.left = `${Math.min(rect.left, window.innerWidth - 200)}px`;
    menu.style.top = `${rect.bottom + 4}px`;
  }

  function closeMenu() {
    if (menu) menu.hidden = true;
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

  function openProxyEdit(trigger, account) {
    if (!proxyEditSheet) return;
    overlayReturn = trigger || document.activeElement;
    const form = document.getElementById("proxy-edit-form");
    form?.reset();
    if (form) {
      form.account_id.value = account.id || "";
      form.email.value = account.email || "";
      form.current_proxy.value = account.proxy_url || "";
      form.proxy.value = "";
      form.proxy_profile_id.value = account.proxy_profile_id || "";
      form.clear.checked = false;
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
      setFormStatus("onboard-status", result.ok || result.success ? "已提交" : (result.error || "失败"), result.ok || result.success ? "muted" : "error");
      toast(result.ok || result.success ? "创建子号任务已提交" : (result.error || "创建失败"), result.ok || result.success ? "success" : "error");
      if (result.operation_id) {
        closeOnboard();
        await openOperationById(result.operation_id);
      }
      await bootPage();
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
      setFormStatus("rotate-status", result.ok || result.success ? "已提交" : (result.error || "失败"), result.ok || result.success ? "muted" : "error");
      toast(result.ok || result.success ? "轮转任务已提交" : (result.error || "轮转失败"), result.ok || result.success ? "success" : "error");
      if (result.operation_id) {
        closeRotate();
        await openOperationById(result.operation_id);
      }
      await bootPage();
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
    const accountId = Number(form.account_id.value || 0);
    if (!accountId) return;
    if (button) button.disabled = true;
    setFormStatus("proxy-edit-status", "正在保存…", "muted");
    try {
      const body = {
        clear: Boolean(form.clear.checked),
        proxy: form.proxy.value || null,
        proxy_profile_id: form.proxy_profile_id.value ? Number(form.proxy_profile_id.value) : null,
      };
      const result = await patchAction(`account-proxy-${accountId}`, `/api/accounts/${accountId}/proxy`, body);
      setFormStatus("proxy-edit-status", result.ok ? "已保存" : (result.error || "失败"), result.ok ? "muted" : "error");
      toast(result.ok ? "母号代理已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
      if (result.ok) {
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
        paintList(kind);
      }, 200);
    });
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
    bindSearch(document.getElementById("accounts-search"), "account");
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

  async function bootOperations() {
    const payload = await fetchEntity("operation-list", "/api/operations");
    renderRows(
      "operations-body",
      payload.items || [],
      7,
      operationRow,
      emptyState("还没有任务", "创建额度刷新、重新授权或拉人后会显示在这里。")
    );
    const op = currentQuery().get("op");
    if (op) await openOperationById(op);
  }

  async function bootPhones() {
    const payload = await fetchEntity("phone-list", "/api/resources/phones");
    const items = payload.items || [];
    renderRows(
      "phones-body",
      items,
      7,
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


  document.getElementById("register-copy-link")?.addEventListener("click", async () => {
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
          toast(result.ok ? `HME 对账完成，差异 ${result.conflicts ?? 0}` : (result.error || "对账失败"), result.ok ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
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
          toast(result.ok ? `全部检测完成：健康 ${result.healthy}/${result.total}` : `检测完成：失败 ${result.failed}/${result.total}`, result.ok ? "success" : "error");
          if (result.operation_id) await openOperationById(result.operation_id);
          await bootPage();
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
    if (event.target === registerSheet) closeRegister();
  });
  phoneImportSheet?.addEventListener("click", (event) => {
    if (event.target === phoneImportSheet) closePhoneImport();
  });
  proxyAddSheet?.addEventListener("click", (event) => {
    if (event.target === proxyAddSheet) closeProxyAdd();
  });
  document.addEventListener("click", (event) => {
    if (menu && !menu.hidden && !event.target.closest("#action-menu, .actions")) closeMenu();
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
