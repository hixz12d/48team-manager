(() => {
  const activeRequests = new Map();
  const state = {
    board: null,
    resources: null,
    settings: null,
    page: "overview",
    resourceTab: "phones",
    selectedAccountId: null,
  };

  const $ = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function requestKey(kind, id) {
    return `${kind}:${id}`;
  }

  async function entityFetch(kind, id, url, options = {}) {
    const key = requestKey(kind, id);
    activeRequests.get(key)?.abort();
    const controller = new AbortController();
    activeRequests.set(key, controller);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal, headers: { Accept: "application/json", ...(options.headers || {}) } });
      if (activeRequests.get(key) !== controller) return null;
      const payload = await response.json();
      if (activeRequests.get(key) !== controller) return null;
      return { ok: response.ok, status: response.status, payload };
    } catch (err) {
      if (err?.name === "AbortError") return null;
      throw err;
    } finally {
      if (activeRequests.get(key) === controller) activeRequests.delete(key);
    }
  }

  function quotaText(account) {
    const quota = account.quota;
    if (!quota) return "—";
    if (!quota.success) return "官方失败";
    const seven = quota.seven_day_used_percent;
    return seven == null ? "—" : `${seven}%`;
  }

  function authText(account) {
    return account.auth_state || "unknown";
  }

  function bindingText(account) {
    return account.binding?.binding_state || "unbound";
  }


  function replaceAccount(payload) {
    if (!state.board) return;
    for (const group of state.board.workspaces || []) {
      const index = (group.accounts || []).findIndex((item) => item.id === payload.id);
      if (index >= 0) {
        group.accounts[index] = { ...group.accounts[index], ...payload };
        group.member_count = group.accounts.length;
        patchAccountRow(payload);
        patchGroupHeader(group);
        return;
      }
    }
  }

  function removeAccountRow(accountId) {
    if (!state.board) return;
    for (const group of state.board.workspaces || []) {
      const before = group.accounts.length;
      group.accounts = group.accounts.filter((item) => item.id !== accountId);
      if (group.accounts.length !== before) {
        group.member_count = group.accounts.length;
        document.getElementById(`account-${accountId}`)?.remove();
        patchGroupHeader(group);
      }
    }
    if (state.selectedAccountId === accountId) closeDrawer();
  }

  function patchGroupHeader(group) {
    const node = document.getElementById(`workspace-${group.id}`);
    if (!node) return;
    const count = node.querySelector("[data-seat]");
    if (count) count.textContent = `${group.member_count}${group.seat_limit ? "/" + group.seat_limit : ""}`;
  }

  function accountRowHtml(account) {
    return `<tr id="account-${account.id}" data-account-id="${account.id}">
      <td>${escapeHtml(account.email)}</td>
      <td>${escapeHtml(account.local_purpose)}</td>
      <td>${escapeHtml(quotaText(account))}</td>
      <td><span class="v2-pill ${escapeHtml(account.auth_state)}">${escapeHtml(authText(account))}</span></td>
      <td><span class="v2-pill ${escapeHtml(bindingText(account))}">${escapeHtml(bindingText(account))}</span></td>
    </tr>`;
  }

  function patchAccountRow(account) {
    const row = document.getElementById(`account-${account.id}`);
    if (!row) return;
    const busy = row.classList.contains("busy");
    row.outerHTML = accountRowHtml(account);
    const next = document.getElementById(`account-${account.id}`);
    if (busy && next) next.classList.add("busy");
  }

  function renderOverview() {
    const overview = state.board?.overview || {};
    const alerts = overview.alerts || [];
    $("page-overview").innerHTML = `
      <h2>Overview</h2>
      <div class="v2-stats">
        <div class="v2-stat"><span>需要授权</span><b>${overview.needs_auth || 0}</b></div>
        <div class="v2-stat"><span>周额度满</span><b>${overview.weekly_full || 0}</b></div>
        <div class="v2-stat"><span>Binding conflict</span><b>${overview.identity_conflict || 0}</b></div>
        <div class="v2-stat"><span>号码可用次数</span><b>${overview.phones_available || 0}</b></div>
        <div class="v2-stat"><span>失败任务</span><b>${overview.failed_operations || 0}</b></div>
      </div>
      <div class="v2-alerts">
        ${alerts.length ? alerts.map((item) => `<div class="v2-alert">${escapeHtml(item.email || item.operation_id || "")} · ${escapeHtml(item.detail || item.kind)}</div>`).join("") : `<div class="v2-muted">没有异常</div>`}
      </div>`;
  }

  function renderAccounts() {
    const groups = state.board?.workspaces || [];
    $("page-accounts").innerHTML = `<h2>Accounts</h2>` + groups.map((group) => `
      <section class="v2-group" id="workspace-${group.id}">
        <h3><span>${escapeHtml(group.name)}</span><span class="v2-muted" data-seat>${group.member_count}${group.seat_limit ? "/" + group.seat_limit : ""}</span></h3>
        <table>
          <thead><tr><th>账号</th><th>用途</th><th>7日</th><th>授权</th><th>Sub2API</th></tr></thead>
          <tbody>
            ${(group.accounts || []).map(accountRowHtml).join("") || `<tr><td colspan="5" class="v2-muted">空</td></tr>`}
          </tbody>
        </table>
      </section>`).join("");
  }

  function renderAutomation() {
    const ops = state.board?.operations || [];
    $("page-automation").innerHTML = `<h2>Automation</h2>
      <table>
        <thead><tr><th>状态</th><th>类型</th><th>邮箱</th><th>步骤</th><th>错误</th></tr></thead>
        <tbody>
          ${ops.map((op) => `<tr>
            <td><span class="v2-pill ${escapeHtml(op.state)}">${escapeHtml(op.state)}</span></td>
            <td>${escapeHtml(op.action)}</td>
            <td>${escapeHtml(op.email)}</td>
            <td>${escapeHtml(op.current_step || "")}</td>
            <td>${escapeHtml(op.error || "")}</td>
          </tr>`).join("") || `<tr><td colspan="5" class="v2-muted">没有任务</td></tr>`}
        </tbody>
      </table>`;
  }

  function renderTasks() {
    const ops = state.board?.operations || [];
    $("task-count").textContent = String(ops.length);
    $("task-list").innerHTML = ops.slice(0, 30).map((op) => `
      <div class="v2-task">
        <span class="v2-pill ${escapeHtml(op.state)}">${escapeHtml(op.state)}</span>
        ${escapeHtml(op.action)} ${escapeHtml(op.email || "")}
        <small>${escapeHtml(op.current_step || op.message || op.error || "")}</small>
      </div>`).join("") || `<div class="v2-muted">没有任务</div>`;
    document.body.classList.toggle("has-tasks", true);
  }

  function kv(label, value) {
    return `<div>${escapeHtml(label)}</div><div>${escapeHtml(value ?? "—")}</div>`;
  }

  function renderDrawer(account) {
    const quota = account.quota || {};
    const binding = account.binding || {};
    const proxy = account.proxy || {};
    const ops = account.operations || [];
    $("account-drawer").innerHTML = `
      <h3>${escapeHtml(account.email)}</h3>
      <div class="v2-kv">
        ${kv("Official Plan", account.official_plan)}
        ${kv("Workspace", account.workspace_name)}
        ${kv("Official Role", account.membership?.official_role)}
        ${kv("Local Purpose", account.local_purpose)}
        ${kv("5h", quota.five_hour_used_percent == null ? "—" : quota.five_hour_used_percent + "%")}
        ${kv("7d", quota.seven_day_used_percent == null ? "—" : quota.seven_day_used_percent + "%")}
        ${kv("Last official", quota.queried_at || "从未")}
        ${kv("Auth", account.auth_state)}
        ${kv("Token", account.has_access_token ? "已存" : "无")}
        ${kv("Refresh", account.has_refresh_token ? "已存" : "无")}
        ${kv("Binding ID", binding.remote_account_id)}
        ${kv("Binding", binding.binding_state)}
        ${kv("Proxy", proxy.name || proxy.host || "—")}
        ${kv("Exit IP", proxy.last_exit_ip)}
        ${kv("Version", account.version)}
      </div>
      <div class="v2-row">
        <button type="button" data-act="refresh">Refresh Official</button>
        <button type="button" data-act="close">关闭</button>
        <button type="button" class="danger" data-act="archive">归档</button>
      </div>
      <h4>Recent Operations</h4>
      ${(ops || []).map((op) => `<div class="v2-task"><span class="v2-pill ${escapeHtml(op.state)}">${escapeHtml(op.state)}</span> ${escapeHtml(op.action)} <small>${escapeHtml(op.current_step || "")}</small></div>`).join("") || `<div class="v2-muted">无</div>`}
    `;
    $("account-drawer").classList.remove("hidden");
    document.body.classList.add("has-drawer");
    $("account-drawer").querySelector("[data-act=close]").onclick = closeDrawer;
    $("account-drawer").querySelector("[data-act=refresh]").onclick = () => refreshOfficial(account.id);
    $("account-drawer").querySelector("[data-act=archive]").onclick = () => confirmDanger(`归档 ${account.email}？只改这一行。`, () => archiveAccount(account.id, account.version));
  }

  function closeDrawer() {
    state.selectedAccountId = null;
    $("account-drawer").classList.add("hidden");
    document.body.classList.remove("has-drawer");
  }

  async function openAccount(accountId) {
    state.selectedAccountId = accountId;
    const row = document.getElementById(`account-${accountId}`);
    if (row) row.classList.add("busy");
    const result = await entityFetch("account", accountId, `/admin/v2/api/accounts/${accountId}`);
    if (row) row.classList.remove("busy");
    if (!result?.ok) return;
    replaceAccount(result.payload);
    renderDrawer(result.payload);
  }

  async function refreshOfficial(accountId) {
    const row = document.getElementById(`account-${accountId}`);
    if (row) row.classList.add("busy");
    const result = await entityFetch("account", accountId, `/admin/v2/api/accounts/${accountId}/refresh-official`, { method: "POST" });
    if (row) row.classList.remove("busy");
    if (!result?.ok) return;
    replaceAccount(result.payload);
    if (state.selectedAccountId === accountId) renderDrawer(result.payload);
  }

  async function archiveAccount(accountId, version) {
    const result = await entityFetch("account", accountId, `/admin/v2/api/accounts/${accountId}/archive`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version }),
    });
    if (!result) return;
    if (result.status === 409) return;
    if (!result.ok) return;
    removeAccountRow(accountId);
  }

  function confirmDanger(text, onOk) {
    $("confirm-text").textContent = text;
    $("confirm-modal").classList.remove("hidden");
    const cancel = () => $("confirm-modal").classList.add("hidden");
    $("confirm-cancel").onclick = cancel;
    $("confirm-ok").onclick = () => {
      cancel();
      onOk();
    };
  }

  function renderResources() {
    const data = state.resources || { phones: { items: [], stats: {} }, hme: [], proxies: [] };
    const tab = state.resourceTab;
    $("page-resources").innerHTML = `
      <h2>Resources</h2>
      <div class="v2-tabs">
        <button data-tab="phones" class="${tab === "phones" ? "active" : ""}">Phones</button>
        <button data-tab="hme" class="${tab === "hme" ? "active" : ""}">HME</button>
        <button data-tab="proxies" class="${tab === "proxies" ? "active" : ""}">Proxies</button>
      </div>
      ${tab === "phones" ? renderPhones(data.phones) : ""}
      ${tab === "hme" ? renderHme(data.hme) : ""}
      ${tab === "proxies" ? renderProxies(data.proxies) : ""}
    `;
    $("page-resources").querySelectorAll("[data-tab]").forEach((btn) => {
      btn.onclick = () => {
        state.resourceTab = btn.dataset.tab;
        renderResources();
      };
    });
    $("page-resources").querySelectorAll("[data-phone]").forEach((row) => {
      row.onclick = () => loadPhoneAttempts(Number(row.dataset.phone));
    });
  }

  function renderPhones(bundle) {
    const items = bundle.items || [];
    return `<table>
      <thead><tr><th>号码</th><th>状态</th><th>成功</th><th>风险</th><th>租约</th><th>最后结果</th></tr></thead>
      <tbody>${items.map((row) => `<tr data-phone="${row.id}">
        <td>${escapeHtml(row.number)}</td>
        <td>${escapeHtml(row.status)}</td>
        <td>${escapeHtml(row.used_count)}/${escapeHtml(row.max_uses)}</td>
        <td>${escapeHtml(row.risk_count)}</td>
        <td>${escapeHtml(row.reserved_by || "")}</td>
        <td>${escapeHtml(row.last_error_type || "")}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="v2-muted">空</td></tr>`}</tbody>
    </table><div id="phone-attempts"></div>`;
  }

  function renderHme(items) {
    return `<table>
      <thead><tr><th>alias</th><th>local</th><th>label</th><th>lease/job</th><th>error</th></tr></thead>
      <tbody>${(items || []).map((row) => `<tr class="${row.conflict ? "busy" : ""}">
        <td>${escapeHtml(row.email)}</td>
        <td><span class="v2-pill ${escapeHtml(row.local_state)}">${escapeHtml(row.local_state)}</span></td>
        <td>${escapeHtml(row.label_desired)}${row.label_sync_pending ? " · pending" : ""}</td>
        <td>${escapeHtml(row.job_id)}</td>
        <td>${escapeHtml(row.last_error)}</td>
      </tr>`).join("") || `<tr><td colspan="5" class="v2-muted">空</td></tr>`}</tbody>
    </table>`;
  }

  function renderProxies(items) {
    return `<table>
      <thead><tr><th>name</th><th>region</th><th>exit IP</th><th>status</th><th>bound</th></tr></thead>
      <tbody>${(items || []).map((row) => `<tr>
        <td>${escapeHtml(row.name || row.host)}</td>
        <td>${escapeHtml(row.region)}</td>
        <td>${escapeHtml(row.last_exit_ip)}</td>
        <td>${escapeHtml(row.status)}</td>
        <td>${escapeHtml(row.bound_accounts)}</td>
      </tr>`).join("") || `<tr><td colspan="5" class="v2-muted">空</td></tr>`}</tbody>
    </table>`;
  }

  async function loadPhoneAttempts(phoneId) {
    const result = await entityFetch("phone", phoneId, `/admin/v2/api/resources/phones/${phoneId}/attempts`);
    const box = $("phone-attempts");
    if (!box || !result?.ok) return;
    box.innerHTML = `<h3>PhoneAttempt</h3>` + (result.payload.items || []).map((item) => `<div class="v2-task">${escapeHtml(item.result)} <small>${escapeHtml(item.provider_message || item.finished_at || "")}</small></div>`).join("");
  }

  function renderSettings() {
    const s = state.settings || {};
    $("page-settings").innerHTML = `
      <h2>Settings</h2>
      <form id="settings-form">
        <label>Sub2API URL</label>
        <input name="sub2api_base_url" value="${escapeHtml(s.sub2api_base_url || "")}">
        <label>Sub2API Admin API key</label>
        <input name="sub2api_api_key" placeholder="${escapeHtml(s.sub2api_api_key || "")}" autocomplete="off">
        <label>HME URL</label>
        <input name="hme_base_url" value="${escapeHtml(s.hme_base_url || "")}">
        <label>HME Token</label>
        <input name="hme_service_token" placeholder="${escapeHtml(s.hme_service_token || "")}" autocomplete="off">
        <label>HME Account</label>
        <input name="hme_account_id" value="${escapeHtml(s.hme_account_id || "")}">
        <label>OpenAI quota interval (min)</label>
        <input name="official_quota_probe_interval_minutes" type="number" value="${escapeHtml(s.official_quota_probe_interval_minutes || 60)}">
        <label>OpenAI quota stagger (min)</label>
        <input name="official_quota_probe_stagger_minutes" type="number" value="${escapeHtml(s.official_quota_probe_stagger_minutes || 60)}">
        <label><input type="checkbox" name="official_quota_probe_enabled" ${s.official_quota_probe_enabled ? "checked" : ""}> Official quota probe</label>
        <label><input type="checkbox" name="auto_reauth_enabled" ${s.auto_reauth_enabled ? "checked" : ""}> Auto Reauth</label>
        <label><input type="checkbox" name="auto_rotate_enabled" ${s.auto_rotate_enabled ? "checked" : ""}> Auto Rotate</label>
        <p class="v2-muted">强制补位始终关闭。空密钥表示不改。</p>
        <div class="v2-row"><button type="submit">保存</button></div>
      </form>`;
    $("settings-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = event.target;
      const body = {
        sub2api_base_url: form.sub2api_base_url.value,
        hme_base_url: form.hme_base_url.value,
        hme_account_id: form.hme_account_id.value,
        official_quota_probe_interval_minutes: Number(form.official_quota_probe_interval_minutes.value),
        official_quota_probe_stagger_minutes: Number(form.official_quota_probe_stagger_minutes.value),
        official_quota_probe_enabled: form.official_quota_probe_enabled.checked,
        auto_reauth_enabled: form.auto_reauth_enabled.checked,
        auto_rotate_enabled: form.auto_rotate_enabled.checked,
      };
      if (form.sub2api_api_key.value.trim()) body.sub2api_api_key = form.sub2api_api_key.value.trim();
      if (form.hme_service_token.value.trim()) body.hme_service_token = form.hme_service_token.value.trim();
      const result = await entityFetch("settings", "main", "/admin/v2/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (result?.ok) {
        state.settings = result.payload;
        renderSettings();
      }
    };
  }

  function showPage(name) {
    state.page = name;
    document.querySelectorAll(".v2-page").forEach((node) => node.classList.add("hidden"));
    $(`page-${name}`).classList.remove("hidden");
    document.querySelectorAll(".v2-nav button").forEach((btn) => btn.classList.toggle("active", btn.dataset.page === name));
    if (name === "resources" && !state.resources) loadResources();
    if (name === "settings" && !state.settings) loadSettings();
  }

  async function loadBoard() {
    const result = await entityFetch("board", "main", "/admin/v2/api/board");
    if (!result?.ok) return;
    state.board = result.payload;
    renderOverview();
    renderAccounts();
    renderAutomation();
    renderTasks();
  }

  async function loadResources() {
    const result = await entityFetch("resources", "main", "/admin/v2/api/resources");
    if (!result?.ok) return;
    state.resources = result.payload;
    renderResources();
  }

  async function loadSettings() {
    const result = await entityFetch("settings", "main", "/admin/v2/api/settings");
    if (!result?.ok) return;
    state.settings = result.payload;
    renderSettings();
  }

  document.querySelectorAll(".v2-nav button").forEach((btn) => {
    btn.onclick = () => showPage(btn.dataset.page);
  });

  $("page-accounts").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-account-id]");
    if (row) openAccount(Number(row.dataset.accountId));
  });

  loadBoard();
  setInterval(() => {
    entityFetch("operations", "drawer", "/admin/v2/api/operations?limit=40").then((result) => {
      if (!result?.ok || !state.board) return;
      state.board.operations = result.payload.items || [];
      renderTasks();
      if (state.page === "automation") renderAutomation();
    });
  }, 8000);
})();
