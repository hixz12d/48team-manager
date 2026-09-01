(() => {
  const controllers = new Map();
  const palette = document.getElementById("command-palette");
  const commandInput = document.getElementById("command-input");
  const commandList = document.getElementById("command-list");
  const drawer = document.getElementById("operations-drawer");
  const SECRET_MASK = "••••••";

  const destinations = [
    { label: "去总览", href: "/" },
    { label: "去团队", href: "/workspaces" },
    { label: "去账号", href: "/accounts" },
    { label: "去任务", href: "/operations" },
    { label: "打开设置", href: "/settings" },
  ];

  const purposeLabels = {
    mother: "母号",
    child: "子号",
    standby: "待命",
    disabled: "停用",
  };

  const stateLabels = {
    active: "在用",
    available: "空闲",
    unused: "没用过",
    standby: "待命",
    conflict: "有冲突",
    archived: "已归档",
    unknown: "未知",
  };

  const statusLabels = {
    queued: "排队中",
    running: "进行中",
    waiting: "等着",
    success: "完成",
    failed: "失败",
    cancelled: "已取消",
    manual_required: "要人工看",
    pending: "待同步",
    verified: "已核对",
    missing: "对不上",
    unbound: "没绑",
    none: "没有",
    set: "有",
    off: "关着",
  };

  function abortEntity(key) {
    const previous = controllers.get(key);
    if (previous) previous.abort();
    const next = new AbortController();
    controllers.set(key, next);
    return next;
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
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return response.json();
  }

  function labelOf(map, value) {
    if (value == null || value === "") return "—";
    return map[value] || String(value);
  }

  function renderOverview(payload) {
    const root = document.getElementById("overview-attention");
    if (!root) return;
    if (!payload.attention || payload.attention.length === 0) {
      root.textContent = "暂时没什么异常";
      return;
    }
    root.replaceChildren();
    payload.attention.forEach((item) => {
      const row = document.createElement("div");
      row.textContent = item.message || item;
      root.append(row);
    });
  }

  function cell(row, text) {
    const td = document.createElement("td");
    td.textContent = text == null || text === "" ? "—" : String(text);
    row.append(td);
  }

  function renderRows(bodyId, items, emptyText, columns, renderItem) {
    const body = document.getElementById(bodyId);
    if (!body) return;
    body.replaceChildren();
    if (!items.length) {
      const row = document.createElement("tr");
      const empty = document.createElement("td");
      empty.colSpan = columns;
      empty.className = "muted";
      empty.textContent = emptyText;
      row.append(empty);
      body.append(row);
      return;
    }
    items.forEach((item) => body.append(renderItem(item)));
  }

  function workspaceRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.name);
    cell(row, item.owner_email);
    cell(row, item.members);
    cell(row, item.quota);
    cell(row, labelOf(statusLabels, item.rotation));
    cell(row, item.last_sync);
    cell(row, labelOf(stateLabels, item.status) === String(item.status) ? item.status : labelOf(stateLabels, item.status));
    return row;
  }

  function accountRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.email);
    cell(row, labelOf(purposeLabels, item.purpose));
    cell(row, item.workspace);
    cell(row, item.quota_7d);
    cell(row, item.auth);
    cell(row, labelOf(statusLabels, item.sub2api));
    cell(row, labelOf(statusLabels, item.proxy));
    cell(row, labelOf(stateLabels, item.state));
    return row;
  }

  function operationRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, labelOf(statusLabels, item.status));
    cell(row, item.operation);
    cell(row, item.target);
    cell(row, item.current_step);
    cell(row, item.started);
    cell(row, item.duration);
    return row;
  }

  function phoneRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.number);
    cell(row, item.status);
    cell(row, item.used_count);
    cell(row, item.remaining);
    cell(row, item.reserved_by);
    cell(row, item.last_error_type);
    return row;
  }

  function hmeRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.email);
    cell(row, item.state);
    cell(row, item.label);
    cell(row, item.pending ? "待同步" : "");
    cell(row, item.job_id);
    return row;
  }

  function proxyRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.name);
    cell(row, `${item.scheme}://${item.host}:${item.port}`);
    cell(row, item.status);
    cell(row, item.region);
    cell(row, item.last_exit_ip);
    return row;
  }

  function fillSettings(payload) {
    const form = document.getElementById("settings-form");
    if (!form) return;
    const connections = payload.connections || {};
    const automation = payload.automation || {};
    const resources = payload.resources || {};
    const secrets = payload.secrets || {};
    const account = payload.account || {};
    form.sub2api_base_url.value = connections.sub2api_base_url || "";
    form.sub2api_admin_email.value = connections.sub2api_admin_email || "";
    form.hme_base_url.value = connections.hme_base_url || "";
    form.hme_account_id.value = connections.hme_account_id || "";
    form.cf_mail_base_url.value = connections.cf_mail_base_url || "";
    form.cf_mail_address.value = connections.cf_mail_address || "";
    form.sub2api_api_key.value = secrets.sub2api_api_key || "";
    form.sub2api_admin_password.value = secrets.sub2api_admin_password || "";
    form.hme_service_token.value = secrets.hme_service_token || secrets.hme_token || "";
    form.cf_mail_admin_password.value = secrets.cf_mail_admin_password || "";
    form.official_quota_probe.checked = Boolean(automation.official_quota_probe);
    form.sms_max_uses_per_phone.value = resources.sms_max_uses_per_phone ?? "";
    form.sms_cooldown_sec.value = resources.sms_cooldown_sec ?? "";
    form.sms_reserve_sec.value = resources.sms_reserve_sec ?? "";
    const accountEl = document.getElementById("settings-account");
    if (accountEl) accountEl.textContent = account.username ? `当前登录账号：${account.username}` : "登录账号会显示在这里。";
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

  async function saveSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const statusEl = document.getElementById("settings-status");
    const oldPassword = String(form.old_password.value || "");
    const newPassword = String(form.new_password.value || "");
    const confirmPassword = String(form.confirm_password.value || "");
    const payload = {
      connections: {
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
      },
      automation: {
        official_quota_probe: form.official_quota_probe.checked,
      },
      resources: {
        sms_max_uses_per_phone: numberOrNull(form.sms_max_uses_per_phone.value),
        sms_cooldown_sec: numberOrNull(form.sms_cooldown_sec.value),
        sms_reserve_sec: numberOrNull(form.sms_reserve_sec.value),
      },
    };
    if (oldPassword || newPassword || confirmPassword) {
      payload.password = {
        old_password: oldPassword,
        new_password: newPassword,
        confirm_password: confirmPassword,
      };
    }
    try {
      const saved = await fetchEntity("settings-save", "/api/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
      fillSettings(saved);
      form.old_password.value = "";
      form.new_password.value = "";
      form.confirm_password.value = "";
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.className = "muted";
        statusEl.textContent = "已保存。";
      }
    } catch (error) {
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.className = "error";
        statusEl.textContent = error.message || "保存失败";
      }
    }
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

  function probeCard(title, ok, body) {
    const card = document.createElement("div");
    card.className = `probe-card ${ok ? "is-ok" : "is-bad"}`;
    const heading = document.createElement("strong");
    heading.textContent = `${title} · ${ok ? "通了" : "没通"}`;
    card.append(heading);
    if (typeof body === "string") {
      const p = document.createElement("p");
      p.className = ok ? "muted" : "error";
      p.textContent = body;
      card.append(p);
    } else if (body) {
      card.append(body);
    }
    return card;
  }

  function renderProbeResult(payload) {
    const box = document.getElementById("settings-probe-result");
    if (!box) return;
    box.hidden = false;
    box.replaceChildren();
    const sub = payload.sub2api || {};
    if (sub.ok) {
      const list = document.createElement("ul");
      (sub.groups || []).forEach((group) => {
        const item = document.createElement("li");
        const owners = (group.owners || []).join("、") || "还没看到母号";
        item.textContent = `${group.name} · ${owners}`;
        list.append(item);
      });
      if (!list.childElementCount) {
        const item = document.createElement("li");
        item.textContent = "一个分组都没有。";
        list.append(item);
      }
      const summary = document.createElement("p");
      summary.className = "muted";
      summary.textContent = `共 ${sub.group_count || 0} 个分组，${sub.account_count || 0} 个账号。` ;
      const wrap = document.createElement("div");
      wrap.append(summary, list);
      box.append(probeCard("Sub2API", true, wrap));
    } else {
      box.append(probeCard("Sub2API", false, sub.error || "连不上 Sub2API"));
    }
    const hme = payload.hme || {};
    if (hme.ok) {
      box.append(
        probeCard(
          "iCloud HME",
          true,
          `${hme.account_name || hme.account_id || "当前账号"} 一共 ${hme.alias_count || 0} 个邮箱，启用 ${hme.active_count || 0} 个，还能领 ${hme.unused_count || 0} 个。`
        )
      );
    } else {
      box.append(probeCard("iCloud HME", false, hme.error || "连不上 HME"));
    }
    const mail = payload.mail || {};
    if (mail.ok) {
      box.append(probeCard("临时邮箱", true, `${mail.address} 能读到信。`));
    } else {
      box.append(probeCard("临时邮箱", false, mail.error || "连不上临时邮箱"));
    }
  }

  async function probeSettings() {
    const form = document.getElementById("settings-form");
    const button = document.getElementById("settings-probe");
    const box = document.getElementById("settings-probe-result");
    if (!form || !box) return;
    if (button) button.disabled = true;
    box.hidden = false;
    box.replaceChildren();
    const pending = document.createElement("p");
    pending.className = "muted";
    pending.textContent = "正在检测…";
    box.append(pending);
    try {
      const payload = await fetchEntity("settings-probe", "/api/settings/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ connections: connectionsPayload(form) }),
      });
      renderProbeResult(payload);
    } catch (error) {
      box.replaceChildren();
      box.append(probeCard("检测", false, error.message || "检测失败"));
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function bootPage() {
    const page = document.body.dataset.page;
    try {
      if (page === "overview") {
        renderOverview(await fetchEntity("overview", "/api/overview"));
      } else if (page === "workspaces") {
        const payload = await fetchEntity("workspace-list", "/api/workspaces");
        renderRows("workspaces-body", payload.items || [], "还没有团队。", 7, workspaceRow);
      } else if (page === "accounts") {
        const filter = document.querySelector("[data-filter='purpose']");
        if (filter && !filter.dataset.bound) {
          filter.dataset.bound = "1";
          filter.addEventListener("change", () => bootPage());
        }
        const purpose = filter?.value || "all";
        const includeArchived = purpose === "archived";
        const payload = await fetchEntity(
          "account-list",
          `/api/accounts?purpose=${encodeURIComponent(purpose)}&include_archived=${includeArchived}`
        );
        renderRows("accounts-body", payload.items || [], "还没有账号。归档的默认不显示。", 8, accountRow);
      } else if (page === "operations") {
        const payload = await fetchEntity("operation-list", "/api/operations");
        renderRows("operations-body", payload.items || [], "还没有任务。", 6, operationRow);
      } else if (page === "phones") {
        const payload = await fetchEntity("phone-list", "/api/resources/phones");
        renderRows("phones-body", payload.items || [], "还没有手机号。", 6, phoneRow);
      } else if (page === "hme") {
        const payload = await fetchEntity("hme-list", "/api/resources/hme");
        renderRows("hme-body", payload.items || [], "还没有 HME 占用记录。", 5, hmeRow);
      } else if (page === "proxies") {
        const payload = await fetchEntity("proxy-list", "/api/resources/proxies");
        renderRows("proxies-body", payload.items || [], "还没有代理。", 5, proxyRow);
      } else if (page === "settings") {
        const form = document.getElementById("settings-form");
        if (form && !form.dataset.bound) {
          form.dataset.bound = "1";
          form.addEventListener("submit", saveSettings);
          document.getElementById("settings-probe")?.addEventListener("click", probeSettings);
        }
        fillSettings(await fetchEntity("settings", "/api/settings"));
      }
    } catch (error) {
      if (error.name !== "AbortError") console.warn(error);
    }
  }

  function openDrawer() {
    drawer.hidden = false;
  }

  function closeDrawer() {
    drawer.hidden = true;
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
  drawer?.addEventListener("click", (event) => {
    if (event.target === drawer) closeDrawer();
  });

  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      palette.showModal();
      renderPalette("");
      commandInput.focus();
    }
    if (event.key === "Escape") {
      closeDrawer();
    }
  });
  commandInput?.addEventListener("input", () => renderPalette(commandInput.value));

  window.Team48 = { abortEntity, fetchEntity };
  bootPage();
})();
