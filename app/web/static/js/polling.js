/* One in-flight read per poller, with visibility and page lifecycle cleanup. */
(() => {
  function createPoller({ read, onData, onError, delay = () => 15000 }) {
    let timer = null, controller = null, pending = null, disposed = false, failures = 0, suspended = false, requested = false;
    const clear = () => { if (timer !== null) clearTimeout(timer); timer = null; };
    const schedule = ms => {
      clear();
      if (!disposed && !suspended && document.visibilityState !== "hidden") timer = setTimeout(refresh, ms);
    };
    async function refresh() {
      clear();
      if (disposed || suspended || document.visibilityState === "hidden") return;
      if (pending) { requested = true; return pending; }
      controller = new AbortController();
      const signal = controller.signal;
      pending = (async () => {
        let next = 15000;
        try {
          const data = await Promise.resolve().then(() => read(signal));
          if (signal.aborted || disposed) return;
          failures = 0;
          await onData(data);
          next = delay(data);
        } catch (error) {
          if (signal.aborted || disposed || error.name === "AbortError") return;
          failures += 1;
          next = Math.min(60000, 3000 * 2 ** (failures - 1));
          onError?.(error);
        } finally {
          controller = null;
          pending = null;
          schedule(requested ? 0 : next);
          requested = false;
        }
      })();
      return pending;
    }
    function pause() { suspended = true; clear(); controller?.abort(); }
    function visibility() { if (document.visibilityState === "hidden") pause(); else resume(); }
    function resume() { suspended = false; void refresh(); }
    function stop() { disposed = true; pause(); }
    function destroy() {
      stop();
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", pause);
      window.removeEventListener("pageshow", resume);
    }
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", pause);
    window.addEventListener("pageshow", resume);
    return { refresh, stop, destroy };
  }
  window.Team48Polling = { createPoller };
})();
