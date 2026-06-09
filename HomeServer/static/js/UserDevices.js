/**
 * devices.js  —  EBS SmartHome
 *
 * Renders device cards grouped by room, handles on/off toggle and brightness
 * control via the API, and provides search + type filtering.
 *
 * Reads room data from a JSON island: <script id="rooms-data" type="application/json">.
 * Public surface: window.DevicesModule = { init(dashData), initialized }
 */
window.DevicesModule = (function () {
  'use strict';

  let _canControl  = false;
  let _rooms       = [];
  let _initialized = false;

  const DEVICE_ICONS = {
    light:  'fa-lightbulb',
    socket: 'fa-plug',
    fan:    'fa-fan',
    heater: 'fa-fire-flame-curved',
    cooker: 'fa-kitchen-set',
    lock:   'fa-lock',
    sensor: 'fa-microchip',
    camera: 'fa-camera',
    others: 'fa-cube',
  };

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Initialise the module: read room data, render cards, and bind UI events.
   * Safe to call multiple times — only runs once.
   * @param {Object} dashData - The parsed dashboard-data JSON island.
   */
  function init(dashData) {
    if (_initialized) return;
    _initialized = true;

    _canControl = !!dashData.canControl;
    _rooms      = JSON.parse(document.getElementById('rooms-data').textContent || '[]');

    renderAll();
    bindSearch();
    bindRoomToggles();
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  /**
   * Render all device cards into #devicesContainer, applying optional filters.
   * Shows an empty-state message when no rooms or no matching devices exist.
   * @param {string} filter     - Substring to match against device names.
   * @param {string} typeFilter - Device type string to match exactly.
   */
  function renderAll(filter = '', typeFilter = '') {
    const container = document.getElementById('devicesContainer');
    if (!container) return;

    if (!_rooms.length) {
      container.innerHTML = `<div class="empty-state">
        <i class="fas fa-plug-circle-xmark"></i>
        <p>No devices found. Rooms with devices will appear here.</p>
      </div>`;
      return;
    }

    let html       = '';
    let anyVisible = false;

    _rooms.forEach(room => {
      const devices = (room.devices || []).filter(d => {
        const matchName = !filter || d.name.toLowerCase().includes(filter.toLowerCase());
        const matchType = !typeFilter || d.type === typeFilter;
        return matchName && matchType;
      });

      if (!devices.length) return;
      anyVisible = true;

      html += `<div class="dev-room-group">
        <div class="dev-room-label">
          <i class="fas fa-${room.icon || 'door-open'}"></i>
          ${esc(room.name)}
        </div>
        <div class="dev-grid">
          ${devices.map(d => deviceCard(d, room)).join('')}
        </div>
      </div>`;
    });

    if (!anyVisible) {
      container.innerHTML = `<div class="empty-state">
        <i class="fas fa-magnifying-glass"></i>
        <p>No devices match your search.</p>
      </div>`;
      return;
    }

    container.innerHTML = html;
    bindToggles(container);
    bindBrightness(container);
  }

  /**
   * Build the HTML string for a single device card.
   * Includes a brightness slider for lights that are on and have brightness data.
   * @param {Object} d    - Device object.
   * @param {Object} room - Parent room object.
   * @returns {string} HTML string.
   */
  function deviceCard(d, room) {
    const icon     = DEVICE_ICONS[d.type] || 'fa-cube';
    const onCls    = d.isOn ? 'dev-on' : '';
    const offCls   = d.isOnline ? '' : ' dev-offline';
    const disabled = !_canControl || !d.isOnline ? 'disabled' : '';
    const checked  = d.isOn ? 'checked' : '';

    let extra = '';
    if (d.type === 'light' && d.brightness !== null && d.isOn) {
      extra = `<div class="dev-brightness">
        <i class="fas fa-sun"></i>
        <input type="range" class="brightness-range js-brightness"
               data-device-id="${d.id}"
               min="0" max="100" value="${d.brightness ?? 100}"
               ${disabled}>
        <span class="bval">${d.brightness ?? 100}%</span>
      </div>`;
    }

    const powerStr = d.power !== null ? `${d.power}W` : '';

    return `<div class="dev-card ${onCls}${offCls}"
               data-device-id="${d.id}"
               data-device-type="${d.type}"
               data-room-id="${room.id}">
      <div class="dev-card-top">
        <div class="dev-type-icon"><i class="fas ${icon}"></i></div>
        <div class="dev-name">${esc(d.name)}</div>
        ${_canControl ? `<label class="toggle-sw dev-toggle">
          <input type="checkbox" class="js-dev-toggle" data-device-id="${d.id}"
                 ${checked} ${disabled}>
          <span class="toggle-track"></span>
        </label>` : ''}
      </div>
      <div class="dev-meta">
        <span class="dev-status-dot"></span>
        <span>${d.isOnline ? (d.isOn ? 'On' : 'Off') : 'Offline'}</span>
        ${powerStr ? `<span>· ${powerStr}</span>` : ''}
      </div>
      ${extra}
    </div>`;
  }

  // ── Event binding ──────────────────────────────────────────────────────────

  /**
   * @param {HTMLElement} container
   */
  function bindToggles(container) {
    container.querySelectorAll('.js-dev-toggle').forEach(input => {
      input.addEventListener('change', async function () {
        const id   = this.dataset.deviceId;
        const on   = this.checked;
        const card = this.closest('.dev-card');
        this.disabled = true;

        try {
          const res = await apiFetch(`/api/devices/${id}/toggle`, { action: on ? 'on' : 'off' });
          if (!res.ok) throw new Error();
          card.classList.toggle('dev-on', on);
          const dot = card.querySelector('.dev-status-dot');
          if (dot) dot.title = on ? 'On' : 'Off';
          updateRoomDeviceState(id, on);
        } catch {
          this.checked = !on;
          showToast('error', 'Could not toggle device. Please try again.');
        } finally {
          this.disabled = false;
        }
      });
    });
  }

  /**
   * Bind brightness range sliders inside the given container.
   * Updates the adjacent percentage label on input, POSTs the value on change.
   * @param {HTMLElement} container
   */
  function bindBrightness(container) {
    container.querySelectorAll('.js-brightness').forEach(range => {
      const valEl = range.nextElementSibling;
      range.addEventListener('input', function () {
        if (valEl) valEl.textContent = `${this.value}%`;
      });
      range.addEventListener('change', async function () {
        const id = this.dataset.deviceId;
        try {
          await apiFetch(`/api/devices/${id}/brightness`, { brightness: parseInt(this.value) });
        } catch {
          showToast('error', 'Could not set brightness.');
        }
      });
    });
  }

  /**
   * Bind room-level "turn all on/off" buttons.
   * Re-renders the device grid after a successful toggle.
   */
  function bindRoomToggles() {
    document.querySelectorAll('.js-room-all-toggle').forEach(btn => {
      btn.addEventListener('click', async function () {
        const rid    = this.dataset.roomId;
        const anyOn  = this.dataset.anyOn === 'true';
        const action = anyOn ? 'off' : 'on';

        try {
          const res = await apiFetch(`/api/rooms/${rid}/toggle-all`, { action });
          if (!res.ok) throw new Error();
          this.dataset.anyOn = anyOn ? 'false' : 'true';
          this.innerHTML = `<i class="fas fa-power-off"></i> ${anyOn ? 'All On' : 'All Off'}`;
          showToast('success', anyOn ? 'All devices turned off' : 'All devices turned on');

          const devSec = document.getElementById('section-devices');
          if (devSec?.classList.contains('is-active')) {
            renderAll(
              document.getElementById('deviceSearch')?.value || '',
              document.getElementById('deviceTypeFilter')?.value || '',
            );
          }
        } catch {
          showToast('error', 'Failed to toggle room devices.');
        }
      });
    });
  }

  /**
   * Wire up the device search input and type-filter dropdown.
   * Both controls trigger a debounced re-render.
   */
  function bindSearch() {
    const searchInp = document.getElementById('deviceSearch');
    const typeFilter= document.getElementById('deviceTypeFilter');

    function doFilter() {
      renderAll(searchInp?.value || '', typeFilter?.value || '');
    }

    searchInp?.addEventListener('input',  debounce(doFilter, 250));
    typeFilter?.addEventListener('change', doFilter);
  }

  // ── Utilities ──────────────────────────────────────────────────────────────

  /**
   * @param {string|number} deviceId
   * @param {boolean}       isOn
   */
  function updateRoomDeviceState(deviceId, isOn) {
    const id = parseInt(deviceId, 10);
    _rooms.forEach(r => {
      const dev = (r.devices || []).find(d => d.id === id);
      if (dev) dev.isOn = isOn;
    });
  }

  /**
   * Authenticated JSON POST helper shared by all device API calls.
   * @param {string} url
   * @param {Object} body
   * @returns {Promise<Response>}
   */
  async function apiFetch(url, body) {
    return fetch(url, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
      body:    JSON.stringify(body),
    });
  }

  /**
   * Read the CSRF token from the dashboard-data JSON island.
   * @returns {string}
   */
  function getCsrf() {
    try {
      const island = document.getElementById('dashboard-data');
      if (island) return JSON.parse(island.textContent).csrfToken || '';
    } catch {}
    return '';
  }

  /**
   * Escape a value for safe HTML insertion.
   * @param {*} str
   * @returns {string}
   */
  function esc(str) {
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /**
   * Return a debounced version of fn that fires after ms milliseconds of silence.
   * @param {Function} fn
   * @param {number}   ms
   * @returns {Function}
   */
  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  /**
   * Display a toast notification via the global toastManager if available.
   * @param {'success'|'error'|'info'|'warning'} type
   * @param {string} message
   */
  function showToast(type, message) {
    if (window.toastManager) {
      window.toastManager.show({ type, message, title: type.charAt(0).toUpperCase() + type.slice(1) });
    }
  }

  return { init, get initialized() { return _initialized; } };

})();