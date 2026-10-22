/* Team purchase ledger: cost line for team cards, and the editor mounted in the team drawer.
   The 2FA secret is write-only; it is fetched only by the copy button and never kept in the DOM. */
(function (root) {
  const CURRENCIES = [["USD", "USD 美元"], ["CNY", "CNY 人民币"]];
  const TIMEZONE = "Asia/Shanghai";
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const fmt = () => root.Team48Format;
  const app = () => root.Team48 || {};
  const zero = value => value == null || value === "" || Number(value) === 0;

  // 金额显示：整数去掉 .00；人民币换成 ¥。不同币种从不相加。
  function money(value, currency = "USD", {trimCents = false} = {}) {
    let text = fmt()?.formatCost ? fmt().formatCost(value) : `$${value}`;
    if (trimCents) text = text.replace(/\.00$/, "");
    return currency === "CNY" ? text.replace("$", "¥") : text;
  }

  function costLine(group) {
    const purchase = group?.purchase || {};
    const settled = group?.revenue?.settled, count = group?.revenue?.count || 0;
    const running = group?.usage?.windows?.lifetime;
    const hasCost = purchase.cost != null && purchase.cost !== "";
    const currency = purchase.currency || "USD";
    const hasReceived = !zero(settled) || (running && !zero(running.user_cost));
    const received = Number(settled || 0) + Number(running?.user_cost || 0);
    const costText = hasCost ? `成本 ${money(purchase.cost, currency, {trimCents: true})}` : "成本未填";
    const text = `${costText} · ${hasReceived ? `已收 ${money(String(received))}` : "收入 —"}`;
    const notes = [];
    if (hasCost) notes.push(`成本：${[purchase.payment_card, money(purchase.cost, currency)].filter(Boolean).join(" · ")}（手动记录）`);
    else notes.push("成本未填：在「管理团队 → 团队信息」里补填");
    notes.push(`已入账 ${money(settled ?? "0")}${count ? `（${count} 笔离队记录）` : ""}`);
    if (running) {
      notes.push(`在跑 ${money(running.user_cost ?? "0")}`);
      if (running.coverage && running.coverage.synced < running.coverage.total) notes.push(`在跑：${running.coverage.synced}/${running.coverage.total} 个成员已同步，部分成员未同步`);
      if (running.stale) notes.push("在跑：旧快照");
    } else notes.push("在跑：尚无全程用量快照");
    if (hasCost && currency !== "USD") notes.push("成本是人民币，已收是美元，两者不相加");
    notes.push("U 是 Sub2API 扣费，不等于实收，不算利润");
    return {text, title: notes.join("\n"), tone: hasCost ? "neutral" : "muted"};
  }

  // 422 的 detail 是数组（字段校验），其余是字符串；都转成一句中文。
  function errorText(reason) {
    const detail = reason?.payload?.detail;
    if (Array.isArray(detail) && detail.length) {
      return detail.map(item => String(item?.msg || "").replace(/^Value error,\s*/, "")).filter(Boolean).join("；") || "填写内容有误";
    }
    return app().friendlyError?.(reason) || reason?.message || "请求失败";
  }

  async function request(key, url, method, body) {
    const fetchEntity = app().fetchEntity;
    const options = {method, headers: {"Content-Type": "application/json"}};
    if (body !== undefined) options.body = JSON.stringify(body);
    if (fetchEntity) return fetchEntity(key, url, options);
    const response = await fetch(url, {...options, headers: {Accept: "application/json", ...options.headers}});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`), {payload, status: response.status});
    return payload;
  }

  async function copyText(text) {
    if (navigator.clipboard?.writeText && root.isSecureContext !== false) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = el("textarea");
    area.value = text; area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0";
    document.body.append(area); area.select();
    try { if (!document.execCommand("copy")) throw new Error("复制被浏览器拒绝"); } finally { area.remove(); }
  }

  const sameCost = (a, b) => (a == null || a === "") ? (b == null || b === "") : (b != null && b !== "" && Number(a) === Number(b));

  function editor(workspace, {onSaved} = {}) {
    let record = {...(workspace.purchase || {})}, busy = false, replacing = false, clearArmed = null;
    const id = workspace.id;
    const base = `/api/workspaces/${encodeURIComponent(id)}/purchase`;
    const form = el("form", "sheet-section workspace-purchase-editor");
    form.dataset.workspacePurchase = String(id);
    const heading = el("div", "workspace-purchase-heading");
    heading.append(el("h3", "", "购买信息"), el("span", "workspace-purchase-source", "手动记录"));
    const hint = el("p", "hint", "一个团队只记一笔购买。费用没填时团队卡显示“成本未填”，不当作 0。");

    const field = (labelText, control, extra = "") => {
      const label = el("label", `workspace-purchase-field${extra ? ` ${extra}` : ""}`);
      label.append(el("span", "", labelText), control);
      return label;
    };
    const card = el("input"); card.type = "text"; card.name = "payment_card"; card.maxLength = 100; card.autocomplete = "off"; card.placeholder = "如 Kast银色卡";
    const cost = el("input"); cost.type = "number"; cost.name = "cost"; cost.min = "0"; cost.step = "0.01"; cost.inputMode = "decimal"; cost.placeholder = "未填";
    const currency = el("select"); currency.name = "currency";
    for (const [value, text] of CURRENCIES) { const option = el("option", "", text); option.value = value; currency.append(option); }
    const note = el("textarea"); note.name = "note"; note.rows = 2; note.maxLength = 2000; note.placeholder = "可选";
    const grid = el("div", "workspace-purchase-grid");
    grid.append(field("支付卡", card), field("费用", cost), field("币种", currency), field("备注", note, "is-wide"));

    // 2FA：已存时只显示状态和按钮，明文只在点「复制」时临时取一次。
    const totp = el("div", "workspace-purchase-totp");
    totp.setAttribute("role", "group"); totp.setAttribute("aria-label", "母号 2FA");
    const totpHead = el("div", "workspace-purchase-totp-head");
    const totpState = el("span", "workspace-purchase-totp-state");
    const totpButtons = el("div", "workspace-purchase-totp-actions");
    const copy = el("button", "button compact", "复制"); copy.type = "button";
    const replace = el("button", "button compact ghost", "更换"); replace.type = "button";
    const clear = el("button", "button compact ghost workspace-purchase-clear", "清除"); clear.type = "button";
    totpButtons.append(copy, replace, clear);
    totpHead.append(el("span", "workspace-purchase-totp-label", "母号 2FA"), totpState, totpButtons);
    const secret = el("input"); secret.type = "password"; secret.name = "totp_secret"; secret.autocomplete = "off"; secret.spellcheck = false;
    secret.placeholder = "粘贴 2FA 密钥（Base32，可带空格）"; secret.setAttribute("aria-label", "2FA 密钥");
    const secretLabel = field("新的 2FA 密钥", secret, "workspace-purchase-secret");
    totp.append(totpHead, secretLabel);

    const actions = el("div", "workspace-purchase-actions");
    const submit = el("button", "button primary", "保存购买信息"); submit.type = "submit";
    const reset = el("button", "button ghost", "撤销修改"); reset.type = "button";
    actions.append(submit, reset);
    const message = el("p", "workspace-purchase-message"); message.setAttribute("role", "status");
    const error = el("p", "workspace-purchase-error"); error.setAttribute("role", "alert"); error.hidden = true;
    const updated = el("small", "muted workspace-purchase-updated");

    function fill() {
      card.value = record.payment_card || "";
      cost.value = record.cost ?? "";
      currency.value = record.currency || "USD";
      note.value = record.note || "";
      secret.value = "";
      replacing = false;
    }
    function changes() {
      const out = {};
      const cardValue = card.value.trim() || null;
      if (cardValue !== (record.payment_card || null)) out.payment_card = cardValue;
      if (!sameCost(cost.value.trim(), record.cost)) out.cost = cost.value.trim() || null;
      if (currency.value !== (record.currency || "USD")) out.currency = currency.value;
      const noteValue = note.value.trim() || null;
      if (noteValue !== (record.note || null)) out.note = noteValue;
      if (secret.value.trim()) out.totp_secret = secret.value;
      return out;
    }
    function disarmClear() {
      if (clearArmed) clearTimeout(clearArmed);
      clearArmed = null; clear.textContent = "清除"; clear.classList.remove("is-armed");
    }
    function render() {
      const stored = record.totp_state === "stored";
      const dirty = Object.keys(changes()).length > 0;
      totpState.textContent = stored ? "● 已存" : "未存";
      totpState.classList.toggle("is-stored", stored);
      totpButtons.hidden = !stored;
      secretLabel.hidden = stored && !replacing;
      secretLabel.firstChild.textContent = stored ? "新的 2FA 密钥" : "2FA 密钥";
      replace.textContent = replacing ? "取消更换" : "更换";
      submit.disabled = busy || !dirty;
      reset.disabled = busy || !dirty;
      submit.textContent = busy ? "保存中…" : "保存购买信息";
      updated.textContent = record.updated_at
        ? `上次记录：${new Intl.DateTimeFormat("zh-CN", {timeZone: TIMEZONE, dateStyle: "medium", timeStyle: "short"}).format(new Date(record.updated_at))}` : "";
    }
    function changed() {
      error.hidden = true; error.textContent = "";
      message.textContent = Object.keys(changes()).length ? "有修改，记得保存" : "";
      render();
    }
    function setBusy(value) {
      busy = value;
      if (value) form.setAttribute("aria-busy", "true"); else form.removeAttribute("aria-busy");
      for (const control of form.querySelectorAll("button, input, select, textarea")) control.disabled = value;
      render();
    }
    async function patch(body, successText) {
      setBusy(true); error.hidden = true; message.textContent = "";
      try {
        const result = await request(`workspace-purchase-${id}`, base, "PATCH", body);
        record = {...(result?.purchase || record)};
        fill();
        message.textContent = successText;
        onSaved?.(result.purchase);
        return true;
      } catch (reason) {
        error.textContent = `保存失败：${errorText(reason)}。已保留所填内容，可以重试。`;
        error.hidden = false;
        return false;
      } finally {
        setBusy(false);
      }
    }

    for (const control of [card, cost, currency, note, secret]) {
      control.addEventListener("input", changed);
      control.addEventListener("change", changed);
    }
    reset.addEventListener("click", () => { fill(); disarmClear(); changed(); });
    replace.addEventListener("click", () => {
      replacing = !replacing;
      if (!replacing) secret.value = "";
      changed();
      if (replacing) secret.focus();
    });
    copy.addEventListener("click", async () => {
      copy.disabled = true;
      try {
        const result = await request(`workspace-purchase-totp-${id}`, `${base}/totp`, "POST");
        await copyText(result.secret);
        app().toast?.("已复制 2FA", "success");
      } catch (reason) {
        app().toast?.(`复制失败：${errorText(reason)}`, "error");
      } finally {
        copy.disabled = busy;
      }
    });
    // 清除要点两次：第一次变成「确认清除」，5 秒内再点才真的清。
    clear.addEventListener("click", async () => {
      if (!clearArmed) {
        clear.textContent = "确认清除？"; clear.classList.add("is-armed");
        clearArmed = setTimeout(disarmClear, 5000);
        return;
      }
      disarmClear();
      await patch({totp_secret: null}, "2FA 已清除");
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const body = changes();
      if (busy || !Object.keys(body).length || !form.reportValidity()) return;
      await patch(body, "已保存，团队卡片会同步更新");
    });

    form.append(heading, hint, grid, totp, actions, message, error, updated);
    fill();
    render();
    return form;
  }

  // 抽屉折叠标题：购买 · Kast银色卡 · $125；什么都没填时提示去填写。
  function summary(purchase) {
    const p = purchase || {};
    const parts = [p.payment_card, p.cost != null && p.cost !== "" ? money(p.cost, p.currency || "USD", {trimCents: true}) : ""].filter(Boolean);
    if (parts.length) return `购买 · ${parts.join(" · ")}`;
    return p.note || p.totp_state === "stored" ? "购买 · 费用未填 · 修改" : "购买信息未填 · 填写";
  }

  const api = {costLine, editor, summary};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Team48Purchase = api;
})(typeof window !== "undefined" ? window : globalThis);
