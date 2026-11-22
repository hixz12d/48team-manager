// Batch incognito queue worker: drives one clean incognito window per email,
// closes and verifies all incognito windows are gone between runs, pauses for
// human intervention when required, and skips single-account failures automatically.
import {
  ACTIVE,
  BATCH_ACTIVE,
  BATCH_COOLDOWN_MIN,
  BATCH_COOLDOWN_MAX,
  autoRetryAllowed,
  isBatchHumanPause,
  parseBatchEmails,
  pauseReason,
  publicBatch,
} from './shared.mjs';

export const BATCH_ALARM = 'batch-watchdog';
export const BATCH_COOLDOWN_ALARM = 'batch-cooldown';

const KEY = 'batch';
// Let the popup receive batch-start and open popup.html?tab=1 before the new incognito window takes focus.
const START_OPEN_DELAY = 250;
const CALLBACK_RETRY_LIMIT = 2;
const CALLBACK_RETRY_DELAY = 5000;
const CLOSE_POLL_LIMIT = 3000;
const CLOSE_SETTLE_DELAY = 600;
const ACTIVE_ITEM_STATUSES = new Set(['running', 'waiting_human', 'paused']);

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const between = (min, max) => min + Math.floor(Math.random() * (max - min + 1));

function parseHandoffOption(handoff, hasTeam48) {
  if (handoff?.rs === true) {
    if (!hasTeam48) throw new Error('插件未配置 Team48 地址和令牌，无法开启自动接入 rs。');
    return {handoffMode: 'rs', workspaceId: null, workspaceName: ''};
  }
  if (handoff?.auto === true) {
    if (!hasTeam48) throw new Error('插件未配置 Team48 地址和令牌，无法开启自动接入。');
    return {handoffMode: 'auto', workspaceId: null, workspaceName: ''};
  }
  const workspaceId = Number(handoff?.workspaceId);
  if (Number.isInteger(workspaceId) && workspaceId > 0) {
    if (!hasTeam48) throw new Error('插件未配置 Team48 地址和令牌，无法开启指定团队接入。');
    return {handoffMode: 'workspace', workspaceId, workspaceName: String(handoff.workspaceName || '').slice(0, 120)};
  }
  return {handoffMode: 'none', workspaceId: null, workspaceName: ''};
}

function handoffSpecForBatch(batch) {
  if (batch.handoffMode === 'rs') return {rs: true};
  if (batch.handoffMode === 'auto') return {auto: true};
  if (batch.handoffMode === 'workspace' && Number.isInteger(batch.workspaceId) && batch.workspaceId > 0) {
    return {workspaceId: batch.workspaceId, workspaceName: batch.workspaceName || ''};
  }
  return null;
}

function itemStage(job) {
  return job.phase === 'oauth' ? `oauth:${job.handoff?.status || job.stage || ''}` : String(job.stage || '');
}

function syncItemFromJob(item, job) {
  if (!item || !job) return;
  if (job.password) item.password = String(job.password);
  if (job.passwordSet) item.passwordSet = true;
  item.stage = itemStage(job);
  const workspaceName = job.handoff?.workspaceName || job.autoHandoff?.workspaceName || '';
  if (workspaceName) item.workspaceName = String(workspaceName).slice(0, 120);
  const text = job.handoff?.message || job.message || '';
  if (text) item.message = String(text).slice(0, 240);
}

function buildSuccessMessage(job, handoffMode) {
  if (handoffMode === 'none') return String(job.message || '注册完成').slice(0, 240);
  const parts = [String(job.handoff?.message || '已完成授权并提交 Team48')];
  const followups = job.handoff?.followups || {};
  if (followups.sub2api && followups.sub2api.ok === false) {
    parts.push(`Sub2API 失败：${followups.sub2api.message || '请到控制台查看'}`);
  }
  if (followups.codex_rs && followups.codex_rs.ok === false) {
    parts.push(`codex-rs 失败：${followups.codex_rs.message || '请到控制台查看'}`);
  }
  return parts.join('；').slice(0, 240);
}

function buildFinishedMessage(batch) {
  let succeeded = 0, failed = 0, skipped = 0;
  for (const item of batch.items || []) {
    if (item.status === 'succeeded') succeeded++;
    else if (item.status === 'failed') failed++;
    else if (item.status === 'skipped') skipped++;
  }
  return `批量已完成：成功 ${succeeded}，失败 ${failed}，跳过 ${skipped}。`;
}

function findNextPendingIndex(batch) {
  return (batch?.items || []).findIndex(item => item.status === 'pending');
}

function findActiveItemIndex(batch) {
  if (!batch || !Number.isInteger(batch.currentIndex)) return -1;
  const item = batch.items?.[batch.currentIndex];
  return item && ACTIVE_ITEM_STATUSES.has(item.status) ? batch.currentIndex : -1;
}

function prepareClose(batch, job) {
  batch.closing = true;
  batch.closingWindowId = Number.isInteger(batch.windowId) ? batch.windowId : Number.isInteger(job?.windowId) ? job.windowId : null;
  batch.closingTabId = Number.isInteger(job?.tabId) ? job.tabId : null;
  batch.windowId = null;
  batch.currentJobId = null;
  batch.cooldownUntil = null;
  batch.attentionReason = null;
  if (!batch.userPaused) batch.status = 'running';
  batch.message = '正在关闭无痕窗口并清空会话…';
}

function markBatchFinished(batch) {
  batch.status = 'finished';
  batch.finishedAt = Date.now();
  batch.cooldownUntil = null;
  batch.windowId = null;
  batch.currentJobId = null;
  batch.attentionReason = null;
  batch.userPaused = false;
  batch.closing = false;
  delete batch.closingWindowId;
  delete batch.closingTabId;
  delete batch.startAfter;
  batch.message = buildFinishedMessage(batch);
}

/**
 * host: {
 *   isRunner, hasTeam48, serial, loadJob, saveJob, removeJob, expireJob,
 *   startJob, pauseJob, resumeJob, applyStop, failHandoff, ensureHandoff,
 *   releaseDebugger, isHandoffActive
 * }
 */
export function createBatchController(host) {
  const loadBatch = async () => (await chrome.storage.session.get(KEY))[KEY] || null;
  const saveBatch = async batch => {
    await chrome.storage.session.set({[KEY]: batch});
    return batch;
  };
  const removeBatch = async () => {
    await chrome.storage.session.remove(KEY);
  };

  let pumpTask = null;
  let pumpDirty = false;
  let delayTimer = null;

  function clearDelayTimer() {
    if (delayTimer !== null) {
      clearTimeout(delayTimer);
      delayTimer = null;
    }
  }

  function scheduleWake(ms) {
    clearDelayTimer();
    const wait = Math.max(100, Math.min(60000, Math.round(ms)));
    delayTimer = setTimeout(() => {
      delayTimer = null;
      kick();
    }, wait);
  }

  async function ensureWatchdog() {
    if (!(await chrome.alarms.get(BATCH_ALARM))) {
      await chrome.alarms.create(BATCH_ALARM, {periodInMinutes: 0.5});
    }
  }

  async function clearBatchAlarms() {
    clearDelayTimer();
    await Promise.all([
      chrome.alarms.clear(BATCH_ALARM).catch(() => {}),
      chrome.alarms.clear(BATCH_COOLDOWN_ALARM).catch(() => {}),
    ]);
  }

  async function hasAnyIncognitoWindow() {
    const windows = await chrome.windows.getAll().catch(() => []);
    return windows.some(w => w.incognito);
  }

  // Closes the batch incognito window (and any leftover incognito window) and verifies
  // that Chrome has completely torn down the incognito profile session pool.
  async function closeBatchIncognito(preferredWindowId, tabId) {
    if (Number.isInteger(tabId)) {
      await host.releaseDebugger(tabId).catch(() => {});
    }
    const targets = new Set();
    if (Number.isInteger(preferredWindowId)) targets.add(preferredWindowId);
    const initial = await chrome.windows.getAll().catch(() => []);
    for (const win of initial) {
      if (win.incognito) targets.add(win.id);
    }
    for (const id of targets) {
      await chrome.windows.remove(id).catch(() => {});
    }
    const deadline = Date.now() + CLOSE_POLL_LIMIT;
    let cleared = false;
    while (Date.now() < deadline) {
      const remaining = (await chrome.windows.getAll().catch(() => [])).filter(w => w.incognito);
      if (!remaining.length) {
        cleared = true;
        break;
      }
      for (const win of remaining) {
        await chrome.windows.remove(win.id).catch(() => {});
      }
      await sleep(200);
    }
    if (!cleared) {
      cleared = !(await hasAnyIncognitoWindow());
    }
    await sleep(CLOSE_SETTLE_DELAY);
    return cleared;
  }

  async function finishClosingPhase(batchId, winId, tabId) {
    await closeBatchIncognito(winId, tabId);
    let cooldownAlarmAt = null;
    let wakeDelay = null;
    let finished = false;
    await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch || batch.id !== batchId || !batch.closing) return;
      batch.closing = false;
      delete batch.closingWindowId;
      delete batch.closingTabId;
      if (!BATCH_ACTIVE.has(batch.status)) {
        await saveBatch(batch);
        finished = true;
        return;
      }
      const nextIndex = findNextPendingIndex(batch);
      if (nextIndex < 0) {
        markBatchFinished(batch);
        await saveBatch(batch);
        finished = true;
        return;
      }
      if (batch.userPaused === true) {
        batch.status = 'paused';
        batch.cooldownUntil = null;
        batch.attentionReason = null;
        batch.message = '已暂停，点继续后开启下一个无痕窗口。';
        await saveBatch(batch);
        return;
      }
      const delay = between(BATCH_COOLDOWN_MIN, BATCH_COOLDOWN_MAX);
      batch.status = 'cooldown';
      batch.cooldownUntil = Date.now() + delay;
      batch.attentionReason = null;
      batch.message = `已清空无痕窗口，冷却 ${Math.ceil(delay / 1000)} 秒后开启第 ${nextIndex + 1}/${batch.items.length} 个…`;
      await saveBatch(batch);
      cooldownAlarmAt = batch.cooldownUntil;
      wakeDelay = delay + 50;
    });
    if (finished) {
      await clearBatchAlarms();
      return 'idle';
    }
    if (cooldownAlarmAt) {
      await chrome.alarms.create(BATCH_COOLDOWN_ALARM, {when: cooldownAlarmAt});
      scheduleWake(wakeDelay);
    }
    return 'idle';
  }

  async function openNextBatchItem(batchId) {
    await chrome.alarms.clear(BATCH_COOLDOWN_ALARM).catch(() => {});
    // Verify incognito permission is still granted before opening a fresh window.
    if (!(await chrome.extension.isAllowedIncognitoAccess())) {
      await host.serial(async () => {
        const batch = await loadBatch();
        if (!batch || batch.id !== batchId || !BATCH_ACTIVE.has(batch.status)) return;
        const entering = batch.status !== 'waiting_human';
        batch.status = 'waiting_human';
        batch.cooldownUntil = null;
        batch.attentionReason = 'other';
        if (entering) batch.attentionAt = Date.now();
        batch.message = '无痕运行权限已关闭，请到 chrome://extensions 重新开启后再点继续。';
        await saveBatch(batch);
      });
      return 'idle';
    }
    // Verify no leftover incognito window exists before creating a fresh one.
    if (await hasAnyIncognitoWindow()) {
      await closeBatchIncognito(null, null);
      if (await hasAnyIncognitoWindow()) {
        await host.serial(async () => {
          const batch = await loadBatch();
          if (!batch || batch.id !== batchId || !BATCH_ACTIVE.has(batch.status)) return;
          const entering = batch.status !== 'waiting_human';
          batch.status = 'waiting_human';
          batch.cooldownUntil = null;
          batch.attentionReason = 'other';
          if (entering) batch.attentionAt = Date.now();
          batch.message = '检测到仍有未关闭的无痕窗口，无法保证 Cookie 隔离，请手动关闭全部无痕窗口后再点继续。';
          await saveBatch(batch);
        });
        return 'idle';
      }
    }

    // Reserve the next pending index before creating the incognito window.
    const plan = await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch || batch.id !== batchId || !BATCH_ACTIVE.has(batch.status) || batch.userPaused || batch.closing) {
        return {abort: true};
      }
      if (findActiveItemIndex(batch) >= 0) return {abort: true};
      const nextIndex = findNextPendingIndex(batch);
      if (nextIndex < 0) {
        markBatchFinished(batch);
        await saveBatch(batch);
        return {finished: true};
      }
      return {
        nextIndex,
        email: batch.items[nextIndex].email,
        hidePanel: batch.hidePanel === true,
        handoff: handoffSpecForBatch(batch),
        total: batch.items.length,
      };
    });
    if (plan.finished) {
      await clearBatchAlarms();
      return 'idle';
    }
    if (plan.abort) return 'idle';

    // Create a brand new incognito window outside serial.
    let win = null;
    let tab = null;
    let createError = null;
    try {
      win = await chrome.windows.create({url: 'about:blank', incognito: true, focused: true});
      if (!win || !Number.isInteger(win.id) || win.incognito !== true) {
        throw new Error('无法创建无痕窗口，请确认已开启「允许在无痕模式下运行」。');
      }
      tab = win.tabs?.[0] || null;
      if (!tab) {
        const tabs = await chrome.tabs.query({windowId: win.id});
        tab = tabs[0] || await chrome.tabs.create({windowId: win.id, url: 'about:blank', active: true});
      }
      if (!tab || !Number.isInteger(tab.id)) {
        throw new Error('新建无痕窗口缺少可用标签页。');
      }
    } catch (error) {
      createError = error;
    }

    if (createError) {
      if (Number.isInteger(win?.id)) await chrome.windows.remove(win.id).catch(() => {});
      await host.serial(async () => {
        const batch = await loadBatch();
        if (!batch || batch.id !== batchId || !BATCH_ACTIVE.has(batch.status)) return;
        const item = batch.items[plan.nextIndex];
        if (item && item.status === 'pending') {
          batch.currentIndex = plan.nextIndex;
          item.status = 'failed';
          item.startedAt = Date.now();
          item.finishedAt = Date.now();
          item.errorCode = 'window_create_failed';
          item.message = String(createError?.message || '创建无痕窗口失败。').slice(0, 240);
        }
        prepareClose(batch, null);
        await saveBatch(batch);
      });
      return 'again';
    }

    // Start the single-account job inside the new incognito tab.
    let shouldDiscardWindow = false;
    await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch || batch.id !== batchId || !BATCH_ACTIVE.has(batch.status) || batch.userPaused || batch.closing) {
        shouldDiscardWindow = true;
        return;
      }
      const item = batch.items[plan.nextIndex];
      if (!item || item.status !== 'pending') {
        shouldDiscardWindow = true;
        return;
      }
      delete batch.startAfter;
      batch.status = 'running';
      batch.cooldownUntil = null;
      batch.attentionReason = null;
      batch.currentIndex = plan.nextIndex;
      batch.windowId = win.id;
      batch.currentJobId = null;
      item.status = 'running';
      item.startedAt = Date.now();
      item.finishedAt = null;
      item.errorCode = null;
      item.stage = 'signup';
      item.message = '正在初始化邮箱并打开注册页…';
      batch.message = `正在处理第 ${plan.nextIndex + 1}/${plan.total} 个：${item.email}`;
      await saveBatch(batch);

      try {
        const job = await host.startJob({
          email: item.email,
          mode: 'auto',
          profile: null,
          hidePanel: plan.hidePanel,
          handoff: plan.handoff,
          tabId: tab.id,
          windowId: win.id,
          batchId: batch.id,
        });
        // Re-read batch in case a user command modified it while startMailbox awaited.
        const latest = await loadBatch();
        if (!latest || latest.id !== batchId || !BATCH_ACTIVE.has(latest.status)) {
          if (job && ACTIVE.has(job.status)) {
            host.applyStop(job);
            await host.saveJob(job);
          }
          shouldDiscardWindow = true;
          return;
        }
        const latestItem = latest.items[plan.nextIndex];
        latest.currentJobId = job.id;
        latest.windowId = Number.isInteger(job.windowId) ? job.windowId : win.id;
        if (latestItem && ACTIVE_ITEM_STATUSES.has(latestItem.status)) {
          syncItemFromJob(latestItem, job);
        }
        await saveBatch(latest);
      } catch (error) {
        const latest = await loadBatch();
        if (!latest || latest.id !== batchId || !BATCH_ACTIVE.has(latest.status)) {
          shouldDiscardWindow = true;
          return;
        }
        const latestItem = latest.items[plan.nextIndex];
        if (latestItem && ACTIVE_ITEM_STATUSES.has(latestItem.status)) {
          latestItem.status = 'failed';
          latestItem.finishedAt = Date.now();
          latestItem.errorCode = 'start_failed';
          latestItem.message = String(error?.message || '启动注册失败。').slice(0, 240);
        }
        prepareClose(latest, {tabId: tab.id, windowId: win.id});
        await saveBatch(latest);
      }
    });

    if (shouldDiscardWindow) {
      await closeBatchIncognito(win.id, tab.id);
      return 'idle';
    }
    return 'again';
  }

  async function pumpStep() {
    if (host.isRunner()) return 'idle';
    const snapshot = await loadBatch();
    if (!snapshot) {
      await clearBatchAlarms();
      return 'idle';
    }
    if (snapshot.closing) {
      return finishClosingPhase(snapshot.id, snapshot.closingWindowId, snapshot.closingTabId);
    }
    if (!BATCH_ACTIVE.has(snapshot.status)) {
      await clearBatchAlarms();
      return 'idle';
    }
    await ensureWatchdog();

    // Cooldown phase: wait until cooldownUntil, then open the next incognito window.
    if (snapshot.status === 'cooldown') {
      const remain = (snapshot.cooldownUntil || 0) - Date.now();
      if (remain > 0) {
        if (!(await chrome.alarms.get(BATCH_COOLDOWN_ALARM))) {
          await chrome.alarms.create(BATCH_COOLDOWN_ALARM, {when: snapshot.cooldownUntil});
        }
        scheduleWake(remain + 50);
        return 'idle';
      }
      return openNextBatchItem(snapshot.id);
    }

    // Initial launch grace period so the action popup can open popup.html?tab=1 before losing focus.
    if (snapshot.status === 'running' && snapshot.currentIndex < 0 && !snapshot.currentJobId) {
      if (snapshot.startAfter && Date.now() < snapshot.startAfter) {
        scheduleWake(snapshot.startAfter - Date.now() + 20);
        return 'idle';
      }
      return openNextBatchItem(snapshot.id);
    }

    // Check window existence outside serial before syncing current item.
    const windowExists = Number.isInteger(snapshot.windowId)
      ? !!(await chrome.windows.get(snapshot.windowId).catch(() => null))
      : false;

    let needHandoff = false;
    let nextAction = 'idle';
    let waitMs = null;

    await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch || batch.id !== snapshot.id || !BATCH_ACTIVE.has(batch.status) || batch.closing) {
        nextAction = batch?.closing ? 'again' : 'idle';
        return;
      }
      if (batch.status === 'cooldown') {
        nextAction = 'again';
        return;
      }

      const activeIdx = findActiveItemIndex(batch);
      if (activeIdx < 0) {
        // No active item: either paused between items, waiting to open next, or all done.
        if (batch.status === 'paused' || batch.status === 'waiting_human') {
          if (findNextPendingIndex(batch) < 0) {
            markBatchFinished(batch);
            await saveBatch(batch);
            nextAction = 'finished';
          }
          return;
        }
        if (Number.isInteger(batch.windowId)) {
          prepareClose(batch, null);
          await saveBatch(batch);
          nextAction = 'again';
          return;
        }
        if (findNextPendingIndex(batch) >= 0) {
          nextAction = 'open_next';
        } else {
          markBatchFinished(batch);
          await saveBatch(batch);
          nextAction = 'finished';
        }
        return;
      }

      const item = batch.items[activeIdx];
      const total = batch.items.length;
      const pos = `${activeIdx + 1}/${total}`;
      const job = await host.expireJob(await host.loadJob());
      const jobMatches = !!job && job.batchId === batch.id && (!batch.currentJobId || job.id === batch.currentJobId);

      if (!jobMatches) {
        item.status = 'failed';
        item.finishedAt = Date.now();
        item.errorCode = 'job_lost';
        item.message = '当前注册任务状态已丢失，自动跳过。';
        prepareClose(batch, null);
        await saveBatch(batch);
        nextAction = 'again';
        return;
      }

      syncItemFromJob(item, job);

      // 1. Success check
      const succeeded = batch.handoffMode === 'none'
        ? job.status === 'done'
        : job.handoff?.status === 'done';
      if (succeeded) {
        item.status = 'succeeded';
        item.finishedAt = Date.now();
        item.errorCode = null;
        item.message = buildSuccessMessage(job, batch.handoffMode);
        prepareClose(batch, job);
        await saveBatch(batch);
        nextAction = 'again';
        return;
      }

      // 2. Callback retry: up to 2 automatic retries with 5s spacing before failing handoff.
      if (job.handoff?.status === 'callback_retry') {
        if (batch.userPaused) {
          await saveBatch(batch);
          return;
        }
        const retries = Number(job.batchCallbackRetries) || 0;
        if (retries < CALLBACK_RETRY_LIMIT) {
          if (!job.batchCallbackRetryAt) {
            job.batchCallbackRetryAt = Date.now() + CALLBACK_RETRY_DELAY;
            const retryMsg = `提交回调网络波动，5 秒后自动重试（第 ${retries + 1}/${CALLBACK_RETRY_LIMIT} 次）…`;
            item.message = retryMsg;
            batch.message = `第 ${pos} 个：${retryMsg}`;
            await host.saveJob(job);
            await saveBatch(batch);
            waitMs = CALLBACK_RETRY_DELAY + 50;
            return;
          }
          const remain = job.batchCallbackRetryAt - Date.now();
          if (remain > 0) {
            await saveBatch(batch);
            waitMs = remain + 50;
            return;
          }
          job.batchCallbackRetries = retries + 1;
          delete job.batchCallbackRetryAt;
          job.handoff.status = 'completing';
          job.handoff.message = `正在自动重新提交回调（第 ${job.batchCallbackRetries}/${CALLBACK_RETRY_LIMIT} 次）…`;
          item.status = 'running';
          item.message = job.handoff.message;
          if (!batch.userPaused) {
            batch.status = 'running';
            batch.attentionReason = null;
          }
          batch.message = `第 ${pos} 个：${item.message}`;
          await host.saveJob(job);
          await saveBatch(batch);
          needHandoff = true;
          return;
        }
        host.failHandoff(job, '授权回调自动重试 2 次仍失败，已跳过此号。', 'callback_retry_exhausted');
        await host.saveJob(job);
        // Fall through directly into handoff.status === 'failed' below.
      }

      // 3. Handoff failed -> mark failed, stop job, close window, move on.
      if (job.handoff?.status === 'failed') {
        const errCode = job.handoff.errorCode || 'handoff_failed';
        const errMsg = String(job.handoff.message || '团队接入授权失败。').slice(0, 240);
        if (job.status !== 'stopped') {
          host.applyStop(job);
          await host.saveJob(job);
        }
        item.status = 'failed';
        item.finishedAt = Date.now();
        item.errorCode = errCode;
        item.message = errMsg;
        prepareClose(batch, job);
        await saveBatch(batch);
        nextAction = 'again';
        return;
      }

      // 4. Signup done while waiting for Team48 handoff (syncing / resolving / waiting_join / opening / completing).
      if (job.status === 'done' && batch.handoffMode !== 'none') {
        if (!job.handoff) {
          item.status = 'failed';
          item.finishedAt = Date.now();
          item.errorCode = 'handoff_missing';
          item.message = '注册已完成但未触发团队接入，已自动跳过。';
          prepareClose(batch, job);
          await saveBatch(batch);
          nextAction = 'again';
          return;
        }
        const needsWindow = ['resolving', 'syncing', 'waiting_join', 'opening'].includes(job.handoff.status);
        if (needsWindow && Number.isInteger(batch.windowId) && !windowExists) {
          host.failHandoff(job, '当前无痕窗口已关闭，无法继续同页授权。', 'window_closed');
          host.applyStop(job);
          await host.saveJob(job);
          item.status = 'failed';
          item.finishedAt = Date.now();
          item.errorCode = 'window_closed';
          item.message = '当前无痕窗口已关闭，无法继续同页授权。';
          prepareClose(batch, job);
          await saveBatch(batch);
          nextAction = 'again';
          return;
        }
        if (batch.status !== 'running' && !batch.userPaused) {
          batch.status = 'running';
          batch.attentionReason = null;
        }
        item.status = 'running';
        batch.message = `第 ${pos} 个：${job.handoff.message || '正在接入 Team48…'}`;
        if (host.isHandoffActive(job.handoff.status)) needHandoff = true;
        await saveBatch(batch);
        return;
      }

      // 5. Incognito window disappeared while job is still active -> stop job so step 7 handles failure cleanly.
      if (ACTIVE.has(job.status) && Number.isInteger(batch.windowId) && !windowExists) {
        host.applyStop(job);
        job.message = job.phase === 'oauth' ? '当前授权无痕窗口已关闭。' : '当前注册无痕窗口已关闭。';
        await host.saveJob(job);
      }

      // 6. Paused: human pause keeps window open; non-human pause waits for autoRetry or fails & skips.
      if (job.status === 'paused') {
        if (isBatchHumanPause(job)) {
          if (batch.userPaused === true) {
            batch.status = 'paused';
            item.status = 'paused';
            batch.attentionReason = null;
            batch.message = `批量已暂停（第 ${pos} 个：${item.email}），点继续即可恢复。`;
          } else {
            const entering = batch.status !== 'waiting_human';
            batch.status = 'waiting_human';
            item.status = 'waiting_human';
            batch.attentionReason = pauseReason(job.pauseReason);
            if (entering) batch.attentionAt = Date.now();
            batch.message = `需要人工处理（第 ${pos} 个）：${job.message || '请切换到无痕窗口处理后继续。'}`;
          }
          await saveBatch(batch);
          return;
        }
        if (autoRetryAllowed(job)) {
          if (batch.status === 'waiting_human') {
            batch.status = 'running';
            batch.attentionReason = null;
          }
          item.status = 'running';
          batch.message = `第 ${pos} 个：${job.message || '等待页面自动重试…'}`;
          await saveBatch(batch);
          return;
        }
        const errCode = job.pauseReason || 'paused';
        const errMsg = String(job.message || '单号遇到异常暂停，已自动跳过。').slice(0, 240);
        host.applyStop(job);
        await host.saveJob(job);
        item.status = 'failed';
        item.finishedAt = Date.now();
        item.errorCode = errCode;
        item.message = errMsg;
        prepareClose(batch, job);
        await saveBatch(batch);
        nextAction = 'again';
        return;
      }

      // 7. Stopped (ambiguous team conflict, 10m expiry, or closed incognito window).
      if (job.status === 'stopped') {
        const errCode = job.autoHandoff?.resolve?.status === 'ambiguous' ? 'workspace_ambiguous' : 'stopped';
        const errMsg = String(job.message || '当前无痕窗口已关闭或任务超时。').slice(0, 240);
        item.status = 'failed';
        item.finishedAt = Date.now();
        item.errorCode = errCode;
        item.message = errMsg;
        prepareClose(batch, job);
        await saveBatch(batch);
        nextAction = 'again';
        return;
      }

      // 8. Running normally (also restores batch if user clicked Continue inside the page card).
      if (job.status === 'running') {
        if (batch.status === 'waiting_human' || batch.status === 'paused') {
          batch.status = 'running';
          batch.userPaused = false;
          batch.attentionReason = null;
        }
        item.status = 'running';
        batch.message = `正在处理第 ${pos} 个：${item.email}`;
        if (job.handoff && host.isHandoffActive(job.handoff.status)) needHandoff = true;
        await saveBatch(batch);
      }
    });

    if (needHandoff) host.ensureHandoff();
    if (nextAction === 'finished') {
      await clearBatchAlarms();
      return 'idle';
    }
    if (nextAction === 'open_next') {
      return openNextBatchItem(snapshot.id);
    }
    if (waitMs !== null) {
      scheduleWake(waitMs);
      return 'idle';
    }
    return nextAction;
  }

  async function pumpLoop() {
    if (host.ready) await host.ready;
    for (let guard = 0; guard < 200; guard++) {
      pumpDirty = false;
      const result = await pumpStep();
      if (result === 'again' || pumpDirty) continue;
      return;
    }
  }

  function kick() {
    if (host.isRunner()) return;
    pumpDirty = true;
    if (!pumpTask) {
      pumpTask = pumpLoop().catch(() => {}).finally(() => {
        pumpTask = null;
        if (pumpDirty) kick();
      });
    }
  }

  async function waitPumpIdle() {
    clearDelayTimer();
    const running = pumpTask;
    if (running) await running.catch(() => {});
  }

  // ---- Popup batch-* commands ----

  async function startBatch({emailsText, hidePanel, handoff} = {}) {
    if (host.isRunner()) throw new Error('服务器运行器模式下不支持批量注册。');
    if (!(await chrome.extension.isAllowedIncognitoAccess())) {
      throw new Error('请先在 chrome://extensions 打开本插件详情，开启「允许在无痕模式下运行」。');
    }
    const parsed = parseBatchEmails(emailsText);
    if (!parsed.ok) throw new Error(parsed.message);
    const {handoffMode, workspaceId, workspaceName} = parseHandoffOption(handoff, host.hasTeam48());

    const created = await host.serial(async () => {
      const existingBatch = await loadBatch();
      if (BATCH_ACTIVE.has(existingBatch?.status)) {
        throw new Error('已有进行中的批量注册任务，请先等待完成或停止当前批次。');
      }
      const job = await host.expireJob(await host.loadJob());
      if (job && ACTIVE.has(job.status)) throw new Error('已有单次注册任务进行中，请先停止当前注册。');
      if (host.isHandoffActive(job?.handoff?.status)) throw new Error('上一个账号仍在接入 Team48，请等待完成。');
      if (job?.handoff?.status === 'callback_retry') throw new Error('上一个账号的授权回调尚未提交，请先重新提交或清除。');
      if (await hasAnyIncognitoWindow()) {
        throw new Error('检测到已有无痕窗口打开。为保证每个账号 Cookie 完全隔离且不误关你的窗口，请先关闭全部无痕窗口后再开始批量注册。');
      }
      const now = Date.now();
      const batch = {
        id: crypto.randomUUID(),
        status: 'running',
        message: `批量队列已创建（共 ${parsed.emails.length} 个），正在打开第 1 个无痕窗口…`,
        startedAt: now,
        finishedAt: null,
        cooldownUntil: null,
        attentionAt: null,
        attentionReason: null,
        currentIndex: -1,
        currentJobId: null,
        windowId: null,
        hidePanel: hidePanel === true,
        handoffMode,
        workspaceId,
        workspaceName,
        userPaused: false,
        closing: false,
        startAfter: now + START_OPEN_DELAY,
        items: parsed.emails.map((email, index) => ({
          index,
          email,
          password: '',
          passwordSet: false,
          status: 'pending',
          stage: '',
          workspaceName: handoffMode === 'workspace' ? workspaceName : '',
          message: '',
          errorCode: null,
          startedAt: null,
          finishedAt: null,
        })),
      };
      await saveBatch(batch);
      return batch;
    });

    await ensureWatchdog();
    scheduleWake(START_OPEN_DELAY);
    return {batch: publicBatch(created)};
  }

  async function pauseBatch() {
    clearDelayTimer();
    await chrome.alarms.clear(BATCH_COOLDOWN_ALARM).catch(() => {});
    const updated = await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch || !BATCH_ACTIVE.has(batch.status)) throw new Error('当前没有进行中的批量任务。');
      if (batch.status === 'paused' && batch.userPaused) return batch;
      const wasCooldown = batch.status === 'cooldown';
      batch.userPaused = true;
      batch.status = 'paused';
      batch.cooldownUntil = null;
      batch.attentionReason = null;
      if (batch.closing) {
        batch.message = '正在关闭当前无痕窗口，关闭后将保持暂停。';
        await saveBatch(batch);
        return batch;
      }
      const activeIdx = findActiveItemIndex(batch);
      if (activeIdx >= 0) {
        const item = batch.items[activeIdx];
        item.status = 'paused';
        item.message = '已暂停';
        batch.message = `批量已暂停（第 ${activeIdx + 1}/${batch.items.length} 个：${item.email}）。`;
        await saveBatch(batch);
        const job = await host.loadJob();
        if (job && job.batchId === batch.id && job.id === batch.currentJobId && job.status === 'running') {
          host.pauseJob(job, '批量队列已暂停，点击继续即可恢复。', 'user');
          await host.saveJob(job);
        }
      } else {
        batch.message = wasCooldown ? '已暂停冷却，点击继续后开启下一个无痕窗口。' : '批量已暂停，点击继续后继续执行。';
        await saveBatch(batch);
      }
      return batch;
    });
    return {batch: publicBatch(updated)};
  }

  async function resumeBatch() {
    const updated = await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch || !['paused', 'waiting_human'].includes(batch.status)) {
        throw new Error('当前批量任务无需继续。');
      }
      batch.userPaused = false;
      batch.attentionReason = null;
      batch.status = 'running';
      if (batch.closing) {
        batch.message = '正在关闭当前无痕窗口，随后继续执行…';
        await saveBatch(batch);
        return batch;
      }
      const activeIdx = findActiveItemIndex(batch);
      if (activeIdx >= 0) {
        const item = batch.items[activeIdx];
        const job = await host.expireJob(await host.loadJob());
        if (job && job.batchId === batch.id && job.id === batch.currentJobId && job.status === 'paused') {
          await host.resumeJob(job);
          syncItemFromJob(item, job);
        }
        if (ACTIVE_ITEM_STATUSES.has(item.status)) item.status = 'running';
        batch.message = `继续处理第 ${activeIdx + 1}/${batch.items.length} 个：${item.email}`;
      } else {
        batch.message = '正在准备开启下一个无痕窗口…';
      }
      await saveBatch(batch);
      return batch;
    });
    await ensureWatchdog();
    kick();
    return {batch: publicBatch(updated)};
  }

  async function skipBatch() {
    clearDelayTimer();
    await chrome.alarms.clear(BATCH_COOLDOWN_ALARM).catch(() => {});
    let finished = false;
    const updated = await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch || !BATCH_ACTIVE.has(batch.status)) throw new Error('当前没有进行中的批量任务。');
      if (batch.closing) return batch;
      const activeIdx = findActiveItemIndex(batch);
      if (activeIdx >= 0) {
        const item = batch.items[activeIdx];
        const job = await host.loadJob();
        if (job && job.batchId === batch.id && job.id === batch.currentJobId) {
          let changed = false;
          if (job.handoff?.status === 'callback_retry') {
            host.failHandoff(job, '已手动跳过此号。', 'skipped');
            changed = true;
          }
          if (job.status !== 'stopped') {
            host.applyStop(job);
            changed = true;
          }
          if (changed) await host.saveJob(job);
        }
        item.status = 'skipped';
        item.errorCode = null;
        item.message = '已手动跳过';
        item.finishedAt = Date.now();
        batch.userPaused = false;
        prepareClose(batch, job);
        await saveBatch(batch);
        return batch;
      }
      // During cooldown or between-item pause, skip the next pending email.
      const nextIndex = findNextPendingIndex(batch);
      if (nextIndex < 0) {
        markBatchFinished(batch);
        await saveBatch(batch);
        finished = true;
        return batch;
      }
      const nextItem = batch.items[nextIndex];
      batch.currentIndex = nextIndex;
      nextItem.status = 'skipped';
      nextItem.errorCode = null;
      nextItem.message = '已手动跳过';
      nextItem.finishedAt = Date.now();
      batch.userPaused = false;
      batch.attentionReason = null;
      if (findNextPendingIndex(batch) < 0) {
        markBatchFinished(batch);
        finished = true;
      } else {
        const delay = between(BATCH_COOLDOWN_MIN, BATCH_COOLDOWN_MAX);
        batch.status = 'cooldown';
        batch.cooldownUntil = Date.now() + delay;
        batch.message = `已跳过 ${nextItem.email}，冷却 ${Math.ceil(delay / 1000)} 秒后开启下一个…`;
      }
      await saveBatch(batch);
      return batch;
    });
    if (finished) {
      await clearBatchAlarms();
    } else {
      await ensureWatchdog();
      kick();
    }
    return {batch: publicBatch(updated)};
  }

  async function stopBatch() {
    clearDelayTimer();
    let wasActive = false;
    let closeWinId = null;
    let closeTabId = null;
    const updated = await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch) throw new Error('当前没有批量任务。');
      if (!BATCH_ACTIVE.has(batch.status) && !batch.closing) return batch;
      wasActive = true;
      const activeIdx = findActiveItemIndex(batch);
      if (activeIdx >= 0) {
        const item = batch.items[activeIdx];
        item.status = 'stopped';
        item.finishedAt = Date.now();
        item.message = '批量已停止';
      }
      const job = await host.loadJob();
      if (job && job.batchId === batch.id) {
        closeTabId = job.tabId;
        let changed = false;
        if (job.handoff?.status === 'callback_retry') {
          host.failHandoff(job, '批量注册已停止。', 'stopped');
          changed = true;
        }
        if (ACTIVE.has(job.status) || host.isHandoffActive(job.handoff?.status) ||
            job.handoff?.status === 'authorizing') {
          host.applyStop(job);
          changed = true;
        }
        if (changed) await host.saveJob(job);
      }
      closeWinId = Number.isInteger(batch.windowId) ? batch.windowId :
        Number.isInteger(batch.closingWindowId) ? batch.closingWindowId : job?.windowId ?? null;
      batch.status = 'stopped';
      batch.finishedAt = Date.now();
      batch.cooldownUntil = null;
      batch.windowId = null;
      batch.currentJobId = null;
      batch.attentionReason = null;
      batch.userPaused = false;
      batch.closing = false;
      delete batch.closingWindowId;
      delete batch.closingTabId;
      delete batch.startAfter;
      batch.message = '批量注册已停止。';
      await saveBatch(batch);
      return batch;
    });

    if (wasActive) {
      await clearBatchAlarms();
      await waitPumpIdle();
      if (Number.isInteger(closeWinId) || Number.isInteger(closeTabId) || await hasAnyIncognitoWindow()) {
        await closeBatchIncognito(closeWinId, closeTabId);
      }
    }
    return {batch: publicBatch(updated)};
  }

  async function clearBatch() {
    await host.serial(async () => {
      const batch = await loadBatch();
      if (!batch) return;
      if (BATCH_ACTIVE.has(batch.status)) throw new Error('批量任务进行中，请先停止后再清空。');
      const job = await host.loadJob();
      if (job && job.batchId === batch.id && !ACTIVE.has(job.status) && !host.isHandoffActive(job.handoff?.status)) {
        await host.removeJob();
      }
      await removeBatch();
    });
    await clearBatchAlarms();
    return {batch: null};
  }

  async function focusBatch() {
    const batch = await loadBatch();
    if (!batch) throw new Error('当前没有批量任务。');
    const job = await host.loadJob();
    const jobOwned = !!job && job.batchId === batch.id && (!batch.currentJobId || job.id === batch.currentJobId);
    const tabId = jobOwned && Number.isInteger(job.tabId) ? job.tabId : null;
    const windowId = Number.isInteger(batch.windowId) ? batch.windowId : (jobOwned && Number.isInteger(job.windowId) ? job.windowId : null);
    if (!Number.isInteger(windowId) && !Number.isInteger(tabId)) {
      throw new Error('当前没有打开的无痕窗口（可能正在冷却等待）。');
    }
    try {
      let targetWin = windowId;
      if (Number.isInteger(tabId)) {
        const tab = await chrome.tabs.update(tabId, {active: true});
        if (Number.isInteger(tab?.windowId)) targetWin = tab.windowId;
      }
      if (Number.isInteger(targetWin)) {
        await chrome.windows.update(targetWin, {focused: true});
      }
    } catch {
      kick();
      throw new Error('当前无痕窗口已关闭，正在同步状态。');
    }
    return {batch: publicBatch(batch)};
  }

  async function handleMessage(message) {
    if (message.type === 'batch-start') return startBatch(message);
    if (message.type === 'batch-pause') return pauseBatch();
    if (message.type === 'batch-resume') return resumeBatch();
    if (message.type === 'batch-skip') return skipBatch();
    if (message.type === 'batch-stop') return stopBatch();
    if (message.type === 'batch-clear') return clearBatch();
    if (message.type === 'batch-focus') return focusBatch();
    throw new Error('未知批量操作。');
  }

  function onAlarm(name) {
    if (name === BATCH_ALARM || name === BATCH_COOLDOWN_ALARM) kick();
  }

  function onWindowRemoved(windowId) {
    if (host.isRunner()) return;
    loadBatch().then(batch => {
      if (batch && BATCH_ACTIVE.has(batch.status) && batch.windowId === windowId) kick();
    }).catch(() => {});
  }

  function boot() {
    if (host.isRunner()) return;
    Promise.resolve(host.ready).then(() => loadBatch()).then(batch => {
      if (batch && (BATCH_ACTIVE.has(batch.status) || batch.closing)) kick();
    }).catch(() => {});
  }

  async function isActive() {
    const batch = await loadBatch();
    return !!(batch && (BATCH_ACTIVE.has(batch.status) || batch.closing));
  }

  function ensureAlive() {
    if (host.isRunner() || pumpTask) return;
    boot();
  }

  return {
    load: loadBatch,
    isActive,
    boot,
    ensureAlive,
    jobSaved: kick,
    onAlarm,
    onWindowRemoved,
    handleMessage,
  };
}
