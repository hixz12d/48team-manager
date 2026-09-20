/* Persistent auto-rotation control. Polls observations; only a click changes policy. */
(() => {
  const button = document.getElementById("auto-rotation-toggle");
  const status = document.getElementById("auto-rotation-status");
  if (!button || !status) return;
  let enabled = false, saving = false, scopeReady = false;
  const states = { running: "执行中", queued: "排队中", waiting: "等待中", partial: "部分完成",
    success: "已完成", failed: "失败", manual_required: "待核对", cancelled: "已取消" };
  const poller = window.Team48Polling.createPoller({
    read: async signal => {
      const response = await fetch("/api/runtime/status", { signal, cache: "no-store" });
      if (!response.ok) throw new Error("后台状态暂时无法读取");
      return response.json();
    },
    onData: data => {
      if (saving) return;
      const rotation = data.auto_rotation || {};
      enabled = Boolean(rotation.enabled);
      scopeReady = rotation.scope === "all" || (rotation.workspace_ids || []).length > 0;
      button.disabled = false;
      button.textContent = enabled ? "关闭自动轮转" : scopeReady ? "开启自动轮转" : "选择工作空间后启用";
      button.setAttribute("aria-pressed", String(enabled));
      button.classList.toggle("primary", enabled);
      const scope = rotation.scope === "all" ? "全部工作空间（含新增）" : `仅选中的 ${(rotation.workspace_ids || []).length} 个工作空间`;
      const pieces = [enabled ? "已开启 · 每分钟检查" : "已关闭", scope, `每团队每日上限 ${rotation.daily_limit ?? 2} 次`];
      if (enabled && data.runner?.state !== "healthy") pieces.push("后台心跳未就绪，请核对运行状态");
      if (rotation.blocked_workspaces) pieces.push(`${rotation.blocked_workspaces} 个团队有未完成轮转，暂停进一步换号`);
      const scan = rotation.last_scan || {};
      if (scan.state === "failed") pieces.push("最近巡检失败，将在下一分钟重试");
      else if (scan.state === "running") pieces.push("巡检执行中");
      else if (scan.finished_at) pieces.push(`最近巡检 ${new Date(scan.finished_at).toLocaleTimeString("zh-CN")}，${scan.capped || 0} 个团队达日限`);
      else if (enabled) pieces.push("等待首次巡检");
      status.replaceChildren(document.createTextNode(pieces.join(" · ")));
      if (rotation.last_operation) {
        const op = rotation.last_operation;
        const link = document.createElement("a");
        link.href = `/operations?op=${encodeURIComponent(op.id)}`;
        link.textContent = ` · 最近轮转：${states[op.state] || "待核对"}，查看详情`;
        status.append(link);
      }
    },
    onError: () => { status.textContent = "后台状态读取失败，保留开关状态；稍后自动重试"; button.disabled = true; },
    delay: data => data?.counts?.running ? 2000 : 15000,
  });
  button.addEventListener("click", async () => {
    if (saving) return;
    if (!enabled && !scopeReady) { window.location.href = "/settings#auto-rotation-scope"; return; }
    saving = true; button.disabled = true;
    status.textContent = enabled ? "正在关闭，已开始的任务可在任务页取消…" : "正在开启自动轮转…";
    try {
      const response = await fetch("/api/settings", { method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ automation: { auto_rotate: !enabled } }),
      });
      if (!response.ok) throw new Error("save failed");
    } catch (_) {
      status.textContent = "开关保存未确认，请稍后重新核对";
    } finally {
      saving = false;
      await poller.refresh();
    }
  });
  void poller.refresh();
})();
