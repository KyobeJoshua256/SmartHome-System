(function () {
  'use strict';

  const DATA = JSON.parse(document.getElementById('dashboard-data').textContent);

  const sidebar       = document.getElementById('dbSidebar');
  const sidebarVeil   = document.getElementById('sidebarVeil');
  const sidebarToggle = document.getElementById('sidebarToggle');
  const sections      = document.querySelectorAll('.db-section');
  const navItems      = document.querySelectorAll('.snav-item[data-section]');
  const greetWord     = document.getElementById('greetWord');
  const topbarClock   = document.getElementById('topbarClock');
  const topbarDate    = document.getElementById('topbarDate');
  const statUnread    = document.getElementById('statUnread');
  const navUnreadBadge= document.getElementById('navUnreadBadge');

  // ── CSRF Manager ───────────────────────────────────────────────────────────
  const CsrfManager = (() => {
    const REFRESH_INTERVAL_MS = (3600 * 0.75) * 1000; // 45 minutes
    const REFRESH_URL         = '/chat/csrf-refresh';

    let _refreshTimer = null;
    let _inflight     = null; // dedup concurrent refresh calls

    // Resolves (with the token string or null) once the boot-time warm fetch
    // completes.  Any code that needs a fresh token before making its first
    // POST should await CsrfManager.ready instead of doing its own fetch.
    let _readyResolve;
    const ready = new Promise(r => { _readyResolve = r; });

    /** Write a fresh token into DATA, the dashboard-data island, and the meta
     *  tag so every read path sees the same value. */
    function _apply(token) {
      DATA.csrfToken = token;
      try {
        const island = document.getElementById('dashboard-data');
        if (island) {
          const parsed = JSON.parse(island.textContent);
          parsed.csrfToken = token;
          island.textContent = JSON.stringify(parsed);
        }
      } catch (_) {}
      try {
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) meta.setAttribute('content', token);
      } catch (_) {}
    }

    /** Fetch a fresh token from the server. Returns the token string or null. */
    async function _fetch() {
      if (_inflight) return _inflight; // coalesce concurrent requests
      _inflight = (async () => {
        try {
          const res = await window._origFetch(REFRESH_URL, { credentials: 'same-origin' });
          if (!res.ok) return null;
          const json = await res.json();
          if (json.csrf_token) {
            _apply(json.csrf_token);
            return json.csrf_token;
          }
          return null;
        } catch (_) {
          return null;
        } finally {
          _inflight = null;
        }
      })();
      return _inflight;
    }

    /** Schedule the next background refresh. */
    function _schedule() {
      clearTimeout(_refreshTimer);
      _refreshTimer = setTimeout(async () => {
        await _fetch();
        _schedule(); // reschedule after each refresh
      }, REFRESH_INTERVAL_MS);
    }

    /** Public: warm token immediately, then start the background refresh cycle. */
    async function init() {
      try {
        const token = await _fetch();
        _readyResolve(token);
      } catch (_) {
        _readyResolve(null); // never leave callers hanging
      }
      _schedule();
    }

    /** Public: force an immediate refresh (e.g. after a 400/403). */
    async function refresh() {
      return _fetch();
    }

    return { init, refresh, ready };
  })();

  // ── CSRF helper ──────────────────────────────────────────────────────────────
  // NOTE: deliberately does NOT read from <meta name="csrf-token">.
  // The meta tag holds the token that was rendered into the HTML, which is
  // stale after login.  CsrfManager.init() fetches a fresh token and writes
  // it to DATA and the dashboard-data island; those are the only sources
  // getCsrf() should read from.
  function getCsrf() {
    try {
      if (DATA.csrfToken) return DATA.csrfToken;
      const island = document.getElementById('dashboard-data');
      if (island) {
        const parsed = JSON.parse(island.textContent);
        if (parsed.csrfToken) return parsed.csrfToken;
      }
    } catch (e) { console.warn('Error getting CSRF token:', e); }
    return '';
  }

  // ── Fetch interceptor (single definition) ─────────────────────────────────
  window._origFetch = window.fetch;
  window.fetch = async function csrfAwareFetch(input, init = {}) {
    const url = typeof input === 'string' ? input : (input.url || '');
    const isSameOrigin = url.startsWith('/') || url.startsWith(window.location.origin);
    
    if (!isSameOrigin) return window._origFetch(input, init);

    const method = (init.method || 'GET').toUpperCase();
    const isMutating = ['POST', 'PUT', 'DELETE', 'PATCH'].includes(method);

    let modifiedInit = { ...init };
    
    if (isMutating && !modifiedInit.headers?.['X-CSRFToken']) {
      // Wait for the boot-time warm fetch before reading the token.
      // This guarantees the interceptor never injects a stale rendered token.
      await CsrfManager.ready;
      const token = getCsrf();
      if (token) {
        modifiedInit.headers = {
          ...(modifiedInit.headers || {}),
          'X-CSRFToken': token,
        };
      }
    }

    const res = await window._origFetch(input, modifiedInit);

    if ((res.status === 400 || res.status === 403) && isSameOrigin && isMutating) {
      const cloned = res.clone();
      let bodyText = '';
      try { bodyText = await cloned.text(); } catch (_) {}
      
      const looksLikeCsrf = bodyText.includes('CSRF') || bodyText.includes('csrf') ||
                            res.headers.get('content-type')?.includes('text/html');
      
      if (looksLikeCsrf) {
        const newToken = await CsrfManager.refresh();
        if (newToken) {
          const retryInit = { ...modifiedInit };
          retryInit.headers = {
            ...(retryInit.headers || {}),
            'X-CSRFToken': newToken
          };
          return window._origFetch(input, retryInit);
        }
      }
    }
    
    return res;
  };

  // ── Sidebar functions ─────────────────────────────────────────────────────
  function toggleSidebar() {
    const open = sidebar.classList.toggle('open');
    sidebarVeil.classList.toggle('visible', open);
  }

  function closeSidebar() {
    sidebar.classList.remove('open');
    sidebarVeil.classList.remove('visible');
  }

  sidebarToggle.addEventListener('click', toggleSidebar);
  sidebarVeil.addEventListener('click', closeSidebar);

  // ── Section switching ──────────────────────────────────────────────────────
  function switchSection(name) {
    sections.forEach(s => s.classList.remove('is-active'));
    navItems.forEach(n => n.classList.remove('active'));

    const target = document.getElementById(`section-${name}`);
    if (target) {
      target.classList.add('is-active');
      document.getElementById('dbMain').scrollTo({ top: 0, behavior: 'smooth' });
    }
    const navItem = document.querySelector(`.snav-item[data-section="${name}"]`);
    if (navItem) navItem.classList.add('active');

    if (name === 'messages' && window.MessagingModule && !MessagingModule.initialized) {
      MessagingModule.init(DATA);
    }
    if (name === 'devices' && window.DevicesModule && !DevicesModule.initialized) {
      DevicesModule.init(DATA);
    }
  }

  navItems.forEach(item => {
    item.addEventListener('click', e => {
      e.preventDefault();
      switchSection(item.dataset.section);
      closeSidebar();
    });
  });

  document.querySelectorAll('[data-goto]').forEach(btn => {
    btn.addEventListener('click', () => switchSection(btn.dataset.goto));
  });

  window.dashboardSwitch = switchSection;

  // ── Clock ──────────────────────────────────────────────────────────────────
  const TZ = 'Africa/Kampala';

  function _eatNow() {
    const now = new Date();
    const fmt = part => new Intl.DateTimeFormat('en-UG', { timeZone: TZ, [part]: 'numeric' })
      .format(now);
    return {
      h:     parseInt(fmt('hour'),   10),
      m:     parseInt(fmt('minute'), 10),
      s:     parseInt(fmt('second'), 10),
      day:   new Intl.DateTimeFormat('en-UG', { timeZone: TZ, weekday: 'long'  }).format(now),
      date:  parseInt(fmt('day'),    10),
      month: new Intl.DateTimeFormat('en-UG', { timeZone: TZ, month:   'long'  }).format(now),
      year:  parseInt(fmt('year'),   10),
    };
  }

  function updateClock() {
    const t = _eatNow();
    if (topbarClock) {
      topbarClock.textContent =
        `${String(t.h).padStart(2,'0')}:${String(t.m).padStart(2,'0')}:${String(t.s).padStart(2,'0')}`;
    }
    if (topbarDate) {
      topbarDate.textContent =
        `${t.day}, ${String(t.date).padStart(2,'0')} ${t.month} ${t.year}`;
    }
  }
  updateClock();
  setInterval(updateClock, 1000);

  // ── Greeting ───────────────────────────────────────────────────────────────
  function setGreeting() {
    const h    = _eatNow().h;
    const word = h < 5 ? 'night' : h < 12 ? 'morning' : h < 17 ? 'afternoon' : h < 21 ? 'evening' : 'night';
    if (greetWord) greetWord.textContent = word;
  }
  setGreeting();

  // ── Automation toggles ─────────────────────────────────────────────────────
  document.querySelectorAll('.js-auto-toggle').forEach(input => {
    input.addEventListener('change', async function () {
      const rid    = this.dataset.rid;
      const active = this.checked;
      const card   = this.closest('.auto-card');
      try {
        const res = await fetch(`/api/reminders/${rid}/toggle`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
          body:    JSON.stringify({ active }),
        });
        if (!res.ok) throw new Error();
        card?.classList.toggle('auto-card--off', !active);
      } catch {
        this.checked = !active;
        showToast('error', 'Failed to update automation');
      }
    });
  });

  // ── Unread badge ───────────────────────────────────────────────────────────
  window.dashboardSetUnread = function (count) {
    if (statUnread)     statUnread.textContent = count;
    if (navUnreadBadge) {
      navUnreadBadge.textContent = count;
      navUnreadBadge.hidden = count === 0;
    }
  };

  // ── Alert bell ─────────────────────────────────────────────────────────────
  const alertBell = document.getElementById('alertBell');
  if (alertBell) {
    alertBell.addEventListener('click', () => showToast('info', 'Alerts panel coming soon.'));
  }

  // ── CSRF background refresh ────────────────────────────────────────────────
  CsrfManager.init();

  // ── Preload messaging ──────────────────────────────────────────────────────
  if (window.MessagingModule && DATA.chat && DATA.chat.conversations) {
    MessagingModule.preload(DATA.chat.conversations);
  }

  // ── Utilities ──────────────────────────────────────────────────────────────
  function showToast(type, message) {
    if (window.toastManager) {
      window.toastManager.show({ type, message, title: type.charAt(0).toUpperCase() + type.slice(1) });
    } else {
      console.warn(`[${type}] ${message}`);
    }
  }

})();