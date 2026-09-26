/* Overview team board: per team, per account — Sub2API presence, last authorization, last push, today's switches. */
(() => {
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const REMOTE = {
    healthy: ["在 Sub2API", "success"], paused: ["已暂停调度", "warning"], missing: ["远端已删除", "error"],
    unbound: ["不在 Sub2API", "muted"], auth_error: ["远端授权异常", "error"], error: ["远端异常", "error"],
    rate_limited: ["远端限流", "warning"], temporary_hold: ["暂不可调度", "warning"], binding_review: ["绑定待核对", "warning"],
    identity_mismatch: ["远端身份不一致", "error"], identity_unconfirmed: ["远端身份待核对", "warning"], unknown: ["待核对", "muted"],
  };
  const FILTERS = [["all", "全部"], ["missing", "不在 Sub2API"], ["problem", "远端异常"], ["stale", "超过 7 天未授权"]];
  const DAY = 86400000;
  let filter = "all", data = null, root = null, api = {};

  function relative(value) {
    if (!value) return "";
    const ms = Date.now() - Date.parse(value);
    if (!Number.isFinite(ms)) return "";
    if (ms < 60000) return "刚刚";
    if (ms < 3600000) return `${Math.floor(ms / 60000)} 分钟前`;
    if (ms < DAY) return `${Math.floor(ms / 3600000)} 小时前`;
    if (ms < 30 * DAY) return `${Math.floor(ms / DAY)} 天前`;
    return new Date(value).toLocaleDateString("zh-CN");
  }
  function timeCell(value, emptyText) {
    const td = el("td", "board-time");
    if (!value) { td.append(el("span", "muted", emptyText)); return td; }
    const t = el("time", "", relative(value)); t.dateTime = value;
    t.title = new Date(value).toLocaleString("zh-CN", { hour12: false });
    if (Date.now() - Date.parse(value) > 7 * DAY) t.className = "text-warning";
    td.append(t); return td;
  }
  function remoteOf(account) {
    const r = account.remote_status || {};
    if (data?.sub2api_status?.configured === false) return { key: "not_configured", label: "未配置", tone: "muted", r };
    if (r.state === "multiple") return { key: "binding_review", label: "多个远端绑定", tone: "warning", r };
    const failed = Boolean(r.error_code);
    const key = failed ? (r.last_known_state || "unknown") : (r.state || "unbound");
    const [label, tone] = REMOTE[key] || REMOTE.unknown;
    return { key, label: failed ? `${label}（上次）` : label, tone: failed ? "warning" : tone, r, failed };
  }
  function members(group) {
    const list = [];
    if (group.mother) list.push({ ...group.mother, board_role: "母号" });
    for (const a of group.current_children || []) list.push({ ...a, board_role: "子号" });
    for (const a of group.invited || []) list.push({ ...a, board_role: "邀请中" });
    return list;
  }
  function matches(account) {
    const remote = remoteOf(account);
    if (filter === "missing") return ["unbound", "missing"].includes(remote.key);
    if (filter === "problem") return remote.tone === "error" || remote.tone === "warning";
    if (filter === "stale") return account.board_role !== "邀请中" && (!account.last_authorized_at || Date.now() - Date.parse(account.last_authorized_at) > 7 * DAY);
    return true;
  }
  function counts(list) {
    const inSub = list.filter(a => remoteOf(a).key === "healthy").length;
    const out = list.filter(a => ["unbound", "missing"].includes(remoteOf(a).key)).length;
    return { inSub, out };
  }
  function row(account) {
    const tr = el("tr"); tr.dataset.account = String(account.id || "");
    const who = el("td", "board-email");
    const link = el("a", "", account.email || "—"); link.href = account.id ? `/accounts?account=${encodeURIComponent(account.id)}` : "/accounts";
    who.append(link, el("span", "board-role", account.board_role));
    const remote = remoteOf(account);
    const remoteTd = el("td");
    const badge = el("span", `management-badge tone-${remote.tone}`, remote.label);
    badge.dataset.remoteState = remote.key;
    badge.title = [remote.r.remote_id ? `远端 #${remote.r.remote_id}` : "", remote.r.label, remote.r.checked_at ? `核对于 ${new Date(remote.r.checked_at).toLocaleString("zh-CN", { hour12: false })}` : "尚未核对"].filter(Boolean).join(" · ");
    remoteTd.append(badge);
    const auth = el("td");
    const needs = account.health?.needs_auth || account.needs_auth;
    auth.append(el("span", `management-badge tone-${needs ? "warning" : "muted"}`, needs ? "需授权" : "有效"));
    tr.append(who, remoteTd, auth, timeCell(account.last_authorized_at, "无记录"), timeCell(remote.r.pushed_at, remote.key === "unbound" ? "未推送" : "无记录"));
    return tr;
  }
  function team(group) {
    const all = members(group), shown = all.filter(matches);
    if (filter !== "all" && !shown.length) return null;
    const section = el("section", "board-team"); section.dataset.workspace = String(group.id);
    const head = el("header", "board-team-head");
    const title = el("h2", "", group.display_name || group.name || `团队 ${group.id}`);
    const meta = el("p", "board-team-meta");
    const seats = group.occupied_seats != null && group.seat_limit ? `${group.occupied_seats}/${group.seat_limit} 席` : "席位未同步";
    const switches = window.Team48SwitchCount?.countForToday?.(group.switch_count) ?? 0;
    const expiry = window.Team48Expiry?.summary?.(group.expiry);
    const c = counts(all);
    const parts = [seats, `今日切换 ${switches} 次`, `在 Sub2API ${c.inSub}/${all.length}`];
    if (expiry?.date) parts.push(`到期 ${expiry.label}`);
    meta.textContent = parts.join(" · ");
    if (c.out) meta.append(el("span", "text-warning", ` · ${c.out} 个不在 Sub2API`));
    const manage = el("a", "button compact", "管理团队"); manage.href = `/accounts?view=teams&workspace=${encodeURIComponent(group.id)}`;
    head.append(el("div", "", null), manage); head.firstChild.append(title, meta);
    const table = el("table", "board-table");
    const thead = el("thead"); const hr = el("tr");
    for (const text of ["账号", "Sub2API", "授权", "上次授权", "上次推送"]) { const th = el("th", "", text); th.scope = "col"; hr.append(th); }
    thead.append(hr);
    const tbody = el("tbody");
    shown.forEach(a => tbody.append(row(a)));
    if (!shown.length) { const tr = el("tr"); const td = el("td", "muted", "这个团队还没有接入的账号"); td.colSpan = 5; tr.append(td); tbody.append(tr); }
    table.append(thead, tbody);
    const scroll = el("div", "board-table-scroll"); scroll.append(table);
    section.append(head, scroll);
    return section;
  }
  function render() {
    if (!root || !data) return;
    const bar = el("div", "board-filters"); bar.setAttribute("role", "group"); bar.setAttribute("aria-label", "筛选账号");
    for (const [key, label] of FILTERS) {
      const b = el("button", "button compact", label); b.type = "button"; b.setAttribute("aria-pressed", String(filter === key));
      b.addEventListener("click", () => { filter = key; render(); root.querySelector(`[aria-pressed="true"]`)?.focus(); });
      bar.append(b);
    }
    const note = el("p", "hint", data.sub2api_status?.configured === false ? "Sub2API 未配置，远端状态暂不可用。" : "远端状态每 15 秒核对一次；时间悬停可看精确值。");
    const list = el("div", "board-list");
    (data.groups || []).map(team).filter(Boolean).forEach(n => list.append(n));
    if (!list.children.length) list.append(el("p", "muted", (data.groups || []).length ? "没有符合筛选的账号" : "还没有团队，点「账号与团队」页右上角登记。"));
    root.replaceChildren(bar, note, list);
  }
  async function load() {
    const response = await fetch("/api/accounts/portfolio", { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw new Error("board_unavailable");
    data = await response.json(); render();
  }
  function mount(node, options = {}) {
    root = node; api = options;
    if (!root) return;
    const poller = window.Team48Polling?.createPoller({ read: async () => { await load(); return data; }, onData: () => {}, onError: () => { if (!data) root.textContent = "看板暂时无法读取，稍后自动重试"; }, delay: () => 30000 });
    if (poller) void poller.refresh(); else void load().catch(() => { root.textContent = "看板暂时无法读取"; });
  }
  window.Team48Board = { mount, relative, remoteOf, render: payload => { data = payload; render(); } };
})();
