/* Manual workspace calendar reminders. Dates never alter official subscription state. */
(function (root) {
  const TIMEZONE = "Asia/Shanghai";
  function parseDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000")) return null;
    const date = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? date : null;
  }
  function today(now = new Date(), timezone = TIMEZONE) {
    const parts = new Intl.DateTimeFormat("en", {timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(now);
    const part = kind => parts.find(p => p.type === kind).value;
    return `${part("year").padStart(4, "0")}-${part("month")}-${part("day")}`;
  }
  function addMonths(value, months) {
    const date = parseDate(value);
    if (!date || !Number.isInteger(months)) return "";
    const day = date.getUTCDate();
    date.setUTCDate(1);
    date.setUTCMonth(date.getUTCMonth() + months);
    const last = new Date(date.getTime());
    last.setUTCMonth(last.getUTCMonth() + 1);
    last.setUTCDate(0);
    date.setUTCDate(Math.min(day, last.getUTCDate()));
    return date.getUTCFullYear() > 0 && date.getUTCFullYear() <= 9999 ? date.toISOString().slice(0, 10) : "";
  }
  function summary(record, now = new Date()) {
    const date = parseDate(record?.date);
    if (!date) return {tone: "muted", label: "尚未填写", date: "", days: null};
    const days = Math.round((date - parseDate(today(now, record?.timezone || TIMEZONE))) / 86400000);
    return {
      tone: days < 0 ? "error" : days <= 7 ? "warning" : "neutral",
      label: days < 0 ? `已过期 ${-days} 天` : days === 0 ? "今天到期" : `剩余 ${days} 天`,
      date: record.date.replaceAll("-", "/"), days,
    };
  }
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  function trigger(workspace, onClick) {
    const info = summary(workspace.expiry);
    const button = el("button", `workspace-expiry-trigger expiry-${info.tone}`);
    button.type = "button";
    button.dataset.focusKey = `expiry:${workspace.id}`;
    button.setAttribute("aria-label", `${workspace.display_name || workspace.name || "团队"}：${info.date ? "修改" : "填写"}到期日期`);
    if (info.date) {
      button.append(el("span", "", `到期 ${info.date}`), el("span", "expiry-status", info.label), el("small", "", "手动记录"));
    } else button.append(el("span", "", "＋ 填写到期日期"));
    button.title = info.date ? "手动记录的到期日期，点击修改" : "补填日期后，列表会显示剩余天数";
    button.addEventListener("click", event => { event.stopPropagation(); onClick(button); });
    return button;
  }
  function editor(workspace, {save, friendlyError}) {
    let record = workspace.expiry || {}, saved = record.date || "", busy = false;
    const form = el("form", "sheet-section workspace-expiry-editor");
    form.dataset.workspaceExpiry = String(workspace.id);
    const heading = el("div", "expiry-heading");
    heading.append(el("h3", "", "到期日期"), el("span", "expiry-source", "手动记录"));
    const hint = el("p", "hint", "按北京时间记录。到期前 7 天会在团队卡片提醒，不会自动停用团队。");
    const preview = el("div", "expiry-preview");
    preview.setAttribute("aria-live", "polite");
    const label = el("label", "expiry-date-label", "选择日期");
    const input = el("input");
    input.type = "date"; input.name = "expires_on"; input.value = saved;
    input.min = "0001-01-01"; input.max = "9999-12-31";
    input.setAttribute("aria-describedby", `expiry-help-${workspace.id}`);
    hint.id = `expiry-help-${workspace.id}`;
    label.append(input);
    const shortcuts = el("div", "expiry-shortcuts");
    const quickLabel = el("span", "muted", "从今天起：");
    shortcuts.append(quickLabel);
    const actions = el("div", "expiry-actions");
    const submit = el("button", "button primary", "保存日期"); submit.type = "submit";
    const reset = el("button", "button ghost", "撤销修改"); reset.type = "button";
    const message = el("p", "expiry-message"); message.setAttribute("role", "status");
    const error = el("p", "expiry-error"); error.setAttribute("role", "alert"); error.hidden = true;
    const updated = el("small", "muted expiry-updated");
    function render() {
      const dirty = input.value !== saved;
      const info = summary({...record, date: input.value});
      preview.className = `expiry-preview expiry-${info.tone}`;
      preview.replaceChildren(el("strong", "", info.date || "尚未填写"), el("span", "", info.date ? info.label : "补填后会显示剩余天数"));
      submit.disabled = busy || !dirty;
      reset.disabled = busy || !dirty;
      submit.textContent = busy ? "保存中…" : !input.value && saved ? "保存清空" : "保存日期";
      updated.textContent = record.updated_at
        ? `上次记录：${new Intl.DateTimeFormat("zh-CN", {timeZone: TIMEZONE, dateStyle: "medium", timeStyle: "short"}).format(new Date(record.updated_at))}` : "";
    }
    function changed() {
      error.hidden = true; error.textContent = "";
      message.textContent = input.value !== saved ? (!input.value ? "保存后将清空这条日期记录" : "日期已修改，记得保存") : "";
      render();
    }
    for (const [text, months] of [["一个月后", 1], ["一年后", 12]]) {
      const button = el("button", "button ghost", text); button.type = "button";
      button.addEventListener("click", () => { input.value = addMonths(today(), months); changed(); });
      shortcuts.append(button);
    }
    const clear = el("button", "button ghost expiry-clear", "清空日期"); clear.type = "button";
    clear.addEventListener("click", () => { input.value = ""; changed(); input.focus(); });
    shortcuts.append(clear);
    input.addEventListener("input", changed);
    input.addEventListener("change", changed);
    reset.addEventListener("click", () => { input.value = saved; changed(); input.focus(); });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (busy || input.value === saved || !input.reportValidity()) return;
      busy = true; error.hidden = true; message.textContent = "";
      for (const control of form.querySelectorAll("button, input")) control.disabled = true;
      form.setAttribute("aria-busy", "true"); render();
      try {
        record = await save(input.value || null);
        saved = record.date || ""; input.value = saved;
        message.textContent = saved ? "已保存，团队卡片已更新" : "日期已清空";
      } catch (reason) {
        error.textContent = `保存失败：${friendlyError(reason)}。已保留所填日期，可以重试。`;
        error.hidden = false;
      } finally {
        busy = false; form.removeAttribute("aria-busy");
        for (const control of form.querySelectorAll("button, input")) control.disabled = false;
        render();
      }
    });
    actions.append(submit, reset);
    form.append(heading, hint, preview, label, shortcuts, actions, message, error, updated);
    render();
    return form;
  }
  const api = {today, addMonths, summary, trigger, editor};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Team48Expiry = api;
})(typeof window !== "undefined" ? window : globalThis);
