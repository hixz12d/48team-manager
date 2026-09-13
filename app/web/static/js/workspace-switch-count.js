/* Manual counters use Beijing dates even when the browser is in another timezone. */
(function (root) {
  const records = new Map(), pending = new Set();
  let started = false, timer;
  const today = now => root.Team48Expiry.today(now);
  function countForToday(record, now = new Date()) {
    return record?.date === today(now) ? record.count || 0 : 0;
  }
  function millisecondsUntilReset(now = new Date()) {
    return new Date(`${today(now)}T00:00:00+08:00`).getTime() + 86400000 - now.getTime();
  }
  function remember(id, record) {
    const previous = records.get(id);
    // A poll started before a click may return after it. Counts only increase within a day.
    if (record && (!previous || record.date > previous.date ||
      record.date === previous.date && record.count >= previous.count)) records.set(id, record);
  }
  function paint(node) {
    const id = node.dataset.switchWorkspace;
    node.querySelector("[data-switch-label]").textContent = `今日切换 ${countForToday(records.get(id))} 次`;
    const button = node.querySelector("button");
    button.disabled = pending.has(id);
    button.textContent = pending.has(id) ? "保存中…" : "＋1";
    button.setAttribute("aria-busy", String(pending.has(id)));
  }
  function refresh() {
    document.querySelectorAll(".workspace-switch-counter").forEach(paint);
  }
  function scheduleReset() {
    clearTimeout(timer);
    refresh();
    timer = setTimeout(scheduleReset, millisecondsUntilReset() + 20);
  }
  function widget(workspace, {increment, onError}) {
    const id = String(workspace.id);
    remember(id, workspace.switch_count);
    if (!started) {
      started = true;
      scheduleReset();
      document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleReset(); });
      root.addEventListener("pageshow", scheduleReset);
    }
    const node = document.createElement("div");
    node.className = "workspace-switch-counter";
    node.dataset.switchWorkspace = id;
    node.title = "手动记录切换次数，北京时间每天 00:00 清零";
    const label = document.createElement("span");
    label.dataset.switchLabel = "";
    label.setAttribute("role", "status");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button ghost";
    button.dataset.focusKey = `switch-count:${id}`;
    button.setAttribute("aria-label", `${workspace.display_name || workspace.name || "团队"}：切换次数加 1`);
    button.addEventListener("click", async event => {
      event.stopPropagation();
      if (pending.has(id)) return;
      pending.add(id); paint(node); refresh();
      try {
        remember(id, await increment());
      } catch (error) {
        onError(error);
      } finally {
        pending.delete(id); paint(node); refresh();
      }
    });
    const hint = document.createElement("small");
    hint.className = "muted";
    hint.textContent = "北京时间 00:00 清零";
    node.append(label, button, hint);
    paint(node);
    return node;
  }
  const api = {countForToday, millisecondsUntilReset, widget};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Team48SwitchCount = api;
})(typeof window !== "undefined" ? window : globalThis);
