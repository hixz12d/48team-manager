(() => {
  const readControllers = new Map();
  const readVersions = new Map();
  const inFlightWrites = new Map();
  const currentOperations = new Map();
  const CURRENT_OPERATION_STORAGE = "team48:current-operations";
  const palette = document.getElementById("command-palette");
  const commandInput = document.getElementById("command-input");
  const commandList = document.getElementById("command-list");
  const shellRoot = document.querySelector(".shell");
  const overlayState = {
    active: null,
    returnFocus: null,
    context: null,
    previousContext: null,
  };
  const overlayRegistry = new Map([
    ["entity", document.getElementById("entity-sheet")],
    ["register", document.getElementById("register-sheet")],
    ["reauth", document.getElementById("reauth-sheet")],
    ["phone-import", document.getElementById("phone-import-sheet")],
    ["proxy-add", document.getElementById("proxy-add-sheet")],
    ["proxy-edit", document.getElementById("proxy-edit-sheet")],
  ]);
  const sheet = overlayRegistry.get("entity");
  const menu = document.getElementById("action-menu");
  const registerSheet = overlayRegistry.get("register");
  const reauthSheet = overlayRegistry.get("reauth");
  const phoneImportSheet = overlayRegistry.get("phone-import");
  const proxyAddSheet = overlayRegistry.get("proxy-add");
  const proxyEditSheet = overlayRegistry.get("proxy-edit");
  let focusTrapRoot = null;
  let teamDetailState = null;
  const SECRET_MASK = "••••••";
  const pageCache = { items: [], kind: "", portfolio: null };
  let settingsBaseline = "";
  let settingsDirty = false;
  let searchTimer = null;

  const destinations = [
    { label: "去总览", href: "/" },
    { label: "去团队", href: "/workspaces" },
    { label: "去账号", href: "/accounts" },
    { label: "去手机号", href: "/resources/phones" },
    { label: "去 HME", href: "/resources/hme" },
    { label: "去代理", href: "/resources/proxies" },
    { label: "打开设置", href: "/settings" },
  ];

  const purposeLabels = { mother: "母号", child: "子号", standby: "待命", disabled: "停用", free: "空闲号" };
  const stateLabels = {
    active: "在用",
    available: "空闲",
    unused: "没用过",
    standby: "待命",
    conflict: "有冲突",
    archived: "已归档",
    unknown: "未授权",
    disabled: "停用",
    free: "空闲",
  };
  const statusLabels = {
    queued: "排队",
    running: "进行中",
    waiting: "等待",
    success: "完成",
    failed: "失败",
    cancelled: "已取消",
    manual_required: "需人工",
    partial: "部分完成",
    pending: "待确认",
    verified: "已验证",
    missing: "缺失",
    unbound: "未绑定",
    conflict: "冲突",
    none: "未绑定",
    set: "已设",
    off: "关着",
    unchecked: "未检测",
    healthy: "正常",
    normal: "正常",
    active: "正常",
    degraded: "降级",
    failed: "失败",
    refresh_due: "需刷新",
    refreshing: "刷新中",
    oauth_required: "要授权",
    phone_required: "需手机",
    deactivated: "已停用",
    ok: "正常",
    needs_auth: "要授权",
    identity_conflict: "账号对不上",
    vacancy: "有空位",
    billing: "账单异常",
    quota_probe: "额度刷新",
    reauth: "重新授权",
    onboard: "拉人",
    rotate: "轮转",
    workspace_sync: "同步官方成员",
    kick_member: "踢出成员",
    purge_child: "永久删除子号",
    revoke_invite: "撤回邀请",
    hme_reconcile: "HME 对账",
    hme_label_retry: "HME 标签重试",
    auth_probe: "检查授权",
    reconcile: "对账",
    sub2api_sync: "Sub2API 对账",
    sub2api_reconcile: "Sub2API 对账",
    sub2api_push: "Sub2API 推送",
    remote_missing: "远端未找到",
    not_eligible: "不适用",
    not_synced: "尚未同步",
    sync_failed: "同步失败",
    needs_management: "有人还没接入",
    membership_drift: "本地和官方对不上",
    full: "已满席",
    snapshot_updated: "快照已更新",
    verification_failed: "核对失败",
    proxy_check: "代理检测",
    free_register: "空闲号注册",
    reregister: "重注册",
  };
  const roleLabels = {
    owner: "所有者",
    member: "成员",
    admin: "管理员",
    "account-owner": "所有者",
    account_owner: "所有者",
    "workspace-owner": "所有者",
    "standard-user": "成员",
    "account-admin": "管理员",
  };

  const ERROR_CODE_MESSAGES = {
    missing_token: "该账号尚未授权，请先完成授权。",
    token_revoked: "账号授权已失效，请重新授权。",
    token_invalidated: "账号授权已失效，请重新授权。",
    callback_invalid: "授权回调无效，请重新复制完整回调地址。",
    callback_expired: "授权回调已过期，请重新生成授权链接。",
    account_not_found: "本地账号档案不存在，请先核对账号关系。",
    owner_account_missing: "该团队没有绑定本地母号，暂时无法直接授权。",
    identity_conflict: "该账号身份存在冲突，请先核对账号关系。",
    already_linked: "该成员已接入本地，无需重复接入。",
    membership_not_found: "未找到该成员的本地关系，请刷新后重试。",
    operation_in_progress: "相同操作正在进行，请等待当前操作完成。",
    operation_conflict: "相同操作正在进行，请等待当前操作完成。",
  };
  const ACTIVE_OPERATION_STATES = new Set(["pending", "queued", "running", "waiting"]);
  const TERMINAL_OPERATION_STATES = new Set(["success", "failed", "manual_required", "cancelled", "partial"]);

  function abortEntity(key) {
    const previous = readControllers.get(key);
    if (previous) previous.abort();
    const next = new AbortController();
    readControllers.set(key, next);
    return next;
  }

  function nextReadVersion(key) {
    const version = (readVersions.get(key) || 0) + 1;
    readVersions.set(key, version);
    return version;
  }

  function isLatestRead(key, version) {
    return readVersions.get(key) === version;
  }

  function isAbortError(error) {
    return Boolean(error) && (error.name === "AbortError" || /aborted/i.test(String(error.message || "")));
  }

  function extractErrorPayload(error) {
    if (error && typeof error === "object") {
      if (error.payload && typeof error.payload === "object") return error.payload;
      if (error.detail && typeof error.detail === "object") return error;
    }
    return {};
  }

  function extractErrorCode(error) {
    if (error && typeof error === "object" && error.errorCode) return error.errorCode;
    const payload = extractErrorPayload(error);
    const detail = payload.detail;
    if (typeof detail === "object" && detail) return detail.error_code || payload.error_code || null;
    return payload.error_code || null;
  }

  function extractErrorMessage(error) {
    const payload = extractErrorPayload(error);
    const detail = payload.detail;
    if (typeof detail === "object" && detail) return detail.message || payload.message || payload.error || "";
    if (typeof detail === "string") return detail;
    if (error && typeof error === "object") return error.message || payload.message || payload.error || "";
    return String(error || "");
  }

  class RequestError extends Error {
    constructor(message, { status, payload, errorCode } = {}) {
      super(message);
      this.name = "RequestError";
      this.status = status || 0;
      this.payload = payload || {};
      this.errorCode = errorCode || null;
    }
  }

  function legacyAuthStatus(item) {
    const state = item?.auth_state ?? item?.auth ?? item?.owner_auth_state ?? item?.owner_auth ?? "unknown";
    const needsAuth = ["oauth_required", "manual_required", "deactivated", "phone_required", "refresh_due", "unknown"].includes(state)
      || item?.needs_auth === true
      || item?.owner_needs_auth === true
      || item?.has_access_token === false;
    return {
      state,
      needsAuth,
      action: item?.auth_action ?? item?.owner_auth_action ?? (needsAuth ? "reauthorize" : null),
      reason: item?.auth_reason ?? item?.owner_auth_reason ?? (needsAuth ? state : null),
    };
  }

  function authStatus(item) {
    if (!item) return { state: "unknown", needsAuth: false, action: null, reason: null };
    if (typeof item.needs_auth === "boolean" || typeof item.owner_needs_auth === "boolean") {
      return {
        state: item.auth_state ?? item.owner_auth_state ?? item.auth ?? item.owner_auth ?? "unknown",
        needsAuth: Boolean(item.needs_auth ?? item.owner_needs_auth),
        action: item.auth_action ?? item.owner_auth_action ?? null,
        reason: item.auth_reason ?? item.owner_auth_reason ?? null,
      };
    }
    return legacyAuthStatus(item);
  }

  function needsAuth(item) {
    return authStatus(item).needsAuth;
  }

  function authActionLabel(action) {
    return action === "authorize" ? "授权" : "重新授权";
  }

  function roleLabel(value) {
    if (value == null || value === "") return "";
    const key = String(value).trim().toLowerCase().replaceAll("_", "-");
    return roleLabels[key] || roleLabels[value] || String(value);
  }

  function friendlyError(error) {
    if (isAbortError(error)) return "";
    const code = extractErrorCode(error);
    if (code && ERROR_CODE_MESSAGES[code]) return ERROR_CODE_MESSAGES[code];
    const text = extractErrorMessage(error) || "请求失败";
    if (/local access token missing/i.test(text) || /undecryptable/i.test(text)) {
      return ERROR_CODE_MESSAGES.missing_token;
    }
    if (/token_revoked|token_invalidated|invalidated oauth token/i.test(text)) {
      return ERROR_CODE_MESSAGES.token_revoked;
    }
    if (text.length > 180 || text.trim().startsWith("{") || text.trim().startsWith("[")) {
      return "请求失败，请重试。";
    }
    return text;
  }

  async function parseJsonResponse(response, key) {
    const payload = await response.json().catch(() => ({}));
    if (response.ok) return payload;
    const detail = payload.detail;
    const errorCode = typeof detail === "object" && detail ? detail.error_code : payload.error_code;
    const message = typeof detail === "object" && detail
      ? (detail.message || payload.message || payload.error || `请求失败: ${key}`)
      : (typeof detail === "string" ? detail : (payload.message || payload.error || `请求失败: ${key}`));
    throw new RequestError(message, { status: response.status, payload, errorCode });
  }

  async function fetchEntity(key, url, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const isWrite = !['GET', 'HEAD', 'OPTIONS'].includes(method);
    if (isWrite) return writeEntity(key, url, options);
    const version = nextReadVersion(key);
    const controller = abortEntity(key);
    const { headers, ...rest } = options;
    try {
      const response = await fetch(url, {
        ...rest,
        method,
        headers: { Accept: "application/json", ...(headers || {}) },
        signal: controller.signal,
      });
      const payload = await parseJsonResponse(response, key);
      if (!isLatestRead(key, version)) {
        const stale = new Error("stale read");
        stale.name = "AbortError";
        throw stale;
      }
      return payload;
    } catch (error) {
      if (isAbortError(error) || !isLatestRead(key, version)) {
        const abortError = error;
        abortError.name = "AbortError";
        throw abortError;
      }
      throw error;
    } finally {
      if (readControllers.get(key) === controller) readControllers.delete(key);
    }
  }

  async function writeEntity(key, url, options = {}) {
    if (inFlightWrites.has(key)) return inFlightWrites.get(key);
    const { headers, ...rest } = options;
    const pending = (async () => {
      const response = await fetch(url, {
        ...rest,
        headers: { Accept: "application/json", ...(headers || {}) },
      });
      return parseJsonResponse(response, key);
    })();
    inFlightWrites.set(key, pending);
    try {
      return await pending;
    } finally {
      inFlightWrites.delete(key);
    }
  }

  function labelOf(map, value) {
    if (value == null || value === "") return "—";
    return map[value] || String(value);
  }

  function toneFor(code) {
    if (["conflict", "identity_conflict", "failed", "manual_required", "deactivated", "danger"].includes(code)) return "danger";
    if (["warning", "needs_auth", "refresh_due", "oauth_required", "phone_required", "vacancy", "billing", "waiting", "pending", "quota_full", "unknown", "membership_drift", "needs_management", "not_synced"].includes(code)) return "warning";
    if (["success", "verified", "healthy", "ok", "active", "running"].includes(code)) return "success";
    if (["queued", "accent"].includes(code)) return "accent";
    return "";
  }

  function relativeTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    const future = seconds < 0;
    const abs = Math.abs(seconds);
    let text = "刚刚";
    if (abs >= 60 && abs < 3600) text = `${Math.floor(abs / 60)} 分钟${future ? "后" : "前"}`;
    else if (abs >= 3600 && abs < 86400) text = `${Math.floor(abs / 3600)} 小时${future ? "后" : "前"}`;
    else if (abs >= 86400) text = `${Math.floor(abs / 86400)} 天${future ? "后" : "前"}`;
    else if (abs >= 8 && abs < 60) text = `${abs} 秒${future ? "后" : "前"}`;
    return text;
  }

  function countdownLabel(resetAt, usedPercent) {
    if (usedPercent === 0 && !resetAt) return "现在";
    if (!resetAt) return "—";
    const date = new Date(resetAt);
    if (Number.isNaN(date.getTime())) return "—";
    const delta = date.getTime() - Date.now();
    if (delta <= 0) return "待刷新";
    const hours = Math.floor(delta / 3600000);
    const minutes = Math.floor((delta % 3600000) / 60000);
    if (hours >= 48) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
    if (hours >= 1) return `${hours}h ${minutes}m`;
    return `${Math.max(1, minutes)}m`;
  }

  function meterTone(percent) {
    if (percent == null) return "";
    if (percent >= 90) return "danger";
    if (percent >= 75) return "warning";
    return "";
  }

  function timeNode(value) {
    const span = document.createElement("span");
    span.className = "cell-sub";
    span.textContent = relativeTime(value);
    if (value) span.title = String(value);
    return span;
  }

  function statusNode(code, text) {
    const span = document.createElement("span");
    span.className = "status";
    const tone = toneFor(code);
    if (tone) span.dataset.tone = tone;
    span.textContent = text || labelOf(statusLabels, code);
    return span;
  }

  function cell(row, content, className) {
    const td = document.createElement("td");
    if (className) td.className = className;
    if (content instanceof Node) td.append(content);
    else td.textContent = content == null || content === "" ? "—" : String(content);
    row.append(td);
    return td;
  }

  function twoLine(primary, secondary) {
    const wrap = document.createElement("div");
    wrap.className = "cell-main";
    const strong = document.createElement("strong");
    strong.textContent = primary || "—";
    wrap.append(strong);
    if (secondary) {
      const sub = document.createElement("span");
      sub.className = "cell-sub";
      sub.textContent = secondary;
      wrap.append(sub);
    }
    return wrap;
  }

  function emptyState(title, detail, compact) {
    const wrap = document.createElement("div");
    wrap.className = compact ? "empty-state compact" : "empty-state";
    const strong = document.createElement("strong");
    strong.textContent = title;
    wrap.append(strong);
    if (detail) {
      const p = document.createElement("p");
      p.textContent = detail;
      wrap.append(p);
    }
    return wrap;
  }

  function statusBar(text) {
    const wrap = document.createElement("div");
    wrap.className = "status-bar";
    wrap.textContent = text;
    return wrap;
  }

  function showPageError(message) {
    const box = document.getElementById("page-feedback");
    const text = document.getElementById("page-feedback-text");
    if (!box || !text) return;
    text.textContent = message;
    box.hidden = false;
  }

  function hidePageError() {
    const box = document.getElementById("page-feedback");
    if (box) box.hidden = true;
  }

  function toast(message, tone = "muted", action) {
      if (!message) return;
      const region = document.getElementById("toast-region");
      if (!region) return;
      const item = document.createElement("div");
      item.className = `toast toast-${tone || "muted"}`;
      item.setAttribute("role", tone === "error" ? "alert" : "status");
      const textNode = document.createElement("span");
      textNode.textContent = message;
      item.append(textNode);
      if (action && action.label && action.onClick) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button ghost";
        button.textContent = action.label;
        button.addEventListener("click", () => {
          item.remove();
          action.onClick();
        });
        item.append(button);
      }
      region.append(item);
      window.setTimeout(() => item.remove(), 6500);
    }

  function operationTone(result) {
    if (result?.tone) return result.tone;
    const outcome = result?.outcome || "";
    if (result?.partial || result?.status === "partial" || result?.status === "manual_required") return "warning";
    if (["remote_missing", "not_eligible", "schema_mismatch", "verification_failed", "conflict"].includes(outcome)) return "warning";
    if (result?.ok || result?.success) return outcome === "remote_missing" ? "warning" : "success";
    return "error";
  }

  async function handleActionResult(result, { successMessage, refresh = true, stableKey, context } = {}) {
      const started = startCurrentOperation(stableKey, result, context);
      if (started) return result;
      const failed = !(result?.ok || result?.success) || result?.partial || ["partial", "failed", "manual_required"].includes(result?.status);
      const rawMessage = result?.message || (failed ? (result?.error || "操作失败") : (successMessage || "已完成"));
      const message = failed ? friendlyError(rawMessage) : rawMessage;
      const retryable = failed && Boolean(context?.retry);
      const action = retryable
        ? { label: "重试", onClick: context.retry }
        : (failed && result?.operation_id ? { label: "技术详情", onClick: () => openOperationById(result.operation_id) } : undefined);
      toast(message, operationTone(result), action);
      if (refresh) await bootPage();
      return result;
    }

  function confirmDanger(message) {
    return window.confirm(message);
  }

  function focusableNodes(root) {
      if (!root) return [];
      return Array.from(
        root.querySelectorAll('a[href], button:not([disabled]), textarea, input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')
      ).filter((node) => !node.hasAttribute("disabled") && node.getAttribute("aria-hidden") !== "true" && !node.closest("[hidden]"));
    }

  function activateFocusTrap(root) {
    focusTrapRoot = root;
    const nodes = focusableNodes(root);
    (nodes[0] || root)?.focus?.();
  }

  function clearFocusTrap() {
    focusTrapRoot = null;
  }

  function handleFocusTrap(event) {
    if (!focusTrapRoot || event.key !== "Tab") return;
    const nodes = focusableNodes(focusTrapRoot);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function overlayElement(name) {
    return overlayRegistry.get(name) || null;
  }

  function overlayNameOf(node) {
    for (const [name, element] of overlayRegistry.entries()) {
      if (element === node) return name;
    }
    return null;
  }

  function getActiveOverlay() {
    return overlayState.active;
  }

  function assertSinglePrimaryOverlay() {
    const count = document.querySelectorAll(
      '.sheet:not([hidden]) [aria-modal="true"], .drawer:not([hidden]) [aria-modal="true"]'
    ).length;
    if (count > 1) {
      console.error(`[OverlayManager] expected <= 1 primary overlay, found ${count}`);
    }
  }

  function setOverlayLock(locked) {
    if (shellRoot) {
      shellRoot.inert = locked;
      shellRoot.setAttribute("aria-hidden", locked ? "true" : "false");
    }
    document.body.classList.toggle("overlay-open", locked);
    document.body.style.overflow = locked ? "hidden" : "";
  }

  function hideOverlayElement(element) {
    if (!element) return;
    element.hidden = true;
  }

  function showOverlayElement(name, { initialFocus } = {}) {
    const overlay = overlayElement(name);
    if (!overlay) return null;
    overlay.hidden = false;
    overlayState.active = name;
    setOverlayLock(true);
    const panel = overlay.querySelector("[role='dialog']") || overlay;
    activateFocusTrap(panel);
    const focusTarget = typeof initialFocus === "string"
      ? overlay.querySelector(initialFocus)
      : initialFocus;
    (focusTarget || focusableNodes(panel)[0] || panel)?.focus?.();
    assertSinglePrimaryOverlay();
    return overlay;
  }

  function deactivateOverlay({ restoreFocus = true, discardPrevious = true } = {}) {
    const name = overlayState.active;
    const overlay = overlayElement(name);
    const returnTarget = overlayState.returnFocus;
    if (overlay) hideOverlayElement(overlay);
    overlayState.active = null;
    overlayState.returnFocus = null;
    overlayState.context = null;
    if (discardPrevious) overlayState.previousContext = null;
    clearFocusTrap();
    setOverlayLock(false);
    if (restoreFocus && returnTarget?.isConnected && !returnTarget.closest("[hidden]")) returnTarget.focus?.();
    return { name, overlay, returnTarget };
  }

  function openOverlay(name, { returnFocus, context, initialFocus } = {}) {
    const overlay = overlayElement(name);
    if (!overlay) return null;
    closeMenu();
    if (overlayState.active && overlayState.active !== name) {
      console.error(`[OverlayManager] ${overlayState.active} already open; use replaceOverlay() to switch to ${name}`);
      return overlayElement(overlayState.active);
    }
    if (!overlayState.active) {
      overlayState.returnFocus = returnFocus || document.activeElement;
      overlayState.context = context || null;
    } else if (context !== undefined) {
      overlayState.context = context;
    }
    return showOverlayElement(name, { initialFocus });
  }

  function replaceOverlay(name, options = {}) {
    const current = overlayState.active;
    if (current) {
      overlayState.previousContext = {
        name: current,
        returnFocus: overlayState.returnFocus,
        context: overlayState.context,
      };
      deactivateOverlay({ restoreFocus: false, discardPrevious: false });
    }
    return openOverlay(name, options);
  }

  function closeOverlay({ restoreFocus = true, discardPrevious = true } = {}) {
    if (!overlayState.active) return null;
    return deactivateOverlay({ restoreFocus, discardPrevious });
  }

  function restoreOverlayContext() {
    const previous = overlayState.previousContext;
    deactivateOverlay({ restoreFocus: false, discardPrevious: true });
    if (!previous?.name) return null;
    overlayState.previousContext = null;
    return openOverlay(previous.name, {
      returnFocus: previous.returnFocus,
      context: previous.context,
    });
  }

  function setButtonBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      button.dataset.idleLabel = button.textContent;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      if (label) button.textContent = label;
      return;
    }
    button.disabled = false;
    button.removeAttribute("aria-busy");
    if (button.dataset.idleLabel) button.textContent = button.dataset.idleLabel;
    delete button.dataset.idleLabel;
  }

  async function patchAction(key, url, body) {
    return fetchEntity(key, url, {
      method: "PATCH",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  async function postAction(key, url, body) {
    const options = {
      method: "POST",
      headers: { Accept: "application/json" },
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetchEntity(key, url, options);
  }

  async function deleteAction(key, url) {
    return fetchEntity(key, url, {
      method: "DELETE",
      headers: { Accept: "application/json" },
    });
  }

  function persistCurrentOperations() {
    const payload = Array.from(currentOperations.values()).map((entry) => ({
      stableKey: entry.stableKey,
      operationId: entry.operationId,
      entityType: entry.entityType,
      entityId: entry.entityId,
      action: entry.action,
      state: entry.state,
      startedAt: entry.startedAt,
    }));
    try {
      sessionStorage.setItem(CURRENT_OPERATION_STORAGE, JSON.stringify(payload));
    } catch {
      /* ignore quota / private mode */
    }
  }

  function cancelCurrentOperationPolling(stableKey) {
    const entry = currentOperations.get(stableKey);
    if (!entry) return;
    if (entry.timer) window.clearTimeout(entry.timer);
    entry.controller?.abort?.();
    entry.timer = null;
    entry.controller = null;
  }

  function stopPolling() {
    Array.from(currentOperations.keys()).forEach((key) => cancelCurrentOperationPolling(key));
  }

  function operationPollDelay(entry) {
    const elapsed = Date.now() - (entry.startedAt || Date.now());
    if (elapsed < 4000) return 1200;
    if (elapsed < 15000) return 2000;
    return 4000;
  }

  function startCurrentOperation(stableKey, response, context = {}) {
    const operationId = response?.operation_id || response?.id;
    const state = String(response?.status || response?.state || "").toLowerCase();
    if (!stableKey || !operationId || !ACTIVE_OPERATION_STATES.has(state)) return false;
    cancelCurrentOperationPolling(stableKey);
    const entry = {
      stableKey,
      operationId,
      entityType: context.entityType || null,
      entityId: context.entityId || null,
      action: context.action || null,
      state,
      controller: null,
      timer: null,
      startedAt: Date.now(),
      successMessage: context.successMessage,
    };
    currentOperations.set(stableKey, entry);
    persistCurrentOperations();
    pollCurrentOperation(stableKey);
    return true;
  }

  async function finishCurrentOperation(stableKey, result) {
    const entry = currentOperations.get(stableKey);
    cancelCurrentOperationPolling(stableKey);
    currentOperations.delete(stableKey);
    persistCurrentOperations();
    const failed = !(result?.ok || result?.success) || result?.partial || ["partial", "failed", "manual_required"].includes(result?.status || result?.state);
    const rawMessage = result?.message || (failed ? (result?.error || "操作失败") : (entry?.successMessage || "已完成"));
    const message = failed ? friendlyError(rawMessage) : rawMessage;
    toast(
      message,
      operationTone(result),
      failed && result?.operation_id ? { label: "技术详情", onClick: () => openOperationById(result.operation_id) } : undefined,
    );
    if (teamDetailState?.workspaceId) {
      try {
        await reloadTeamDetails();
        return;
      } catch (error) {
        if (!isAbortError(error)) toast(friendlyError(error), "error");
      }
    }
    await bootPage();
  }

  async function pollCurrentOperation(stableKey) {
    const entry = currentOperations.get(stableKey);
    if (!entry) return;
    cancelCurrentOperationPolling(stableKey);
    const controller = new AbortController();
    entry.controller = controller;
    try {
      const detail = await fetch(`/api/operations/${encodeURIComponent(entry.operationId)}`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      }).then((response) => parseJsonResponse(response, `operation-${entry.operationId}`));
      const state = String(detail.state || detail.status || "").toLowerCase();
      entry.state = state;
      persistCurrentOperations();
      if (TERMINAL_OPERATION_STATES.has(state)) {
        await finishCurrentOperation(stableKey, detail);
        return;
      }
      entry.timer = window.setTimeout(() => pollCurrentOperation(stableKey), operationPollDelay(entry));
    } catch (error) {
      if (isAbortError(error)) return;
      entry.timer = window.setTimeout(() => pollCurrentOperation(stableKey), Math.max(3000, operationPollDelay(entry)));
    }
  }

  function resumeCurrentOperations() {
    let stored = [];
    try {
      stored = JSON.parse(sessionStorage.getItem(CURRENT_OPERATION_STORAGE) || "[]");
    } catch {
      stored = [];
    }
    if (!Array.isArray(stored)) return;
    stored.forEach((item) => {
      if (!item?.stableKey || !item?.operationId) return;
      if (currentOperations.has(item.stableKey)) return;
      currentOperations.set(item.stableKey, {
        ...item,
        controller: null,
        timer: null,
        startedAt: item.startedAt || Date.now(),
      });
      pollCurrentOperation(item.stableKey);
    });
  }

  async function openOperationById(operationId) {
    if (!operationId) return;
    try {
      const detail = await fetchEntity(`operation-${operationId}`, `/api/operations/${encodeURIComponent(operationId)}`);
      openSheet("operation", detail);
    } catch (error) {
      toast(friendlyError(error), "error");
    }
  }

  function renderRows(bodyId, items, columns, renderItem, empty) {
    const body = document.getElementById(bodyId);
    if (!body) return;
    body.replaceChildren();
    if (!items.length) {
      const row = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = columns;
      td.append(empty);
      row.append(td);
      body.append(row);
      return;
    }
    items.forEach((item) => body.append(renderItem(item)));
  }

  function menuButton(kind, item) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button ghost";
    button.textContent = "…";
    button.dataset.menuTrigger = "1";
    button.setAttribute("aria-label", "更多操作");
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", "action-menu");
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openMenu(button, kind, item);
    });
    return button;
  }

  function bindRow(row, kind, item) {
      row.classList.add("is-interactive");
      row.tabIndex = 0;
      row.dataset.entityId = String(item.id);
      const open = () => kind === "workspace" ? openWorkspaceDetails(row, item) : openSheet(kind, item, row);
      row.addEventListener("click", (event) => {
        if (event.target.closest("button, a, input, select, details")) return;
        open();
      });
      row.addEventListener("keydown", (event) => {
        if (event.target === row && event.key === "Enter") open();
      });
    }

  function shortId(value) {
    const text = String(value || "");
    if (text.length <= 12) return text;
    return `${text.slice(0, 8)}…`;
  }


  function workspaceOfficialSummary(item) {
    const official = item.official || {};
    if ((official.sync_state || item.last_sync == null) === "never" || (item.last_sync == null && official.joined_people_total == null && item.members == null)) {
      return { main: "尚未同步", sub: "本地 " + String(item.managed?.count ?? item.managed_count ?? 0) };
    }
    if (official.sync_state === "failed" || item.health === "sync_failed") {
      return { main: "同步失败", sub: "数据可能过期" };
    }
    const people = official.joined_people_total ?? item.members;
    const children = official.joined_member_count;
    let main = people == null ? "—" : `${people} 人`;
    const owners = official.official_owner_count ?? official.owner_count;
    if (owners != null) main += ` · Owner ${owners}`;
    if (children != null) main += ` · Member ${children}`;
    if (official.occupied_seats != null && official.seat_limit != null) {
      main += ` · 席位 ${official.occupied_seats}/${official.seat_limit}`;
    }
    const managed = item.managed?.count ?? item.managed_count ?? 0;
    const pending = item.reconciliation?.actionable_count ?? item.reconciliation?.remote_only ?? 0;
    const sub = pending ? `本地 ${managed} · ${pending} 位待接入` : `本地 ${managed}`;
    return { main, sub };
  }

  function workspacePrimaryAction(workspace) {
    if (workspace.owner_auth_reason === "owner_account_missing" || workspace.owner_auth_state === "owner_account_missing") {
      return { id: "owner-missing", label: "去账号核对" };
    }
    if (workspace.owner_needs_auth && workspace.owner_account_id) {
      return {
        id: "owner-auth",
        label: workspace.owner_auth_action === "authorize" ? "授权" : "重新授权",
      };
    }
    if ((workspace.official?.sync_state ?? "never") === "never") {
      return { id: "sync", label: "同步" };
    }
    return { id: "manage", label: "管理" };
  }

  function runWorkspacePrimaryAction(workspace, trigger) {
    const action = workspacePrimaryAction(workspace);
    if (action.id === "owner-missing") {
      window.location.href = "/accounts";
      return;
    }
    if (action.id === "owner-auth") {
      openWorkspaceDetails(trigger, workspace);
      showTeamAuthStep({
        id: workspace.owner_account_id,
        email: workspace.owner_email,
        workspace_id: workspace.id,
        selectedMemberEmail: workspace.owner_email,
      });
      return;
    }
    if (action.id === "sync") {
      return (entityActions.workspace || []).find((item) => item.id === "workspace.sync")?.run(workspace, trigger);
    }
    return openWorkspaceDetails(trigger, workspace);
  }

  function workspaceRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "workspace", item);
    const summary = workspaceOfficialSummary(item);
    const ownerStatus = authStatus({
      needs_auth: item.owner_needs_auth,
      auth_state: item.owner_auth_state,
      auth_action: item.owner_auth_action,
      auth_reason: item.owner_auth_reason,
    });
    const healthCode = ownerStatus.reason === "owner_account_missing"
      ? "owner_account_missing"
      : (ownerStatus.needsAuth ? "needs_auth" : (item.health || item.status));
    const healthLabel = ownerStatus.reason === "owner_account_missing"
      ? "母号本地档案缺失"
      : (ownerStatus.needsAuth ? "母号要授权" : labelOf(statusLabels, item.health || item.status));
    cell(row, twoLine(item.display_name || item.name, shortId(item.official_workspace_id)));
    cell(row, item.owner_email);
    cell(row, twoLine(summary.main, summary.sub), "num");
    cell(row, statusNode(healthCode, healthLabel));
    const lastSync = cell(row, timeNode(item.last_sync), "row-action-host");
    const actions = document.createElement("div");
    actions.className = "row-actions row-actions-contextual";
    const primary = workspacePrimaryAction(item);
    const primaryButton = document.createElement("button");
    primaryButton.type = "button";
    primaryButton.className = primary.id === "owner-auth" ? "button danger compact" : "button compact";
    primaryButton.dataset.action = primary.id === "manage" ? "workspace.manage" : `workspace.${primary.id}`;
    primaryButton.textContent = primary.label;
    primaryButton.addEventListener("click", (event) => {
      event.stopPropagation();
      runWorkspacePrimaryAction(item, primaryButton);
    });
    actions.append(primaryButton, menuButton("workspace", item));
    lastSync.append(actions);
    return row;
  }

  function quotaMeter(label, percent, resetAt) {
    const row = document.createElement("div");
    const tone = meterTone(percent);
    row.className = tone ? `quota-meter is-${tone}` : "quota-meter";
    const tag = document.createElement("span");
    tag.className = "meter-window";
    tag.textContent = label;
    const bar = document.createElement("div");
    bar.className = "quota-bar";
    bar.setAttribute("role", "meter");
    bar.setAttribute("aria-label", `${label} 使用率`);
    if (percent != null) {
      bar.setAttribute("aria-valuenow", String(percent));
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      const fill = document.createElement("span");
      fill.style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
      bar.append(fill);
    }
    const pct = document.createElement("span");
    pct.className = "meter-pct tabular";
    pct.textContent = percent == null ? "—" : `${percent}%`;
    const eta = document.createElement("span");
    eta.className = "meter-ttl cell-sub tabular";
    eta.textContent = countdownLabel(resetAt, percent);
    row.append(tag, bar, pct, eta);
    return row;
  }

  function compactMetric(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(number);
  }

  function usageWindowLine(label, windowData) {
    const line = document.createElement("div");
    line.className = "usage-window tabular";
    const tag = document.createElement("span");
    tag.className = "meter-window";
    tag.textContent = label;
    const values = document.createElement("span");
    values.className = "usage-window-values";
    if (!windowData || windowData.last_success_at == null) {
      values.textContent = "—";
    } else {
      values.textContent = [
        windowData.requests == null ? null : `${compactMetric(windowData.requests)} req`,
        windowData.tokens == null ? null : `${compactMetric(windowData.tokens)} tok`,
        windowData.user_cost == null ? null : `U $${windowData.user_cost}`,
        windowData.account_cost == null ? null : `A $${windowData.account_cost}`,
      ].filter(Boolean).join(" · ") || "—";
      line.title = [
        windowData.standard_cost == null ? null : `标准计费 S $${windowData.standard_cost}`,
        windowData.billing_margin == null ? null : `计费毛差 Δ $${windowData.billing_margin}`,
        windowData.last_success_at ? `同步 ${relativeTime(windowData.last_success_at)}` : null,
        windowData.sync_status === "failed" ? (windowData.error || "最近同步失败，当前为旧数据") : null,
      ].filter(Boolean).join("；");
    }
    const state = document.createElement("span");
    state.className = "usage-window-state";
    state.textContent = windowData?.sync_status === "failed" ? "失败" : (windowData?.stale ? "旧" : "");
    line.append(tag, values, state);
    return line;
  }

  function workspaceUsageSummary(usage) {
    const windows = usage?.windows || {};
    const entry = windows.seven_day || windows.today || windows.five_hour;
    if (!entry?.last_success_at) return null;
    const label = entry === windows.seven_day ? "7d" : (entry === windows.today ? "今日" : "5h");
    const node = document.createElement("span");
    node.className = "workspace-usage-summary tabular";
    node.textContent = [
      label,
      entry.user_cost == null ? null : `U $${entry.user_cost}`,
      entry.account_cost == null ? null : `A $${entry.account_cost}`,
    ].filter(Boolean).join(" · ");
    const coverage = entry.coverage;
    node.title = [
      entry.standard_cost == null ? null : `标准计费 S $${entry.standard_cost}`,
      entry.billing_margin == null ? null : `计费毛差 Δ $${entry.billing_margin}`,
      coverage ? `覆盖 ${coverage.synced}/${coverage.total} 个已管理账号` : null,
      entry.stale ? "包含旧快照" : null,
    ].filter(Boolean).join("；");
    return node;
  }

  function quotaCell(item) {
    const wrap = document.createElement("div");
    wrap.className = "quota-cell";
    const quota = item.quota || {};
    const usage = item.usage || {};
    const windows = usage.windows || {};
    const primaryWindow = windows.five_hour || windows.today;
    if (primaryWindow || windows.seven_day) {
      const usageBlock = document.createElement("div");
      usageBlock.className = "usage-window-list";
      usageBlock.append(
        usageWindowLine(primaryWindow === windows.today ? "今日" : "5h", primaryWindow),
        usageWindowLine("7d", windows.seven_day),
      );
      wrap.append(usageBlock);
    }
    wrap.append(
      quotaMeter("5h", quota.five_hour_used_percent, quota.five_hour_reset_at),
      quotaMeter("7d", quota.seven_day_used_percent, quota.seven_day_reset_at),
    );
    const footer = document.createElement("div");
    footer.className = "quota-footer";
    const source = document.createElement("span");
    source.className = "cell-sub";
    const freshness = quota.queried_at ? relativeTime(quota.queried_at) : "无快照";
    source.textContent = (quota.source === "official" ? "官方" : (quota.source || "官方")) + " · " + freshness;
    if (quota.queried_at) source.title = quota.queried_at;
    footer.append(source);
    wrap.append(footer);
    return wrap;
  }

  function accountRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "account", item);
    const plan = [roleLabel(item.official_role), item.official_plan].filter(Boolean).join(" · ");
    cell(row, twoLine(item.email, plan || null));
    cell(row, item.workspace);
    cell(row, labelOf(purposeLabels, item.purpose));
    cell(row, statusNode(item.auth, labelOf(statusLabels, item.auth)));
    cell(row, quotaCell(item));
    cell(row, statusNode(item.sub2api, labelOf(statusLabels, item.sub2api)));
    const state = cell(row, statusNode(item.state, labelOf(stateLabels, item.state)), "row-action-host");
    const actions = document.createElement("div");
    actions.className = "row-actions row-actions-contextual";
    actions.append(menuButton("account", item));
    state.append(actions);
    return row;
  }

  function operationRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "operation", item);
    const checkWrap = document.createElement("div");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "operation-select";
    check.dataset.publicId = item.id;
    check.disabled = !["success", "partial", "failed", "cancelled", "manual_required"].includes(item.state || item.status) || Boolean(item.archived);
    check.addEventListener("click", (event) => event.stopPropagation());
    check.addEventListener("change", updateOperationsBulkButton);
    checkWrap.append(check);
    cell(row, checkWrap, "num");
    cell(row, statusNode(item.state || item.status, labelOf(statusLabels, item.state || item.status)));
    cell(row, item.operation_label || labelOf(statusLabels, item.operation));
    cell(row, twoLine(item.target_label || item.target || item.email || "—", item.outcome ? labelOf(statusLabels, item.outcome) : null));
    cell(row, item.workspace_name || item.workspace || "—");
    cell(row, item.business_step || (["success", "done"].includes(item.current_step) ? "已完成" : (item.current_step || "—")));
    cell(row, timeNode(item.updated || item.started));
    cell(row, menuButton("operation", item), "actions");
    return row;
  }

  function phoneRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "phone", item);
    cell(row, item.number);
    cell(row, statusNode(item.status, item.status));
    cell(row, `${item.used_count} / ${item.max_uses}`, "num");
    cell(row, item.risk_count ?? 0, "num");
    cell(row, item.reserved_by || "—");
    cell(row, timeNode(item.cooldown_until));
    cell(row, item.last_error_type || "—");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "button ghost";
    const enable = item.status !== "active";
    toggle.textContent = enable ? "启用" : "停用";
    toggle.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!confirmDanger(`${enable ? "启用" : "停用"}手机号 ${item.number}？`)) return;
      toggle.disabled = true;
      try {
        const result = await patchAction(`phone-status-${item.id}`, `/api/resources/phones/${item.id}`, {
          status: enable ? "active" : "disabled",
        });
        toast(result.ok ? "手机号状态已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
        await bootPage();
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        toggle.disabled = false;
      }
    });
    actions.append(toggle);
    const menuBtn = document.createElement("button");
    menuBtn.type = "button";
    menuBtn.className = "button ghost";
    menuBtn.textContent = "更多";
    menuBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      openMenu(menuBtn, "phone", item);
    });
    actions.append(menuBtn);
    cell(row, actions, "actions");
    return row;
  }

  
function hmeRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "hme", item);
    cell(row, item.email);
    cell(row, item.state);
    cell(row, item.label || "—");
    cell(row, item.pending ? "待同步" : "已同步");
    cell(row, item.job_id || "—");
    cell(row, timeNode(item.expires_at));
    const actions = document.createElement("div");
    actions.className = "row-actions";
    if (item.pending) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "button ghost";
      retry.textContent = "重试标签";
      retry.dataset.action = "hme.retry-label";
      retry.addEventListener("click", async (event) => {
        event.stopPropagation();
        retry.disabled = true;
        try {
          const result = await postAction(`hme-retry-${item.id}`, `/api/resources/hme/${item.id}/retry-label`);
          await handleActionResult(result, { successMessage: result.message || "标签已同步" });
        } catch (error) {
          toast(friendlyError(error), "error");
        } finally {
          retry.disabled = false;
        }
      });
      actions.append(retry);
    }
    actions.append(menuButton("hme", item));
    cell(row, actions, "actions");
    return row;
  }

  function proxyRow(item) {
    const row = document.createElement("tr");
    bindRow(row, "proxy", item);
    cell(row, item.name);
    cell(row, item.region || "—");
    cell(row, item.last_exit_ip || "—");
    cell(row, statusNode(item.status, item.status === "active" ? "启用" : item.status));
    cell(row, statusNode(item.health_state || "unchecked", labelOf(statusLabels, item.health_state || "unchecked")));
    cell(row, `${item.scheme}://${item.host}:${item.port}`);
    cell(row, timeNode(item.last_checked_at));
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const probe = document.createElement("button");
    probe.type = "button";
    probe.className = "button ghost";
    probe.textContent = "检测";
    probe.dataset.action = "proxy.probe";
    probe.addEventListener("click", async (event) => {
      event.stopPropagation();
      probe.disabled = true;
      try {
        const result = await postAction(`proxy-probe-${item.id}`, `/api/resources/proxies/${item.id}/probe`);
        await handleActionResult(result, { successMessage: result.message || (result.last_exit_ip ? ("检测成功 " + result.last_exit_ip) : "检测完成") });
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        probe.disabled = false;
      }
    });
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "button ghost";
    edit.textContent = "编辑";
    edit.setAttribute("aria-label", "编辑代理");
    edit.addEventListener("click", (event) => {
      event.stopPropagation();
      openProxyProfileEdit(edit, item);
    });
    actions.append(edit, probe, menuButton("proxy", item));
    cell(row, actions, "actions");
    return row;
  }

  function matchesQuery(item, query, fields) {
    if (!query) return true;
    const hay = fields.map((key) => String(item[key] || "")).join(" ").toLowerCase();
    return hay.includes(query);
  }

  function currentQuery() {
    return new URLSearchParams(window.location.search);
  }

  function writeQuery(next) {
    const url = new URL(window.location.href);
    url.search = next.toString();
    window.history.replaceState({}, "", url);
  }

  function filterItems(kind, items) {
    const params = currentQuery();
    const q = (params.get("q") || "").trim().toLowerCase();
    if (kind === "workspace") {
      return items.filter((item) =>
        matchesQuery(item, q, ["name", "owner_email", "official_workspace_id", "id"])
      );
    }
    if (kind === "account") {
      return items.filter((item) =>
        matchesQuery(item, q, ["email", "workspace", "official_user_id", "official_account_id"])
      );
    }
    return items;
  }

  function setCount(id, shown, total) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = shown === total ? `共 ${total} 个` : `${shown} / ${total}`;
  }

  function renderOverview(payload) {
    const summary = payload.summary || {};
    const set = (key, value, alert) => {
      const node = document.querySelector(`[data-summary="${key}"]`);
      if (!node) return;
      node.textContent = value ?? 0;
      node.parentElement.classList.toggle("is-alert", Boolean(alert && value));
    };
    const attention = (payload.attention || []).filter((item) => item.kind !== "operation");
    set("workspaces", summary.workspaces ?? payload.workspaces);
    set("accounts", summary.accounts ?? payload.accounts);
    set("attention", attention.length, true);
    set("conflicts", summary.identity_conflicts, true);

    const attentionRoot = document.getElementById("overview-attention");
    const attentionPanel = document.getElementById("overview-attention-panel");
    attentionRoot.replaceChildren();
    if (!attention.length) {
      if (attentionPanel) {
        const header = attentionPanel.querySelector(".panel-header");
        if (header) header.hidden = true;
      }
      attentionRoot.append(statusBar("当前没有需要处理的事项"));
    } else {
      if (attentionPanel) {
        const header = attentionPanel.querySelector(".panel-header");
        if (header) header.hidden = false;
      }
      const list = document.createElement("div");
      list.className = "attention-list";
      attention.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const left = document.createElement("div");
        left.append(twoLine(item.email || item.message, item.message));
        if (item.workspace) {
          const extra = document.createElement("div");
          extra.className = "cell-sub";
          extra.textContent = item.workspace;
          left.append(extra);
        }
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button ghost";
        button.textContent = item.action || "查看";
        button.addEventListener("click", () => {
          if (item.href) {
            window.location.href = item.href;
            return;
          }
          if (item.account_id) {
            window.location.href = `/accounts?account=${encodeURIComponent(item.account_id)}`;
            return;
          }
          if (item.workspace_id) {
            window.location.href = `/workspaces?workspace=${encodeURIComponent(item.workspace_id)}`;
            return;
          }
          window.location.href = "/accounts?purpose=conflict";
        });
        row.append(left, button);
        list.append(row);
      });
      attentionRoot.append(list);
    }

    const healthRoot = document.getElementById("overview-health");
    healthRoot.replaceChildren();
    const health = payload.workspace_health || [];
    if (!health.length) {
      healthRoot.append(emptyState("还没有工作区", "点右上角「登记团队」，用母号 OAuth 授权。", true));
    } else {
      const list = document.createElement("div");
      list.className = "health-list";
      health.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const seats = item.joined_people_total != null ? `${item.joined_people_total} 人 · Owner ${item.official_owner_count ?? item.owner_count ?? 0} / Member ${item.official_member_count ?? item.joined_member_count ?? item.members ?? 0}` : (item.sync_state === "never" ? "尚未同步" : (item.members == null ? "尚未同步" : `${item.members} 子成员`));
        const sub = [item.owner_email, seats, item.last_sync ? relativeTime(item.last_sync) : null].filter(Boolean).join(" · ");
        row.append(twoLine(item.display_name || item.name, sub), statusNode(item.health, labelOf(statusLabels, item.health)));
        list.append(row);
      });
      healthRoot.append(list);
    }

    stopPolling();
  }


  function kvSection(title, rows) {
    const section = document.createElement("section");
    section.className = "sheet-section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const dl = document.createElement("dl");
    dl.className = "kv";
    rows.forEach(([label, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value == null || value === "" ? "—" : String(value);
      dl.append(dt, dd);
    });
    section.append(heading, dl);
    return section;
  }

  function openSheet(kind, item, trigger) {
    if (!sheet) return;
    const title = document.getElementById("sheet-title");
    const subtitle = document.getElementById("sheet-subtitle");
    const body = document.getElementById("sheet-body");
    body.replaceChildren();
    if (kind === "account") {
      title.textContent = item.email;
      subtitle.textContent = [labelOf(purposeLabels, item.purpose), item.workspace].filter(Boolean).join(" · ");
      body.append(
        kvSection("运行摘要", [
          ["授权", labelOf(statusLabels, item.auth)],
          ["运行状态", labelOf(stateLabels, item.state)],
          ["官方计划", item.official_plan],
          ["官方角色", roleLabel(item.official_role)],
          ["Membership", item.membership_state],
        ]),
        kvSection("官方额度", [
          ["5h", item.quota?.five_hour_used_percent == null ? "—" : `${item.quota.five_hour_used_percent}%`],
          ["7d", item.quota?.seven_day_used_percent == null ? "—" : `${item.quota.seven_day_used_percent}%`],
          ["查询时间", item.quota?.queried_at || "—"],
        ]),
        kvSection("Binding", [
          ["状态", labelOf(statusLabels, item.sub2api)],
          ["远端 ID", item.binding?.remote_id],
          ["核对邮箱", item.binding?.verified_email],
          ["最近错误", item.binding?.last_error],
        ]),
        kvSection("技术信息", [
          ["Official user ID", item.official_user_id],
          ["Official account ID", item.official_account_id],
          ["AT", item.has_access_token ? "已保存" : "未设置"],
          ["RT", item.has_refresh_token ? "已保存" : "未设置"],
          ["代理", item.proxy_url || labelOf(statusLabels, item.proxy)],
          ["身份审计", item.identity],
          ["原因", (item.reasons || []).join("；")],
        ])
      );
    } else if (kind === "operation") {
      title.textContent = labelOf(statusLabels, item.operation);
      subtitle.textContent = item.target || item.email || item.id;
      const result = item.result || {};
      const steps = item.steps || [];
      const logs = item.log || [];
      body.append(
        kvSection("任务", [
          ["状态", labelOf(statusLabels, item.state || item.status)],
          ["摘要", result.message || item.error || "—"],
          ["对象", item.target || item.email],
          ["开始", relativeTime(item.started) + (item.started ? ` · ${item.started}` : "")],
          ["更新", relativeTime(item.updated) + (item.updated ? ` · ${item.updated}` : "")],
        ])
      );
      if (result.joined != null || result.remote_only != null) {
        body.append(
          kvSection("同步结果", [
            ["官方已加入", result.joined],
            ["待邀请", result.invited],
            ["本地受管", result.managed],
            ["仅官方", result.remote_only],
            ["仅本地", result.local_only],
            ["原始条数", result.raw_item_count],
            ["解析条数", result.parsed_item_count],
          ])
        );
      }
      if (steps.length) {
        body.append(kvSection("步骤", steps.map((step) => [step.step_name, `${labelOf(statusLabels, step.state)}${step.error_message ? " · " + step.error_message : ""}`])));
      }
      if (logs.length) {
        body.append(kvSection("日志", logs.slice(-8).map((row) => [row.ts || "", row.message || row.stage || ""])));
      }
    } else if (kind === "phone") {
      title.textContent = item.number;
      body.append(
        kvSection("手机号", [
          ["状态", item.status],
          ["成功 / 上限", `${item.used_count} / ${item.max_uses}`],
          ["风险", item.risk_count],
          ["租约", item.reserved_by],
          ["最后结果", item.last_error_type],
        ])
      );
    } else if (kind === "hme") {
      title.textContent = item.email;
      body.append(
        kvSection("HME", [
          ["状态", item.state],
          ["标签", item.label],
          ["同步", item.pending ? "待同步" : "已同步"],
          ["任务", item.job_id],
          ["到期", item.expires_at],
          ["错误", item.last_error],
        ])
      );
    } else if (kind === "proxy") {
      title.textContent = item.name;
      const sections = [
        kvSection("详情", [
          ["地区", item.region],
          ["出口 IP", item.last_exit_ip],
          ["状态", item.status],
          ["健康", item.health_state],
          ["地址", `${item.scheme}://${item.host}:${item.port}`],
          ["最近检测", item.last_checked_at],
          ["绑定数", item.binding_count ?? (item.bindings || []).length],
        ]),
      ];
      const bindings = item.bindings || [];
      if (bindings.length) {
        sections.push(
          kvSection(
            "绑定账号",
            bindings.map((row) => [row.email, [row.purpose, row.auth, row.state].filter(Boolean).join(" · ")])
          )
        );
      } else if (item.binding_count === 0) {
        sections.push(kvSection("绑定账号", [["账号", "当前没有账号绑定到这份代理"]]));
      }
      body.append(...sections);
    }
    openOverlay("entity", { returnFocus: trigger, context: { kind, item }, initialFocus: "[data-close-sheet]" });
  }

  function closeSheet() {
      if (teamDetailState) teamDetailState = null;
      closeOverlay();
    }

  async function pushSub2ApiAccount(item) {
    const query = item.workspace_id ? `?workspace_id=${encodeURIComponent(item.workspace_id)}` : "";
    const body = {};
    const preview = await fetchEntity(
      `account-sub2api-preview-${item.id}`,
      `/api/accounts/${item.id}/sub2api/preview${query}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
      },
    );
    const updateText = (preview.would_update || []).join(", ") || "无";
    const preserveText = (preview.would_preserve || []).join(", ");
    const prompt = [
      preview.action === "create" ? "确认创建 Sub2API 账号？" : "确认更新 Sub2API 账号？",
      `将写入：${updateText}`,
      preserveText ? `将保留：${preserveText}` : null,
      ...(preview.warnings || []),
    ].filter(Boolean).join("\n");
    if (!window.confirm(prompt)) return;
    const result = await postAction(
      `account-sub2api-push-${item.id}`,
      `/api/accounts/${item.id}/sub2api/push${query}`,
      body,
    );
    await handleActionResult(result, {
      successMessage: result.message || (preview.action === "create" ? "Sub2API 推送完成" : "Sub2API 更新完成"),
    });
  }

  const entityActions = {
    workspace: [
      { id: "workspace.manage", label: "管理", run: (item, trigger) => openWorkspaceDetails(trigger, item) },
      {
        id: "workspace.sync",
        label: "同步",
        run: async (item) => {
          const result = await postAction(`workspace-sync-${item.id}`, `/api/workspaces/${item.id}/sync`);
          await handleActionResult(result, { successMessage: "同步完成" });
        },
      },
    ],
    team: [
      {
        id: "team.member.invite",
        label: "邀请加入 Team",
        run: ({ workspace, values }, trigger) => inviteTeamMember(workspace, values, trigger),
      },
      {
        id: "team.member.link",
        label: "接入本地",
        visible: ({ row }) => presentTeamMember(row).primaryId === "team.member.link",
        run: ({ workspace, row }, trigger) => linkTeamMember(workspace, row, trigger),
      },
      {
        id: "team.member.reauth",
        label: ({ row }) => authActionLabel(authStatus(row).action),
        visible: ({ row }) => presentTeamMember(row).primaryId === "team.member.reauth",
        run: ({ workspace, row }) => showTeamAuthStep({ id: row.id || row.local_account_id, email: row.email, workspace_id: workspace.id, selectedMemberEmail: row.email }),
      },
      {
        id: "team.member.role",
        label: ({ row }) => String(row.role || row.official_role || "").toLowerCase() === "owner" ? "改成 Member" : "改成 Owner",
        visible: ({ row }) => teamMemberIsJoined(row) && Boolean(row.email),
        run: ({ workspace, row }, trigger) => changeTeamMemberRole(workspace, row, trigger, String(row.role || row.official_role || "").toLowerCase() === "owner" ? "member" : "owner"),
      },
      {
        id: "team.member.local-remove",
        label: "仅移出本地",
        danger: true,
        visible: ({ row }) => Boolean(row.id || row.local_account_id),
        run: ({ workspace, row }, trigger) => removeTeamMember(workspace, row, trigger, "local"),
      },
      {
        id: "team.member.official-remove",
        label: ({ row }) => presentTeamMember(row).code === "invited" ? "撤回邀请" : "移出官方席位",
        danger: true,
        visible: ({ row }) => Boolean(row.email),
        run: ({ workspace, row }, trigger) => removeTeamMember(workspace, row, trigger, "official"),
      },
      {
        id: "team.member.purge",
        label: "永久删除",
        danger: true,
        visible: ({ row }) => Boolean(row.email),
        run: ({ workspace, row }, trigger) => removeTeamMember(workspace, row, trigger, "purge"),
      },
    ],
    account: [
      { id: "account.view", label: "查看详情", run: (item, trigger) => openSheet("account", item, trigger) },
      {
        id: "account.reauth",
        label: "授权",
        run: (item, trigger) => openReauth(item, trigger),
      },
      {
        id: "account.refresh",
        label: "刷新状态",
        run: async (item) => {
          const result = await postAction(`account-refresh-${item.id}`, `/api/accounts/${item.id}/refresh`);
          await handleActionResult(result, { successMessage: result.message || "刷新完成" });
        },
      },
      {
        id: "account.auth",
        label: "检查授权",
        run: async (item) => {
          const result = await postAction(`account-auth-${item.id}`, `/api/accounts/${item.id}/auth/probe`);
          await handleActionResult(result, { successMessage: result.message || "授权探测完成" });
        },
      },
      {
        id: "account.quota",
        label: "刷新额度",
        run: async (item) => {
          const result = await postAction(`account-quota-${item.id}`, `/api/accounts/${item.id}/quota/probe`);
          await handleActionResult(result, { successMessage: result.message || "额度刷新完成" });
        },
      },
      {
        id: "account.sub2api",
        label: "Sub2API",
        visible: () => false,
        run: async () => {},
      },
      {
        id: "account.sub2api.push",
        label: "推送到 Sub2API",
        visible: (item) => (item.sub2api_publish?.eligible !== false) && ["missing", "unbound", "none", "pending"].includes(item.sub2api),
        run: (item) => pushSub2ApiAccount(item),
      },
      {
        id: "account.sub2api.update",
        label: "更新 Sub2API",
        visible: (item) => (item.sub2api_publish?.eligible !== false) && item.sub2api === "verified",
        run: (item) => pushSub2ApiAccount(item),
      },
      {
        id: "account.sub2api.reconcile",
        label: "重新对账 Sub2API",
        visible: (item) => item.sub2api_publish?.eligible !== false,
        run: async (item) => {
          const result = await postAction(`account-sub2api-reconcile-${item.id}`, `/api/accounts/${item.id}/sub2api/reconcile`);
          await handleActionResult(result, { successMessage: result.message || "Sub2API 对账完成" });
        },
      },
      {
        id: "account.sub2api.usage",
        label: "同步用量与计费",
        visible: (item) => item.sub2api === "verified",
        run: async (item) => {
          const query = item.workspace_id ? `?workspace_id=${encodeURIComponent(item.workspace_id)}` : "";
          const result = await postAction(`account-sub2api-usage-${item.id}`, `/api/accounts/${item.id}/sub2api/usage/sync${query}`, {});
          await handleActionResult(result, { successMessage: result.message || "用量同步完成" });
        },
      },
      {
        id: "account.copy",
        label: "复制邮箱",
        visible: (item) => Boolean(item.email),
        run: async (item) => {
          const ok = await copyText(item.email);
          toast(ok ? "邮箱已复制" : "复制失败", ok ? "success" : "error");
        },
      },
      {
        id: "account.proxy",
        label: "修改代理",
        visible: (item) => item.purpose === "mother",
        run: (item, trigger) => openProxyEdit(trigger, item),
      },

    ],
    operation: [
      { id: "operation.view", label: "查看详情", run: (item, trigger) => openSheet("operation", item, trigger) },
      {
        id: "operation.cancel",
        label: "请求取消",
        visible: (item) => Boolean(item.can_cancel),
        run: async (item) => {
          const result = await postAction(`operation-cancel-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/cancel`);
          toast(result.ok ? "已请求取消" : (result.error || "取消失败"), result.ok ? "success" : "error");
          await openOperationById(item.id);
          await bootPage();
        },
      },
      {
        id: "operation.retry",
        label: "安全重试",
        visible: (item) => Boolean(item.can_retry),
        run: async (item) => {
          const result = await postAction(`operation-retry-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/retry`);
          await handleActionResult(result, { successMessage: result.message || "重试完成" });
        },
      },
      {
        id: "operation.archive",
        label: "清除记录",
        visible: (item) => !item.archived && ["success", "partial", "failed", "cancelled", "manual_required"].includes(item.state || item.status),
        run: async (item) => {
          if (!confirmDanger(`确认清除任务 ${item.id}？记录会软归档，可在“已清除”恢复。`)) return;
          const result = await patchAction(`operation-archive-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/archive`, { reason: "manual_clear" });
          toast(result.ok ? "已清除记录" : (result.error || "清除失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
      {
        id: "operation.restore",
        label: "恢复记录",
        visible: (item) => Boolean(item.archived),
        run: async (item) => {
          const result = await patchAction(`operation-restore-${item.id}`, `/api/operations/${encodeURIComponent(item.id)}/restore`, {});
          toast(result.ok ? "已恢复记录" : (result.error || "恢复失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
    ],
    phone: [
      { id: "phone.view", label: "查看详情", run: (item, trigger) => openSheet("phone", item, trigger) },
      {
        id: "phone.copy",
        label: "复制号码",
        visible: (item) => Boolean(item.number),
        run: async (item) => {
          const ok = await copyText(item.number);
          toast(ok ? "号码已复制" : "复制失败", ok ? "success" : "error");
        },
      },
      {
        id: "phone.toggle",
        label: "启用/停用",
        run: async (item) => {
          const enable = item.status !== "active";
          if (!confirmDanger(`${enable ? "启用" : "停用"}手机号 ${item.number}？`)) return;
          const result = await patchAction(`phone-status-${item.id}`, `/api/resources/phones/${item.id}`, {
            status: enable ? "active" : "disabled",
          });
          toast(result.ok ? "手机号状态已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
      {
        id: "phone.reset-cooldown",
        label: "重置冷却",
        run: async (item) => {
          if (!confirmDanger(`确认重置 ${item.number} 的冷却时间？`)) return;
          const result = await postAction(`phone-reset-${item.id}`, `/api/resources/phones/${item.id}/reset-cooldown`);
          toast(result.ok ? "冷却已重置" : (result.error || "重置失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },

    ],
    hme: [
      { id: "hme.view", label: "查看详情", run: (item, trigger) => openSheet("hme", item, trigger) },
      {
        id: "hme.retry-label",
        label: "重试标签同步",
        visible: (item) => Boolean(item.pending),
        run: async (item) => {
          const result = await postAction(`hme-retry-${item.id}`, `/api/resources/hme/${item.id}/retry-label`);
          await handleActionResult(result, { successMessage: result.message || "标签已同步" });
        },
      },
      {
        id: "hme.release",
        label: "安全释放",
        visible: (item) => item.state === "reserved" && !item.pending,
        run: async (item) => {
          if (!window.confirm(`确认安全释放 ${item.email}？仅 reserved 且未进入注册的租约可释放。`)) return;
          const result = await postAction(`hme-release-${item.id}`, `/api/resources/hme/${item.id}/release`);
          toast(result.ok ? "已释放" : (result.error || "释放失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
    ],
    proxy: [
      { id: "proxy.view", label: "查看详情", run: (item, trigger) => openSheet("proxy", item, trigger) },
      {
        id: "proxy.edit",
        label: "编辑",
        run: (item, trigger) => openProxyProfileEdit(trigger, item),
      },
      {
        id: "proxy.probe",
        label: "检测",
        run: async (item) => {
          const result = await postAction(`proxy-probe-${item.id}`, `/api/resources/proxies/${item.id}/probe`);
          await handleActionResult(result, { successMessage: result.message || (result.last_exit_ip ? ("检测成功 " + result.last_exit_ip) : "检测完成") });
        },
      },
      {
        id: "proxy.sub2api.sync",
        label: "同步到 Sub2API",
        run: async (item) => {
          const result = await postAction(`proxy-sub2api-${item.id}`, `/api/resources/proxies/${item.id}/sub2api/sync`, {});
          await handleActionResult(result, { successMessage: result.action === "unchanged" ? "远端代理已是最新" : "代理同步完成" });
        },
      },
      {
        id: "proxy.copy",
        label: "复制脱敏地址",
        run: async (item) => {
          const ok = await copyText(item.url || `${item.scheme}://${item.host}:${item.port}`);
          toast(ok ? "已复制" : "复制失败", ok ? "success" : "error");
        },
      },
      {
        id: "proxy.bindings",
        label: "查看绑定账号",
        run: async (item, trigger) => {
          const payload = await fetchEntity(`proxy-bindings-${item.id}`, `/api/resources/proxies/${item.id}/bindings`);
          const bound = payload.items || [];
          openSheet("proxy", {
            ...item,
            bindings: bound,
            binding_count: payload.count ?? bound.length,
          }, trigger);
        },
      },
      {
        id: "proxy.disable",
        label: "停用代理",
        visible: (item) => item.status === "active",
        run: async (item) => {
          if (!confirmDanger(`确认停用代理 ${item.name || item.host}？不会删除档案。`)) return;
          const result = await patchAction(`proxy-status-${item.id}`, `/api/resources/proxies/${item.id}`, { status: "disabled" });
          toast(result.ok ? "代理已停用" : (result.error || "停用失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },
      {
        id: "proxy.enable",
        label: "启用代理",
        visible: (item) => item.status !== "active",
        run: async (item) => {
          const result = await patchAction(`proxy-status-${item.id}`, `/api/resources/proxies/${item.id}`, { status: "active" });
          toast(result.ok ? "代理已启用" : (result.error || "启用失败"), result.ok ? "success" : "error");
          await bootPage();
        },
      },

    ],
  };

  function openMenu(button, kind, item) {
    if (!menu) return;
    menu.replaceChildren();
    const add = (label, handler, className) => {
      const option = document.createElement("button");
      option.type = "button";
      option.setAttribute("role", "menuitem");
      option.textContent = label;
      if (className) option.className = className;
      option.addEventListener("click", async () => {
        closeMenu();
        try {
          await handler();
        } catch (error) {
          toast(friendlyError(error), "error");
        }
      });
      menu.append(option);
    };
    const actions = (entityActions[kind] || []).filter((action) => !action.visible || action.visible(item));
    actions.forEach((action) => add(action.label, () => action.run(item, button)));
    if (!menu.childElementCount) {
      button.hidden = true;
      return;
    }
    const rect = button.getBoundingClientRect();
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    const width = Math.max(menu.offsetWidth || 180, 180);
    const height = menu.offsetHeight || 0;
    let left = rect.left;
    let top = rect.bottom + 4;
    if (left + width > window.innerWidth - 8) left = Math.max(8, window.innerWidth - width - 8);
    if (top + height > window.innerHeight - 8) top = Math.max(8, rect.top - height - 4);
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
  }

  function closeMenu() {
    if (!menu) return;
    menu.hidden = true;
    document.querySelectorAll('[data-menu-trigger][aria-expanded="true"]').forEach((node) => node.setAttribute("aria-expanded", "false"));
  }

  function setRegisterStatus(text, tone) {
      const statusEl = document.getElementById("register-status");
      if (!statusEl) return;
      statusEl.hidden = !text;
      statusEl.className = tone || "muted";
      statusEl.setAttribute("role", tone === "error" ? "alert" : "status");
      statusEl.textContent = text || "";
    }

  function resetRegisterForm() {
    const form = document.getElementById("register-form");
    const oauth = document.getElementById("register-oauth");
    const authorize = document.getElementById("register-authorize-url");
    const openLink = document.getElementById("register-open-link");
    const submit = document.getElementById("register-submit");
    if (form) form.reset();
    if (oauth) oauth.hidden = true;
    if (authorize) authorize.value = "";
    if (openLink) openLink.href = "#";
    if (submit) submit.textContent = "生成授权链接";
    setRegisterStatus("", "muted");
  }

  function teamMemberRows(workspace) {
      const rows = [];
      const indexes = new Map();
      const push = (row) => {
        const email = String(row?.email || "").trim();
        if (!email || row?.purpose === "mother" || row?.is_owner) return;
        const key = email.toLowerCase();
        const existingIndex = indexes.get(key);
        if (existingIndex != null) {
          const existing = rows[existingIndex];
          rows[existingIndex] = { ...existing, ...row, id: row.id || existing.id, auth: row.auth || existing.auth };
          return;
        }
        indexes.set(key, rows.length);
        rows.push(row);
      };
      (workspace?.managed?.accounts || workspace?.member_accounts || []).forEach((row) => push({ ...row, kind: "managed" }));
      (workspace?.reconciliation?.items || []).forEach((row) => {
        if (row?.status === "owner") return;
        const kind = row?.status === "remote_only" ? "unmanaged" : row?.status;
        push({ ...row, kind, id: row.local_account_id || row.candidate_account_id });
      });
      rows.sort((a, b) => String(a.email || "").localeCompare(String(b.email || "")));
      return rows;
    }

    function presentTeamMember(row) {
      const kind = teamMemberKind(row);
      const auth = authStatus(row);
      const accountId = row.id || row.local_account_id || row.candidate_account_id || null;
      if (kind === "owner" || row.purpose === "mother" || row.is_owner) {
        return {
          code: "owner",
          label: "母号",
          primaryId: auth.needsAuth && accountId ? "team.member.reauth" : null,
          secondaryIds: [],
        };
      }
      if (kind === "unmanaged" || kind === "remote_only") {
        return { code: "remote_only", label: "官方已加入，未接入本地", primaryId: "team.member.link", secondaryIds: [] };
      }
      if (kind === "invited") {
        return { code: "invited", label: "等待接受邀请", primaryId: null, secondaryIds: ["team.member.official-remove"] };
      }
      if (kind === "conflict") {
        return { code: "conflict", label: "身份冲突", primaryId: null, secondaryIds: ["team.member.local-remove"] };
      }
      if (kind === "local_only") {
        return { code: "local_only", label: "本地有记录，官方未找到", primaryId: null, secondaryIds: ["team.member.local-remove"] };
      }
      if (auth.needsAuth && accountId) {
        return {
          code: "needs_auth",
          label: "已接入，需授权",
          primaryId: "team.member.reauth",
          secondaryIds: ["team.member.role", "team.member.official-remove", "team.member.local-remove"],
        };
      }
      return {
        code: "managed",
        label: "已接入",
        primaryId: null,
        secondaryIds: ["team.member.role", "team.member.official-remove", "team.member.local-remove"],
      };
    }

    function teamMemberState(row) {
      const presented = presentTeamMember(row);
      return { code: presented.code === "remote_only" ? "warning" : presented.code, label: presented.label };
    }

  function teamMemberKind(row) {
    const remoteState = String(row?.remote_state || "").trim().toLowerCase();
    const status = String(row?.status || "").trim().toLowerCase();
    const kind = String(row?.kind || "").trim().toLowerCase();
    const membership = String(row?.membership_state || "").trim().toLowerCase();
    if (kind === "owner" || status === "owner" || row?.purpose === "mother" || row?.is_owner) return "owner";
    if (remoteState === "joined" || status === "managed" || status === "remote_only") {
      if (status === "remote_only" || kind === "unmanaged" || kind === "remote_only") return "remote_only";
      return kind === "conflict" ? "conflict" : "managed";
    }
    if (remoteState === "invited" || status === "invited" || kind === "invited" || membership === "invited") return "invited";
    if (kind === "unmanaged" || kind === "remote_only" || status === "remote_only") return "remote_only";
    if (kind === "conflict" || status === "conflict") return "conflict";
    if (kind === "local_only" || status === "local_only" || membership === "local_only") return "local_only";
    return kind || status || membership || "managed";
  }

  function teamMemberIsJoined(row) {
    return !["invited", "local_only", "unmanaged", "remote_only"].includes(teamMemberKind(row));
  }

    function teamMemberMenu(workspace, row) {
        const context = { workspace, row };
        if (!row.email && !row.id && !row.local_account_id) return null;
        const details = document.createElement("details");
        details.className = "member-action-menu";
        const summary = document.createElement("summary");
        summary.setAttribute("aria-label", `打开 ${row.email || "成员"} 的次级操作`);
        summary.textContent = "…";
        const options = document.createElement("div");
        options.className = "member-action-options";
        const secondaryIds = new Set(presentTeamMember(row).secondaryIds);
        (entityActions.team || []).forEach((action) => {
          if (!secondaryIds.has(action.id) || (action.visible && !action.visible(context))) return;
          const button = document.createElement("button");
          button.type = "button";
          button.className = action.danger ? "button ghost compact danger" : "button ghost compact";
          button.textContent = typeof action.label === "function" ? action.label(context) : action.label;
          button.addEventListener("click", async () => {
            details.open = false;
            await action.run(context, button);
          });
          options.append(button);
        });
        details.append(summary, options);
        return options.childElementCount ? details : null;
      }

    function renderTeamMember(workspace, row) {
        const item = document.createElement("div");
        item.className = "team-member-row";
        const presented = presentTeamMember(row);
        const email = String(row.email || "").trim();
        if (email && teamDetailState?.selectedMemberEmail && email.toLowerCase() === String(teamDetailState.selectedMemberEmail).toLowerCase()) {
          item.classList.add("is-selected");
        }
        item.dataset.memberEmail = email;
        const identity = document.createElement("div");
        identity.className = "team-member-identity";
        identity.append(twoLine(row.email || row.name || "—", roleLabel(row.role || row.official_role) || null));
        const stateWrap = document.createElement("div");
        stateWrap.className = "team-member-state";
        stateWrap.append(statusNode(presented.code, presented.label));
        const actions = document.createElement("div");
        actions.className = "row-actions";
        const context = { workspace, row };
        const primary = presented.primaryId && (entityActions.team || []).find((action) => action.id === presented.primaryId);
        if (primary && (!primary.visible || primary.visible(context))) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = presented.primaryId === "team.member.reauth" ? "button danger compact" : "button primary compact";
          button.textContent = typeof primary.label === "function" ? primary.label(context) : primary.label;
          button.addEventListener("click", () => primary.run(context, button));
          actions.append(button);
        }
        const secondary = teamMemberMenu(workspace, row);
        if (secondary) actions.append(secondary);
        item.append(identity, stateWrap, actions);
        return item;
      }

  function renderTeamInviteControls(workspace) {
    const wrapper = document.createElement("div");
    wrapper.className = "team-invite-controls";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "button ghost compact";
    toggle.textContent = "邀请成员";
    toggle.setAttribute("aria-expanded", "false");
    const form = document.createElement("form");
    form.className = "team-invite-form stack";
    form.hidden = true;
    const emailLabel = document.createElement("label");
    emailLabel.textContent = "邮箱 / 邮件原文";
    const emailLine = document.createElement("textarea");
    emailLine.name = "email_line";
    emailLine.rows = 2;
    emailLine.placeholder = "留空则自动领取 HME";
    emailLabel.append(emailLine);
    const roleLabelEl = document.createElement("label");
    roleLabelEl.textContent = "官方角色";
    const role = document.createElement("select");
    role.name = "role";
    [["owner", "Owner"], ["member", "Member"]].forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      role.append(option);
    });
    roleLabelEl.append(role);
    const more = document.createElement("details");
    more.className = "team-detail-disclosure";
    const moreSummary = document.createElement("summary");
    moreSummary.textContent = "更多邀请参数";
    const moreBody = document.createElement("div");
    moreBody.className = "team-detail-disclosure-body";
    const phoneLabel = document.createElement("label");
    phoneLabel.textContent = "手机号行";
    const phone = document.createElement("input");
    phone.name = "phone_line";
    phone.placeholder = "可选，通常由号池分配";
    phoneLabel.append(phone);
    const proxyLabel = document.createElement("label");
    proxyLabel.textContent = "代理";
    const proxy = document.createElement("input");
    proxy.name = "proxy";
    proxy.placeholder = "可选 socks5://host:port";
    proxyLabel.append(proxy);
    const forceLabel = document.createElement("label");
    forceLabel.className = "check";
    const force = document.createElement("input");
    force.type = "checkbox";
    force.name = "force";
    forceLabel.append(force, document.createTextNode(" 强制跳过部分门禁"));
    const skipLabel = document.createElement("label");
    skipLabel.className = "check";
    const skip = document.createElement("input");
    skip.type = "checkbox";
    skip.name = "skip_invite";
    skipLabel.append(skip, document.createTextNode(" 跳过官方邀请"));
    moreBody.append(phoneLabel, proxyLabel, forceLabel, skipLabel);
    more.append(moreSummary, moreBody);
    const status = document.createElement("p");
    status.className = "muted";
    status.hidden = true;
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button ghost";
    cancel.textContent = "取消";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "button primary";
    submit.textContent = "发送邀请";
    actions.append(cancel, submit);
    form.append(emailLabel, roleLabelEl, more, status, actions);
    const setOpen = (open) => {
      form.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.textContent = open ? "收起邀请" : "邀请成员";
      if (open) emailLine.focus();
    };
    toggle.addEventListener("click", () => setOpen(form.hidden));
    cancel.addEventListener("click", () => setOpen(false));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const action = (entityActions.team || []).find((candidate) => candidate.id === "team.member.invite");
      const values = Object.fromEntries(new FormData(form).entries());
      values.force = force.checked;
      values.skip_invite = skip.checked;
      status.hidden = false;
      status.className = "muted";
      status.setAttribute("role", "status");
      status.textContent = "正在发送邀请…";
      try {
        await action.run({ workspace, values }, submit);
      } catch (error) {
        status.className = "error";
        status.setAttribute("role", "alert");
        status.textContent = friendlyError(error);
      }
    });
    wrapper.append(toggle, form);
    return wrapper;
  }

  function canRotateWorkspace(workspace) {
      return workspace?.rotation?.eligible === true || workspace?.rotation_eligible === true || workspace?.can_rotate === true;
    }

    function renderTeamRotationControls(container, workspace) {
      const form = document.createElement("form");
      form.className = "team-rotation-form stack";
      const emailLabel = document.createElement("label");
      emailLabel.textContent = "要轮转的成员";
      const email = document.createElement("input");
      email.type = "email";
      email.name = "email";
      email.required = true;
      emailLabel.append(email);
      const replacementLabel = document.createElement("label");
      replacementLabel.textContent = "补位邮箱 / 原文";
      const replacement = document.createElement("textarea");
      replacement.name = "email_line";
      replacement.rows = 3;
      replacement.placeholder = "可留空，由现有策略选择待命账号";
      replacementLabel.append(replacement);
      const forceLabel = document.createElement("label");
      forceLabel.className = "check";
      const force = document.createElement("input");
      force.type = "checkbox";
      force.name = "force_refill";
      forceLabel.append(force, document.createTextNode(" 强制补位"));
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "button danger";
      submit.textContent = "执行受控轮转";
      form.append(emailLabel, replacementLabel, forceLabel, submit);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!confirmDanger(`确认对 ${email.value} 执行受控轮转？这会修改官方 Team 席位。`)) return;
        setButtonBusy(submit, true, "轮转中");
        try {
          const result = await postAction(`rotate-${workspace.id}-${email.value}`, `/api/workspaces/${workspace.id}/rotate`, {
            email: email.value.trim(),
            email_line: replacement.value || "",
            force_refill: force.checked,
            reason: "console_team_detail",
          });
          await handleActionResult(result, { successMessage: result.message || "轮转已提交", refresh: false });
          await reloadTeamDetails();
        } catch (error) {
          toast(friendlyError(error), "error");
        } finally {
          setButtonBusy(submit, false);
        }
      });
      container.append(form);
    }

    function renderTeamDetails(workspace) {
      if (!sheet || !workspace) return;
      teamDetailState = { ...(teamDetailState || {}), workspaceId: workspace.id, workspace, view: "details" };
      const title = document.getElementById("sheet-title");
      const subtitle = document.getElementById("sheet-subtitle");
      const body = document.getElementById("sheet-body");
      title.textContent = workspace.display_name || workspace.name || "团队详情";
      subtitle.textContent = workspace.official_workspace_id || "";
      body.replaceChildren();
      const official = workspace.official || {};
      const seats = official.occupied_seats != null && official.seat_limit != null
        ? `${official.occupied_seats} / ${official.seat_limit}`
        : (official.joined_people_total == null ? "尚未同步" : `${official.joined_people_total} 人`);
      body.append(kvSection("团队概览", [
        ["团队名称", workspace.display_name || workspace.name],
        ["Workspace ID", workspace.official_workspace_id],
        ["官方席位", seats],
        ["最近同步", workspace.last_sync ? relativeTime(workspace.last_sync) : "尚未同步"],
        ["健康状态", workspace.owner_auth_reason === "owner_account_missing" ? "母号本地档案缺失" : (workspace.owner_needs_auth ? "母号要授权" : labelOf(statusLabels, workspace.health || workspace.status))],
      ]));

      const mother = document.createElement("section");
      mother.className = "sheet-section team-mother-section";
      const motherTitle = document.createElement("h3");
      motherTitle.textContent = "母号";
      const motherGrid = document.createElement("div");
      motherGrid.className = "team-mother-grid";
      motherGrid.append(kvSection("", [
        ["邮箱", workspace.owner_email],
        ["授权状态", labelOf(statusLabels, workspace.owner_auth_state || workspace.owner_auth)],
        ["代理状态", workspace.owner_proxy || (workspace.owner_proxy_set ? "已绑定" : "未绑定")],
        ["额度更新时间", workspace.owner_quota_updated_at ? relativeTime(workspace.owner_quota_updated_at) : "暂无额度快照"],
      ]));
      const motherAside = document.createElement("div");
      motherAside.className = "team-mother-aside";
      const ownerQuota = workspace.owner_quota || {};
      if ([ownerQuota.five_hour_used_percent, ownerQuota.seven_day_used_percent, ownerQuota.queried_at].some((value) => value != null)) {
        motherAside.append(quotaCell({ quota: ownerQuota }));
      }
      const ownerAction = workspacePrimaryAction(workspace);
      if (ownerAction.id === "owner-missing" || ownerAction.id === "owner-auth") {
        const ownerButton = document.createElement("button");
        ownerButton.type = "button";
        ownerButton.className = ownerAction.id === "owner-auth" ? "button danger" : "button";
        ownerButton.textContent = ownerAction.label;
        ownerButton.addEventListener("click", () => runWorkspacePrimaryAction(workspace, ownerButton));
        motherAside.append(ownerButton);
      }
      if (motherAside.childElementCount) motherGrid.append(motherAside);
      mother.append(motherTitle, motherGrid);
      body.append(mother);

      const members = document.createElement("section");
      members.className = "sheet-section";
      const membersTitle = document.createElement("h3");
      const rows = teamMemberRows(workspace);
      membersTitle.textContent = `成员（${rows.length}）`;
      const list = document.createElement("div");
      list.className = "team-member-list";
      if (!rows.length) list.append(emptyState("没有子成员", "同步官方成员后，这里会显示当前 Team 成员。", true));
      else rows.forEach((row) => list.append(renderTeamMember(workspace, row)));
      members.append(membersTitle, renderTeamInviteControls(workspace), list);
      body.append(members);

      if (canRotateWorkspace(workspace)) {
        const advanced = document.createElement("details");
        advanced.className = "team-detail-disclosure";
        const summary = document.createElement("summary");
        summary.textContent = "高级操作";
        const content = document.createElement("div");
        content.className = "team-detail-disclosure-body";
        renderTeamRotationControls(content, workspace);
        advanced.append(summary, content);
        body.append(advanced);
      }

      const danger = document.createElement("details");
      danger.className = "team-detail-disclosure team-danger-zone";
      const dangerSummary = document.createElement("summary");
      dangerSummary.textContent = "危险操作";
      const dangerBody = document.createElement("div");
      dangerBody.className = "team-detail-disclosure-body";
      const warning = document.createElement("p");
      warning.className = "hint";
      warning.textContent = "删除只清理本地团队数据，不会修改官方 Team。成员和历史关联会按后端安全规则处理。";
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "button danger";
      deleteButton.textContent = "删除本地团队";
      deleteButton.addEventListener("click", async () => {
        const name = workspace.display_name || workspace.name || workspace.owner_email || `#${workspace.id}`;
        if (!confirmDanger(`确认删除本地团队「${name}」？只删除本地数据，不改官方 Team。`)) return;
        setButtonBusy(deleteButton, true, "删除中");
        try {
          const result = await deleteAction(`workspace-delete-${workspace.id}`, `/api/workspaces/${workspace.id}`);
          closeSheet();
          await handleActionResult(result, { successMessage: "已删除本地团队" });
        } catch (error) {
          toast(friendlyError(error), "error");
          setButtonBusy(deleteButton, false);
        }
      });
      dangerBody.append(warning, deleteButton);
      danger.append(dangerSummary, dangerBody);
      body.append(danger);
    }

    function openWorkspaceDetails(trigger, workspace) {
      if (!workspace || !sheet) return;
      teamDetailState = { workspaceId: workspace.id, workspace, view: "details" };
      renderTeamDetails(workspace);
      openOverlay("entity", { returnFocus: trigger, context: { kind: "workspace", workspace }, initialFocus: "[data-close-sheet]" });
    }

    async function fetchTeamDetails(workspaceId) {
        const payload = await fetchEntity("workspace-list", "/api/workspaces");
        if (document.body.dataset.page === "workspaces") {
          pageCache.kind = "workspace";
          pageCache.items = payload.items || [];
          paintList("workspace");
          if (overlayState.returnFocus && !overlayState.returnFocus.isConnected) {
            overlayState.returnFocus = document.querySelector(`[data-entity-id="${workspaceId}"] [data-action="workspace.manage"]`)
              || document.querySelector(`[data-entity-id="${workspaceId}"]`)
              || document.getElementById("page-root");
          }
        }
        const workspace = (payload.items || []).find((item) => Number(item.id) === Number(workspaceId));
        if (!workspace) throw new Error("团队已不存在或无法读取");
        if (teamDetailState) teamDetailState.workspace = workspace;
        return workspace;
      }

    async function reloadTeamDetails() {
      if (!teamDetailState?.workspaceId) return;
      const workspace = await fetchTeamDetails(teamDetailState.workspaceId);
      renderTeamDetails(workspace);
      const selected = teamDetailState.selectedMemberEmail;
      const panel = sheet?.querySelector(".sheet-panel");
      if (panel && teamDetailState.scrollTop) panel.scrollTop = teamDetailState.scrollTop;
      if (selected) {
        const target = sheet?.querySelector(`[data-member-email="${CSS.escape(selected)}"]`);
        target?.scrollIntoView({ block: "nearest" });
      }
    }

  async function inviteTeamMember(workspace, values, button) {
    setButtonBusy(button, true, "发送中");
    try {
      const result = await postAction(`workspace-invite-${workspace.id}`, `/api/workspaces/${workspace.id}/onboard`, {
        email_line: String(values.email_line || "").trim(),
        phone_line: String(values.phone_line || "").trim(),
        proxy: String(values.proxy || "").trim(),
        role: values.role === "member" ? "member" : "owner",
        force: Boolean(values.force),
        skip_invite: Boolean(values.skip_invite),
      });
      await handleActionResult(result, { successMessage: result.message || "邀请已提交", refresh: false });
      await reloadTeamDetails();
      return result;
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function linkTeamMember(workspace, row, button) {
      const email = String(row.email || "").trim();
      if (!email) return;
      if (teamDetailState) {
        teamDetailState.stage = "linking_member";
        teamDetailState.selectedMemberEmail = email;
      }
      setButtonBusy(button, true, "接入中");
      button.closest(".team-member-row")?.setAttribute("aria-busy", "true");
      try {
        const body = { email };
        if (row.id || row.local_account_id || row.candidate_account_id) body.account_id = row.id || row.local_account_id || row.candidate_account_id;
        const result = await postAction(`workspace:${workspace.id}:member:${email}:link`, `/api/workspaces/${workspace.id}/members/link`, body);
        toast("已接入本地", "success");
        const refreshed = await fetchTeamDetails(workspace.id);
        if (result.needs_auth && result.account_id) {
          if (teamDetailState) {
            teamDetailState.workspace = refreshed;
            teamDetailState.stage = "authorization_required";
            teamDetailState.selectedMemberEmail = result.email || email;
          }
          showTeamAuthStep({ id: result.account_id, email: result.email || email, workspace_id: workspace.id, selectedMemberEmail: result.email || email });
        } else {
          renderTeamDetails(refreshed);
        }
      } catch (error) {
        if (extractErrorCode(error) === "already_linked") {
          toast(friendlyError(error), "warning");
          await reloadTeamDetails();
          return;
        }
        toast(friendlyError(error), "error", { label: "重试", onClick: () => linkTeamMember(workspace, row, button) });
        setButtonBusy(button, false);
        button.closest(".team-member-row")?.removeAttribute("aria-busy");
      }
    }

    async function changeTeamMemberRole(workspace, row, button, role) {
      const email = String(row.email || "").trim();
      if (!email || !confirmDanger(`确认把 ${email} 的官方角色改为 ${role === "owner" ? "Owner" : "Member"}？`)) return;
      setButtonBusy(button, true, "更新中");
      try {
        const body = { email, role };
        if (row.user_id || row.official_user_id) body.user_id = row.user_id || row.official_user_id;
        const result = await patchAction(`workspace-role-${workspace.id}-${email}`, `/api/workspaces/${workspace.id}/members/role`, body);
        toast(result.message || "成员角色已更新", operationTone(result));
        await reloadTeamDetails();
      } catch (error) {
        toast(friendlyError(error), "error");
        setButtonBusy(button, false);
      }
    }

    async function removeTeamMember(workspace, row, button, mode) {
      const email = String(row.email || "").trim();
      const accountId = row.id || row.local_account_id;
      const kind = presentTeamMember(row).code;
      const copy = mode === "purge"
        ? `永久删除 ${email}？这会移出官方席位、下架 Sub2API 并清理本地档案，且不可恢复。`
        : mode === "official"
          ? `${kind === "invited" ? "撤回" : "移出"} ${email} 的官方席位？这会修改官方 Team。`
          : `只从本地移除 ${email}？官方 Team 不会改变。`;
      if (!confirmDanger(copy)) return;
      setButtonBusy(button, true, "处理中");
      try {
        let result;
        if (mode === "purge") {
          result = await postAction(`workspace-purge-${workspace.id}-${email}`, `/api/workspaces/${workspace.id}/members/purge`, { email, reason: "console_team_detail" });
        } else if (mode === "official") {
          const endpoint = kind === "invited" ? "revoke-invite" : "kick";
          result = await postAction(`workspace-${endpoint}-${workspace.id}-${email}`, `/api/workspaces/${workspace.id}/${endpoint}`, { email, reason: "console_team_detail" });
        } else {
          result = await postAction(`workspace-remove-${workspace.id}-${accountId}`, `/api/workspaces/${workspace.id}/members/remove`, { email, account_id: accountId });
        }
        await handleActionResult(result, { successMessage: mode === "purge" ? "已永久删除" : "成员已更新", refresh: false });
        await reloadTeamDetails();
      } catch (error) {
        toast(friendlyError(error), "error");
        setButtonBusy(button, false);
      }
    }

    async function showTeamAuthStep(account) {
      if (!sheet || !account?.id || !teamDetailState) return;
      teamDetailState.view = "auth";
      teamDetailState.account = account;
      teamDetailState.stage = teamDetailState.stage || "authorization_required";
      teamDetailState.selectedMemberEmail = account.selectedMemberEmail || account.email || teamDetailState.selectedMemberEmail;
      const panel = sheet.querySelector(".sheet-panel");
      teamDetailState.scrollTop = panel?.scrollTop || teamDetailState.scrollTop || 0;
      const title = document.getElementById("sheet-title");
      const subtitle = document.getElementById("sheet-subtitle");
      const body = document.getElementById("sheet-body");
      title.textContent = "账号授权";
      subtitle.textContent = `正在授权：${account.email || "账号"}`;
      body.replaceChildren();
      const back = document.createElement("button");
      back.type = "button";
      back.className = "button ghost team-auth-back";
      back.textContent = "← 返回团队详情";
      back.addEventListener("click", () => {
        teamDetailState.stage = "workspace_detail";
        teamDetailState.view = "details";
        renderTeamDetails(teamDetailState.workspace);
        if (panel) panel.scrollTop = teamDetailState.scrollTop || 0;
      });
      const form = document.createElement("form");
      form.className = "team-auth-form sheet-section";
      const linkLabel = document.createElement("label");
      linkLabel.textContent = "授权链接";
      const authorize = document.createElement("textarea");
      authorize.rows = 4;
      authorize.readOnly = true;
      linkLabel.append(authorize);
      const linkActions = document.createElement("div");
      linkActions.className = "settings-probe-actions";
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "button";
      copy.textContent = "复制链接";
      const open = document.createElement("a");
      open.className = "button";
      open.target = "_blank";
      open.rel = "noopener";
      open.textContent = "打开授权";
      open.href = "#";
      const regenerate = document.createElement("button");
      regenerate.type = "button";
      regenerate.className = "button ghost";
      regenerate.textContent = "重新生成链接";
      regenerate.hidden = true;
      linkActions.append(copy, open, regenerate);
      const callbackLabel = document.createElement("label");
      callbackLabel.textContent = "回调地址";
      const callback = document.createElement("textarea");
      callback.rows = 4;
      callback.placeholder = "http://localhost:1455/auth/callback?code=...&state=...";
      callbackLabel.append(callback);
      const status = document.createElement("p");
      status.className = "muted";
      status.setAttribute("role", "status");
      status.textContent = "正在生成授权链接…";
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "button primary";
      submit.textContent = "完成授权";
      form.append(linkLabel, linkActions, callbackLabel, status, submit);
      body.append(back, form);
      activateFocusTrap(panel || sheet);
      back.focus();
      let ticket = "";
      const startAuth = async () => {
        teamDetailState.stage = "authorizing";
        status.className = "muted";
        status.setAttribute("role", "status");
        status.textContent = "正在生成授权链接…";
        regenerate.hidden = true;
        const started = await fetchEntity(`account:${account.id}:reauth:start`, `/api/accounts/${account.id}/reauth`, {
          method: "POST",
          headers: { Accept: "application/json" },
        });
        ticket = started.ticket || "";
        authorize.value = started.authorize_url || "";
        open.href = started.authorize_url || "#";
        status.textContent = started.message || "打开授权链接登录，完成后粘贴回调地址。";
        callback.focus();
      };
      copy.addEventListener("click", async () => {
        const copied = await copyText(authorize.value);
        status.textContent = copied ? "授权链接已复制。" : "复制失败，请手动选中链接。";
        status.className = copied ? "muted" : "error";
        status.setAttribute("role", copied ? "status" : "alert");
      });
      regenerate.addEventListener("click", async () => {
        try {
          await startAuth();
        } catch (error) {
          status.textContent = friendlyError(error);
          status.className = "error";
          status.setAttribute("role", "alert");
        }
      });
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!ticket || !callback.value.trim()) {
          status.textContent = ticket ? "请粘贴完整回调地址。" : "授权会话尚未准备好，请重试。";
          status.className = "error";
          status.setAttribute("role", "alert");
          return;
        }
        setButtonBusy(submit, true, "授权中");
        try {
          const result = await fetchEntity(`account:${account.id}:reauth:complete:${ticket}`, `/api/accounts/${account.id}/reauth/complete`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify({ ticket, callback_url: callback.value.trim() }),
          });
          teamDetailState.stage = "authorization_completed";
          await reloadTeamDetails();
          toast(result.message || "授权成功", "success");
        } catch (error) {
          const code = extractErrorCode(error);
          status.textContent = friendlyError(error);
          status.className = "error";
          status.setAttribute("role", "alert");
          regenerate.hidden = code !== "callback_expired" ? true : false;
          if (code === "callback_expired") regenerate.hidden = false;
          setButtonBusy(submit, false);
        }
      });
      try {
        await startAuth();
      } catch (error) {
        status.textContent = friendlyError(error);
        status.className = "error";
        status.setAttribute("role", "alert");
        regenerate.hidden = false;
      }
    }

  async function fillProxyProfileOptions(selectedId) {
    const form = document.getElementById("proxy-edit-form");
    const select = form?.querySelector("[name='proxy_profile_id']");
    if (!select) return;
    select.replaceChildren();
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "不改绑定，只填新 URL";
    select.append(blank);
    try {
      const payload = await fetchEntity("proxy-list-edit", "/api/resources/proxies");
      (payload.items || []).forEach((item) => {
        const option = document.createElement("option");
        option.value = String(item.id);
        option.textContent = `${item.name || item.host}:${item.port}`;
        if (selectedId && Number(selectedId) === Number(item.id)) option.selected = true;
        select.append(option);
      });
    } catch (error) {
      toast(friendlyError(error), "error");
    }
  }

  function setProxyEditMode(mode) {
    const form = document.getElementById("proxy-edit-form");
    if (!form) return;
    form.mode.value = mode;
    const accountBlock = form.querySelector("[data-proxy-edit-account]");
    const profileBlock = form.querySelector("[data-proxy-edit-profile]");
    if (accountBlock) accountBlock.hidden = mode !== "account";
    if (profileBlock) profileBlock.hidden = mode !== "profile";
    const title = document.getElementById("proxy-edit-title");
    const subtitle = document.getElementById("proxy-edit-subtitle");
    if (mode === "profile") {
      if (title) title.textContent = "编辑代理档案";
      if (subtitle) subtitle.textContent = "可改名称或启停。留空名称会恢复自动名。";
    } else {
      if (title) title.textContent = "修改母号代理";
      if (subtitle) subtitle.textContent = "仅母号可改。可填 URL，或绑定已有代理档案。";
    }
  }

  async function openProxyEdit(trigger, account) {
      if (!proxyEditSheet) return;
      const form = document.getElementById("proxy-edit-form");
      form?.reset();
      setProxyEditMode("account");
      if (form) {
        form.account_id.value = account.id || "";
        form.proxy_id.value = "";
        form.email.value = account.email || "";
        form.current_proxy.value = account.proxy_url || "";
        form.proxy.value = "";
        form.clear.checked = false;
      }
      await fillProxyProfileOptions(account.proxy_profile_id);
      setFormStatus("proxy-edit-status", "", "muted");
      openOverlay("proxy-edit", { returnFocus: trigger, context: { kind: "account-proxy", account }, initialFocus: "#proxy-edit-submit" });
    }

  function openProxyProfileEdit(trigger, proxy) {
      if (!proxyEditSheet) return;
      const form = document.getElementById("proxy-edit-form");
      form?.reset();
      setProxyEditMode("profile");
      if (form) {
        form.proxy_id.value = proxy.id || "";
        form.account_id.value = "";
        form.name.value = proxy.name || "";
        form.status.value = proxy.status === "disabled" ? "disabled" : "active";
        form.restore_auto_name.checked = false;
      }
      setFormStatus("proxy-edit-status", "", "muted");
      openOverlay("proxy-edit", { returnFocus: trigger, context: { kind: "proxy-profile", proxy }, initialFocus: "#proxy-edit-submit" });
    }

  function closeProxyEdit() {
      closeOverlay();
    }

  async function submitProxyEdit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#proxy-edit-submit");
    const mode = form.mode?.value || "account";
    if (button) button.disabled = true;
    setFormStatus("proxy-edit-status", "正在保存…", "muted");
    try {
      let result;
      if (mode === "profile") {
        const proxyId = Number(form.proxy_id.value || 0);
        if (!proxyId) return;
        result = await patchAction(`proxy-edit-${proxyId}`, `/api/resources/proxies/${proxyId}`, {
          name: form.name.value,
          restore_auto_name: Boolean(form.restore_auto_name.checked),
          status: form.status.value,
        });
        setFormStatus("proxy-edit-status", result.ok ? "已保存" : (result.error || "失败"), result.ok ? "muted" : "error");
        toast(result.ok ? "代理已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
      } else {
        const accountId = Number(form.account_id.value || 0);
        if (!accountId) return;
        const url = (form.proxy.value || "").trim();
        const body = {
          clear: Boolean(form.clear.checked),
          proxy: url || null,
          proxy_profile_id: (!url && form.proxy_profile_id.value) ? Number(form.proxy_profile_id.value) : null,
        };
        result = await patchAction(`account-proxy-${accountId}`, `/api/accounts/${accountId}/proxy`, body);
        setFormStatus("proxy-edit-status", result.ok ? "已保存" : (result.error || "失败"), result.ok ? "muted" : "error");
        toast(result.ok ? "母号代理已更新" : (result.error || "更新失败"), result.ok ? "success" : "error");
      }
      if (result?.ok) {
        closeProxyEdit();
        await bootPage();
      }
    } catch (error) {
      setFormStatus("proxy-edit-status", friendlyError(error), "error");
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

function openRegister(trigger) {
    if (!registerSheet) return;
    resetRegisterForm();
    openOverlay("register", {
      returnFocus: trigger || document.querySelector("[data-open-register]"),
      context: { kind: "register" },
      initialFocus: "[name='email']",
    });
  }

  function closeRegister() {
      closeOverlay();
    }

  function setReauthStatus(text, tone) {
      const statusEl = document.getElementById("reauth-status");
      if (!statusEl) return;
      statusEl.hidden = !text;
      statusEl.className = tone || "muted";
      statusEl.setAttribute("role", tone === "error" ? "alert" : "status");
      statusEl.textContent = text || "";
    }

  function closeReauth() {
      closeOverlay();
    }

  async function openReauth(item, trigger) {
    if (!reauthSheet || !item?.id) return;
    const form = document.getElementById("reauth-form");
    const authorize = document.getElementById("reauth-authorize-url");
    const openLink = document.getElementById("reauth-open-link");
    form?.reset();
    if (form) {
      form.account_id.value = item.id;
      form.email.value = item.email || "";
      form.ticket.value = "";
    }
    if (authorize) authorize.value = "";
    if (openLink) openLink.href = "#";
    setReauthStatus("正在生成授权链接…", "muted");
    openOverlay("reauth", { returnFocus: trigger, context: { kind: "reauth", account: item }, initialFocus: "[name='callback_url']" });
    try {
      const started = await fetchEntity(`account:${item.id}:reauth:start`, `/api/accounts/${item.id}/reauth`, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      if (form) form.ticket.value = started.ticket || "";
      if (authorize) authorize.value = started.authorize_url || "";
      if (openLink) openLink.href = started.authorize_url || "#";
      setReauthStatus(started.message || "打开授权链接，用这个邮箱登录 ChatGPT。登录后把跳转到 localhost:1455 的整段地址贴回来。", "muted");
      form?.querySelector("[name='callback_url']")?.focus();
    } catch (error) {
      setReauthStatus(friendlyError(error), "error");
      toast(friendlyError(error), "error");
    }
  }

  async function submitReauth(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#reauth-submit");
    const accountId = String(form.account_id.value || "").trim();
    const ticket = String(form.ticket.value || "").trim();
    const callbackUrl = String(form.callback_url.value || "").trim();
    if (!accountId || !ticket) {
      setReauthStatus("还没有授权会话，请关掉后重新点重新授权。", "error");
      return;
    }
    if (!callbackUrl) {
      setReauthStatus("请把跳转到 localhost:1455 的整段回调地址贴回来。", "error");
      return;
    }
    if (button) button.disabled = true;
    setReauthStatus("正在用回调换票…", "muted");
    try {
      const result = await fetchEntity(`account:${accountId}:reauth:complete:${ticket}`, `/api/accounts/${accountId}/reauth/complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ ticket, callback_url: callbackUrl }),
      });
      closeReauth();
      await handleActionResult(result, { successMessage: result.message || "授权已更新" });
    } catch (error) {
      setReauthStatus(friendlyError(error), "error");
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  function setFormStatus(id, text, tone) {
      const statusEl = document.getElementById(id);
      if (!statusEl) return;
      statusEl.hidden = !text;
      statusEl.className = tone || "muted";
      statusEl.setAttribute("role", tone === "error" ? "alert" : "status");
      statusEl.textContent = text || "";
    }

  function openPhoneImport(trigger) {
      if (!phoneImportSheet) return;
      const form = document.getElementById("phone-import-form");
      if (form) form.reset();
      setFormStatus("phone-import-status", "", "muted");
      openOverlay("phone-import", {
        returnFocus: trigger || document.querySelector("[data-open-phone-import]"),
        context: { kind: "phone-import" },
        initialFocus: "[name='text']",
      });
    }

  function closePhoneImport() {
      closeOverlay();
    }

  function openProxyAdd(trigger) {
      if (!proxyAddSheet) return;
      const form = document.getElementById("proxy-add-form");
      if (form) form.reset();
      setFormStatus("proxy-add-status", "", "muted");
      openOverlay("proxy-add", {
        returnFocus: trigger || document.querySelector("[data-open-proxy-add]"),
        context: { kind: "proxy-add" },
        initialFocus: "[name='url']",
      });
    }

  function closeProxyAdd() {
      closeOverlay();
    }

  async function submitPhoneImport(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#phone-import-submit");
    if (button) button.disabled = true;
    setFormStatus("phone-import-status", "正在导入…", "muted");
    try {
      const data = new FormData(form);
      const result = await fetchEntity("phone-import", "/api/resources/phones/import", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ text: String(data.get("text") || "") }),
      });
      const imported = result.imported ?? 0;
      const skipped = result.skipped ?? 0;
      const errors = result.errors ?? result.invalid ?? 0;
      closePhoneImport();
      await bootPage();
      toast(
        `已导入 ${imported} 个号码，跳过 ${skipped} 个重复项${errors ? `，${errors} 行格式错误` : ""}`,
        "success",
        { label: "查看号码", onClick: () => { window.location.href = "/resources/phones"; } }
      );
    } catch (error) {
      setFormStatus("phone-import-status", friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function submitProxyAdd(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#proxy-add-submit");
    if (button) button.disabled = true;
    setFormStatus("proxy-add-status", "正在添加…", "muted");
    try {
      const data = new FormData(form);
      await fetchEntity("proxy-add", "/api/resources/proxies", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          url: String(data.get("url") || "").trim(),
          name: String(data.get("name") || "").trim() || null,
        }),
      });
      closeProxyAdd();
      await bootPage();
      toast("代理已添加", "success");
    } catch (error) {
      setFormStatus("proxy-add-status", friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function copyText(value) {
    const text = String(value || "");
    if (!text) return false;
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  async function startRegisterOAuth(form) {
    const data = new FormData(form);
    const payload = {
      email: String(data.get("email") || "").trim(),
      proxy: String(data.get("proxy") || "").trim() || null,
    };
    const started = await fetchEntity("register-oauth-start", "/api/workspaces/oauth/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    });
    form.elements.ticket.value = started.ticket || "";
    const authorize = document.getElementById("register-authorize-url");
    const openLink = document.getElementById("register-open-link");
    const oauth = document.getElementById("register-oauth");
    const submit = document.getElementById("register-submit");
    if (authorize) authorize.value = started.authorize_url || "";
    if (openLink) openLink.href = started.authorize_url || "#";
    if (oauth) oauth.hidden = false;
    if (submit) submit.textContent = "完成授权";
    form.querySelector("[name='callback_url']")?.focus();
    setRegisterStatus("打开授权链接，登录母号后把回调地址贴回来。", "muted");
  }

  async function completeRegisterOAuth(form) {
    const data = new FormData(form);
    await fetchEntity("register-oauth-complete", "/api/workspaces/oauth/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        ticket: String(data.get("ticket") || "").trim(),
        callback_url: String(data.get("callback_url") || "").trim(),
      }),
    });
    closeRegister();
    await bootPage();
  }

  async function submitRegister(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("#register-submit");
    if (button) button.disabled = true;
    try {
      if (form.elements.ticket.value) {
        setRegisterStatus("正在用回调换票并登记…", "muted");
        await completeRegisterOAuth(form);
      } else {
        setRegisterStatus("正在生成授权链接…", "muted");
        await startRegisterOAuth(form);
      }
    } catch (error) {
      setRegisterStatus(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  function secretPlaceholder(state) {
    return state === "stored" ? "已保存，留空则不修改" : "尚未设置";
  }

  function snapshotSettings(form) {
    const data = new FormData(form);
    return JSON.stringify({
      sub2api_base_url: data.get("sub2api_base_url"),
      sub2api_api_key: data.get("sub2api_api_key"),
      sub2api_admin_email: data.get("sub2api_admin_email"),
      sub2api_admin_password: data.get("sub2api_admin_password"),
      hme_base_url: data.get("hme_base_url"),
      hme_service_token: data.get("hme_service_token"),
      hme_account_id: data.get("hme_account_id"),
      cf_mail_base_url: data.get("cf_mail_base_url"),
      cf_mail_address: data.get("cf_mail_address"),
      cf_mail_admin_password: data.get("cf_mail_admin_password"),
      official_quota_probe: form.official_quota_probe.checked,
      sms_max_uses_per_phone: data.get("sms_max_uses_per_phone"),
      sms_cooldown_min: data.get("sms_cooldown_min"),
      sms_reserve_min: data.get("sms_reserve_min"),
    });
  }

  function updateDirty() {
    const form = document.getElementById("settings-form");
    const bar = document.getElementById("settings-savebar");
    const status = document.getElementById("settings-status");
    const discard = document.getElementById("settings-discard");
    if (!form || !bar) return;
    settingsDirty = snapshotSettings(form) !== settingsBaseline;
    bar.classList.toggle("is-dirty", settingsDirty);
    if (discard) discard.hidden = !settingsDirty;
    if (status && !status.dataset.locked) {
      status.className = "muted";
      status.textContent = settingsDirty ? "有未保存的修改" : "没有未保存的修改";
    }
  }

  function fillSettings(payload) {
    const form = document.getElementById("settings-form");
    if (!form) return;
    const connections = payload.connections || {};
    const automation = payload.automation || {};
    const resources = payload.resources || {};
    const secretState = payload.secret_state || {};
    const account = payload.account || {};
    form.sub2api_base_url.value = connections.sub2api_base_url || "";
    form.sub2api_admin_email.value = connections.sub2api_admin_email || "";
    form.hme_base_url.value = connections.hme_base_url || "";
    form.hme_account_id.value = connections.hme_account_id || "";
    form.cf_mail_base_url.value = connections.cf_mail_base_url || "";
    form.cf_mail_address.value = connections.cf_mail_address || "";
    form.sub2api_api_key.value = "";
    form.sub2api_admin_password.value = "";
    form.hme_service_token.value = "";
    form.cf_mail_admin_password.value = "";
    form.official_quota_probe.checked = Boolean(automation.official_quota_probe);
    form.sms_max_uses_per_phone.value = resources.sms_max_uses_per_phone ?? "";
    form.sms_cooldown_min.value = resources.sms_cooldown_sec ? Math.round(resources.sms_cooldown_sec / 60) : "";
    form.sms_reserve_min.value = resources.sms_reserve_sec ? Math.round(resources.sms_reserve_sec / 60) : "";
    form.querySelectorAll("[data-secret]").forEach((input) => {
      const key = input.dataset.secret;
      input.placeholder = secretPlaceholder(secretState[key]);
    });
    const authHint = document.getElementById("sub2api-auth-hint");
    if (authHint) {
      const mode = connections.sub2api?.auth_mode;
      authHint.textContent =
        mode === "api_key" ? "认证方式：API Key" : mode === "admin" ? "认证方式：管理员账号" : "尚未配置认证";
    }
    const configuredText = (flag) => (flag ? "已配置" : "未设置");
    const setMeta = (service, flag) => {
      const meta = document.querySelector(`[data-service-meta="${service}"]`);
      if (meta && !meta.dataset.probed) meta.textContent = `${configuredText(flag)} · 未检测`;
    };
    setMeta("sub2api", connections.sub2api?.configured);
    setMeta("hme", connections.hme?.configured);
    setMeta("mail", connections.mail?.configured);
    const accountEl = document.getElementById("settings-account");
    if (accountEl) accountEl.textContent = account.username ? `当前登录账号：${account.username}` : "登录账号会显示在这里。";
    settingsBaseline = snapshotSettings(form);
    const status = document.getElementById("settings-status");
    if (status) delete status.dataset.locked;
    updateDirty();
  }

  function numberOrNull(value) {
    const text = String(value ?? "").trim();
    if (!text) return null;
    const number = Number(text);
    return Number.isFinite(number) ? number : null;
  }

  function secretOrNull(value) {
    const text = String(value ?? "").trim();
    if (!text || text === SECRET_MASK) return null;
    return text;
  }

  function minutesToSeconds(value) {
    const minutes = numberOrNull(value);
    return minutes == null ? null : minutes * 60;
  }

  function connectionsPayload(form) {
    return {
      sub2api_base_url: form.sub2api_base_url.value,
      sub2api_admin_email: form.sub2api_admin_email.value,
      hme_base_url: form.hme_base_url.value,
      hme_account_id: form.hme_account_id.value,
      cf_mail_base_url: form.cf_mail_base_url.value,
      cf_mail_address: form.cf_mail_address.value,
      sub2api_api_key: secretOrNull(form.sub2api_api_key.value),
      sub2api_admin_password: secretOrNull(form.sub2api_admin_password.value),
      hme_service_token: secretOrNull(form.hme_service_token.value),
      cf_mail_admin_password: secretOrNull(form.cf_mail_admin_password.value),
    };
  }

  function setServiceState(service, text, summary) {
    const meta = document.querySelector(`[data-service-meta="${service}"]`);
    const sum = document.querySelector(`[data-service-summary="${service}"]`);
    if (meta) {
      meta.textContent = text;
      meta.dataset.probed = "1";
    }
    if (sum && summary) sum.textContent = summary;
  }

  function probeCopy(payload) {
    const sub = payload.sub2api || {};
    const hme = payload.hme || {};
    const mail = payload.mail || {};
    if (sub.ok) {
      setServiceState("sub2api", `连接正常 · ${relativeTime(sub.checked_at)}`, `${sub.group_count || 0} 个分组 · ${sub.account_count || 0} 个账号`);
    } else {
      setServiceState("sub2api", sub.error || "检测失败", sub.error || "连不上 Sub2API");
    }
    if (hme.ok) {
      setServiceState(
        "hme",
        `连接正常 · ${relativeTime(hme.checked_at)}`,
        `${hme.account_name || hme.account_id || "当前账号"} · ${hme.alias_count || 0} 个别名 · ${hme.unused_count || 0} 个可用`
      );
    } else {
      setServiceState("hme", hme.error || "检测失败", hme.error || "连不上 HME");
    }
    if (mail.ok) {
      setServiceState("mail", `连接正常 · ${relativeTime(mail.checked_at)}`, mail.address || "连接成功");
    } else {
      setServiceState("mail", mail.error || "检测失败", mail.error || "连不上临时邮箱");
    }
  }

  async function probeSettings(focus) {
    const form = document.getElementById("settings-form");
    if (!form) return;
    const buttons = [...document.querySelectorAll("[data-probe], #settings-probe-all")];
    buttons.forEach((button) => {
      button.disabled = true;
    });
    const target = focus || "all";
    if (target === "all") {
      ["sub2api", "hme", "mail"].forEach((service) => setServiceState(service, "检测中…"));
    } else {
      setServiceState(target, "检测中…");
    }
    try {
      const payload = await fetchEntity("settings-probe", "/api/settings/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ connections: connectionsPayload(form), target: focus || "all" }),
      });
      probeCopy(payload);
    } catch (error) {
      const message = friendlyError(error);
      (target === "all" ? ["sub2api", "hme", "mail"] : [target]).forEach((service) => setServiceState(service, message));
    } finally {
      buttons.forEach((button) => {
        button.disabled = false;
      });
    }
  }

  function renderSub2ApiManagement(status, templates) {
    const usageEl = document.getElementById("sub2api-usage-status");
    if (usageEl && status) {
      const snapshot = status.last_success_at ? relativeTime(status.last_success_at) : "尚无成功快照";
      const failures = status.failed_windows ? ` · ${status.failed_windows} 个失败窗口` : "";
      usageEl.textContent = `${status.verified_bindings || 0} 个已验证绑定 · ${status.snapshot_count || 0} 个窗口 · ${snapshot}${failures}`;
      usageEl.className = status.failed_windows ? "hint error" : "hint";
    }
    const templateEl = document.getElementById("sub2api-template-status");
    if (templateEl && templates) {
      templateEl.textContent = templates.supported
        ? `${(templates.items || []).length} 个远端模板可用`
        : (templates.message || "当前远端不支持账号推送模板");
      templateEl.className = "hint";
    }
  }

  async function loadSub2ApiManagement() {
    const statusTask = fetchEntity("sub2api-status", "/api/sub2api/status")
      .then((status) => renderSub2ApiManagement(status, null))
      .catch((error) => {
        const usageEl = document.getElementById("sub2api-usage-status");
        if (usageEl) {
          usageEl.textContent = friendlyError(error);
          usageEl.className = "hint error";
        }
      });
    const templateTask = fetchEntity("sub2api-templates", "/api/sub2api/templates")
      .then((templates) => renderSub2ApiManagement(null, templates))
      .catch((error) => renderSub2ApiManagement(null, {
        supported: false,
        message: friendlyError(error),
      }));
    await Promise.allSettled([statusTask, templateTask]);
  }

  async function syncAllSub2ApiUsage(button) {
    if (button) button.disabled = true;
    try {
      const result = await postAction("sub2api-usage-sync-all", "/api/sub2api/usage/sync", {});
      await handleActionResult(result, { successMessage: result.message || "Sub2API 用量同步完成" });
    } catch (error) {
      toast(friendlyError(error), "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const statusEl = document.getElementById("settings-status");
    const saveButton = document.getElementById("settings-save");
    const payload = {
      connections: connectionsPayload(form),
      automation: { official_quota_probe: form.official_quota_probe.checked },
      resources: {
        sms_max_uses_per_phone: numberOrNull(form.sms_max_uses_per_phone.value),
        sms_cooldown_sec: minutesToSeconds(form.sms_cooldown_min.value),
        sms_reserve_sec: minutesToSeconds(form.sms_reserve_min.value),
      },
    };
    if (saveButton) saveButton.disabled = true;
    if (statusEl) {
      statusEl.dataset.locked = "1";
      statusEl.className = "muted";
      statusEl.textContent = "正在保存…";
    }
    try {
      const saved = await fetchEntity("settings-save", "/api/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
      fillSettings(saved);
      if (statusEl) {
        statusEl.className = "muted";
        statusEl.textContent = "已保存 · 刚刚";
      }
      toast("设置已保存", "success");
    } catch (error) {
      if (statusEl) {
        statusEl.className = "error";
        statusEl.textContent = friendlyError(error);
      }
    } finally {
      if (saveButton) saveButton.disabled = false;
      if (statusEl) window.setTimeout(() => delete statusEl.dataset.locked, 1200);
    }
  }

  async function savePassword(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const statusEl = document.getElementById("password-status");
    const button = form.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.className = "muted";
      statusEl.textContent = "正在修改密码…";
    }
    try {
      await fetchEntity("password-save", "/api/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          password: {
            old_password: form.old_password.value,
            new_password: form.new_password.value,
            confirm_password: form.confirm_password.value,
          },
        }),
      });
      form.reset();
      if (statusEl) {
        statusEl.className = "muted";
        statusEl.textContent = "密码已修改。";
      }
    } catch (error) {
      if (statusEl) {
        statusEl.className = "error";
        statusEl.textContent = friendlyError(error);
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  function bindSearch(input, kind) {
    if (!input || input.dataset.bound) return;
    input.dataset.bound = "1";
    const params = currentQuery();
    if (params.get("q")) input.value = params.get("q");
    input.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        const next = currentQuery();
        const value = input.value.trim();
        if (value) next.set("q", value);
        else next.delete("q");
        writeQuery(next);
        if (kind === "account" && (currentQuery().get("view") || "portfolio") !== "flat") renderPortfolio(pageCache.portfolio || { groups: [], unassigned: [] });
        else paintList(kind);
      }, 200);
    });
  }

  function kindBadge(kind) {
    const span = document.createElement("span");
    span.className = kind === "mother" ? "badge badge-primary" : (kind === "child" ? "badge badge-info" : (kind === "unmanaged" ? "badge badge-warning" : "badge badge-muted"));
    span.textContent = { mother: "母号", child: "子号", history: "历史", unmanaged: "未接入", invited: "待接受", unassigned: "未归属" }[kind] || kind;
    return span;
  }

  function portfolioAccountRow(account, kind) {
    const row = document.createElement("div");
    row.className = kind === "history" ? "portfolio-row is-history" : (kind === "mother" ? "portfolio-row is-mother" : "portfolio-row");
    const canOpen = Boolean(account.id) && kind !== "unmanaged" && kind !== "invited";
    if (canOpen) bindRow(row, "account", account);

    const identity = document.createElement("div");
    identity.className = "portfolio-cell portfolio-identity";
    identity.append(kindBadge(kind));
    const label = twoLine(account.email || account.name || "—", roleLabel(account.official_role) || account.note || null);
    identity.append(label);
    row.append(identity);

    const authCol = document.createElement("div");
    authCol.className = "portfolio-cell portfolio-auth";
    authCol.append(statusNode(account.auth || account.membership_state, labelOf(statusLabels, account.auth || account.membership_state)));
    row.append(authCol);

    const quotaCol = document.createElement("div");
    quotaCol.className = "portfolio-cell portfolio-quota";
    quotaCol.append(canOpen ? quotaCell(account) : document.createTextNode(kind === "unmanaged" ? "未接入，无法读取额度" : (kind === "invited" ? "待接受邀请" : "尚未获取")));
    row.append(quotaCol);

    const sub2Col = document.createElement("div");
    sub2Col.className = "portfolio-cell portfolio-sub2 row-action-host";
    sub2Col.append(statusNode(account.sub2api, labelOf(statusLabels, account.sub2api)));
    if (canOpen) {
      const actions = document.createElement("div");
      actions.className = "row-actions row-actions-contextual";
      actions.append(menuButton("account", account));
      sub2Col.append(actions);
    }
    row.append(sub2Col);
    return row;
  }

  function renderPortfolio(payload) {
    const root = document.getElementById("accounts-portfolio");
    const table = document.querySelector("#accounts-body")?.closest(".table-scroll");
    if (!root) return;
    pageCache.portfolio = payload || pageCache.portfolio || { groups: [], unassigned: [] };
    const data = pageCache.portfolio;
    root.hidden = false;
    if (table) table.hidden = true;
    root.replaceChildren();
    const q = (currentQuery().get("q") || "").trim().toLowerCase();
    const purpose = currentQuery().get("purpose") || "all";
    const groups = data.groups || [];
    let shown = 0;

    const matchesPurpose = (account) => {
      if (!purpose || purpose === "all") return true;
      if (!account) return false;
      if (purpose === "conflict") return account.state === "conflict";
      if (purpose === "archived") return account.state === "archived" || account.kind === "history";
      if (purpose === "needs_auth") return needsAuth(account);
      if (purpose === "quota_full") return (account.quota || {}).seven_day_used_percent === 100;
      return account.purpose === purpose;
    };
    const haystack = (group) => [
      group.display_name,
      group.name,
      group.owner_email,
      group.official_workspace_id,
      (group.mother || {}).email,
      ...(group.current_children || []).map((row) => row.email),
      ...(group.unmanaged || []).map((row) => row.email),
      ...(group.history || []).map((row) => row.email),
    ].join(" ").toLowerCase();

    groups.forEach((group) => {
      if (q && !haystack(group).includes(q)) return;
      const mother = group.mother && matchesPurpose(group.mother) ? group.mother : null;
      const currentChildren = (group.current_children || []).filter(matchesPurpose);
      const unmanaged = (group.unmanaged || []).filter((row) => purpose === "all" || purpose === "conflict" ? matchesPurpose(row) : false);
      const history = (group.history || []).filter(matchesPurpose);
      if (purpose && purpose !== "all" && !mother && !currentChildren.length && !unmanaged.length && !history.length) return;
      shown += 1;
      const box = document.createElement("section");
      box.className = "portfolio-group";
      const head = document.createElement("div");
      head.className = "portfolio-head";
      head.tabIndex = 0;
      head.setAttribute("role", "button");
      head.setAttribute("aria-expanded", "true");

      const toggle = document.createElement("span");
      toggle.className = "toggle-icon";
      toggle.textContent = "▾";
      toggle.setAttribute("aria-hidden", "true");

      const body = document.createElement("div");
      body.className = "portfolio-body";
      const collapse = () => {
        const closed = box.classList.toggle("is-collapsed");
        head.setAttribute("aria-expanded", closed ? "false" : "true");
      };
      head.addEventListener("click", (event) => {
        if (event.target.closest(".row-actions")) return;
        collapse();
      });
      head.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        collapse();
      });

      const counts = group.counts || {};
      const meta = document.createElement("div");
      meta.className = "portfolio-meta";
      meta.append(twoLine(group.display_name || group.name, shortId(group.official_workspace_id)));

      const ownerCol = document.createElement("div");
      ownerCol.className = "portfolio-owner";
      ownerCol.append(twoLine(group.owner_email || "无母号", `${counts.joined_people ?? ((counts.managed_children ?? counts.current_children ?? 0) + (counts.unmanaged ?? 0) + (group.mother ? 1 : 0))} 人 · ${counts.managed_children ?? counts.current_children ?? 0} 子号 · ${counts.unmanaged ?? 0} 未接入`));
      const groupUsage = workspaceUsageSummary(group.usage);
      if (groupUsage) ownerCol.append(groupUsage);

      const healthCol = document.createElement("div");
      healthCol.className = "portfolio-health";
      healthCol.append(statusNode(group.health, labelOf(statusLabels, group.health)));

      const syncCol = document.createElement("div");
      syncCol.className = "portfolio-sync";
      syncCol.append(timeNode(group.last_sync));

      head.append(toggle, meta, ownerCol, healthCol, syncCol);

      if (mother) body.append(portfolioAccountRow(mother, "mother"));
      currentChildren.forEach((row) => body.append(portfolioAccountRow(row, "child")));
      unmanaged.forEach((row) => body.append(portfolioAccountRow(row, "unmanaged")));
      if (history.length) {
        const hist = document.createElement("details");
        hist.className = "portfolio-history";
        const summary = document.createElement("summary");
        summary.className = "portfolio-history-toggle";
        summary.textContent = "历史归档成员 (" + history.length + ")";
        hist.append(summary);
        history.forEach((row) => hist.append(portfolioAccountRow(row, "history")));
        body.append(hist);
      }
      box.append(head, body);
      root.append(box);
    });
    (data.unassigned || []).forEach((row) => {
      if (q && !String(row.email || "").toLowerCase().includes(q)) return;
      if (!matchesPurpose(row)) return;
      shown += 1;
      root.append(portfolioAccountRow(row, "unassigned"));
    });
    if (!shown) root.append(emptyState("没有符合当前筛选的工作区", "清除筛选或换一个关键词。", true));
    setCount("accounts-count", shown, (data.groups || []).length + (data.unassigned || []).length);
  }

  function paintList(kind) {
    const items = filterItems(kind, pageCache.items);
    if (kind === "workspace") {
      renderRows(
        "workspaces-body",
        items,
        5,
        workspaceRow,
        items.length === 0 && pageCache.items.length
          ? emptyState("没有符合当前筛选的工作区", "清除筛选或换一个关键词。")
          : emptyState("还没有工作区", "点「登记团队」，填母号邮箱并完成 OAuth 授权。")
      );
      setCount("workspaces-count", items.length, pageCache.items.length);
    } else if (kind === "account") {
      const view = currentQuery().get("view") || "portfolio";
      const table = document.querySelector("#accounts-body")?.closest(".table-scroll");
      const portfolio = document.getElementById("accounts-portfolio");
      if (view === "flat") {
        if (table) table.hidden = false;
        if (portfolio) portfolio.hidden = true;
        renderRows(
          "accounts-body",
          items,
          7,
          accountRow,
          items.length === 0 && pageCache.items.length
            ? emptyState("没有符合当前筛选的账号", "清除筛选或换一个关键词。")
            : emptyState("还没有账号", "先登记团队母号。归档的默认不显示。")
        );
        setCount("accounts-count", items.length, pageCache.items.length);
      } else if (pageCache.portfolio) {
        renderPortfolio(pageCache.portfolio);
      }
    }
  }

  async function bootOverview() {
    renderOverview(await fetchEntity("overview", "/api/overview"));
  }

  async function bootWorkspaces() {
    bindSearch(document.getElementById("workspaces-search"), "workspace");
    const payload = await fetchEntity("workspace-list", "/api/workspaces");
    pageCache.kind = "workspace";
    pageCache.items = payload.items || [];
    paintList("workspace");
  }

  async function bootAccounts() {
    const filter = document.querySelector("[data-filter='purpose']");
    const params = currentQuery();
    if (filter && params.get("purpose")) filter.value = params.get("purpose");
    if (filter && !filter.dataset.bound) {
      filter.dataset.bound = "1";
      filter.addEventListener("change", () => {
        const next = currentQuery();
        if (filter.value && filter.value !== "all") next.set("purpose", filter.value);
        else next.delete("purpose");
        writeQuery(next);
        bootPage();
      });
    }
    document.querySelectorAll("[data-accounts-view]").forEach((button) => {
      if (button.dataset.bound) return;
      button.dataset.bound = "1";
      button.addEventListener("click", () => {
        const next = currentQuery();
        const view = button.dataset.accountsView || "portfolio";
        if (view === "portfolio") next.delete("view");
        else next.set("view", view);
        writeQuery(next);
        bootPage();
      });
    });
    const view = params.get("view") || "portfolio";
    document.querySelectorAll("[data-accounts-view]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.accountsView === view);
    });
    bindSearch(document.getElementById("accounts-search"), "account");
    if (view !== "flat") {
      const payload = await fetchEntity("account-portfolio", "/api/accounts/portfolio");
      pageCache.kind = "account";
      pageCache.items = [];
      pageCache.portfolio = payload;
      renderPortfolio(payload);
      return;
    }
    const purpose = filter?.value || params.get("purpose") || "all";
    const includeArchived = purpose === "archived";
    const payload = await fetchEntity(
      "account-list",
      `/api/accounts?purpose=${encodeURIComponent(purpose)}&include_archived=${includeArchived}`
    );
    pageCache.kind = "account";
    pageCache.items = payload.items || [];
    paintList("account");
  }

  function operationsQueryFromUI() {
    const params = currentQuery();
    const q = document.getElementById("operations-search")?.value || params.get("q") || "";
    const state = document.getElementById("operations-state")?.value || params.get("state") || "";
    const type = document.getElementById("operations-type")?.value || params.get("type") || "";
    const source = document.getElementById("operations-source")?.value || params.get("source") || "";
    const range = document.getElementById("operations-range")?.value || params.get("range") || "7d";
    const archivedOnly = document.getElementById("operations-archived-only")?.checked || params.get("archived_only") === "1";
    const page = Number(params.get("page") || 1);
    const pageSize = Number(document.getElementById("operations-page-size")?.value || params.get("page_size") || 50);
    const query = new URLSearchParams();
    if (q) query.set("q", q);
    if (state) query.set("state", state);
    if (type) query.set("type", type);
    if (source) query.set("source", source);
    if (range === "today") query.set("date_from", "today");
    else if (range === "30d") query.set("date_from", "30d");
    else if (range === "all") query.delete("date_from");
    else query.set("date_from", "7d");
    if (archivedOnly) {
      query.set("archived_only", "true");
      query.set("include_archived", "true");
    }
    query.set("page", String(page || 1));
    query.set("page_size", String(pageSize || 50));
    return query;
  }

  function syncOperationsUrl(query) {
    const next = currentQuery();
    ["q", "state", "type", "source", "date_from", "date_to", "archived_only", "include_archived", "page", "page_size", "range"].forEach((key) => next.delete(key));
    query.forEach((value, key) => next.set(key, value));
    const range = document.getElementById("operations-range")?.value;
    if (range) next.set("range", range);
    writeQuery(next);
  }

  function updateOperationsBulkButton() {
    const button = document.getElementById("operations-bulk-archive");
    if (!button) return;
    const selected = Array.from(document.querySelectorAll(".operation-select:checked"));
    button.hidden = selected.length === 0;
    button.textContent = selected.length ? `清除所选（${selected.length}）` : "清除所选";
  }

  async function bootOperations() {
    const search = document.getElementById("operations-search");
    const state = document.getElementById("operations-state");
    const type = document.getElementById("operations-type");
    const source = document.getElementById("operations-source");
    const range = document.getElementById("operations-range");
    const archivedOnly = document.getElementById("operations-archived-only");
    const pageSize = document.getElementById("operations-page-size");
    const params = currentQuery();
    if (search && params.get("q")) search.value = params.get("q");
    if (state && params.get("state")) state.value = params.get("state");
    if (type && params.get("type")) type.value = params.get("type");
    if (source && params.get("source")) source.value = params.get("source");
    if (range && params.get("range")) range.value = params.get("range");
    if (archivedOnly) archivedOnly.checked = params.get("archived_only") === "1" || params.get("archived_only") === "true";
    if (pageSize && params.get("page_size")) pageSize.value = params.get("page_size");

    if (search && !search.dataset.bound) {
      search.dataset.bound = "1";
      let timer = null;
      search.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => {
          const next = currentQuery();
          next.set("page", "1");
          writeQuery(next);
          bootOperations();
        }, 250);
      });
    }
    [state, type, source, range, archivedOnly, pageSize].forEach((node) => {
      if (!node || node.dataset.bound) return;
      node.dataset.bound = "1";
      node.addEventListener("change", () => {
        const next = currentQuery();
        next.set("page", "1");
        writeQuery(next);
        bootOperations();
      });
    });
    const clearBtn = document.getElementById("operations-clear-filters");
    if (clearBtn && !clearBtn.dataset.bound) {
      clearBtn.dataset.bound = "1";
      clearBtn.addEventListener("click", () => {
        if (search) search.value = "";
        if (state) state.value = "";
        if (type) type.value = "";
        if (source) source.value = "";
        if (range) range.value = "7d";
        if (archivedOnly) archivedOnly.checked = false;
        const next = currentQuery();
        ["q", "state", "type", "source", "date_from", "date_to", "archived_only", "include_archived", "page"].forEach((k) => next.delete(k));
        next.set("range", "7d");
        next.set("page", "1");
        writeQuery(next);
        bootOperations();
      });
    }
    const bulk = document.getElementById("operations-bulk-archive");
    if (bulk && !bulk.dataset.bound) {
      bulk.dataset.bound = "1";
      bulk.addEventListener("click", async () => {
        const ids = Array.from(document.querySelectorAll(".operation-select:checked")).map((node) => node.dataset.publicId).filter(Boolean);
        if (!ids.length) return;
        if (!confirmDanger(`确认清除当前选中的 ${ids.length} 条已完成任务？`)) return;
        const result = await postAction("operations-bulk-archive", "/api/operations/bulk-archive", { public_ids: ids, reason: "bulk_clear" });
        toast(result.ok ? `已清除 ${result.count || ids.length} 条` : (result.error || "清除失败"), result.ok ? "success" : "error");
        await bootOperations();
      });
    }
    const selectPage = document.getElementById("operations-select-page");
    if (selectPage && !selectPage.dataset.bound) {
      selectPage.dataset.bound = "1";
      selectPage.addEventListener("change", () => {
        document.querySelectorAll(".operation-select").forEach((node) => {
          if (!node.disabled) node.checked = selectPage.checked;
        });
        updateOperationsBulkButton();
      });
    }
    const prev = document.getElementById("operations-prev");
    const nextBtn = document.getElementById("operations-next");
    if (prev && !prev.dataset.bound) {
      prev.dataset.bound = "1";
      prev.addEventListener("click", () => {
        const next = currentQuery();
        const page = Math.max(1, Number(next.get("page") || 1) - 1);
        next.set("page", String(page));
        writeQuery(next);
        bootOperations();
      });
    }
    if (nextBtn && !nextBtn.dataset.bound) {
      nextBtn.dataset.bound = "1";
      nextBtn.addEventListener("click", () => {
        const next = currentQuery();
        const page = Math.max(1, Number(next.get("page") || 1) + 1);
        next.set("page", String(page));
        writeQuery(next);
        bootOperations();
      });
    }

    const query = operationsQueryFromUI();
    syncOperationsUrl(query);
    const payload = await fetchEntity("operation-list", `/api/operations?${query.toString()}`);
    renderRows(
      "operations-body",
      payload.items || [],
      8,
      operationRow,
      emptyState("没有符合筛选的任务", "调整筛选，或先创建额度刷新、同步、推送任务。")
    );
    const count = document.getElementById("operations-count");
    if (count) {
      const total = payload.total ?? (payload.items || []).length;
      count.textContent = `共 ${total} 条` + (payload.range?.defaulted_to_last_7_days ? " · 默认最近 7 天" : "");
    }
    const pageLabel = document.getElementById("operations-page-label");
    if (pageLabel) pageLabel.textContent = `第 ${payload.page || 1} 页` + (payload.has_more ? " · 还有更多" : "");
    if (prev) prev.disabled = Number(payload.page || 1) <= 1;
    if (nextBtn) nextBtn.disabled = !payload.has_more;
    if (selectPage) selectPage.checked = false;
    updateOperationsBulkButton();
    const op = currentQuery().get("op");
    if (op) await openOperationById(op);
  }

  async function bootPhones() {
    const payload = await fetchEntity("phone-list", "/api/resources/phones");
    const items = payload.items || [];
    renderRows(
      "phones-body",
      items,
      8,
      phoneRow,
      emptyState("还没有手机号", "点「导入号码」，按 号码----接码链接 批量入库。")
    );
    setCount("phones-count", items.length, items.length);
  }

  async function bootHme() {
    const payload = await fetchEntity("hme-list", "/api/resources/hme");
    const items = payload.items || [];
    renderRows(
      "hme-body",
      items,
      7,
      hmeRow,
      emptyState("还没有 HME 占用记录", "领用别名后会显示本地占用和同步状态。")
    );
    setCount("hme-count", items.length, items.length);
  }

  async function bootProxies() {
    const payload = await fetchEntity("proxy-list", "/api/resources/proxies");
    const items = payload.items || [];
    renderRows(
      "proxies-body",
      items,
      8,
      proxyRow,
      emptyState("还没有代理", "点「添加代理」，或在登记母号时填写代理自动入库。")
    );
    setCount("proxies-count", items.length, items.length);
  }

  async function bootSettings() {
    const form = document.getElementById("settings-form");
    const passwordForm = document.getElementById("password-form");
    if (form && !form.dataset.bound) {
      form.dataset.bound = "1";
      form.addEventListener("submit", saveSettings);
      form.addEventListener("input", updateDirty);
      form.addEventListener("change", updateDirty);
      document.getElementById("settings-discard")?.addEventListener("click", () => bootPage());
      document.getElementById("settings-probe-all")?.addEventListener("click", () => probeSettings("all"));
      document.getElementById("sub2api-usage-sync-all")?.addEventListener("click", (event) => syncAllSub2ApiUsage(event.currentTarget));
      document.querySelectorAll("[data-probe]").forEach((button) => {
        button.addEventListener("click", () => probeSettings(button.dataset.probe));
      });
      document.querySelectorAll("[data-edit-toggle]").forEach((button) => {
        button.addEventListener("click", () => {
          const panel = document.querySelector(`[data-edit-panel="${button.dataset.editToggle}"]`);
          if (!panel) return;
          panel.hidden = !panel.hidden;
          button.textContent = panel.hidden ? "编辑" : "收起";
        });
      });
    }
    if (passwordForm && !passwordForm.dataset.bound) {
      passwordForm.dataset.bound = "1";
      passwordForm.addEventListener("submit", savePassword);
    }
    fillSettings(await fetchEntity("settings", "/api/settings"));
    void loadSub2ApiManagement();
  }

  const pageBootstraps = {
    overview: bootOverview,
    workspaces: bootWorkspaces,
    accounts: bootAccounts,
    operations: bootOperations,
    phones: bootPhones,
    hme: bootHme,
    proxies: bootProxies,
    settings: bootSettings,
  };

  async function bootPage() {
    const page = document.body.dataset.page;
    hidePageError();
    try {
      const bootstrap = pageBootstraps[page];
      if (bootstrap) await bootstrap();
      resumeCurrentOperations();
    } catch (error) {
      if (isAbortError(error) || error.name === "AbortError") return;
      showPageError(friendlyError(error));
    }
  }

  function renderPalette(query) {
    const q = query.trim().toLowerCase();
    commandList.replaceChildren();
    destinations
      .filter((item) => item.label.toLowerCase().includes(q) || item.label.includes(query.trim()))
      .forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item.label;
        li.addEventListener("click", () => {
          window.location.href = item.href;
        });
        commandList.append(li);
      });
  }

  document.querySelector("[data-close-sheet]")?.addEventListener("click", closeSheet);
  document.querySelectorAll("[data-open-register]").forEach((button) => {
    button.addEventListener("click", () => openRegister(button));
  });
  document.querySelector("[data-close-register]")?.addEventListener("click", closeRegister);
  document.getElementById("register-form")?.addEventListener("submit", submitRegister);
  document.querySelector("[data-close-reauth]")?.addEventListener("click", closeReauth);
  document.getElementById("reauth-form")?.addEventListener("submit", submitReauth);
  document.querySelectorAll("[data-open-phone-import]").forEach((button) => {
    button.addEventListener("click", () => openPhoneImport(button));
  });
  document.querySelector("[data-close-phone-import]")?.addEventListener("click", closePhoneImport);
  document.getElementById("phone-import-form")?.addEventListener("submit", submitPhoneImport);
  document.querySelectorAll("[data-open-proxy-add]").forEach((button) => {
    button.addEventListener("click", () => openProxyAdd(button));
  });
  document.querySelector("[data-close-proxy-add]")?.addEventListener("click", closeProxyAdd);
  document.getElementById("proxy-add-form")?.addEventListener("submit", submitProxyAdd);


  document.getElementById("register-copy-link")?.addEventListener("click", async () => {
    const authorize = document.getElementById("register-authorize-url");
    const copied = await copyText(authorize?.value);
    setRegisterStatus(copied ? "授权链接已复制。" : "复制失败，请手动选中链接。", copied ? "muted" : "error");
  });
  document.getElementById("reauth-copy-link")?.addEventListener("click", async () => {
    const authorize = document.getElementById("reauth-authorize-url");
    const copied = await copyText(authorize?.value);
    setReauthStatus(copied ? "授权链接已复制。" : "复制失败，请手动选中链接。", copied ? "muted" : "error");
  });
  document.querySelectorAll("[data-action-page]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.actionPage;
      button.disabled = true;
      try {
        if (action === "hme-reconcile") {
          const result = await postAction("hme-reconcile", "/api/resources/hme/reconcile");
          await handleActionResult(result, { successMessage: result.message || ("HME 对账完成，差异 " + (result.conflicts ?? 0)) });
        } else if (action === "hme-retry-pending") {
          const payload = await fetchEntity("hme-list", "/api/resources/hme");
          const pending = (payload.items || []).filter((item) => item.pending);
          let ok = 0;
          for (const item of pending) {
            const result = await postAction(`hme-retry-${item.id}`, `/api/resources/hme/${item.id}/retry-label`);
            if (result.ok) ok += 1;
          }
          toast(`已重试 ${ok}/${pending.length} 个待同步标签`, ok === pending.length ? "success" : "warning");
          await bootPage();
        } else if (action === "proxy-probe-all") {
          const result = await postAction("proxy-probe-all", "/api/resources/proxies/probe-all");
          await handleActionResult(result, { successMessage: result.message || ("检测完成：健康 " + (result.healthy ?? 0) + "/" + (result.total ?? 0)) });
        } else if (action === "workspace-sync-all") {
          const payload = await fetchEntity("workspace-list", "/api/workspaces");
          const items = payload.items || [];
          let ok = 0;
          for (const item of items) {
            const result = await postAction(`workspace-sync-${item.id}`, `/api/workspaces/${item.id}/sync`);
            if (result.ok) ok += 1;
          }
          toast(`同步全部完成：${ok}/${items.length}`, ok === items.length ? "success" : "warning");
          await bootPage();
        }
      } catch (error) {
        toast(friendlyError(error), "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.getElementById("page-retry")?.addEventListener("click", bootPage);
  document.getElementById("sidebar-toggle")?.addEventListener("click", () => {
    document.body.classList.toggle("nav-open");
  });
  overlayRegistry.forEach((overlay, name) => {
    overlay?.addEventListener("click", (event) => {
      if (event.target !== overlay) return;
      if (name === "entity") closeSheet();
      else closeOverlay();
    });
  });
  document.addEventListener("click", (event) => {
    if (menu && !menu.hidden && !event.target.closest("#action-menu, [data-menu-trigger], .row-actions")) closeMenu();
  });

  window.addEventListener("keydown", (event) => {
    if (!getActiveOverlay() && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      palette.showModal();
      renderPalette("");
      commandInput.focus();
    }
    if (event.key === "Escape") {
      if (menu && !menu.hidden) {
        closeMenu();
      } else if (getActiveOverlay()) {
        event.preventDefault();
        if (getActiveOverlay() === "entity") closeSheet();
        else closeOverlay();
      }
      document.body.classList.remove("nav-open");
    }
    handleFocusTrap(event);
  });
  commandInput?.addEventListener("input", () => renderPalette(commandInput.value));
  window.addEventListener("beforeunload", (event) => {
    stopPolling();
    if (!settingsDirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") stopPolling();
  });
  document.querySelector("[data-close-proxy-edit]")?.addEventListener("click", closeProxyEdit);
  document.getElementById("proxy-edit-form")?.addEventListener("submit", submitProxyEdit);

  window.Team48 = {
    abortEntity,
    fetchEntity,
    entityActions,
    pageBootstraps,
    overlayState,
    openOverlay,
    replaceOverlay,
    closeOverlay,
    restoreOverlayContext,
    getActiveOverlay,
    presentTeamMember,
    workspacePrimaryAction,
  };
  bootPage();
})();
