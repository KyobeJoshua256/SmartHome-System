/* ============================================
   EBS SmartHome Login — Complete Backend Integration
   Real-time identifier validation + industrial lockout clock
   Lockout sync via Socket.IO (/auth namespace)
   ============================================ */

// ============================================
// Configuration
// ============================================

const CONFIG = {
    ANIMATION_DURATION: 600,
    TOKEN_EXPIRY_TIME: 300,
    AUTO_REMOVE_MESSAGES_DELAY: 5000,
    IDENTIFIER_CHECK_DEBOUNCE: 420,
    IDENTIFIER_MIN_LENGTH: 3,
    TYPING_SPEED: 100,
    TYPING_PAUSE: 2000,
    API_TIMEOUT: 5000
};

// ============================================
// Dynamic Texts (typing effect)
// ============================================

const DYNAMIC_TEXTS = [
    { text: "Securing your smart home",       type: "security"    },
    { text: "Advanced IoT integration",        type: "technology"  },
    { text: "Energy-efficient automation",     type: "energy"      },
    { text: "24/7 intelligent monitoring",     type: "integration" },
    { text: "Seamless device connectivity",    type: "technology"  },
    { text: "Real-time security alerts",       type: "security"    }
];

// ============================================
// Helper: HTML escape
// ============================================

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============================================
// Helper: Phone number validation
// ============================================

function isValidPhoneNumber(input) {
    const clean = input.replace(/[\s\-\(\)]/g, '');
    if (clean.startsWith('+') && clean.slice(1).match(/^\d{10,15}$/)) return true;
    if (clean.match(/^\d{10,15}$/)) return true;
    return false;
}

// ============================================
// Socket.IO — /auth namespace
// Replaces all polling. Handles lockout sync
// and the initial lockout status check.
// ============================================

const AuthSocket = {
    _socket: null,
    _ready: false,
    _queue: [],   // callbacks waiting for connection

    init() {
        if (typeof io === 'undefined') {
            console.warn('[AuthSocket] Socket.IO client not loaded — falling back to API polling.');
            return;
        }

        this._socket = io('/auth', {
            transports: ['websocket', 'polling'],
            reconnectionAttempts: 5,
            reconnectionDelay: 2000,
            withCredentials: true
        });

        this._socket.on('connect', () => {
            this._ready = true;
            // Flush any queued work
            this._queue.forEach(fn => fn());
            this._queue = [];

            // On (re)connect: immediately ask server for current lockout state.
            // Server responds with 'lockout:status'.
            this._socket.emit('lockout:check');
        });

        this._socket.on('disconnect', () => {
            this._ready = false;
        });

        // Server → client: current lockout state (response to lockout:check,
        // and also pushed whenever state changes server-side).
        this._socket.on('lockout:status', (data) => {
            LockoutClock._onServerStatus(data);
        });

        // Server → client: lockout lifted (timer expired server-side)
        this._socket.on('lockout:cleared', () => {
            LockoutClock._onUnlocked();
        });

        // Server → client: new lockout was set (e.g. from another tab/device)
        this._socket.on('lockout:set', (data) => {
            if (data && data.locked_until_ms > Date.now()) {
                LockoutClock.show(
                    data.identifier,
                    data.locked_until_ms,
                    data.total_duration_seconds
                );
            }
        });

        this._socket.on('connect_error', (err) => {
            console.debug('[AuthSocket] Connection error:', err.message);
        });
    },

    /**
     * Emit an event. If not connected yet, queue it.
     */
    emit(event, data = {}) {
        if (this._ready && this._socket) {
            this._socket.emit(event, data);
        } else {
            this._queue.push(() => this._socket?.emit(event, data));
        }
    },

    /**
     * Tell the server to store a new lockout record.
     * Server will broadcast 'lockout:set' to other sessions.
     */
    setLockout(identifier, lockedUntilMs, durationSeconds) {
        this.emit('lockout:set_server', {
            identifier,
            locked_until_ms: lockedUntilMs,
            duration_seconds: durationSeconds
        });
    },

    /**
     * Tell the server the lockout is cleared.
     */
    clearLockout() {
        this.emit('lockout:clear_server', {});
    },

    isAvailable() {
        return this._socket !== null;
    }
};

// ============================================
// API Service Module
// (HTTP calls — identifier check only;
//  lockout sync now goes through AuthSocket)
// ============================================

const APIService = {

    async checkIdentifier(raw, tab = 'password') {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), CONFIG.API_TIMEOUT);
            const response = await fetch(
                `/auth/api/check-identifier?q=${encodeURIComponent(raw)}&tab=${tab}`,
                {
                    method: 'GET',
                    headers: { 'Accept': 'application/json' },
                    credentials: 'same-origin',
                    signal: controller.signal
                }
            );
            clearTimeout(timeoutId);
            if (!response.ok) return null;
            return await response.json();
        } catch (error) {
            if (error.name === 'AbortError') return { status: 'error', reason: 'timeout' };
            return null;
        }
    },

    async checkUserStatus(identifier, csrfToken) {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), CONFIG.API_TIMEOUT);
            const response = await fetch(
                `/auth/api/check-user/${encodeURIComponent(identifier)}`,
                {
                    method: 'GET',
                    headers: {
                        'X-CSRF-Token': csrfToken,
                        'Accept': 'application/json',
                        'Content-Type': 'application/json'
                    },
                    credentials: 'same-origin',
                    signal: controller.signal
                }
            );
            clearTimeout(timeoutId);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return await response.json();
        } catch (error) {
            if (error.name === 'AbortError') return { exists: false, error: 'timeout' };
            console.debug('User status check failed:', error);
            return { exists: false, error: error.message };
        }
    }
};

// ============================================
// LOCKOUT CLOCK MODULE
// ============================================

const LockoutClock = {
    _interval: null,
    _endMs: 0,
    _totalMs: 0,
    _identifier: '',
    _R: 62,
    _CX: 80,
    _CY: 80,
    _STORAGE_KEY: 'lockoutClockState',

    init() {
        this._restoreFromStorage();
        // Socket handles the server status check on connect
    },

    /**
     * Called by AuthSocket when server sends lockout:status
     */
    _onServerStatus(data) {
        if (data.locked && data.locked_until_ms > Date.now()) {
            this.show(
                data.identifier || this._identifier,
                data.locked_until_ms,
                data.total_duration_seconds
            );
        } else if (data.expired || (!data.locked && this._endMs > 0)) {
            this.hide();
        }
    },

    async show(identifier, endMs, totalDurationSeconds) {
        this._identifier = identifier;
        this._endMs = typeof endMs === 'number' ? endMs : parseInt(endMs);
        this._totalMs = totalDurationSeconds
            ? totalDurationSeconds * 1000
            : Math.max(this._endMs - Date.now(), 1000);

        this._saveToStorage();

        // Tell server (which will also notify other tabs/sessions)
        AuthSocket.setLockout(identifier, this._endMs, Math.ceil(this._totalMs / 1000));

        this._hidePasswordAndButton();

        window.dispatchEvent(new CustomEvent('userLocked', {
            detail: { identifier, endMs: this._endMs }
        }));

        if (!document.getElementById('lockoutClockOverlay')) {
            this._inject();
        }
        const overlay = document.getElementById('lockoutClockOverlay');
        if (overlay) {
            overlay.classList.remove('hiding');
            overlay.classList.add('visible');
        }

        this._tick();
        this._startInterval();
        showNotification(`Account "${escapeHtml(identifier)}" is temporarily locked. Please wait.`, 'warning');
    },

    hide() {
        this._stopInterval();
        this._clearStorage();

        const overlay = document.getElementById('lockoutClockOverlay');
        if (overlay) {
            overlay.classList.add('hiding');
            setTimeout(() => { if (overlay.parentNode) overlay.remove(); }, 420);
        }
        this._restorePasswordAndButton();
        window.dispatchEvent(new CustomEvent('userUnlocked'));
    },

    dismiss() {
        this._stopInterval();
        const overlay = document.getElementById('lockoutClockOverlay');
        if (overlay) {
            overlay.classList.add('hiding');
            setTimeout(() => { if (overlay.parentNode) overlay.remove(); }, 420);
        }
    },

    _saveToStorage() {
        try {
            localStorage.setItem(this._STORAGE_KEY, JSON.stringify({
                identifier: this._identifier,
                endMs: this._endMs,
                totalMs: this._totalMs,
                timestamp: Date.now()
            }));
        } catch (e) {}
    },

    _clearStorage() {
        try { localStorage.removeItem(this._STORAGE_KEY); } catch (e) {}
    },

    _restoreFromStorage() {
        let stored;
        try {
            const raw = localStorage.getItem(this._STORAGE_KEY);
            if (!raw) return;
            stored = JSON.parse(raw);
        } catch (e) { this._clearStorage(); return; }

        const { identifier, endMs, totalMs, timestamp } = stored;
        if ((Date.now() - (timestamp || 0)) > 3600000) { this._clearStorage(); return; }
        if (!endMs || endMs <= Date.now()) { this._clearStorage(); return; }

        this._identifier = identifier || '';
        this._endMs = endMs;
        this._totalMs = totalMs || Math.max(endMs - Date.now(), 1000);
        // Socket connect event will confirm with server whether to actually show
    },

    _inject() {
        const circumference = (2 * Math.PI * this._R).toFixed(2);
        const el = document.createElement('div');
        el.id = 'lockoutClockOverlay';
        el.className = 'lockout-clock-overlay';
        el.setAttribute('role', 'timer');
        el.setAttribute('aria-live', 'polite');
        el.innerHTML = `
            <button class="lockout-clock-dismiss" onclick="window.LockoutClock.dismiss()" type="button" aria-label="Dismiss lockout notice">
                <i class="fas fa-times"></i>
            </button>
            <div class="lockout-clock-header">
                <i class="fas fa-shield-alt"></i>
                <h4>Account Temporarily Locked</h4>
            </div>
            <div class="lockout-svg-clock" id="lockoutSvgClock">
                <svg viewBox="0 0 160 160" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <circle class="clock-glow-ring" cx="${this._CX}" cy="${this._CY}" r="${this._R}" />
                    <g id="lockoutTickMarks"></g>
                    <circle class="clock-track" cx="${this._CX}" cy="${this._CY}" r="${this._R}" />
                    <circle class="clock-arc" id="lockoutArc" cx="${this._CX}" cy="${this._CY}" r="${this._R}" stroke-dasharray="${circumference}" stroke-dashoffset="0" />
                </svg>
                <div class="lockout-clock-digits">
                    <span class="lockout-digits-time" id="lockoutDigitsTime">--:--</span>
                    <span class="lockout-digits-label">remaining</span>
                </div>
            </div>
            <p class="lockout-clock-msg">Too many failed attempts.<br><strong>This account has been temporarily locked for security.</strong></p>
            <p class="lockout-clock-hint">The form will unlock automatically when the timer ends.</p>
            <div class="lockout-account-info">
                <i class="fas fa-user-lock"></i>
                <span id="lockoutIdentifierDisplay">${escapeHtml(this._identifier)}</span>
            </div>
        `;

        const anchor = document.getElementById('lockoutClockAnchor');
        if (anchor) {
            anchor.appendChild(el);
        } else {
            const identifierInput = document.querySelector('input[name="identifier"]');
            const target = identifierInput?.closest('.form-group');
            if (target?.parentNode) {
                target.parentNode.insertBefore(el, target.nextSibling);
            } else {
                const flashContainer = document.getElementById('flash-messages-container');
                (flashContainer
                    ? flashContainer.insertAdjacentElement('afterend', el)
                    : document.body.appendChild(el));
            }
        }

        this._buildTicks();
    },

    _buildTicks() {
        const g = document.getElementById('lockoutTickMarks');
        if (!g) return;
        let html = '';
        for (let i = 0; i < 12; i++) {
            const angle = (i / 12) * 2 * Math.PI - Math.PI / 2;
            const inner = this._R + 10, outer = this._R + 16;
            const x1 = (this._CX + Math.cos(angle) * inner).toFixed(2);
            const y1 = (this._CY + Math.sin(angle) * inner).toFixed(2);
            const x2 = (this._CX + Math.cos(angle) * outer).toFixed(2);
            const y2 = (this._CY + Math.sin(angle) * outer).toFixed(2);
            html += `<line class="clock-tick" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
        }
        g.innerHTML = html;
    },

    _tick() {
        const now = Date.now();
        const remainingMs = Math.max(0, this._endMs - now);
        const remainingSec = Math.ceil(remainingMs / 1000);

        const digitsEl = document.getElementById('lockoutDigitsTime');
        if (digitsEl) {
            const m = Math.floor(remainingSec / 60);
            const s = remainingSec % 60;
            digitsEl.textContent = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
        }

        const arc = document.getElementById('lockoutArc');
        if (arc) {
            const circumference = 2 * Math.PI * this._R;
            const progress = remainingMs / this._totalMs;
            arc.style.strokeDashoffset = (circumference * (1 - progress)).toFixed(2);
        }

        const svgClock = document.getElementById('lockoutSvgClock');
        if (svgClock) {
            svgClock.classList.remove('state-warning', 'state-critical');
            if (remainingSec <= 60)       svgClock.classList.add('state-critical');
            else if (remainingSec <= 300) svgClock.classList.add('state-warning');
        }

        const overlay = document.getElementById('lockoutClockOverlay');
        if (overlay && remainingSec > 0) {
            const m = Math.floor(remainingSec / 60), s = remainingSec % 60;
            overlay.setAttribute('aria-label', `Account locked. ${m} minutes ${s} seconds remaining.`);
        }

        if (remainingMs <= 0) this._onUnlocked();
    },

    _startInterval() {
        this._stopInterval();
        this._interval = setInterval(() => this._tick(), 100);
    },

    _stopInterval() {
        if (this._interval) { clearInterval(this._interval); this._interval = null; }
    },

    _onUnlocked() {
        this._stopInterval();
        this.hide();
        AuthSocket.clearLockout();

        window.dispatchEvent(new CustomEvent('lockoutExpired', {
            detail: { identifier: this._identifier }
        }));

        showNotification('Account unlocked. You may now sign in.', 'success');

        const focusTarget = document.getElementById('identifier')
                         || document.getElementById('phone')
                         || document.querySelector('input[name="identifier"]');
        if (focusTarget) {
            focusTarget.focus();
            focusTarget.dispatchEvent(new Event('input', { bubbles: true }));
        }
    },

    _hidePasswordAndButton() {
        ['passwordGroup', 'rememberGroup'].forEach(id => {
            document.getElementById(id)?.classList.add('lockout-hidden');
        });
        ['loginBtn', 'sendTokenBtn', 'verifyTokenBtn', 'resendTokenBtn',
         'token-submit-btn'].forEach(id => {
            const el = document.getElementById(id);
            if (el) { el.classList.add('btn-locked'); el.disabled = true; }
        });
    },

    _restorePasswordAndButton() {
        ['passwordGroup', 'rememberGroup'].forEach(id => {
            document.getElementById(id)?.classList.remove('lockout-hidden');
        });
        ['loginBtn', 'sendTokenBtn', 'verifyTokenBtn', 'resendTokenBtn',
         'token-submit-btn'].forEach(id => {
            const el = document.getElementById(id);
            if (el) { el.classList.remove('btn-locked'); el.disabled = false; }
        });
    }
};

window.LockoutClock = LockoutClock;

// ============================================
// Real-time Identifier Validation
// ============================================

(function initRealtimeValidation() {

    function debounce(fn, delay) {
        let t;
        return function (...args) {
            clearTimeout(t);
            t = setTimeout(() => fn.apply(this, args), delay);
        };
    }

    function fmtTime(totalSec) {
        const m = Math.floor(totalSec / 60), s = totalSec % 60;
        return m > 0 ? `${m}m ${s}s` : `${s}s`;
    }

    function getOrCreateHint(inputEl, hintId) {
        let hint = document.getElementById(hintId);
        if (!hint) {
            hint = document.createElement('div');
            hint.id = hintId;
            hint.className = 'en-id-hint';
            inputEl.parentNode.insertBefore(hint, inputEl.nextSibling);
        }
        return hint;
    }

    function renderHint(hintEl, state, payload) {
        const icons = {
            checking: '⏳', found: '✅', not_found: '❌', locked: '🔒',
            inactive: '⛔', no_otp: '📵', otp_cooldown: '⏱',
            no_password: '⚠️', error: '⚠️', invalid_for_token: '📵',
            otp_unavailable: '⚠️'
        };
        const colours = {
            checking: '#8888aa', found: '#3ddc84', not_found: '#ff5f57',
            locked: '#ffcc00', inactive: '#ff5f57', no_otp: '#ff9500',
            otp_cooldown: '#ff9500', no_password: '#ff9500', error: '#ff5f57',
            invalid_for_token: '#ff5f57', otp_unavailable: '#ff9500'
        };
        const messages = {
            checking:          'Checking…',
            not_found:         'No account found with that username, email, or phone.',
            locked:            payload?.remaining_seconds
                               ? `Account locked — ${fmtTime(payload.remaining_seconds)} remaining.`
                               : 'Account is locked.',
            inactive:          'This account has been deactivated.',
            no_otp:            'SMS login is not enabled for this account.',
            otp_cooldown:      payload?.otp_cooldown
                               ? `Wait ${payload.otp_cooldown}s before requesting another code.`
                               : (payload?.otp_blocked_reason || 'OTP temporarily unavailable.'),
            no_password:       'This account has no password set — try the OTP tab.',
            error:             'Could not verify. Please try again.',
            invalid_for_token: payload?.message || 'Please enter a valid phone number only.',
            otp_unavailable:   payload?.reason  || 'SMS login not available for this account.'
        };

        if (state === 'idle') {
            hintEl.innerHTML = '';
            hintEl.style.display = 'none';
            return;
        }

        const text  = state === 'found' && payload?.custom ? payload.custom : (messages[state] || '');
        const icon  = icons[state]   || '•';
        const color = colours[state] || '#888';

        hintEl.style.display = 'flex';
        hintEl.innerHTML = `
            <span class="en-hint-icon" style="color:${color}">${icon}</span>
            <span class="en-hint-text" style="color:${color}">${escapeHtml(text)}</span>
        `;
    }

    function startHintCountdown(hintEl, data, pwInput, submitBtn) {
        if (!data.locked_until_ms) return;
        LockoutClock.show(data.username || '', data.locked_until_ms, data.total_duration_seconds || 1800);
        const iv = setInterval(() => {
            const remaining = Math.max(0, Math.floor((data.locked_until_ms - Date.now()) / 1000));
            if (remaining <= 0) {
                clearInterval(iv);
                renderHint(hintEl, 'found', { custom: 'Account unlocked — please try again.' });
                if (pwInput) pwInput.disabled = false;
                if (submitBtn) submitBtn.disabled = false;
                return;
            }
            renderHint(hintEl, 'locked', { remaining_seconds: remaining });
        }, 1000);
    }

    function initPasswordTab() {
        const idInput = document.getElementById('identifier');
        const pwInput = document.getElementById('password');
        if (!idInput) return;

        const hintEl = getOrCreateHint(idInput, 'pw-identifier-hint');
        let lastChecked = '';

        const check = debounce(async function () {
            const raw = idInput.value.trim();
            if (raw.length < CONFIG.IDENTIFIER_MIN_LENGTH) {
                renderHint(hintEl, 'idle');
                if (pwInput) pwInput.disabled = false;
                return;
            }
            if (raw === lastChecked) return;
            lastChecked = raw;
            renderHint(hintEl, 'checking');

            const data = await APIService.checkIdentifier(raw, 'password');
            if (idInput.value.trim() !== raw) return;

            if (!data || data.status === 'error')   { renderHint(hintEl, 'error'); return; }
            if (data.status === 'too_short')         { renderHint(hintEl, 'idle');  return; }
            if (data.status === 'not_found')         { renderHint(hintEl, 'not_found'); if (pwInput) pwInput.disabled = false; return; }
            if (data.status === 'inactive')          { renderHint(hintEl, 'inactive');  if (pwInput) pwInput.disabled = true;  return; }
            if (data.status === 'locked')            { renderHint(hintEl, 'locked', data); if (pwInput) pwInput.disabled = true; startHintCountdown(hintEl, data, pwInput, null); return; }

            if (data.status === 'found') {
                if (!data.has_password) { renderHint(hintEl, 'no_password'); if (pwInput) pwInput.disabled = false; return; }
                const attemptsNote = data.remaining_attempts < data.max_attempts
                    ? ` · ${data.remaining_attempts} attempt${data.remaining_attempts === 1 ? '' : 's'} remaining`
                    : '';
                renderHint(hintEl, 'found', { custom: `Account found: ${data.username}${attemptsNote}` });
                if (pwInput) pwInput.disabled = false;
            }
        }, CONFIG.IDENTIFIER_CHECK_DEBOUNCE);

        idInput.addEventListener('input', check);
        idInput.addEventListener('paste', () => setTimeout(check, 50));
    }

    function initTokenTab() {
        const idInput  = document.getElementById('phone');
        const submitBtn = document.getElementById('sendTokenBtn')
                       || document.querySelector('#tokenForm [type="submit"]');
        if (!idInput || idInput.disabled) return;

        const hintEl = getOrCreateHint(idInput, 'token-identifier-hint');
        let lastChecked = '';

        idInput.addEventListener('input', function () {
            const cleaned = this.value.replace(/[^0-9+\s\-\(\)]/g, '');
            if (cleaned !== this.value) this.value = cleaned;
        });

        const check = debounce(async function () {
            const raw = idInput.value.trim();
            if (raw.length < CONFIG.IDENTIFIER_MIN_LENGTH) {
                renderHint(hintEl, 'idle');
                if (submitBtn) submitBtn.disabled = false;
                return;
            }
            if (!isValidPhoneNumber(raw)) {
                renderHint(hintEl, 'invalid_for_token', { message: 'Please enter a valid phone number (e.g. +256700000000).' });
                if (submitBtn) submitBtn.disabled = true;
                return;
            }
            if (raw === lastChecked) return;
            lastChecked = raw;
            renderHint(hintEl, 'checking');

            const data = await APIService.checkIdentifier(raw, 'token');
            if (idInput.value.trim() !== raw) return;

            if (!data || data.status === 'error')          { renderHint(hintEl, 'error'); return; }
            if (data.status === 'too_short')               { renderHint(hintEl, 'idle');  return; }
            if (data.status === 'invalid_for_token')       { renderHint(hintEl, 'invalid_for_token', data); if (submitBtn) submitBtn.disabled = true; return; }
            if (data.status === 'not_found')               { renderHint(hintEl, 'not_found'); if (submitBtn) submitBtn.disabled = true; return; }
            if (data.status === 'inactive')                { renderHint(hintEl, 'inactive');  if (submitBtn) submitBtn.disabled = true; return; }
            if (data.status === 'locked')                  { renderHint(hintEl, 'locked', data); if (submitBtn) submitBtn.disabled = true; startHintCountdown(hintEl, data, null, submitBtn); return; }
            if (data.status === 'otp_unavailable')         { renderHint(hintEl, 'otp_unavailable', data); if (submitBtn) submitBtn.disabled = true; return; }

            if (data.status === 'found') {
                if (!data.otp_enabled) { renderHint(hintEl, 'no_otp'); if (submitBtn) submitBtn.disabled = true; return; }
                if (!data.can_otp) {
                    renderHint(hintEl, 'otp_cooldown', data);
                    if (submitBtn) submitBtn.disabled = true;
                    if (data.otp_cooldown > 0) setTimeout(() => { lastChecked = ''; check(); }, (data.otp_cooldown + 1) * 1000);
                    return;
                }
                renderHint(hintEl, 'found', { custom: `Account found: ${data.username} — ready to receive SMS` });
                if (submitBtn) submitBtn.disabled = false;
            }
        }, CONFIG.IDENTIFIER_CHECK_DEBOUNCE);

        idInput.addEventListener('input', check);
        idInput.addEventListener('paste', () => setTimeout(check, 50));
    }

    function injectHintStyles() {
        if (document.getElementById('en-realtime-styles')) return;
        const style = document.createElement('style');
        style.id = 'en-realtime-styles';
        style.textContent = `
            .en-id-hint {
                display: none;
                align-items: center;
                gap: 6px;
                margin-top: 5px;
                font-size: 0.82rem;
                line-height: 1.4;
                padding: 5px 10px;
                border-radius: 6px;
                background: rgba(255,255,255,0.05);
                border: 1px solid rgba(255,255,255,0.08);
                transition: opacity 0.2s ease;
            }
            .en-hint-icon { font-size: 0.9rem; flex-shrink: 0; }
            .en-hint-text { flex: 1; }
            #phone.valid   { border-color: #3ddc84; box-shadow: 0 0 0 2px rgba(61,220,132,0.2); }
            #phone.invalid { border-color: #ff5f57; box-shadow: 0 0 0 2px rgba(255,95,87,0.2); }
        `;
        document.head.appendChild(style);
    }

    function init() {
        injectHintStyles();
        initPasswordTab();
        initTokenTab();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

// ============================================
// Helper Functions
// ============================================

function dismissAlert(btn) {
    const alert = btn.closest('.alert');
    if (alert) {
        alert.style.opacity = '0';
        setTimeout(() => alert.remove(), 300);
    }
}

function showNotification(message, type = 'info', duration = 5000) {
    let container = document.getElementById('notifications');
    if (!container) {
        container = document.createElement('div');
        container.id = 'notifications';
        document.body.appendChild(container);
    }
    const icons = { success: 'fa-check-circle', error: 'fa-exclamation-circle',
                    warning: 'fa-exclamation-triangle', info: 'fa-info-circle' };
    const notification = document.createElement('div');
    notification.className = `notification notification-${type}`;
    notification.innerHTML = `
        <i class="fas ${icons[type] || icons.info}"></i>
        <span>${escapeHtml(message)}</span>
        <button class="notification-close"><i class="fas fa-times"></i></button>
    `;
    container.appendChild(notification);
    notification.querySelector('.notification-close').addEventListener('click', () => {
        notification.style.animation = 'slideOutRight 0.3s ease-out';
        setTimeout(() => notification.remove(), 300);
    });
    setTimeout(() => {
        if (notification.parentNode) {
            notification.style.animation = 'slideOutRight 0.3s ease-out';
            setTimeout(() => notification.remove(), 300);
        }
    }, duration);
}

// ============================================
// Theme Management
// ============================================

function initTheme() {
    const themeToggle = document.getElementById('themeToggle');
    if (!themeToggle) return;
    if (localStorage.getItem('theme') === 'light') document.body.classList.add('light-mode');
    themeToggle.addEventListener('click', () => {
        document.body.classList.toggle('light-mode');
        localStorage.setItem('theme', document.body.classList.contains('light-mode') ? 'light' : 'dark');
    });
}

// ============================================
// Password Strength Meter
// ============================================

function initPasswordStrength() {
    const passwordInput = document.querySelector('input[name="password"]');
    if (!passwordInput) return;
    passwordInput.addEventListener('input', function () {
        const v = this.value;
        let strength = 0;
        if (v.length >= 8)          strength++;
        if (v.length >= 12)         strength++;
        if (/[A-Z]/.test(v))        strength++;
        if (/[a-z]/.test(v))        strength++;
        if (/[0-9]/.test(v))        strength++;
        if (/[^A-Za-z0-9]/.test(v)) strength++;
        document.querySelectorAll('.strength-bar').forEach((bar, i) => {
            bar.classList.toggle('active', i < Math.min(strength, 4));
        });
        const strengthText = document.getElementById('strengthText');
        if (strengthText) {
            const texts = ['Very Weak', 'Weak', 'Fair', 'Good', 'Strong', 'Very Strong'];
            strengthText.textContent = texts[Math.min(strength, 5)] || 'Very Weak';
        }
    });
}

// ============================================
// Password Visibility Toggle
// ============================================

function initPasswordVisibility() {
    document.querySelectorAll('.toggle-password').forEach(btn => {
        btn.addEventListener('click', () => {
            const input = btn.closest('.password-container')?.querySelector('input');
            if (!input) return;
            const isPassword = input.type === 'password';
            input.type = isPassword ? 'text' : 'password';
            const icon = btn.querySelector('i');
            if (icon) {
                icon.classList.toggle('fa-eye',      !isPassword);
                icon.classList.toggle('fa-eye-slash', isPassword);
            }
        });
    });
}

// ============================================
// Typing Effect
// ============================================

function initTypingEffect() {
    const typingText = document.getElementById('typingText');
    if (!typingText) return;
    let isDeleting = false, textIndex = 0;
    const type = () => {
        const current = DYNAMIC_TEXTS[textIndex];
        if (!current) return;
        typingText.textContent = isDeleting
            ? current.text.substring(0, typingText.textContent.length - 1)
            : current.text.substring(0, typingText.textContent.length + 1);
        let speed = isDeleting ? CONFIG.TYPING_SPEED / 2 : CONFIG.TYPING_SPEED;
        if (!isDeleting && typingText.textContent === current.text) {
            speed = CONFIG.TYPING_PAUSE; isDeleting = true;
        } else if (isDeleting && typingText.textContent === '') {
            isDeleting = false;
            textIndex = (textIndex + 1) % DYNAMIC_TEXTS.length;
            speed = 500;
        }
        setTimeout(type, speed);
    };
    type();
}

// ============================================
// Auto-dismiss Flash Messages
// ============================================

function initFlashMessages() {
    document.querySelectorAll('.flash-message').forEach(msg => {
        setTimeout(() => {
            if (msg.parentNode) {
                msg.style.opacity = '0';
                setTimeout(() => msg.remove(), 300);
            }
        }, CONFIG.AUTO_REMOVE_MESSAGES_DELAY);
    });
}

// ============================================
// Token Timer
// ============================================

function initTokenTimer() {
    const expiryTimer = document.getElementById('expiryTimer');
    const progressBar = document.getElementById('timerProgressBar');
    if (!expiryTimer) return;
    let timeLeft = CONFIG.TOKEN_EXPIRY_TIME;
    const timer = setInterval(() => {
        timeLeft--;
        expiryTimer.textContent = timeLeft;
        if (progressBar) progressBar.style.width = `${(timeLeft / CONFIG.TOKEN_EXPIRY_TIME) * 100}%`;
        if (timeLeft <= 30) {
            expiryTimer.style.color = 'var(--color-danger)';
            if (progressBar) progressBar.style.background = 'var(--color-danger)';
        } else if (timeLeft <= 60) {
            expiryTimer.style.color = 'var(--color-warning)';
            if (progressBar) progressBar.style.background = 'var(--color-warning)';
        }
        if (timeLeft <= 0) { clearInterval(timer); showNotification('Token expired. Please request a new one.', 'warning'); }
    }, 1000);
}

// ============================================
// Skeleton Loader
// ============================================

function removeSkeleton() {
    const skeleton = document.getElementById('skeleton');
    if (skeleton) {
        skeleton.style.opacity = '0';
        setTimeout(() => skeleton.remove(), CONFIG.ANIMATION_DURATION);
    }
}

// ============================================
// Particle Background
// ============================================

function initParticles() {
    const canvas = document.getElementById('particles-background');
    if (!canvas?.getContext) return;
    const ctx = canvas.getContext('2d');
    const particles = [];
    function resize() { canvas.width = window.innerWidth; canvas.height = window.innerHeight; }
    window.addEventListener('resize', resize);
    resize();
    for (let i = 0; i < 50; i++) {
        particles.push({
            x: Math.random() * canvas.width, y: Math.random() * canvas.height,
            radius: Math.random() * 2 + 0.5, alpha: Math.random() * 0.4,
            vx: (Math.random() - 0.5) * 0.3, vy: (Math.random() - 0.5) * 0.3
        });
    }
    function draw() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#ffffff';
        particles.forEach(p => {
            ctx.globalAlpha = p.alpha;
            ctx.beginPath();
            ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
            ctx.fill();
            p.x += p.vx; p.y += p.vy;
            if (p.x < 0) p.x = canvas.width;  if (p.x > canvas.width) p.x = 0;
            if (p.y < 0) p.y = canvas.height; if (p.y > canvas.height) p.y = 0;
        });
        requestAnimationFrame(draw);
    }
    draw();
}

// ============================================
// Pre-submit Guard
// ============================================

function initFormValidation() {
    const passwordForm = document.getElementById('passwordForm');
    if (!passwordForm) return;
    passwordForm.addEventListener('submit', async function (e) {
        const identifier = document.getElementById('identifier')?.value.trim();
        if (!identifier) return true;
        const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';
        const status = await APIService.checkUserStatus(identifier, csrfToken);
        if (status.exists && status.is_locked) {
            e.preventDefault();
            const endMs = status.locked_until_ms || (Date.now() + status.remaining_seconds * 1000);
            LockoutClock.show(identifier, endMs, status.total_duration_seconds);
            showNotification('Account is locked. Please wait for the timer to expire.', 'warning');
            return false;
        }
        return true;
    });
}

// ============================================
// Initialization
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initPasswordStrength();
    initPasswordVisibility();
    initTypingEffect();
    initFlashMessages();
    initTokenTimer();
    initParticles();
    initFormValidation();

    // Boot Socket.IO first, then LockoutClock (which depends on it)
    AuthSocket.init();
    LockoutClock.init();

    removeSkeleton();

    if (!document.getElementById('notification-styles')) {
        const style = document.createElement('style');
        style.id = 'notification-styles';
        style.textContent = `
            #notifications { position:fixed; top:20px; right:20px; z-index:10000; display:flex; flex-direction:column; gap:10px; }
            .notification { padding:16px 24px; color:white; border-radius:12px; display:flex; align-items:center; gap:12px; animation:slideInRight 0.3s ease-out; max-width:400px; box-shadow:0 4px 15px rgba(0,0,0,0.2); }
            .notification-success { background:var(--color-success,#10b981); }
            .notification-error   { background:var(--color-danger, #ef4444); }
            .notification-warning { background:var(--color-warning,#f59e0b); }
            .notification-info    { background:var(--color-info,   #06b6d4); }
            .notification-close { background:rgba(255,255,255,0.2); border:none; color:white; cursor:pointer; padding:4px 8px; border-radius:8px; margin-left:auto; }
            .notification-close:hover { background:rgba(255,255,255,0.3); }
            @keyframes slideInRight  { from{transform:translateX(100%);opacity:0} to{transform:translateX(0);opacity:1} }
            @keyframes slideOutRight { to{transform:translateX(100%);opacity:0} }
            @keyframes fadeInUp      { from{opacity:0;transform:translateY(20px)} to{opacity:1;transform:translateY(0)} }
            @keyframes fadeOutDown   { to{opacity:0;transform:translateY(20px)} }
            @keyframes pulse         { 0%,100%{opacity:1} 50%{opacity:0.7} }
            .lockout-hidden { display:none; }
            .btn-locked     { opacity:0.6; cursor:not-allowed; pointer-events:none; }
            .lockout-clock-overlay { position:relative; background:var(--glass-dark); backdrop-filter:blur(10px); border-radius:var(--radius-lg); padding:20px; margin:20px 0; text-align:center; animation:fadeInUp 0.4s ease-out; border:1px solid var(--glass-border); }
            .lockout-clock-overlay.hiding { animation:fadeOutDown 0.4s ease-out forwards; }
            .lockout-clock-dismiss { position:absolute; top:12px; right:12px; background:rgba(255,255,255,0.1); border:none; border-radius:50%; width:30px; height:30px; cursor:pointer; color:var(--text-secondary); transition:all 0.2s; }
            .lockout-clock-dismiss:hover { background:rgba(255,255,255,0.2); color:var(--text-primary); }
            .lockout-clock-header { display:flex; align-items:center; justify-content:center; gap:10px; margin-bottom:20px; }
            .lockout-clock-header i  { font-size:24px; color:#f59e0b; }
            .lockout-clock-header h4 { margin:0; color:var(--text-primary); }
            .lockout-svg-clock { position:relative; width:160px; height:160px; margin:0 auto 20px; }
            .lockout-clock-digits { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); text-align:center; }
            .lockout-digits-time  { font-size:28px; font-weight:700; font-family:monospace; color:var(--text-primary); }
            .lockout-digits-label { font-size:10px; color:var(--text-secondary); display:block; }
            .lockout-clock-msg  { margin:15px 0 5px; color:var(--text-primary); }
            .lockout-clock-hint { font-size:12px; color:var(--text-secondary); margin:0; }
            .lockout-account-info { margin-top:15px; padding-top:10px; border-top:1px solid var(--glass-border); font-size:12px; color:var(--text-secondary); display:flex; align-items:center; justify-content:center; gap:8px; }
        `;
        document.head.appendChild(style);
    }
});

// ============================================
// Global exports
// ============================================
window.showNotification = showNotification;
window.dismissAlert     = dismissAlert;
window.APIService       = APIService;