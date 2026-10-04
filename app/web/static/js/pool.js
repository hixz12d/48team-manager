// 备用号池页面：导入邮箱、列表、拉入团队弹框、继续 / 重新检测 / 移出（依赖 window.Team48）。
(() => {
  if (document.body.dataset.page !== "pool") return;
  const T = window.Team48;
  if (!T) return;

  const STATE_TEXT = { pending: "待拉入", joining: "拉入中", joined: "已入组", failed: "失败", manual_required: "待人工" };
  // 状态符号由 .status 的 data-tone 决定：无 tone ○、accent / success ●、warning ▲、danger ✕
  const STATE_TONE = { pending: "", joining: "accent", joined: "success", failed: "danger", manual_required: "warning" };
  const ROLE_TEXT = { member: "成员 Member", owner: "所有者 Owner" };
  const SEAT_TEXT = { workspace_default: "团队默认", standard: "Standard", premium: "Premium" };
  const IMPACT_TEXT = "会修改官方 Team：向该邮箱发送邀请，服务器登录这个账号接受邀请并授权，成功后推送 Sub2API、今日切换 +1。";
  const REPLACE_TEXT = "先把被替换的子号移出官方团队（本地档案保留，Sub2API 只暂停调度不删）。";

  const body = document.getElementById("pool-body");
  const importSheet = document.getElementById("pool-import-sheet");
  const joinSheet = document.getElementById("pool-join-sheet");
  const importForm = document.getElementById("pool-import-form");
  const joinForm = document.getElementById("pool-join-form");
  const workspaceSelect = document.getElementById("pool-join-workspace");
  const replaceSelect = document.getElementById("pool-join-replace");
  const joinSubmit = document.getElementById("pool-join-submit");
  const busyEntries = new Set();
  let joinEntry = null;
  let joinWorkspaces = [];
  let recommendToken = 0;

  // 抽屉放在 <main> 里会随 .shell 一起被设成 inert，移到 body 下与其他抽屉同级。
  [importSheet, joinSheet].forEach((node) => { if (node) document.body.append(node); });
  T.registerOverlay("pool-import", importSheet);
  T.registerOverlay("pool-join", joinSheet);
  [importSheet, joinSheet].forEach((overlay) => {
    overlay?.addEventListener("click", (event) => {
      if (event.target === overlay) T.closeOverlay();
    });
  });

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function statusSpan(tone, text, title) {
    const span = el("span", "status", text);
    if (tone) span.dataset.tone = tone;
    if (title) span.title = title;
    return span;
  }

  function parseTime(value) {
    if (!value) return null;
    let text = String(value);
    // 数据库时间不带时区时按 UTC 处理
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(text)) text = `${text.replace(" ", "T")}Z`;
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function timeText(value) {
    const date = parseTime(value);
    if (!date) return "—";
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function operationHref(id) {
    return `/operations?op=${encodeURIComponent(id)}`;
  }

  function errorOperationId(error) {
    const detail = error?.payload?.detail;
    return (detail && typeof detail === "object" && detail.operation_id) || error?.payload?.operation_id || null;
  }

  function reportError(error) {
    const message = T.friendlyError(error);
    if (!message) return;
    const operationId = errorOperationId(error);
    const action = operationId ? { label: "查看任务", onClick: () => { window.location.href = operationHref(operationId); } } : undefined;
    T.toast(message, "error", action);
  }

  function setStatus(id, text, tone) {
    const node = document.getElementById(id);
    if (!node) return;
    node.hidden = !text;
    node.className = tone || "muted";
    node.setAttribute("role", tone === "error" ? "alert" : "status");
    node.textContent = text || "";
  }

  function setPageError(message) {
    const box = document.getElementById("page-feedback");
    const text = document.getElementById("page-feedback-text");
    if (!box || !text) return;
    text.textContent = message || "";
    box.hidden = !message;
  }

  // ---------- 列表 ----------

  function renderSummary(summary) {
    const s = summary || {};
    const values = {
      total: s.total ?? 0,
      pending: s.pending ?? 0,
      joining: s.joining ?? 0,
      joined: s.joined ?? 0,
      attention: (s.failed ?? 0) + (s.manual_required ?? 0),
      mailbox_failed: s.mailbox_failed ?? 0,
    };
    Object.entries(values).forEach(([key, value]) => {
      const node = document.querySelector(`[data-pool-summary="${key}"]`);
      if (!node) return;
      node.textContent = String(value);
      const tile = node.closest(".pool-summary-item");
      if (tile?.dataset.poolAlert) tile.classList.toggle("is-alert", value > 0);
    });
  }

  function mailboxCell(item) {
    const mailbox = item.mailbox || {};
    if (mailbox.state === "ready") return statusSpan("success", "可读");
    if (mailbox.state === "failed") return statusSpan("danger", "不可读", mailbox.error || "邮箱不可读");
    return statusSpan("", "未检测");
  }

  function stateCell(item) {
    const wrap = el("div", "pool-state");
    const state = item.state || "pending";
    wrap.append(statusSpan(STATE_TONE[state] || "", item.state_label || STATE_TEXT[state] || state));
    if ((state === "failed" || state === "manual_required") && item.error) {
      wrap.append(el("span", "pool-state-error", item.error));
    }
    if (item.operation_id) {
      const link = el("a", "", "查看任务");
      link.href = operationHref(item.operation_id);
      wrap.append(link);
    }
    return wrap;
  }

  function actionButton(label, className, entryId, onClick) {
    const button = el("button", className, label);
    button.type = "button";
    button.disabled = busyEntries.has(entryId);
    button.addEventListener("click", () => onClick(button));
    return button;
  }

  function actionsCell(item) {
    const wrap = el("div", "row-actions");
    const mailboxState = item.mailbox?.state || "unknown";
    if (item.can_join) wrap.append(actionButton("拉入团队", "button primary", item.id, (button) => openJoin(item, button)));
    if (item.can_continue) wrap.append(actionButton("继续", "button", item.id, (button) => continueJoin(item, button)));
    if (mailboxState === "failed" || mailboxState === "unknown") {
      wrap.append(actionButton("重新检测", "button", item.id, (button) => recheck(item, button)));
    }
    if (item.can_remove) wrap.append(actionButton("移出", "button ghost danger", item.id, (button) => removeEntry(item, button)));
    return wrap;
  }

  function td(content, className) {
    const cell = el("td", className);
    if (content instanceof Node) cell.append(content);
    else cell.textContent = content == null || content === "" ? "—" : String(content);
    return cell;
  }

  function renderRows(items) {
    if (!body) return;
    if (!items.length) {
      const row = document.createElement("tr");
      const cell = td(T.emptyState("号池是空的", "点「导入邮箱」，粘贴本地注册好的账号邮箱。"));
      cell.colSpan = 6;
      row.append(cell);
      body.replaceChildren(row);
      return;
    }
    body.replaceChildren(...items.map((item) => {
      const row = document.createElement("tr");
      row.dataset.entryId = String(item.id);
      const imported = parseTime(item.imported_at);
      const importedCell = td(timeText(item.imported_at), "tabular");
      if (imported) importedCell.title = imported.toLocaleString("zh-CN", { hour12: false });
      row.append(
        td(el("span", "pool-email", item.email || "—")),
        td(mailboxCell(item)),
        td(stateCell(item)),
        td(item.workspace?.name || "—"),
        importedCell,
        td(actionsCell(item), "actions"),
      );
      return row;
    }));
  }

  let lastItems = [];

  function render(payload) {
    lastItems = payload?.items || [];
    renderSummary(payload?.summary);
    renderRows(lastItems);
    const count = document.getElementById("pool-count");
    if (count) count.textContent = `共 ${lastItems.length} 个`;
    setPageError("");
  }

  const poller = window.Team48Polling.createPoller({
    read: () => T.fetchEntity("pool-list", "/api/pool"),
    onData: render,
    onError: (error) => {
      const message = T.friendlyError(error);
      if (!message) return;
      setPageError(message);
      if (!lastItems.length && body) {
        const row = document.createElement("tr");
        const cell = td("号池读取失败，稍后会自动重试。");
        cell.colSpan = 6;
        cell.className = "muted";
        row.append(cell);
        body.replaceChildren(row);
      }
    },
    delay: (payload) => ((payload?.items || []).some((item) => item.state === "joining") ? 5000 : 30000),
  });
  const refresh = () => poller.refresh();
  // 页面"重试"按钮和通用 bootPage 都走这里。
  T.pageBootstraps.pool = refresh;

  // ---------- 导入 ----------

  function resultGroup(title, tone, lines) {
    const group = el("div", "pool-result-group");
    if (tone) group.dataset.tone = tone;
    group.append(el("strong", "", title));
    if (lines.length) {
      const list = document.createElement("ul");
      lines.forEach((line) => list.append(el("li", "", line)));
      group.append(list);
    }
    return group;
  }

  function renderImportResult(result) {
    const box = document.getElementById("pool-import-result");
    if (!box) return;
    const imported = result.imported || [];
    const skipped = result.skipped || [];
    const invalid = result.invalid || [];
    const mailboxFailed = result.mailbox_failed || [];
    const groups = [resultGroup(`已导入 ${imported.length} 个`, imported.length ? "success" : "", imported)];
    if (skipped.length) {
      groups.push(resultGroup(`跳过 ${skipped.length} 个`, "", skipped.map((item) => `${item.email}（${item.reason || "已跳过"}）`)));
    }
    if (invalid.length) groups.push(resultGroup(`格式不对 ${invalid.length} 行`, "danger", invalid.map(String)));
    if (mailboxFailed.length) {
      groups.push(resultGroup(`邮箱不可读 ${mailboxFailed.length} 个`, "warning", mailboxFailed.map((item) => `${item.email}（${item.error || "读不到收件箱"}）`)));
    }
    box.replaceChildren(...groups);
    box.hidden = false;
  }

  function openImport(trigger) {
    if (!importSheet) return;
    importForm?.reset();
    setStatus("pool-import-status", "", "muted");
    const box = document.getElementById("pool-import-result");
    if (box) { box.replaceChildren(); box.hidden = true; }
    T.openOverlay("pool-import", { returnFocus: trigger, context: { kind: "pool-import" }, initialFocus: "[name='text']" });
  }

  async function submitImport(event) {
    event.preventDefault();
    const button = document.getElementById("pool-import-submit");
    if (button?.disabled) return;
    const text = String(new FormData(importForm).get("text") || "");
    if (!text.trim()) {
      setStatus("pool-import-status", "请先粘贴邮箱，每行一个。", "error");
      return;
    }
    if (button) button.disabled = true;
    setStatus("pool-import-status", "正在导入并检测邮箱…", "muted");
    try {
      const result = await T.fetchEntity("pool-import", "/api/pool/import", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ text }),
      });
      setStatus("pool-import-status", "", "muted");
      renderImportResult(result || {});
      await refresh();
    } catch (error) {
      setStatus("pool-import-status", T.friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  document.querySelectorAll("[data-open-pool-import]").forEach((button) => {
    button.addEventListener("click", () => openImport(button));
  });
  importSheet?.querySelector("[data-close-pool-import]")?.addEventListener("click", () => T.closeOverlay());
  importForm?.addEventListener("submit", submitImport);

  // ---------- 拉入团队 ----------

  function workspaceOptionText(ws) {
    const seats = ws.limit != null && ws.occupied != null ? `${ws.occupied}/${ws.limit}` : "席位未知";
    const parts = [ws.name || `团队 ${ws.id}`, seats, `今日切换 ${ws.switch_count ?? 0}`];
    let text = parts.join(" · ");
    if (!ws.eligible) text += `（${ws.reason || "不可选"}）`;
    return text;
  }

  function selectedWorkspace() {
    return joinWorkspaces.find((ws) => ws.id === Number(workspaceSelect?.value));
  }

  function renderReplaceOptions() {
    if (!replaceSelect) return;
    const ws = selectedWorkspace();
    const members = ws?.members || [];
    // 团队一般是满的：除非确知有空位，默认选第一个子号替换（提交前确认框会再列出来）。
    const hasRoom = ws?.limit != null && ws?.occupied != null && ws.occupied < ws.limit;
    const none = new Option(hasRoom ? "不替换（团队有空位）" : "不替换（直接邀请，满员时会失败或多占席位）", "");
    replaceSelect.replaceChildren(none, ...members.map((member) => new Option(
      `${member.email} · ${ROLE_TEXT[member.role] || member.role} · ${SEAT_TEXT[member.seat_intent] || member.seat_intent}`,
      member.email,
    )));
    replaceSelect.value = !hasRoom && members.length ? members[0].email : "";
    replaceSelect.disabled = !members.length;
    applyReplaceDefaults();
  }

  // 选了被替换的号就沿用它的角色和席位，仍可手动改。
  function applyReplaceDefaults() {
    const member = (selectedWorkspace()?.members || []).find((item) => item.email === replaceSelect?.value);
    if (!member) return;
    const role = document.getElementById("pool-join-role");
    const seat = document.getElementById("pool-join-seat");
    if (role) role.value = member.role;
    if (seat) seat.value = member.seat_intent;
  }

  function setJoinEmpty(empty) {
    const notice = document.getElementById("pool-join-empty");
    if (notice) notice.hidden = !empty;
    if (joinSubmit) joinSubmit.disabled = empty;
  }

  async function loadRecommendation(entry) {
    const token = ++recommendToken;
    workspaceSelect.replaceChildren(new Option("正在读取团队…", ""));
    workspaceSelect.disabled = true;
    if (replaceSelect) { replaceSelect.replaceChildren(new Option("不替换", "")); replaceSelect.disabled = true; }
    if (joinSubmit) joinSubmit.disabled = true;
    document.getElementById("pool-join-empty").hidden = true;
    try {
      const result = await T.fetchEntity("pool-recommend", `/api/pool/recommend?entry_id=${encodeURIComponent(entry.id)}`);
      if (token !== recommendToken) return;
      joinWorkspaces = result?.workspaces || [];
      const options = joinWorkspaces.map((ws) => {
        const option = new Option(workspaceOptionText(ws), String(ws.id));
        option.disabled = !ws.eligible;
        return option;
      });
      const eligible = joinWorkspaces.filter((ws) => ws.eligible);
      if (!eligible.length) {
        workspaceSelect.replaceChildren(new Option("暂无可拉入的团队", ""), ...options);
        workspaceSelect.value = "";
        workspaceSelect.disabled = !options.length;
        setJoinEmpty(true);
        return;
      }
      workspaceSelect.replaceChildren(...options);
      const recommended = eligible.find((ws) => ws.id === result.recommended_id) || eligible[0];
      workspaceSelect.value = String(recommended.id);
      workspaceSelect.disabled = false;
      setJoinEmpty(false);
      renderReplaceOptions();
    } catch (error) {
      if (token !== recommendToken) return;
      const message = T.friendlyError(error);
      if (!message) return;
      workspaceSelect.replaceChildren(new Option("读取团队失败", ""));
      setStatus("pool-join-status", message, "error");
      if (joinSubmit) joinSubmit.disabled = true;
    }
  }

  function openJoin(item, trigger) {
    if (!joinSheet || !workspaceSelect) return;
    joinEntry = item;
    joinWorkspaces = [];
    joinForm?.reset();
    setStatus("pool-join-status", "", "muted");
    document.getElementById("pool-join-email").textContent = item.email || "";
    T.openOverlay("pool-join", { returnFocus: trigger, context: { kind: "pool-join", id: item.id }, initialFocus: "#pool-join-workspace" });
    void loadRecommendation(item);
  }

  function startedToast(result) {
    const operationId = result?.operation_id;
    T.toast("已开始拉入，可在任务中心查看进度", "success",
      operationId ? { label: "查看任务", onClick: () => { window.location.href = operationHref(operationId); } } : undefined);
  }

  async function submitJoin(event) {
    event.preventDefault();
    const entry = joinEntry;
    if (!entry || !joinSubmit || joinSubmit.disabled || busyEntries.has(entry.id)) return;
    const workspaceId = Number(workspaceSelect.value);
    const workspace = joinWorkspaces.find((ws) => ws.id === workspaceId);
    if (!workspace || !workspace.eligible) {
      setStatus("pool-join-status", "请选择一个可拉入的团队。", "error");
      return;
    }
    const role = document.getElementById("pool-join-role")?.value || "member";
    const seatIntent = document.getElementById("pool-join-seat")?.value || "workspace_default";
    const replaceEmail = replaceSelect?.value || "";
    if (workspace.full && !replaceEmail) {
      setStatus("pool-join-status", "团队已满，请选一个要替换的子号。", "error");
      return;
    }
    const confirmed = await T.openConfirm({
      title: replaceEmail ? "替换子号" : "拉入团队",
      message: replaceEmail ? `${REPLACE_TEXT}${IMPACT_TEXT}` : IMPACT_TEXT,
      items: [
        `邮箱：${entry.email}`,
        `团队：${workspace.name || workspace.id}`,
        ...(replaceEmail ? [`移出：${replaceEmail}`] : []),
        `角色：${ROLE_TEXT[role] || role}`,
        `席位：${SEAT_TEXT[seatIntent] || seatIntent}`,
      ],
      confirmLabel: replaceEmail ? "确认替换" : "确认拉入",
      tone: "danger",
    }, joinSubmit);
    if (!confirmed) return;
    busyEntries.add(entry.id);
    joinSubmit.disabled = true;
    setStatus("pool-join-status", "正在提交…", "muted");
    try {
      const result = await T.fetchEntity(`pool-join-${entry.id}`, `/api/pool/${encodeURIComponent(entry.id)}/join`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ workspace_id: workspaceId, role, seat_intent: seatIntent, replace_email: replaceEmail }),
      });
      startedToast(result);
      if (T.getActiveOverlay() === "pool-join") T.closeOverlay();
      joinEntry = null;
    } catch (error) {
      setStatus("pool-join-status", T.friendlyError(error), "error");
      reportError(error);
    } finally {
      busyEntries.delete(entry.id);
      joinSubmit.disabled = false;
      await refresh();
    }
  }

  joinSheet?.querySelector("[data-close-pool-join]")?.addEventListener("click", () => T.closeOverlay());
  joinForm?.addEventListener("submit", submitJoin);
  workspaceSelect?.addEventListener("change", () => { setStatus("pool-join-status", "", "muted"); renderReplaceOptions(); });
  replaceSelect?.addEventListener("change", () => { setStatus("pool-join-status", "", "muted"); applyReplaceDefaults(); });

  // ---------- 行内操作 ----------

  async function runRowAction(item, button, request, onSuccess) {
    if (busyEntries.has(item.id)) return;
    busyEntries.add(item.id);
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const result = await request();
      onSuccess(result);
    } catch (error) {
      reportError(error);
    } finally {
      busyEntries.delete(item.id);
      button.disabled = false;
      button.removeAttribute("aria-busy");
      await refresh();
    }
  }

  async function continueJoin(item, button) {
    const confirmed = await T.openConfirm({
      title: "继续拉入",
      message: "会接着在原团队登录入组，可能再次修改官方 Team（已发的邀请不重发，已入组则直接授权）。",
      items: [`邮箱：${item.email}`, `团队：${item.workspace?.name || "原团队"}`],
      confirmLabel: "确认继续",
      tone: "danger",
    }, button);
    if (!confirmed) return;
    await runRowAction(item, button,
      () => T.fetchEntity(`pool-continue-${item.id}`, `/api/pool/${encodeURIComponent(item.id)}/continue`, {
        method: "POST",
        headers: { Accept: "application/json" },
      }),
      startedToast);
  }

  async function recheck(item, button) {
    await runRowAction(item, button,
      () => T.fetchEntity(`pool-recheck-${item.id}`, `/api/pool/${encodeURIComponent(item.id)}/recheck`, {
        method: "POST",
        headers: { Accept: "application/json" },
      }),
      (result) => {
        const state = result?.item?.mailbox?.state;
        if (state === "ready") T.toast(`${item.email} 邮箱可读`, "success");
        else T.toast(`${item.email} 邮箱仍不可读${result?.item?.mailbox?.error ? `：${result.item.mailbox.error}` : ""}`, "warning");
      });
  }

  async function removeEntry(item, button) {
    const confirmed = await T.openConfirm({
      title: "移出号池",
      message: `从号池移出 ${item.email} 并删除本地档案。`,
      items: ["删除这个号的本地账号档案", "不改官方 Team"],
      confirmLabel: "确认移出",
      tone: "danger",
    }, button);
    if (!confirmed) return;
    await runRowAction(item, button,
      () => T.fetchEntity(`pool-remove-${item.id}`, `/api/pool/${encodeURIComponent(item.id)}`, {
        method: "DELETE",
        headers: { Accept: "application/json" },
      }),
      () => T.toast(`已移出 ${item.email}`, "success"));
  }

  void refresh();
})();
