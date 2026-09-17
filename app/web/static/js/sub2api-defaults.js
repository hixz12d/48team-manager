/* Settings for Sub2API account creation. Remote proxy groups allocate on the server. */
(() => {
  let form, read, catalog = {groups: [], proxy_groups: [], proxies: []};
  const field = name => form.elements.namedItem(name);
  const node = (tag, text) => { const n = document.createElement(tag); if (text) n.textContent = text; return n; };
  function value() {
    const mode = field("sub2api_proxy_mode").value;
    return {
      concurrency: Number(field("sub2api_concurrency").value),
      group_ids: [...form.querySelectorAll('[name="sub2api_group_id"]:checked')].map(n => Number(n.value)).sort((a, b) => a - b),
      proxy_id: mode === "proxy" ? Number(field("sub2api_proxy_id").value) || null : null,
      proxy_group_id: mode === "group" ? Number(field("sub2api_proxy_group_id").value) || null : null,
    };
  }
  function modeChanged() {
    const mode = field("sub2api_proxy_mode").value;
    document.getElementById("sub2api-proxy-group-field").hidden = mode !== "group";
    document.getElementById("sub2api-proxy-field").hidden = mode !== "proxy";
    field("sub2api_proxy_group_id").required = mode === "group";
    field("sub2api_proxy_id").required = mode === "proxy";
  }
  function choices(items, ids, label) {
    return [...items, ...ids.filter(id => !items.some(row => row.id === id)).map(id => ({id, name: `已保存的${label}（${id}，待核对）`}))];
  }
  function renderChoices(defaults) {
    const groups = document.getElementById("sub2api-default-groups");
    groups.replaceChildren();
    for (const row of choices(catalog.groups, defaults.group_ids || [], "账号分组")) {
      const label = node("label");
      const input = node("input"); input.type = "checkbox"; input.name = "sub2api_group_id"; input.value = row.id;
      input.checked = (defaults.group_ids || []).includes(row.id);
      label.append(input, node("span", row.name)); groups.append(label);
    }
    if (!groups.children.length) groups.append(node("span", "暂无可选账号分组"));
    for (const [name, items, id, label] of [
      ["sub2api_proxy_group_id", catalog.proxy_groups, defaults.proxy_group_id, "代理分组"],
      ["sub2api_proxy_id", catalog.proxies, defaults.proxy_id, "代理"],
    ]) {
      const select = field(name); select.replaceChildren(new Option(`请选择${label}`, ""));
      for (const row of choices(items, id ? [id] : [], label)) {
        const detail = name === "sub2api_proxy_group_id" && row.available_proxy_count != null
          ? ` · ${row.available_proxy_count}/${row.proxy_count} 个代理可分配 · 每代理最多 ${row.max_accounts_per_proxy} 个账号` : "";
        select.add(new Option(`${row.name || row.host || "未命名代理"}${detail}`, row.id));
      }
      select.value = id || "";
    }
  }
  function fill(defaults = {}) {
    field("sub2api_concurrency").value = defaults.concurrency ?? 5;
    field("sub2api_proxy_mode").value = defaults.proxy_group_id ? "group" : defaults.proxy_id ? "proxy" : "none";
    renderChoices(defaults);
    modeChanged();
  }
  async function refresh() {
    const button = document.getElementById("sub2api-options-refresh"), status = document.getElementById("sub2api-options-status");
    if (button.disabled) return;
    button.disabled = true; status.textContent = "读取已保存连接的分组和代理中…";
    try {
      const data = await read("sub2api-push-options", "/api/sub2api/push-options");
      const draft = value();
      catalog = data;
      renderChoices(draft);
      const errors = [...new Set(Object.values(data.errors || {}))];
      status.textContent = errors.length ? errors.join("；") : "分组和代理已更新。修改连接地址后请先保存，再刷新列表。";
      status.className = errors.length ? "hint error" : "hint";
    } catch (_) {
      status.textContent = "暂时无法读取分组和代理，已保留当前选择，请重试。"; status.className = "hint error";
    } finally { button.disabled = false; }
  }
  function init(settingsForm, fetchEntity) {
    form = settingsForm; read = fetchEntity;
    if (form.dataset.defaultsBound) return;
    form.dataset.defaultsBound = "true";
    field("sub2api_proxy_mode").addEventListener("change", modeChanged);
    document.getElementById("sub2api-options-refresh").addEventListener("click", refresh);
  }
  window.Team48Sub2ApiDefaults = {init, fill, value, refresh};
})();
