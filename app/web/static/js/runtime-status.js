/* Homepage runtime status uses only the authenticated local read model. */
(() => {
  const root = document.getElementById("runtime-status");
  if (!root) return;
  const stateLabels = { queued: "排队中", running: "进行中", waiting: "等待中", waiting_user: "等待人工", waiting_retry: "等待重试", waiting_browser: "等待浏览器", success: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消" };
  const runnerLabels = { healthy: "调度心跳正常", unknown: "尚无心跳", unavailable: "调度未就绪", stale: "调度心跳过期" };
  const el = (tag, cls) => { const n = document.createElement(tag); if (cls) n.className = cls; return n; };
  const text = (node, value) => { if (node.textContent !== String(value ?? "")) node.textContent = value ?? ""; };
  const time = value => value ? new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "未知";
  let updatedAt = null;
  function reconcile(container, items, key, update) {
    const old = new Map([...container.children].map(n => [n.dataset.key, n]));
    items.forEach((item, index) => {
      const id = String(key(item));
      const node = old.get(id) || el(container.tagName === "UL" ? "li" : "div");
      node.dataset.key = id; old.delete(id); update(node, item);
      if (container.children[index] !== node) container.insertBefore(node, container.children[index] || null);
    });
    old.forEach(node => node.remove());
  }
  function operation(node, item) {
    if (!node.firstChild) {
      node.className = "runtime-operation";
      node.append(el("a", "runtime-operation-title"), el("span", "runtime-state"), el("small", "runtime-operation-detail"));
    }
    const [title, status, detail] = node.children;
    title.href = `/operations?op=${encodeURIComponent(item.id)}`;
    text(title, `${item.operation_label} · ${item.target_label}`);
    text(status, stateLabels[item.status] || "状态未知");
    status.dataset.state = item.status;
    const seconds = item.elapsed_seconds;
    const duration = seconds == null ? null : (seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分钟`);
    text(detail, [item.stage_label, duration, `触发：${item.trigger_label}`, item.wait_reason,
      item.next_retry_at ? `重试 ${time(item.next_retry_at)}` : null, item.safe_error_message].filter(Boolean).join(" · "));
  }
  function render(data) {
    updatedAt = data.generated_at;
    const counts = data.counts || {};
    text(document.getElementById("runtime-counts"), `运行 ${counts.running || 0} · 排队 ${counts.queued || 0} · 等待 ${(counts.waiting || 0) + (counts.waiting_user || 0)}`);
    const runner = document.getElementById("runtime-runner");
    text(runner, runnerLabels[data.runner?.state] || "执行状态未知");
    runner.dataset.state = data.runner?.state;
    text(document.getElementById("runtime-browser"), `浏览器${data.browser_slot?.state === "busy" ? "占用中" : data.browser_slot?.state === "free" ? "空闲" : "状态未知"}`);
    text(document.getElementById("runtime-updated"), `更新于 ${time(updatedAt)}`);
    const error = document.getElementById("runtime-error"); error.hidden = true;
    reconcile(document.getElementById("runtime-active"), data.active_operations || [], item => item.id, operation);
    document.getElementById("runtime-idle").hidden = Boolean(data.active_operations?.length);
    const more = document.getElementById("runtime-more");
    const remainder = Math.max(0, (data.active_total || 0) - (data.active_operations?.length || 0));
    more.hidden = !remainder; text(more, `另外 ${remainder} 个任务`);
    reconcile(document.getElementById("runtime-policies"), data.policies || [], item => item.id, (node, item) => {
      if (!node.firstChild) { node.className = "runtime-policy"; node.append(el("span"), el("strong"), el("small")); }
      const [label, state, next] = node.children;
      text(label, item.label);
      text(state, item.state === "not_ready" ? "调度未就绪" : item.enabled ? "开启" : item.requested ? "部署未允许" : "关闭");
      state.dataset.state = item.state;
      text(next, item.enabled && item.next_run_at ? `下次扫描 ${time(item.next_run_at)}` : "");
    });
    reconcile(document.getElementById("runtime-recent"), data.recent_operations || [], item => item.id, (node, item) => {
      if (!node.firstChild) { node.className = "runtime-event"; node.append(el("a"), el("span", "runtime-state"), el("time")); }
      const [label, status, stamp] = node.children;
      label.href = `/operations?op=${encodeURIComponent(item.id)}`;
      text(label, `${item.target_label} · ${item.operation_label}`);
      text(status, stateLabels[item.status] || "状态未知"); status.dataset.state = item.status;
      text(stamp, time(item.finished_at || item.updated_at));
    });
    document.getElementById("runtime-recent-empty").hidden = Boolean(data.recent_operations?.length);
  }
  const poller = window.Team48Polling.createPoller({
    read: async signal => {
      const response = await fetch("/api/runtime/status", { headers: { Accept: "application/json" }, signal, cache: "no-store" });
      if (!response.ok) throw new Error("runtime_unavailable");
      return response.json();
    },
    onData: render,
    onError: () => {
      const error = document.getElementById("runtime-error"); error.hidden = false;
      text(error, `状态更新中断${updatedAt ? `，上次更新于 ${time(updatedAt)}` : "，尚未取得数据"}`);
    },
    delay: data => (data.counts?.running || data.counts?.queued || data.counts?.waiting) ? 2000 : 15000,
  });
  document.getElementById("runtime-refresh").addEventListener("click", () => void poller.refresh());
  window.Team48Runtime = { refresh: poller.refresh, destroy: poller.destroy };
  void poller.refresh();
})();
