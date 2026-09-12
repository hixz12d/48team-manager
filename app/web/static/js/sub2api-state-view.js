/* Redacted remote state in the existing account drawer. No credentials here. */
(() => {
  const states = {not_checked: "尚未核对", unrecorded: "尚无同步记录", pending: "凭据已写入 · 后续处理中", completed: "同步步骤已完成", needs_review: "需要人工核对"};
  const blockers = {schedulable_off: "人工调度开关关闭", account_error: "账号仍有运行错误", rate_limit: "限额尚未重置", temporary_pause: "临时暂停", overload: "过载保护", account_expired: "账号已过期", final_state_unknown: "最终状态未知", runtime_blocked: "仍有运行阻断", unknown_blocker: "存在尚未识别的限制"};
  const steps = {pending: "等待处理", succeeded: "已完成"};
  let disposeCurrent = () => {};
  function mount(parent, item, api) {
    disposeCurrent();
    if (!item.id || item.managed === false) return;
    const panel = document.createElement("section"); panel.className = "sheet-section"; panel.dataset.sub2apiState = "";
    const heading = document.createElement("h3"); heading.textContent = "Sub2API 远端状态";
    const summary = document.createElement("p"); summary.setAttribute("role", "status"); summary.setAttribute("aria-live", "polite"); summary.textContent = "正在读取本地观察记录…";
    const content = document.createElement("div");
    const controls = document.createElement("div"); controls.className = "management-actions";
    const check = document.createElement("button"); check.type = "button"; check.className = "button"; check.textContent = "重新核对状态";
    const retry = document.createElement("button"); retry.type = "button"; retry.className = "button ghost"; retry.textContent = "重试未完成步骤"; retry.disabled = true;
    const note = document.createElement("p"); note.className = "hint"; note.textContent = "同步完成只确认凭据和缓存处理；账号是否实际可用仍需单独验证。";
    controls.append(check, retry); panel.append(heading, summary, content, controls, note); parent.append(panel);
    const ownerStatus = document.createElement("p"); ownerStatus.setAttribute("role", "status");
    const ownerActionMessage = document.createElement("p"); ownerActionMessage.setAttribute("role", "alert");
    const delegate = document.createElement("button"); delegate.type = "button"; delegate.className = "button ghost";
    delegate.textContent = "委托远端刷新"; delegate.disabled = true;
    const ownerNote = document.createElement("p"); ownerNote.className = "hint";
    ownerNote.textContent = "委托后本地只保留访问令牌，Sub2API 负责日常刷新。此操作会替换本地访问凭据，不会清除暂停或认证错误。";
    const recover = document.createElement("button"); recover.type = "button"; recover.className = "button ghost";
    recover.textContent = "验证新授权并恢复认证"; recover.disabled = true;
    const handback = document.createElement("button"); handback.type = "button"; handback.className = "button ghost";
    handback.textContent = "安全交回 Team"; handback.disabled = true;
    panel.append(ownerStatus, delegate, recover, handback, ownerActionMessage, ownerNote);
    const authorityPath = `/api/accounts/${item.id}/sub2api/refresh-authority`;
    let ownerEpoch = -1;
    function renderAuthority(data) {
      if (disposed || !data) return;
      if (Number.isInteger(data.authority_epoch) && data.authority_epoch < ownerEpoch) return;
      if (Number.isInteger(data.authority_epoch)) ownerEpoch = data.authority_epoch;
      ownerStatus.textContent = data.owner === "sub2api" ? `当前刷新方：Sub2API。${data.return_pending ? "正在交回 Team，请继续同一交接" : data.message || "Team 只接收访问令牌"}` : data.owner === "team" ? (data.returned ? `当前刷新方：Team（已安全交回）${data.handoff_acknowledged ? "" : " · 交接暂存凭据清理待确认"}` : "当前刷新方：Team（尚未委托）") : "刷新归属暂未确认";
      delegate.textContent = data.owner === "sub2api" ? "重新采用远端访问令牌" : "委托远端刷新";
      handback.disabled = busy || (data.owner !== "sub2api" && !data.returned);
      handback.textContent = data.returned ? "确认交接收尾" : data.return_pending ? "继续交回 Team" : "安全交回 Team";
      ownerNote.textContent = data.returned ? "Team 负责日常刷新，远端只接收访问令牌。暂停与认证状态仍需单独处理。" : "委托后本地只保留访问令牌。交回前会停止远端新刷新并等待在途结果，不会解除暂停或清除认证错误。";
      delegate.disabled = busy || !!data.returned || !!data.return_pending || !["sub2api", "team"].includes(data.owner);
      recover.disabled = busy || !!data.return_pending || !["sub2api", "team"].includes(data.owner);
    }
    const query = item.workspace_id ? `?workspace_id=${encodeURIComponent(item.workspace_id)}` : "";
    const path = `/api/accounts/${item.id}/sub2api/remote-state`;
    const key = `sub2api-state:${item.id}:${item.workspace_id || "none"}`;
    let latest, busy = false, disposed = false, followUntil = 0, initial = true;
    function render(data) {
      if (disposed || !panel.isConnected) return;
      if (latest?.observation_revision && data.observation_revision && data.observation_revision < latest.observation_revision) return;
      latest = data;
      renderAuthority(data.refresh_authority);
      const snapshot = data.snapshot || {}, op = snapshot.latest_operation || {};
      let text = data.message || states[data.state] || "远端状态未确认";
      if (data.stale && data.snapshot) text += " · 旧快照";
      if (snapshot.remaining_blockers?.length && data.state === "completed") text += " · 仍有运行阻断";
      if (summary.textContent !== text) summary.textContent = text;
      content.replaceChildren(api.kvSection("观察记录", [
        ["最后成功核对", data.checked_at || "尚未成功核对"],
        ["最近尝试", data.last_attempt_at],
        ["运行限制", (snapshot.remaining_blockers || []).map(v => blockers[v] || "其他限制").join("；") || (data.snapshot ? "未观察到以上限制，实际可用性未验证" : "未确认")],
        ["缓存删除", steps[op.token_cache_invalidation] || "未记录"],
        ["共享调度刷新", steps[op.scheduler_refresh] || "未记录"],
        ["凭据版本", snapshot.credential_version],
        ["授权验证", op.validation_scope === "codex_identity_usage_catalog" && op.validated_at ? `${snapshot.operation_is_current && !data.stale ? "此版本" : "历史版本"}身份与 Codex 服务访问已验证（未测试生成）· ${op.validated_at}` : "未记录可信验证"],
        ["认证恢复", op.auth_recovery === "cleared" ? "该次操作已清除匹配的认证错误" : "未执行认证恢复"],
        ["最近同步操作", op.operation_id],
        ["操作对应当前凭据", op.operation_id ? (snapshot.operation_is_current ? "是" : "否，远端凭据随后已变化") : "未记录"],
        ["远端实例", snapshot.instance_id],
      ]));
      retry.disabled = busy || !data.can_retry;
      if (initial && data.state === "pending" && !data.error_code) followUntil = Date.now() + 120000;
      initial = false;
    }
    const poller = window.Team48Polling.createPoller({
      read: async () => {
        if (!panel.isConnected || panel.closest(".sheet")?.hidden) { dispose(); return {}; }
        const stateRead = !busy && latest?.state === "pending" && !latest.error_code && Date.now() < followUntil
          ? api.post(key + ":refresh", path + "/refresh" + query, {}) : api.get(key, path + query);
        const [state, owner] = await Promise.all([stateRead, api.get(key + ":authority", authorityPath).catch(() => ({owner: "unknown"}))]);
        return {...state, refresh_authority: owner};
      },
      onData: render,
      onError: () => { if (!disposed) { summary.textContent = "读取失败，旧观察记录仍保留"; retry.disabled = true; } },
      delay: data => data.state === "pending" && Date.now() < followUntil ? 5000 : 30000,
    });
    async function act(action) {
      if (busy || disposed) return;
      busy = true; check.disabled = true; retry.disabled = true;
      delegate.disabled = true;
      check.setAttribute("aria-busy", "true");
      try {
        const data = await api.post(key + ":" + action, path + "/" + action + query, {});
        if (!disposed) { followUntil = Date.now() + 120000; render(data); }
      } catch (_) {
        if (!disposed) summary.textContent = "请求未确认，请重新核对状态；凭据不会重复提交";
      } finally {
        busy = false; check.disabled = false; check.removeAttribute("aria-busy");
        renderAuthority(latest?.refresh_authority);
        if (!disposed) { retry.disabled = !latest?.can_retry; void poller.refresh(); }
      }
    }
    check.addEventListener("click", () => act("refresh"));
    retry.addEventListener("click", () => act("retry"));
    delegate.addEventListener("click", async () => {
      if (busy || disposed) return;
      busy = true; delegate.disabled = true; check.disabled = true; retry.disabled = true;
      ownerActionMessage.textContent = "";
      try {
        const preview = await api.post(key + ":authority-preview", authorityPath + "/preview" + query, {});
        if (disposed) return;
        if (!preview.ok) { ownerActionMessage.textContent = preview.message || "远端状态未确认"; return; }
        const confirmed = window.confirm(`${preview.email}\n将采用远端账号 #${preview.remote_account_id} 的访问令牌，并移除本地刷新、ID 和会话凭据。\n此后日常刷新由 Sub2API 负责，Team 只接收访问令牌。暂停和认证错误不会自动清除。\n确认以这次核对的远端凭据为准？`);
        if (!confirmed || disposed) return;
        const result = await api.post(key + ":authority-adopt", authorityPath + query, preview.preconditions);
        if (disposed) return;
        if (result.ok) renderAuthority(result);
        ownerActionMessage.textContent = result.message || (result.ok ? "已采用远端访问令牌" : "未能采用远端凭据，请重新核对");
      } catch (_) {
        if (!disposed) ownerActionMessage.textContent = "请求结果未确认，请重新核对刷新归属";
      } finally {
        busy = false; check.disabled = false;
        if (!disposed) { delegate.disabled = false; retry.disabled = !latest?.can_retry; void poller.refresh(); }
      }
    });
    recover.addEventListener("click", async () => {
      if (busy || disposed) return;
      busy = true; recover.disabled = true; delegate.disabled = true; check.disabled = true; retry.disabled = true;
      ownerActionMessage.textContent = "正在核对本地新授权和远端版本";
      const recoveryPath = `/api/accounts/${item.id}/sub2api/auth-recovery`;
      try {
        const preview = await api.post(key + ":recovery-preview", recoveryPath + "/preview" + query, {});
        if (disposed) return;
        if (!preview.ok) { ownerActionMessage.textContent = preview.message || "状态未确认"; return; }
        if (!window.confirm(`${preview.email}\n将验证并推送本地新授权，只清除有凭据版本证据的远端认证错误。\n人工暂停、额度及权限限制不会解除。确认继续？`)) { ownerActionMessage.textContent = "已取消，未提交新授权"; return; }
        ownerActionMessage.textContent = "正在验证新授权；请等待操作回执";
        const result = await api.post(key + ":recovery", recoveryPath + query, preview.preconditions);
        if (!disposed) ownerActionMessage.textContent = result.message || result.error || "请核对操作回执";
      } catch (_) {
        if (!disposed) ownerActionMessage.textContent = "恢复结果未确认，请核对远端操作回执后再处理";
      } finally {
        busy = false;
        if (!disposed) { check.disabled = false; recover.disabled = false; delegate.disabled = false; retry.disabled = !latest?.can_retry; void poller.refresh(); }
      }
    });
    handback.addEventListener("click", async () => {
      if (busy || disposed) return;
      busy = true; handback.disabled = true; delegate.disabled = true; recover.disabled = true; check.disabled = true; retry.disabled = true;
      const returnPath = `/api/accounts/${item.id}/sub2api/refresh-return`;
      ownerActionMessage.textContent = "正在核对交接条件";
      try {
        const preview = await api.post(key + ":return-preview", returnPath + "/preview" + query, {});
        if (disposed) return;
        if (!preview.ok) { ownerActionMessage.textContent = preview.message || "暂不能交接"; return; }
        if (!window.confirm(preview.resume ? "继续同一交接？不会重新发起令牌刷新。" : `${preview.email || item.email || "此账号"}\n停止远端新刷新并等待在途结果，再用远端最后确定的访问和刷新凭据替换本地凭据。\n暂停和认证状态保持不变。确认交回 Team？`)) { ownerActionMessage.textContent = "已取消交回，未发起交接"; return; }
        ownerActionMessage.textContent = "正在排空远端刷新并处理交接";
        const result = await api.post(key + ":return", returnPath + query, preview.preconditions);
        if (!disposed) ownerActionMessage.textContent = result.message || "请继续同一交接以核对结果";
      } catch (_) {
        if (!disposed) ownerActionMessage.textContent = "交接结果未确认，请继续同一交接；不会自动恢复两边同时刷新";
      } finally {
        busy = false;
        if (!disposed) { check.disabled = false; renderAuthority(latest?.refresh_authority); retry.disabled = !latest?.can_retry; void poller.refresh(); }
      }
    });
    const observer = new MutationObserver(() => {
      if (!panel.isConnected || panel.closest(".sheet")?.hidden) dispose();
    });
    observer.observe(parent, {childList: true});
    const overlay = panel.closest(".sheet");
    if (overlay) observer.observe(overlay, {attributes: true, attributeFilter: ["hidden"]});
    function dispose() { if (disposed) return; disposed = true; poller.destroy(); observer.disconnect(); }
    disposeCurrent = dispose;
    void poller.refresh();
  }
  window.Team48Sub2ApiState = {mount, dispose: () => disposeCurrent()};
})();
