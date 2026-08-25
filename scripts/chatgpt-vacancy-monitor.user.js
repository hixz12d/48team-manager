// ==UserScript==
// @name         ChatGPT 席位阈值历史监控
// @namespace    local.codex.chatgpt
// @version      2.1.0
// @description  按当前空间显示成员席位阈值、ordinal < threshold 比较、邮箱与有效期变化历史
// @match        https://chatgpt.com/admin/members*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(() => {
  'use strict';

  const STORAGE_KEY = 'codex:chatgpt-vacancy-monitor:v1';
  const MEMBERS_KEY = 'codex:chatgpt-vacancy-monitor:members:v1';
  const COLLAPSED_KEY = 'codex:chatgpt-vacancy-monitor:collapsed';
  const POSITION_KEY = 'codex:chatgpt-vacancy-monitor:position:v2';
  const USER_URL_PATTERN = /^\/backend-api\/accounts\/([^/]+)\/users\/(user-[^/]+)\/?$/;
  const MEMBERS_URL_PATTERN = /^\/backend-api\/accounts\/([^/]+)\/users\/?$/;

  let records = loadRecords();
  let memberAliases = loadMemberAliases();
  let fallbackAccountId = null;
  let currentAccountId = readAccountCookie();
  let renderPanel = () => {};

  function loadRecords() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
      return {};
    }
  }

  function saveRecords() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
    } catch (error) {
      console.warn('[席位阈值监控] 无法保存历史记录：', error);
    }
  }

  function loadMemberAliases() {
    try {
      const parsed = JSON.parse(localStorage.getItem(MEMBERS_KEY) || '{}');
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
      return {};
    }
  }

  function saveMemberAliases() {
    try {
      localStorage.setItem(MEMBERS_KEY, JSON.stringify(memberAliases));
    } catch (error) {
      console.warn('[席位阈值监控] 无法保存成员邮箱索引：', error);
    }
  }

  function parseUrl(rawUrl, pattern) {
    try {
      const url = new URL(String(rawUrl), location.origin);
      if (url.origin !== location.origin) return null;

      const match = url.pathname.match(pattern);
      if (!match) return null;

      return match;
    } catch {
      return null;
    }
  }

  function parseUserTarget(rawUrl) {
    const match = parseUrl(rawUrl, USER_URL_PATTERN);
    if (!match) return null;

    return {
      accountId: decodeURIComponent(match[1]),
      userId: decodeURIComponent(match[2]),
    };
  }

  function parseMembersTarget(rawUrl) {
    const match = parseUrl(rawUrl, MEMBERS_URL_PATTERN);
    if (!match) return null;
    return { accountId: decodeURIComponent(match[1]) };
  }

  function readAccountCookie() {
    try {
      for (const part of document.cookie.split(';')) {
        const separator = part.indexOf('=');
        if (separator < 0 || part.slice(0, separator).trim() !== '_account') continue;

        let value = part.slice(separator + 1).trim();
        try {
          value = decodeURIComponent(value);
        } catch {
          // Cookie 不是 URI 编码时直接使用原值。
        }
        if (value.startsWith('"') && value.endsWith('"')) {
          value = value.slice(1, -1);
        }
        return value || null;
      }
    } catch {
      // Cookie 不可读时使用最近的成员列表请求作为备用。
    }
    return null;
  }

  function refreshCurrentAccount() {
    const nextAccountId = readAccountCookie() || fallbackAccountId;
    if (nextAccountId === currentAccountId) return;
    currentAccountId = nextAccountId;
    renderPanel();
  }

  function useFallbackAccount(accountId) {
    fallbackAccountId = accountId;
    refreshCurrentAccount();
  }

  function capturePolicy(rawUrl, payload) {
    const target = parseUserTarget(rawUrl);
    if (
      !target ||
      !payload ||
      typeof payload !== 'object' ||
      !Object.prototype.hasOwnProperty.call(payload, 'policy_notice')
    ) {
      return;
    }

    const policy = payload.policy_notice;
    let vacancyOrdinal;
    let freeVacancyThreshold;
    let billingStartsAt;
    let expiresAt;

    if (policy === null) {
      vacancyOrdinal = null;
      freeVacancyThreshold = null;
      billingStartsAt = null;
      expiresAt = null;
    } else if (
      typeof policy === 'object' &&
      Object.prototype.hasOwnProperty.call(policy, 'vacancy_ordinal') &&
      Object.prototype.hasOwnProperty.call(policy, 'free_vacancy_threshold')
    ) {
      vacancyOrdinal = policy.vacancy_ordinal;
      freeVacancyThreshold = policy.free_vacancy_threshold;
      billingStartsAt = policy.billing_starts_at ?? null;
      expiresAt = policy.expires_at ?? null;
    } else {
      return;
    }

    // 先合并其他标签页刚写入的数据，减少并发页面互相覆盖的概率。
    const latestRecords = loadRecords();
    records = { ...latestRecords, ...records };

    const key = `${target.accountId}/${target.userId}`;
    const entry = records[key] || {
      accountId: target.accountId,
      userId: target.userId,
      history: [],
    };
    const previous = entry.history.at(-1);
    const next = {
      capturedAt: new Date().toISOString(),
      vacancyOrdinal,
      freeVacancyThreshold,
      billingStartsAt,
      expiresAt,
    };

    if (
      previous &&
      Object.is(previous.vacancyOrdinal, next.vacancyOrdinal) &&
      Object.is(previous.freeVacancyThreshold, next.freeVacancyThreshold) &&
      Object.is(previous.billingStartsAt, next.billingStartsAt) &&
      Object.is(previous.expiresAt, next.expiresAt)
    ) {
      return;
    }

    entry.history.push(next);
    records[key] = entry;
    saveRecords();
    renderPanel();
  }

  function captureMembers(rawUrl, payload) {
    const target = parseMembersTarget(rawUrl);
    if (!target || !payload || !Array.isArray(payload.items)) return;

    const aliases = { ...(memberAliases[target.accountId] || {}) };
    let changed = false;

    for (const item of payload.items) {
      if (!item || typeof item.email !== 'string' || !item.email) continue;

      for (const alias of [item.id, item.account_user_id]) {
        if (typeof alias !== 'string' || !alias || aliases[alias] === item.email) continue;
        aliases[alias] = item.email;
        changed = true;
      }
    }

    if (!changed) return;
    memberAliases[target.accountId] = aliases;
    saveMemberAliases();
    renderPanel();
  }

  function shouldInspect(method, rawUrl) {
    if (method === 'DELETE') return Boolean(parseUserTarget(rawUrl));
    if (method === 'GET') {
      const target = parseMembersTarget(rawUrl);
      if (!target) return false;
      useFallbackAccount(target.accountId);
      return true;
    }
    return false;
  }

  function captureResponse(method, rawUrl, payload) {
    if (method === 'DELETE') capturePolicy(rawUrl, payload);
    if (method === 'GET') captureMembers(rawUrl, payload);
  }

  function installFetchMonitor() {
    const originalFetch = window.fetch;
    if (typeof originalFetch !== 'function') return;

    window.fetch = function monitoredFetch(...args) {
      const responsePromise = originalFetch.apply(this, args);
      const rawUrl = args[0] instanceof Request ? args[0].url : args[0];
      const method = String(
        args[1]?.method || (args[0] instanceof Request ? args[0].method : 'GET'),
      ).toUpperCase();

      if (shouldInspect(method, rawUrl)) {
        responsePromise
          .then((response) => {
            if (!response.ok) return;
            return response.clone().json()
              .then((payload) => captureResponse(method, rawUrl, payload));
          })
          .catch(() => {});
      }

      return responsePromise;
    };
  }

  function installXhrMonitor() {
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    const requestUrl = Symbol('vacancyMonitorUrl');
    const requestMethod = Symbol('vacancyMonitorMethod');

    XMLHttpRequest.prototype.open = function monitoredOpen(method, url, ...args) {
      this[requestUrl] = url;
      this[requestMethod] = String(method).toUpperCase();
      return originalOpen.call(this, method, url, ...args);
    };

    XMLHttpRequest.prototype.send = function monitoredSend(...args) {
      if (shouldInspect(this[requestMethod], this[requestUrl])) {
        this.addEventListener(
          'loadend',
          () => {
            if (this.status < 200 || this.status >= 300) return;
            try {
              const payload = this.responseType === 'json'
                ? this.response
                : JSON.parse(this.responseText);
              captureResponse(this[requestMethod], this[requestUrl], payload);
            } catch {
              // 非 JSON 响应与当前监控无关。
            }
          },
          { once: true },
        );
      }

      return originalSend.apply(this, args);
    };
  }

  function formatTime(isoTime) {
    if (isoTime === null) return 'null';
    if (!isoTime) return '--';
    const date = new Date(isoTime);
    if (Number.isNaN(date.getTime())) return '--';
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(date);
  }

  function valueText(value) {
    if (value === null) return 'null';
    if (value === undefined) return '--';
    return String(value);
  }

  function compareVacancy(item) {
    const ordinal = item?.vacancyOrdinal;
    const threshold = item?.freeVacancyThreshold;
    if (typeof ordinal === 'number' && typeof threshold === 'number') {
      const free = ordinal < threshold;
      return {
        free,
        symbol: free ? '<' : '≥',
        text: `${ordinal} ${free ? '<' : '≥'} ${threshold}`,
        predicate: `vacancy_ordinal ${free ? '<' : '≥'} free_vacancy_threshold`,
        result: String(free),
        label: free ? '可释放' : '未释放',
      };
    }
    if (ordinal === null && threshold === null) {
      return {
        free: null,
        symbol: '',
        text: 'policy_notice: null',
        predicate: 'vacancy_ordinal < free_vacancy_threshold',
        result: 'null',
        label: '无阈值',
      };
    }
    return {
      free: null,
      symbol: '',
      text: '--',
      predicate: 'vacancy_ordinal < free_vacancy_threshold',
      result: '--',
      label: '未知',
    };
  }

  function findMemberEmail(accountId, userId) {
    return memberAliases[accountId]?.[userId] || null;
  }

  function mountPanel() {
    if (!document.body || document.getElementById('codex-vacancy-monitor')) return;

    const host = document.createElement('div');
    host.id = 'codex-vacancy-monitor';
    const shadow = host.attachShadow({ mode: 'closed' });
    shadow.innerHTML = `
      <style>
        :host {
          all: initial;
          color-scheme: light dark;
        }

        .panel {
          --vm-bg: rgba(250, 250, 249, 0.97);
          --vm-fg: #1c1917;
          --vm-muted: #78716c;
          --vm-line: #d6d3d1;
          --vm-accent: #047857;
          --vm-accent-soft: #d1fae5;
          --vm-warn: #b45309;
          --vm-warn-soft: #ffedd5;
          position: fixed;
          top: 72px;
          right: 16px;
          z-index: 2147483646;
          width: min(480px, calc(100vw - 24px));
          max-height: min(72vh, 680px);
          overflow: hidden;
          color: var(--vm-fg);
          background: var(--vm-bg);
          border: 1px solid var(--vm-line);
          border-top: 3px solid var(--vm-accent);
          border-radius: 6px;
          box-shadow: 0 16px 42px rgba(28, 25, 23, 0.18);
          backdrop-filter: blur(14px);
          font: 13px/1.45 ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
          letter-spacing: 0;
        }

        .toolbar {
          min-height: 54px;
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 0 8px 0 12px;
          border-bottom: 1px solid var(--vm-line);
          cursor: grab;
          touch-action: none;
          user-select: none;
        }

        .toolbar.dragging {
          cursor: grabbing;
        }

        .title-wrap {
          min-width: 0;
          flex: 1;
          display: flex;
          flex-direction: column;
          gap: 3px;
        }

        .title {
          font: 600 14px/1.2 ui-sans-serif, "Microsoft YaHei UI", sans-serif;
          letter-spacing: 0;
        }

        .space-id {
          overflow: hidden;
          color: var(--vm-muted);
          font-size: 10px;
          line-height: 1.2;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .count {
          display: inline-flex;
          min-width: 20px;
          height: 20px;
          align-items: center;
          justify-content: center;
          padding: 0 5px;
          color: #065f46;
          background: var(--vm-accent-soft);
          border-radius: 3px;
          font-size: 11px;
          font-weight: 700;
        }

        button {
          height: 30px;
          border: 1px solid transparent;
          color: var(--vm-muted);
          background: transparent;
          border-radius: 4px;
          font: 600 12px/1 ui-sans-serif, "Microsoft YaHei UI", sans-serif;
          letter-spacing: 0;
          cursor: pointer;
        }

        button:hover {
          color: var(--vm-fg);
          border-color: var(--vm-line);
          background: rgba(120, 113, 108, 0.08);
        }

        button:focus-visible {
          outline: 2px solid var(--vm-accent);
          outline-offset: 1px;
        }

        .clear {
          padding: 0 8px;
        }

        .collapse {
          width: 30px;
          padding: 0;
          font-size: 18px;
        }

        .content {
          max-height: calc(min(72vh, 680px) - 55px);
          overflow: auto;
          overscroll-behavior: contain;
          scrollbar-width: thin;
        }

        .panel.collapsed {
          width: 42px;
          height: 42px;
          max-height: 42px;
          border-color: var(--vm-accent);
          border-top-width: 1px;
          box-shadow: 0 8px 24px rgba(28, 25, 23, 0.2);
        }

        .panel.collapsed .toolbar {
          width: 40px;
          height: 40px;
          min-height: 40px;
          gap: 0;
          padding: 0;
          border-bottom: 0;
        }

        .panel.collapsed .title-wrap,
        .panel.collapsed .count,
        .panel.collapsed .content,
        .panel.collapsed .clear {
          display: none;
        }

        .panel.collapsed .collapse {
          width: 40px;
          height: 40px;
          color: var(--vm-accent);
          border: 0;
          background: transparent;
          cursor: grab;
          font-size: 21px;
        }

        .panel.collapsed .toolbar.dragging .collapse {
          cursor: grabbing;
        }

        .empty {
          padding: 24px 16px;
          color: var(--vm-muted);
          text-align: center;
          font-family: ui-sans-serif, "Microsoft YaHei UI", sans-serif;
        }

        .user + .user {
          border-top: 1px solid var(--vm-line);
        }

        .user-head {
          padding: 11px 12px 9px;
          background: rgba(120, 113, 108, 0.045);
        }

        .email {
          display: block;
          overflow: hidden;
          color: var(--vm-fg);
          font-family: ui-sans-serif, "Microsoft YaHei UI", sans-serif;
          font-weight: 700;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .latest-compare {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 6px;
          margin-top: 6px;
        }

        .status-chip {
          display: inline-flex;
          align-items: center;
          height: 20px;
          padding: 0 6px;
          border-radius: 3px;
          font: 700 11px/1 ui-sans-serif, "Microsoft YaHei UI", sans-serif;
        }

        .status-chip.is-free {
          color: #065f46;
          background: var(--vm-accent-soft);
        }

        .status-chip.is-locked {
          color: var(--vm-warn);
          background: var(--vm-warn-soft);
        }

        .status-chip.is-unknown {
          color: var(--vm-muted);
          background: rgba(120, 113, 108, 0.12);
        }

        .history-row {
          padding: 10px 12px 11px;
        }

        .history-row + .history-row {
          border-top: 1px solid rgba(214, 211, 209, 0.72);
        }

        .history-row:first-child {
          background: rgba(4, 120, 87, 0.055);
        }

        .captured-time {
          margin-bottom: 8px;
          color: var(--vm-muted);
          font-size: 10px;
        }

        .metrics,
        .period,
        .compare {
          display: grid;
          grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
          gap: 8px 16px;
        }

        .compare,
        .period {
          margin-top: 9px;
          padding-top: 8px;
          border-top: 1px dashed rgba(120, 113, 108, 0.3);
        }

        .datum {
          min-width: 0;
        }

        .datum-label {
          display: block;
          overflow-wrap: anywhere;
          color: var(--vm-muted);
          font-size: 10px;
        }

        .datum-value {
          display: block;
          margin-top: 2px;
          font-variant-numeric: tabular-nums;
        }

        .metrics .datum-value,
        .compare .datum-value {
          color: var(--vm-accent);
          font-size: 15px;
          font-weight: 700;
        }

        .compare.is-locked .datum-value {
          color: var(--vm-warn);
        }

        .compare.is-unknown .datum-value {
          color: var(--vm-muted);
          font-size: 13px;
          font-weight: 600;
        }

        @media (prefers-color-scheme: dark) {
          .panel {
            --vm-bg: rgba(28, 25, 23, 0.97);
            --vm-fg: #fafaf9;
            --vm-muted: #a8a29e;
            --vm-line: #44403c;
            --vm-accent: #34d399;
            --vm-accent-soft: #064e3b;
            --vm-warn: #fdba74;
            --vm-warn-soft: #7c2d12;
          }

          .count,
          .status-chip.is-free {
            color: #a7f3d0;
          }
        }

        @media (max-width: 560px) {
          .panel {
            top: 60px;
            right: 8px;
            width: calc(100vw - 16px);
          }
        }
      </style>
      <section class="panel" aria-label="席位阈值历史监控">
        <header class="toolbar">
          <span class="title-wrap">
            <span class="title">席位阈值历史</span>
            <span class="space-id">空间：未识别</span>
          </span>
          <span class="count" aria-label="用户数量">0</span>
          <button class="clear" type="button">清空历史</button>
          <button class="collapse" type="button" title="收起面板" aria-label="收起面板">−</button>
        </header>
        <div class="content"></div>
      </section>
    `;

    const panel = shadow.querySelector('.panel');
    const content = shadow.querySelector('.content');
    const count = shadow.querySelector('.count');
    const spaceId = shadow.querySelector('.space-id');
    const clearButton = shadow.querySelector('.clear');
    const collapseButton = shadow.querySelector('.collapse');
    const toolbar = shadow.querySelector('.toolbar');
    let suppressCollapseClick = false;

    function savePosition() {
      const rect = panel.getBoundingClientRect();
      try {
        localStorage.setItem(POSITION_KEY, JSON.stringify({
          left: Math.round(rect.left),
          top: Math.round(rect.top),
        }));
      } catch (error) {
        console.warn('[席位阈值监控] 无法保存面板位置：', error);
      }
    }

    function setPanelPosition(left, top, shouldSave = false) {
      const rect = panel.getBoundingClientRect();
      const margin = 8;
      const maxLeft = Math.max(margin, window.innerWidth - rect.width - margin);
      const maxTop = Math.max(margin, window.innerHeight - rect.height - margin);
      const safeLeft = Math.min(Math.max(left, margin), maxLeft);
      const safeTop = Math.min(Math.max(top, margin), maxTop);

      panel.style.left = `${Math.round(safeLeft)}px`;
      panel.style.top = `${Math.round(safeTop)}px`;
      panel.style.right = 'auto';

      if (shouldSave) savePosition();
    }

    function restorePosition() {
      try {
        const position = JSON.parse(localStorage.getItem(POSITION_KEY) || 'null');
        if (
          position &&
          Number.isFinite(position.left) &&
          Number.isFinite(position.top)
        ) {
          setPanelPosition(position.left, position.top);
        }
      } catch {
        localStorage.removeItem(POSITION_KEY);
      }
    }

    function keepPanelInViewport(shouldSave = false) {
      const rect = panel.getBoundingClientRect();
      setPanelPosition(rect.left, rect.top, shouldSave);
    }

    const collapsed = localStorage.getItem(COLLAPSED_KEY) === '1';
    panel.classList.toggle('collapsed', collapsed);
    collapseButton.textContent = collapsed ? '+' : '−';
    collapseButton.title = collapsed ? '展开面板' : '收起面板';
    collapseButton.setAttribute('aria-label', collapseButton.title);

    collapseButton.addEventListener('click', () => {
      if (suppressCollapseClick) {
        suppressCollapseClick = false;
        return;
      }

      const anchorRect = collapseButton.getBoundingClientRect();
      const anchorX = anchorRect.left + anchorRect.width / 2;
      const anchorY = anchorRect.top + anchorRect.height / 2;
      const isCollapsed = panel.classList.toggle('collapsed');
      localStorage.setItem(COLLAPSED_KEY, isCollapsed ? '1' : '0');
      collapseButton.textContent = isCollapsed ? '+' : '−';
      collapseButton.title = isCollapsed ? '展开面板' : '收起面板';
      collapseButton.setAttribute('aria-label', collapseButton.title);
      requestAnimationFrame(() => {
        const nextButtonRect = collapseButton.getBoundingClientRect();
        const nextPanelRect = panel.getBoundingClientRect();
        const nextButtonX = nextButtonRect.left + nextButtonRect.width / 2;
        const nextButtonY = nextButtonRect.top + nextButtonRect.height / 2;
        setPanelPosition(
          nextPanelRect.left + anchorX - nextButtonX,
          nextPanelRect.top + anchorY - nextButtonY,
          true,
        );
      });
    });

    let dragState = null;

    toolbar.addEventListener('pointerdown', (event) => {
      const isCollapsed = panel.classList.contains('collapsed');
      if (event.button !== 0 || (!isCollapsed && event.target.closest('button'))) return;

      const rect = panel.getBoundingClientRect();
      dragState = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startLeft: rect.left,
        startTop: rect.top,
        moved: false,
      };
      toolbar.classList.add('dragging');
    });

    function moveDrag(event) {
      if (!dragState || dragState.pointerId !== event.pointerId) return;

      const deltaX = event.clientX - dragState.startX;
      const deltaY = event.clientY - dragState.startY;
      if (!dragState.moved && Math.hypot(deltaX, deltaY) >= 4) {
        dragState.moved = true;
        toolbar.setPointerCapture(event.pointerId);
      }
      if (!dragState.moved) return;

      event.preventDefault();
      setPanelPosition(
        dragState.startLeft + deltaX,
        dragState.startTop + deltaY,
      );
    }

    function finishDrag(event) {
      if (!dragState || dragState.pointerId !== event.pointerId) return;

      const moved = dragState.moved;
      dragState = null;
      toolbar.classList.remove('dragging');
      if (toolbar.hasPointerCapture(event.pointerId)) {
        toolbar.releasePointerCapture(event.pointerId);
      }

      if (moved) {
        suppressCollapseClick = true;
        savePosition();
        setTimeout(() => {
          suppressCollapseClick = false;
        }, 0);
      }
    }

    window.addEventListener('pointermove', moveDrag, { passive: false });
    window.addEventListener('pointerup', finishDrag);
    window.addEventListener('pointercancel', finishDrag);

    clearButton.addEventListener('click', () => {
      if (!confirm('确定清空所有用户的席位阈值历史吗？')) return;
      records = {};
      localStorage.removeItem(STORAGE_KEY);
      renderPanel();
    });

    renderPanel = () => {
      spaceId.textContent = currentAccountId
        ? `空间：${currentAccountId}`
        : '空间：未识别';
      spaceId.title = currentAccountId || '';

      const entries = Object.values(records)
        .filter((entry) => (
          entry.accountId === currentAccountId &&
          Array.isArray(entry.history) &&
          entry.history.length > 0
        ))
        .sort((a, b) => {
          const aTime = a.history.at(-1)?.capturedAt || '';
          const bTime = b.history.at(-1)?.capturedAt || '';
          return bTime.localeCompare(aTime);
        });

      count.textContent = String(entries.length);
      content.replaceChildren();

      if (entries.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = currentAccountId
          ? '当前空间暂无席位阈值历史'
          : '尚未识别当前空间';
        content.append(empty);
        return;
      }

      for (const entry of entries) {
        const section = document.createElement('section');
        section.className = 'user';

        const head = document.createElement('div');
        head.className = 'user-head';

        const email = findMemberEmail(entry.accountId, entry.userId);
        const emailNode = document.createElement('span');
        emailNode.className = 'email';
        emailNode.title = email ? `${email}\n${entry.userId}` : entry.userId;
        emailNode.textContent = email || '邮箱待匹配';

        const latest = compareVacancy(entry.history.at(-1));
        const latestWrap = document.createElement('div');
        latestWrap.className = 'latest-compare';
        const chip = document.createElement('span');
        chip.className = `status-chip ${latest.free === true ? 'is-free' : latest.free === false ? 'is-locked' : 'is-unknown'}`;
        chip.textContent = latest.label;
        const latestText = document.createElement('span');
        latestText.className = 'datum-label';
        latestText.textContent = `${latest.predicate} = ${latest.result} · ${latest.text}`;
        latestWrap.append(chip, latestText);
        head.append(emailNode, latestWrap);

        const historyList = document.createElement('div');
        historyList.className = 'history-list';

        const createDatum = (label, text) => {
          const datum = document.createElement('div');
          datum.className = 'datum';
          const labelNode = document.createElement('span');
          labelNode.className = 'datum-label';
          labelNode.textContent = label;
          const valueNode = document.createElement('span');
          valueNode.className = 'datum-value';
          valueNode.textContent = text;
          datum.append(labelNode, valueNode);
          return datum;
        };

        for (const item of [...entry.history].reverse()) {
          const row = document.createElement('div');
          row.className = 'history-row';
          const compared = compareVacancy(item);

          const capturedTime = document.createElement('div');
          capturedTime.className = 'captured-time';
          capturedTime.textContent = `记录时间：${formatTime(item.capturedAt)}`;

          const metrics = document.createElement('div');
          metrics.className = 'metrics';
          metrics.append(
            createDatum('vacancy_ordinal', valueText(item.vacancyOrdinal)),
            createDatum(
              'free_vacancy_threshold',
              valueText(item.freeVacancyThreshold),
            ),
          );

          const compare = document.createElement('div');
          compare.className = `compare ${compared.free === true ? 'is-free' : compared.free === false ? 'is-locked' : 'is-unknown'}`;
          compare.append(
            createDatum(compared.predicate, compared.result),
            createDatum('比较结果', `${compared.text} · ${compared.label}`),
          );

          const period = document.createElement('div');
          period.className = 'period';
          period.append(
            createDatum('开始时间', formatTime(item.billingStartsAt)),
            createDatum('结束时间', formatTime(item.expiresAt)),
          );

          row.append(capturedTime, metrics, compare, period);
          historyList.append(row);
        }

        section.append(head, historyList);
        content.append(section);
      }
    };

    document.body.append(host);
    restorePosition();
    window.addEventListener('resize', () => keepPanelInViewport(true));
    renderPanel();
  }

  function mountWhenReady() {
    if (document.body) {
      mountPanel();
      return;
    }

    const observer = new MutationObserver(() => {
      if (!document.body) return;
      observer.disconnect();
      mountPanel();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  window.addEventListener('storage', (event) => {
    if (event.key === STORAGE_KEY) records = loadRecords();
    else if (event.key === MEMBERS_KEY) memberAliases = loadMemberAliases();
    else return;
    renderPanel();
  });

  installFetchMonitor();
  installXhrMonitor();
  mountWhenReady();
  window.setInterval(refreshCurrentAccount, 1000);
})();
