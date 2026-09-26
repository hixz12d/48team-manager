/* One runtime read per page; all progress surfaces subscribe to this store. */
(() => {
  if (window.Team48Runtime) return;
  const listeners = new Set(), records = new Map(), pending = new Map(), reads = new Map(), waiters = new Map();
  const activeStates = new Set(['pending', 'queued', 'running', 'waiting']);
  let data = null, stale = false, serial = 0, clock = null;
  const active = item => activeStates.has(item?.state);
  const emit = () => { for (const listener of listeners) { try { listener(data, stale); } catch (error) { console.error(error); } } settle(); };
  // Accepted commands resolve once their terminal detail (with result) has been read.
  function settle() {
    for (const [id, waiter] of waiters) {
      const item = records.get(id);
      if (item && active(item)) continue;
      if (item?.hydrated) { waiters.delete(id); waiter.resolve(item); continue; }
      if (Date.now() - waiter.tried < 5000) continue;
      waiter.tried = Date.now(); void hydrate(id).catch(() => {});
    }
  }
  function waitFor(id) {
    id = String(id);
    const existing = waiters.get(id);
    if (existing) return existing.promise;
    let resolve; const promise = new Promise(done => { resolve = done; });
    waiters.set(id, {promise, resolve, tried: 0}); settle(); return promise;
  }
  function normalize(item) {
    return {...item, kind: item.kind || item.operation, stage_code: item.stage_code || item.current_step,
      stage_label: item.stage_label || item.business_step, started_at: item.started_at || item.started,
      finished_at: item.finished_at || item.finished, updated_at: item.updated_at || item.updated};
  }
  function put(item) {
    if (!item?.id) return;
    const previous = records.get(String(item.id));
    const next = normalize(item);
    // A late runtime response must not revive a completed command.
    if (previous?.localFinished && active(next)) return;
    if (Date.parse(next.updated_at) < Date.parse(previous?.updated_at)) return;
    if (previous?.cancel_requested && active(next)) { next.cancel_requested = true; next.can_cancel = false; }
    records.set(String(item.id), {...previous, ...next});
  }
  function all() {
    return [...records.values(), ...pending.values()].sort((a, b) =>
      Number(active(b)) - Number(active(a)) || Date.parse(b.started_at || b.created_at || 0) - Date.parse(a.started_at || a.created_at || 0));
  }
  async function hydrate(id) {
    id = String(id);
    if (reads.has(id)) return reads.get(id);
    const promise = (async () => {
      const response = await fetch(`/api/operations/${encodeURIComponent(id)}`, {headers: {Accept: 'application/json'}, cache: 'no-store'});
      if (!response.ok) throw new Error('暂时无法读取任务详情');
      const item = await response.json(); put({...item, hydrated: !active(normalize(item))}); emit(); return item;
    })().finally(() => reads.delete(id));
    reads.set(id, promise); return promise;
  }
  const poller = window.Team48Polling.createPoller({
    read: async signal => {
      const response = await fetch('/api/runtime/status', {headers: {Accept: 'application/json'}, signal, cache: 'no-store'});
      if (!response.ok) throw new Error('runtime_unavailable');
      return response.json();
    },
    onData: next => {
      data = next; stale = false;
      const current = [...(next.active_operations || []), ...(next.recent_operations || [])];
      const liveIds = new Set((next.active_operations || []).map(item => String(item.id)));
      for (const item of current) {
        const wasActive = active(records.get(String(item.id)));
        put(item);
        if (wasActive && !active(item)) void hydrate(item.id).catch(() => {});
      }
      // A disappeared task is unknown until its detail confirms the outcome.
      for (const item of records.values()) {
        if ((active(item) || item.state === 'manual_required') && !liveIds.has(String(item.id)) && !item.archived && !item.localPending) void hydrate(item.id).catch(() => {});
      }
      for (const [key, item] of [...pending, ...[...records].filter(([, value]) => value.localPending && value.existing)]) {
        const match = current.find(op => String(op.workspace_id) === String(item.workspace_id) &&
          (op.kind === item.kind || (item.kind === 'replenish' && op.kind === 'onboard')) && !item.existing.has(String(op.id)));
        if (match) { item.matchedId = String(match.id); item.hidden = true; if (!pending.has(key)) records.delete(key); }
      }
      // Keep active jobs plus a bounded history; detail screens can hydrate older IDs.
      const finished = all().filter(item => !active(item));
      for (const item of finished.slice(20)) records.delete(String(item.id));
      emit();
    },
    onError: () => { stale = true; emit(); },
    delay: next => pending.size || (next.counts?.running || next.counts?.queued || next.counts?.waiting || next.counts?.waiting_user) ? 2000 : 15000,
  });
  async function track(workspaceId, kind, run) {
    const key = `pending-${++serial}`;
    const item = {id: key, localPending: true, workspace_id: workspaceId, kind, state: 'pending',
      started_at: new Date().toISOString(), stage_label: '请求已发出，等待后台任务', existing: new Set(records.keys())};
    pending.set(key, item); emit(); void poller.refresh();
    try {
      const result = await run();
      // Conflict IDs refer to another operation; never overwrite that operation's state.
      if (result.error_code === 'operation_conflict') return result;
      const id = typeof result.operation_id === 'string' ? result.operation_id : item.matchedId;
      // 202: the server committed the operation and runs it in the background.
      if (id && (result.accepted || activeStates.has(result.status))) {
        put({...item, id, localPending: false, hidden: false, existing: undefined, state: 'running',
          account_id: result.account_id ?? null, stage_label: '后台任务已开始'});
        return result;
      }
      const state = result.status === 'cancelled' ? 'cancelled' : result.partial || result.status === 'partial' ? 'partial'
        : result.status === 'manual_required' || result.needs_confirm ? 'manual_required' : result.success || result.ok ? 'success' : 'failed';
      const finished = {...item, id: id || key, localPending: !id, hidden: false, existing: undefined,
        state, result, localFinished: true, finished_at: new Date().toISOString(), stage_label: ''};
      if (id) { put(finished); void hydrate(id).catch(() => {}); }
      else records.set(key, finished);
      return result;
    } catch (error) {
      // Network failure is not evidence that an already-submitted workflow stopped.
      if (!item.matchedId && error.errorCode !== 'operation_conflict') records.set(key, {...item, state: 'manual_required', localPending: true,
        stage_label: '请求结果未确认，请核对任务记录后再操作', finished_at: new Date().toISOString()});
      throw error;
    } finally {
      pending.delete(key); emit(); void poller.refresh();
    }
  }
  function subscribe(listener) { listeners.add(listener); listener(data, stale); return () => listeners.delete(listener); }
  function startClock() {
    if (clock !== null || document.visibilityState === 'hidden') return;
    clock = window.setInterval(emit, 1000);
  }
  function stopClock() { if (clock !== null) window.clearInterval(clock); clock = null; }
  document.addEventListener('visibilitychange', () => document.visibilityState === 'hidden' ? stopClock() : startClock());
  window.addEventListener('pagehide', stopClock);
  window.addEventListener('pageshow', startClock);
  window.Team48Runtime = {subscribe, refresh: poller.refresh, hydrate, track, waitFor, put: item => {put(item); emit();},
    all: () => all().filter(item => !item.hidden && !item.archived), get: id => records.get(String(id)), isActive: active,
    snapshot: () => data, isStale: () => stale,
    destroy: () => {poller.destroy(); stopClock(); listeners.clear();}};
  startClock(); void poller.refresh();
})();
