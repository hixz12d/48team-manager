/* Settings for codex-rs: post-authorization push target and first-import defaults. Groups come from the saved connection. */
(() => {
  let form, read, catalog = {groups: [], proxies: null};
  const field = name => form.elements.namedItem(name);
  const node = (tag, text) => { const n = document.createElement(tag); if (text) n.textContent = text; return n; };
  // Returns the PATCH shape: an empty concurrency means "inherit the codex-rs global setting".
  function value() {
    const concurrency = String(field("codex_rs_concurrency").value || "").trim();
    return {
      push_target: form.querySelector('[name="codex_rs_push_target"]:checked')?.value === "codex_rs" ? "codex_rs" : "sub2api",
      group_ids: [...form.querySelectorAll('[name="codex_rs_group_id"]:checked')].map(n => n.value).sort(),
      ...(concurrency ? {concurrency_limit: Number(concurrency)} : {concurrency_inherit: true}),
      weight: Number(field("codex_rs_weight").value),
      enabled: field("codex_rs_enabled").checked,
    };
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
  function fill(settings = {}) {
    const target = settings.push_target === "codex_rs" ? "codex_rs" : "sub2api";
    form.querySelectorAll('[name="codex_rs_push_target"]').forEach(input => { input.checked = input.value === target; });
    field("codex_rs_concurrency").value = settings.concurrency_limit ?? "";
    field("codex_rs_weight").value = settings.weight ?? 1;
    field("codex_rs_enabled").checked = settings.enabled !== false;
    renderGroups((settings.group_ids || []).map(String));
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
  }
  window.Team48CodexRs = {init, fill, value, refresh};
})();
