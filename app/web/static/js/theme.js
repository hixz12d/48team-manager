/* Apply before styles load so navigation never flashes the opposite theme. */
(() => {
  'use strict';
  const storageKey = 'team48-theme';
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  const validPreference = value => ['dark', 'light', 'system'].includes(value);
  let preference = 'dark';
  try {
    const saved = localStorage.getItem(storageKey);
    if (validPreference(saved)) preference = saved;
  } catch (_) {
    // Storage can be unavailable in restricted browsing contexts.
  }

  function applyTheme() {
    document.documentElement.dataset.theme = preference === 'system'
      ? (systemTheme.matches ? 'dark' : 'light')
      : preference;
    document.querySelectorAll('[data-theme-select]').forEach(select => {
      select.value = preference;
    });
  }

  applyTheme();
  document.addEventListener('DOMContentLoaded', () => {
    applyTheme();
    document.querySelectorAll('[data-theme-select]').forEach(select => {
      select.addEventListener('change', () => {
        if (!validPreference(select.value)) return;
        preference = select.value;
        applyTheme();
        try {
          localStorage.setItem(storageKey, preference);
        } catch (_) {
          // Switching still works for this page without persistent storage.
        }
      });
    });
  });
  systemTheme.addEventListener('change', () => {
    if (preference === 'system') applyTheme();
  });
  window.addEventListener('storage', event => {
    if (event.key !== storageKey && event.key !== null) return;
    preference = validPreference(event.newValue) ? event.newValue : 'dark';
    applyTheme();
  });
})();
