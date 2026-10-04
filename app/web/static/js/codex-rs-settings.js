/* Settings for codex-rs: post-authorization push target, first-import defaults and auto refill. Groups come from the saved connection. */
(() => {
  let form, read, catalog = {groups: [], proxies: null}, workspaces = [];
  const REFILL_DEFAULTS = {enabled: false, target: 6, daily_limit: 6, workspace_ids: []};
  const NOT_WORKING_TEXT = {error: "出错", disabled: "已停用", weekly_exhausted: "7 天额度用完"};
  const field = name => form.elements.namedItem(name);
  const node = (tag, text) => { const n = document.createElement(tag); if (text) n.textContent = text; return n; };
  const hint = (text, tone = "") => { const n = node("p", text); n.className = `hint ${tone}`.trim(); return n; };
  const when = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value)
      : date.toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false});
  };
  // Returns the PATCH shape: an empty concurrency means "inherit the codex-rs global setting".
  function value() {
    const concurrency = String(field("codex_rs_concurrency").value || "").trim();
    return {
      push_target: form.querySelector('[name="codex_rs_push_target"]:checked')?.value === "codex_rs" ? "codex_rs" : "sub2api",
      group_ids: [...form.querySelectorAll('[name="codex_rs_group_id"]:checked')].map(n => n.value).sort(),
      ...(concurrency ? {concurrency_limit: Number(concurrency)} : {concurrency_inherit: true}),
      weight: Number(field("codex_rs_weight").value),
      enabled: field("codex_rs_enabled").checked,
      refill: {
        enabled: field("codex_rs_refill_enabled").checked,
        target: Number(field("codex_rs_refill_target").value),
        daily_limit: Number(field("codex_rs_refill_daily_limit").value),
        workspace_ids: refillWorkspaceIds(),
      },
    };
  }
  function refillWorkspaceIds() {
    return [...form.querySelectorAll('[name="codex_rs_refill_workspace"]:checked')].map(n => Number(n.value)).sort((a, b) => a - b);
  }
  function renderRefillWorkspaces(ids) {
    const box = document.getElementById("codex-rs-refill-workspace-list");
    box.replaceChildren();
    const rows = [...workspaces, ...ids.filter(id => !workspaces.some(row => row.id === id))
      .map(id => ({id, name: "已保存的团队（已不存在）", status: "missing"}))];
    for (const row of rows) {
      const label = node("label");
      const input = node("input"); input.type = "checkbox"; input.name = "codex_rs_refill_workspace"; input.value = String(row.id);
      input.checked = ids.includes(row.id);
      label.append(input, node("span", `${row.name} · #${row.id}${row.status && row.status !== "active" && row.status !== "missing" ? "（未激活，不会补进）" : ""}`));
      box.append(label);
    }
    if (!box.children.length) box.append(node("span", "还没有团队，请先登记团队。"));
  }
  function renderGroups(ids) {
    const box = document.getElementById("codex-rs-groups");
    box.replaceChildren();
    const rows = [...catalog.groups, ...ids.filter(id => !catalog.groups.some(row => String(row.id) === id))
      .map(id => ({id, name: `已保存的分组（${id}，待核对）`, enabled: true}))];
    for (const row of rows) {
      const label = node("label");
      const input = node("input"); input.type = "checkbox"; input.name = "codex_rs_group_id"; input.value = String(row.id);
      input.checked = ids.includes(String(row.id));
      label.append(input, node("span", `${row.name || row.id}${row.enabled === false ? "（已停用）" : ""}`));
      box.append(label);
    }
    if (!box.children.length) box.append(node("span", "暂无可选分组；不选则不指定分组"));
  }
  function renderProxies() {
    const status = document.getElementById("codex-rs-proxy-status");
    const proxies = catalog.proxies;
    if (!proxies) { status.textContent = "可用代理：未读取"; status.className = "hint"; return; }
    const available = Number(proxies.available) || 0, total = Number(proxies.total) || 0;
    status.textContent = available
      ? `可用代理 ${available} / ${total} 个（导入时自动挑挂号最少的）`
      : `▲ 可用代理 0 / ${total} 个：没有测试通过的代理，导入会失败`;
    status.className = available ? "hint" : "hint text-warn";
  }
  function fill(settings = {}, rotationWorkspaces = []) {
    const target = settings.push_target === "codex_rs" ? "codex_rs" : "sub2api";
    form.querySelectorAll('[name="codex_rs_push_target"]').forEach(input => { input.checked = input.value === target; });
    field("codex_rs_concurrency").value = settings.concurrency_limit ?? "";
    field("codex_rs_weight").value = settings.weight ?? 1;
    field("codex_rs_enabled").checked = settings.enabled !== false;
    renderGroups((settings.group_ids || []).map(String));
    const refill = {...REFILL_DEFAULTS, ...(settings.refill || {})};
    workspaces = Array.isArray(rotationWorkspaces) ? rotationWorkspaces : [];
    field("codex_rs_refill_enabled").checked = refill.enabled === true;
    field("codex_rs_refill_target").value = refill.target ?? REFILL_DEFAULTS.target;
    field("codex_rs_refill_daily_limit").value = refill.daily_limit ?? REFILL_DEFAULTS.daily_limit;
    renderRefillWorkspaces((refill.workspace_ids || []).map(Number));
  }
  // Read-only status block from GET /api/codex-rs/refill.
  function renderRefill(data) {
    const box = document.getElementById("codex-rs-refill-status");
    if (!box) return;
    box.replaceChildren();
    const config = data.config || {}, state = data.state || {};
    const pool = Number(data.pool_available) || 0;
    const working = state.working ?? "—", total = state.total ?? "—";
    const head = hint(`能干活 ${working} / 目标 ${config.target ?? "—"}（codex-rs 共 ${total} 个）· 上次检查 ${when(state.checked_at)}${data.stale ? " · ▲ 检查已过期" : ""}`,
      data.stale ? "text-warn" : "");
    box.append(head);
    box.append(hint(pool < 2 ? `▲ 号池可拉入 ${pool} 个` : `号池可拉入 ${pool} 个`, pool < 2 ? "text-warn" : ""));
    if (config.enabled === false) box.append(hint("○ 自动补号未开启"));
    const today = state.today || {};
    if (config.enabled) box.append(hint(`今日已补 ${today.count ?? 0} / ${config.daily_limit ?? "—"} 个`));
    if (state.paused) {
      const row = node("div"); row.className = "codex-rs-refill-paused";
      const text = hint(`■ 已暂停：${state.pause_reason || "原因未记录"}`, "text-warn");
      const button = node("button", "恢复"); button.type = "button"; button.className = "button compact";
      button.addEventListener("click", () => resume(button));
      row.append(text, button); box.append(row);
    }
    if (state.blocked_reason) box.append(hint(`本轮未补号：${state.blocked_reason}`, "text-warn"));
    if (state.ok === false && state.error) box.append(hint(String(state.error), "error"));
    if (state.last_result) box.append(hint(`上次补号结果：${state.last_result}`));
    if (state.last_operation_id) {
      const line = hint("上一个补号任务：");
      const link = node("a", String(state.last_operation_id));
      link.href = `/operations?op=${encodeURIComponent(state.last_operation_id)}`;
      line.append(link); box.append(line);
    }
    const notWorking = Array.isArray(state.not_working) ? state.not_working : [];
    if (notWorking.length) {
      box.append(hint(`不算能干活的号（${notWorking.length}）`));
      const list = node("ul"); list.className = "codex-rs-refill-list";
      for (const item of notWorking) {
        list.append(node("li", `${item.email || item.remote_id || "—"} · ${NOT_WORKING_TEXT[item.reason] || item.reason || "—"}${item.since ? ` · 自 ${when(item.since)}` : ""}`));
      }
      box.append(list);
    }
    const disabled = Array.isArray(state.disabled_by_refill) ? [...state.disabled_by_refill].reverse() : [];
    if (disabled.length) {
      box.append(hint(`最近自动关调度的号（${disabled.length}）`));
      const list = node("ul"); list.className = "codex-rs-refill-list";
      for (const item of disabled) {
        const li = node("li", `${item.email || item.remote_id || "—"} · ${when(item.at)} · `);
        const result = node("span", item.ok ? "● 成功" : `失败${item.error ? `：${item.error}` : ""}`);
        result.className = item.ok ? "text-ok" : "error";
        li.append(result); list.append(li);
      }
      box.append(list);
    }
  }
  async function refreshRefill() {
    const box = document.getElementById("codex-rs-refill-status"), button = document.getElementById("codex-rs-refill-refresh");
    if (!box || button?.disabled) return;
    if (button) button.disabled = true;
    try {
      renderRefill(await read("codex-rs-refill", "/api/codex-rs/refill"));
    } catch (error) {
      if (error?.name === "AbortError") return;
      box.replaceChildren(hint("暂时无法读取自动补号状态，请重试。", "error"));
    } finally { if (button) button.disabled = false; }
  }
  async function resume(button) {
    button.disabled = true;
    try {
      renderRefill(await read("codex-rs-refill-resume", "/api/codex-rs/refill/resume", {method: "POST"}));
      window.Team48?.toast?.("自动补号已恢复", "success");
    } catch (error) {
      button.disabled = false;
      window.Team48?.toast?.(window.Team48?.friendlyError?.(error) || "恢复失败，请重试", "error");
    }
  }
  async function refresh() {
    const button = document.getElementById("codex-rs-options-refresh"), status = document.getElementById("codex-rs-options-status");
    if (!button || button.disabled) return;
    button.disabled = true; status.textContent = "读取已保存连接的分组和代理中…"; status.className = "hint";
    try {
      const data = await read("codex-rs-options", "/api/codex-rs/options");
      const draft = value().group_ids;
      catalog = {groups: data.groups || [], proxies: data.proxies || null};
      renderGroups(draft);
      renderProxies();
      const errors = [...new Set(Object.values(data.errors || {}))];
      status.textContent = errors.length ? errors.join("；") : "分组已更新。修改连接地址后请先保存，再刷新列表。";
      status.className = errors.length ? "hint error" : "hint";
    } catch (_) {
      status.textContent = "暂时无法读取 codex-rs 分组和代理，已保留当前选择，请重试。"; status.className = "hint error";
    } finally { button.disabled = false; }
  }
  function init(settingsForm, fetchEntity) {
    form = settingsForm; read = fetchEntity;
    if (!form || form.dataset.codexRsBound) return;
    form.dataset.codexRsBound = "true";
    document.getElementById("codex-rs-options-refresh")?.addEventListener("click", refresh);
    document.getElementById("codex-rs-refill-refresh")?.addEventListener("click", refreshRefill);
    for (const [id, checked] of [["codex-rs-refill-select-all", true], ["codex-rs-refill-clear", false]]) {
      document.getElementById(id)?.addEventListener("click", () => {
        form.querySelectorAll('[name="codex_rs_refill_workspace"]').forEach(input => { input.checked = checked; });
        form.dispatchEvent(new Event("change", {bubbles: true}));
      });
    }
  }
  window.Team48CodexRs = {init, fill, value, refresh, refreshRefill};
})();
