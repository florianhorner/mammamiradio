/* Mamma Mi Radio — admin producer-desk client behavior.
 *
 * Extracted from admin.html (eng review E1) to match the listener.js pattern.
 * This module is intentionally framework-free: it attaches helpers to `window`
 * so the inline <script> in admin.html can call them, and reads/writes the DOM
 * directly. No build step.
 *
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │ undoableToast(msg, onUndo, ttl)                               │
 *   │   click X  ──▶ optimistic remove ──▶ toast (Undo · 5s)        │
 *   │                          │                                    │
 *   │            ┌─────────────┼──────────────┐                     │
 *   │       click Undo     5s elapse      backend NACK              │
 *   │            │             │              │                     │
 *   │        onUndo()       commit       error toast +              │
 *   │      (restore row)   (no-op)     row re-renders next poll     │
 *   └─────────────────────────────────────────────────────────────┘
 *
 * Undo toasts are capped at MAX_TOASTS (5, eng review E8); the oldest undo
 * commits to make room for another undo. Error toasts have their own smaller
 * cap and only evict older errors, so a backend failure notice can never force
 * a pending undo action to commit. Slide animation is a CSS transition, so the
 * global `prefers-reduced-motion: reduce` rule in admin.html neutralizes it for
 * free.
 */
(function () {
  'use strict';

  // ── Undo toast stack ─────────────────────────────────────────────
  const MAX_TOASTS = 5; // eng review E8 — cap visible undo toasts
  const MAX_ERROR_TOASTS = 2; // errors share safe-space, but never evict undo
  const DEFAULT_TTL = 5000; // eng review D4 — 5s window, auto-dismiss = commit

  let _stackEl = null;
  const _live = []; // {el, timer, committed, kind}

  function _ensureStack() {
    if (_stackEl) return _stackEl;
    _stackEl = document.getElementById('undoStack');
    if (!_stackEl) {
      _stackEl = document.createElement('div');
      _stackEl.id = 'undoStack';
      _stackEl.className = 'undo-stack';
      // aria-live=polite so screen readers announce each removal.
      _stackEl.setAttribute('role', 'status');
      _stackEl.setAttribute('aria-live', 'polite');
      document.body.appendChild(_stackEl);
    }
    return _stackEl;
  }

  function _syncToastBodyClass() {
    document.body.classList.toggle('undo-toast-active', _live.length > 0);
    const space = Math.min(220, 72 + _live.length * 56);
    document.documentElement.style.setProperty('--undo-toast-space', space + 'px');
  }

  function _dismiss(entry, { runCommit }) {
    if (entry.committed) return;
    entry.committed = true;
    if (entry.timer) clearTimeout(entry.timer);
    const idx = _live.indexOf(entry);
    if (idx !== -1) _live.splice(idx, 1);
    entry.el.classList.remove('show');
    // Remove after the CSS fade; reduced-motion shortens the transition.
    setTimeout(() => {
      entry.el.remove();
      _syncToastBodyClass();
    }, 220);
    if (runCommit && typeof entry.onCommit === 'function') entry.onCommit();
    _syncToastBodyClass();
  }

  function _countToastsOfKind(kind) {
    return _live.filter((entry) => entry.kind === kind).length;
  }

  function _oldestToastOfKind(kind) {
    return _live.find((entry) => entry.kind === kind) || null;
  }

  /**
   * Show an undoable toast. The destructive action has ALREADY been applied
   * optimistically by the caller. `onUndo` reverts it; it only fires if the
   * user clicks Undo within `ttl`.
   *
   * @param {string} message  human text, e.g. 'Removed "Volare".'
   * @param {function} onUndo  revert callback (re-add the row / re-queue)
   * @param {number} [ttl]     window in ms before auto-commit (default 5000)
   * @param {function} [onCommit]  optional callback when the window closes
   */
  function undoableToast(message, onUndo, ttl, onCommit) {
    const stack = _ensureStack();
    ttl = typeof ttl === 'number' ? ttl : DEFAULT_TTL;

    // Cap undo toasts only: commit the oldest undo to make room (E8).
    while (_countToastsOfKind('undo') >= MAX_TOASTS) {
      const oldestUndo = _oldestToastOfKind('undo');
      if (!oldestUndo) break;
      _dismiss(oldestUndo, { runCommit: true });
    }

    const el = document.createElement('div');
    el.className = 'undo-toast';
    const msg = document.createElement('span');
    msg.className = 'undo-toast-msg';
    msg.textContent = message;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'undo-toast-btn';
    btn.textContent = 'Undo';
    el.appendChild(msg);
    el.appendChild(btn);
    stack.appendChild(el);

    const entry = { el, timer: null, committed: false, kind: 'undo', onCommit: onCommit || null };

    btn.addEventListener('click', () => {
      if (entry.committed) return;
      _dismiss(entry, { runCommit: false }); // undo: skip the commit, run onUndo instead
      try {
        if (typeof onUndo === 'function') onUndo();
      } catch (e) {
        /* swallow — undo is best-effort, next status poll will reconcile */
      }
    });

    // Escape commits (uncertain → commit, matches D4).
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') _dismiss(entry, { runCommit: true });
    });

    _live.push(entry);
    _syncToastBodyClass();
    // rAF so the CSS transition runs from the hidden state.
    requestAnimationFrame(() => el.classList.add('show'));
    entry.timer = setTimeout(() => _dismiss(entry, { runCommit: true }), ttl);
    btn.focus({ preventScroll: true });
    return entry;
  }

  /**
   * Show a plain error toast (no undo) when an optimistic action's backend call
   * fails. The caller relies on the next /api/status poll to re-render the row.
   */
  function errorToast(message) {
    const stack = _ensureStack();
    while (_countToastsOfKind('error') >= MAX_ERROR_TOASTS) {
      const oldestError = _oldestToastOfKind('error');
      if (!oldestError) break;
      _dismiss(oldestError, { runCommit: false });
    }

    const el = document.createElement('div');
    el.className = 'undo-toast undo-toast-error';
    el.textContent = message;
    stack.appendChild(el);
    const entry = { el, timer: null, committed: false, kind: 'error', onCommit: null };
    _live.push(entry);
    _syncToastBodyClass();
    requestAnimationFrame(() => el.classList.add('show'));
    entry.timer = setTimeout(() => _dismiss(entry, { runCommit: false }), DEFAULT_TTL);
  }

  // ── Archivio filter persistence (sessionStorage) ─────────────────
  const ARCHIVIO_KEY = 'mmr.admin.archivio.filters';

  function archivioFilterPersist(filters) {
    try {
      sessionStorage.setItem(ARCHIVIO_KEY, JSON.stringify(filters));
    } catch (e) {
      /* sessionStorage unavailable (private mode) — non-fatal */
    }
  }

  function archivioFilterRestore() {
    try {
      const raw = sessionStorage.getItem(ARCHIVIO_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  // ── Motore Setup auto-collapse ───────────────────────────────────
  /**
   * Collapse the Setup <details> when every readiness item is ready; expand it
   * when any item still needs attention. Driven by the /api/status poll data.
   *
   * @param {boolean} allReady  true when no item is "Da fare"/"Bloccato"
   */
  function motoreSetupAutoCollapse(allReady) {
    const details = document.getElementById('setupGroup');
    if (!details) return;
    // Only auto-toggle when the operator hasn't manually pinned it open.
    if (details.dataset.userPinned === 'true') return;
    details.open = !allReady;
    const badge = document.getElementById('setupReadyBadge');
    if (badge) badge.style.display = allReady ? '' : 'none';
  }

  // ── Combined mode indicator chip (On Air sticky strip) ───────────
  function modeChipRender(chaos, festival) {
    const chip = document.getElementById('modeChip');
    if (!chip) return;
    const modes = [];
    if (chaos) modes.push('CHAOS');
    if (festival) modes.push('FESTIVAL');
    if (modes.length) {
      chip.textContent = 'MODES: ' + modes.join(' · ');
      chip.style.display = '';
    } else {
      chip.textContent = '';
      chip.style.display = 'none';
    }
  }

  // Expose to the inline admin.html script.
  window.undoableToast = undoableToast;
  window.errorToast = errorToast;
  window.archivioFilterPersist = archivioFilterPersist;
  window.archivioFilterRestore = archivioFilterRestore;
  window.motoreSetupAutoCollapse = motoreSetupAutoCollapse;
  window.modeChipRender = modeChipRender;
})();
