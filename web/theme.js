'use strict';
// Apply the saved palette before CSS paints, including when the API is unavailable.
(() => {
  const storageKey = 'jhalcv-theme';
  let theme = 'dark';
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch (_) { /* Theme switching still works when browser storage is disabled. */ }
  const apply = () => {
    document.documentElement.dataset.theme = theme;
    const button = document.querySelector('#theme-toggle');
    if (!button) return;
    button.textContent = theme === 'dark' ? '☾ Dark' : '☀ Light';
    button.setAttribute('aria-pressed', String(theme === 'dark'));
    button.title = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
  };
  apply();
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelector('#theme-toggle').addEventListener('click', () => {
      theme = theme === 'dark' ? 'light' : 'dark';
      apply();
      try { localStorage.setItem(storageKey, theme); } catch (_) { /* Keep this session's choice. */ }
    });
  });
})();
