/**
 * app.js — Main application JavaScript for Mocha web app.
 *
 * Handles:
 *  - WebSocket connections to bridge (/ws/live and /ws/monitor)
 *  - Message handling (speech_segment, speech_end, interrupt, mic_mute, etc.)
 *  - Segment queue + sequential playback
 *  - Audio decoding + playback via Web Audio API
 *  - Viseme data decoding (viseme_b64 -> per-frame 5-channel weights)
 *  - Mic capture (getUserMedia -> PCM16 16kHz -> binary WS frames)
 *  - Text input handling
 *  - Chat log UI updates
 *  - Tab switching
 *  - Pipeline status light updates
 *  - Config slider changes -> config_update via WS
 *  - Interrupt handling
 *
 * This is a non-module script. The VRM renderer (vrm-renderer.js) exposes
 * functions on `window` that this script calls:
 *   window._applyBones(boneFrame)
 *   window._applyVisemes(weights)
 *   window._applyEmotion(emotionId)
 *   window._resetBones()
 *   window._clearLipSync()
 *   window._setupAnalyser(audioCtx, sourceNode) -> {analyser, data}
 *   window._clearVRM()
 *   window._loadVRMFromInput(inputEl)
 */

// ============================================================================
//  Configuration — fetched from /api/config on page load
// ============================================================================

let bridgePort = null;
let bridgeHost = null;
// Expose for presentation.js (loaded before app.js, reads these lazily)
Object.defineProperty(window, 'bridgeHost', { get: () => bridgeHost });
Object.defineProperty(window, 'bridgePort', { get: () => bridgePort });

// ============================================================================
//  DOM references
// ============================================================================

const chatLog       = document.getElementById('chatLog');
const debugBar      = document.getElementById('debugBar');
const statusDot     = document.getElementById('statusDot');
const statusText    = document.getElementById('statusText');
const textInput     = document.getElementById('textInput');
const btnMic        = document.getElementById('btnMic');

// ============================================================================
//  State
// ============================================================================

// -- WebSockets --
let wsLive    = null;   // /ws/live — audio/text communication
let wsMonitor = null;   // /ws/monitor — pipeline status + config
let reconnectLiveTimer    = null;
let reconnectMonitorTimer = null;

// -- Segment queue + playback --
const segmentQueue    = [];
let isPlaying         = false;
let interruptRequested = false;
const _seenSegments   = new Set();  // dedup: "job_id:index"
let currentAudioSource = null;
let audioCtx           = null;

/**
 * Ensure the playback AudioContext exists and is running.
 * Chrome requires resume() after a user gesture before audio can play.
 */
function ensureAudioCtx() {
    if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioCtx.state === 'suspended') {
        audioCtx.resume();
    }
}

// Global one-shot audio unlock. Without this, on page reload the AudioContext
// starts suspended and only Send/mic toggles unlock it — any queued TTS sits
// waiting on `audioCtx.resume()` which can't resolve without a user gesture.
// Any pointer/key interaction anywhere on the page now unlocks it.
(function _installAudioUnlockOnce() {
    const unlock = () => {
        ensureAudioCtx();
        if (audioCtx && audioCtx.state !== 'suspended') {
            document.removeEventListener('pointerdown', unlock, true);
            document.removeEventListener('keydown', unlock, true);
            document.removeEventListener('touchstart', unlock, true);
        }
    };
    document.addEventListener('pointerdown', unlock, true);
    document.addEventListener('keydown', unlock, true);
    document.addEventListener('touchstart', unlock, true);
})();

// -- Mic capture --
let micStream    = null;
let micAudioCtx  = null;
let micProcessor = null;
let micActive    = false;
let micMuted     = false;

// ============================================================================
//  Utility
// ============================================================================

function escapeHtml(text) {
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/**
 * Send a JSON message over the /ws/live WebSocket.
 */
function wsSend(obj) {
    if (wsLive && wsLive.readyState === WebSocket.OPEN) {
        wsLive.send(JSON.stringify(obj));
    }
}

// Always go through the page's own host. The web app (web/app.py) proxies
// /ws/live, /ws/monitor, and /api/* to the bridge on its internal port —
// so the browser never needs to know which port the bridge listens on, and
// public deployments behind a reverse proxy (Cloudflare tunnel, nginx, etc.)
// work the same as direct LAN access.
function _bridgeWsUrl(path) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${location.host}${path}`;
}

function _bridgeHttpUrl(path) {
    return `${location.protocol}//${location.host}${path}`;
}

// ============================================================================
//  Presence — session id, activity tracking, page visibility
// ============================================================================

const SESSION_ID_KEY = 'parrot.session_id';
const LAST_SEEN_KEY  = 'parrot.last_seen_at';
const JWT_KEY        = 'parrot.jwt';
const IDLE_MS        = 60 * 1000;   // report idle after 60s of no interaction

function getJwt() {
    try { return localStorage.getItem(JWT_KEY) || ''; } catch (_) { return ''; }
}

function authHeaders() {
    const t = getJwt();
    return t ? { 'Authorization': `Bearer ${t}` } : {};
}

/**
 * Welcome-first onboarding: claim (or create) an anonymous profile keyed to
 * this browser's persistent session_id. Backend resolves the same user across
 * visits, links new IPs to the same account, and seeds a fresh per-user
 * data/users/{uid}/ directory on first contact.
 *
 * Returns true on success (JWT now in localStorage), false on network error
 * — caller falls back to the manual /login page.
 */
async function tryAnonBootstrap() {
    try {
        const r = await fetch('/api/auth/anon', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                browser_token: getSessionId(),
                user_agent: navigator.userAgent || null,
            }),
        });
        if (!r.ok) return false;
        const j = await r.json();
        if (!j || !j.access_token) return false;
        localStorage.setItem(JWT_KEY, j.access_token);
        return true;
    } catch (_) {
        return false;
    }
}

function _randId() {
    try {
        if (crypto && crypto.randomUUID) return crypto.randomUUID();
    } catch (_) {}
    return 'sid_' + Math.random().toString(36).slice(2, 14) + Date.now().toString(36);
}

function getSessionId() {
    try {
        let sid = localStorage.getItem(SESSION_ID_KEY);
        if (!sid) {
            sid = _randId();
            localStorage.setItem(SESSION_ID_KEY, sid);
        }
        return sid;
    } catch (_) {
        return _randId();
    }
}

function getLastSeenIso() {
    try { return localStorage.getItem(LAST_SEEN_KEY) || ''; } catch (_) { return ''; }
}

function touchLastSeen() {
    try { localStorage.setItem(LAST_SEEN_KEY, new Date().toISOString()); } catch (_) {}
}

let _presenceIdleTimer = null;
let _presenceIsIdle    = false;
let _presenceHeartbeat = null;

function _reportActive() {
    if (_presenceIsIdle) {
        _presenceIsIdle = false;
        wsSend({ type: 'user_active' });
    }
    if (_presenceIdleTimer) clearTimeout(_presenceIdleTimer);
    _presenceIdleTimer = setTimeout(() => {
        _presenceIsIdle = true;
        wsSend({ type: 'user_idle' });
    }, IDLE_MS);
}

function initPresence() {
    for (const evt of ['keydown', 'mousedown', 'touchstart']) {
        document.addEventListener(evt, _reportActive, { passive: true });
    }
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            wsSend({ type: 'page_hidden' });
        } else {
            wsSend({ type: 'page_visible' });
            _reportActive();
        }
    });
    // Tab closing — best-effort nudge to the bridge so it can finalise the
    // diary page before the process drops us. WS send isn't guaranteed on
    // unload; we also send a sendBeacon fallback to a cheap endpoint so the
    // server at least gets *some* trigger.
    window.addEventListener('beforeunload', () => {
        try { wsSend({ type: 'page_closing' }); } catch (e) {}
        try {
            if (navigator.sendBeacon) {
                const blob = new Blob([JSON.stringify({reason: 'beforeunload'})],
                                       { type: 'application/json' });
                navigator.sendBeacon('/api/diary/write-now', blob);
            }
        } catch (e) {}
    });
    // Periodic last_seen ping so reconnect_hello can gauge how long we were away.
    _presenceHeartbeat = setInterval(touchLastSeen, 30 * 1000);
    touchLastSeen();
    _reportActive();
}

/**
 * Request that Mocha temporarily stop autonomous check-ins (reconnect hello,
 * drift riffs, bored/lonely pings). The user-triggered "hush" path.
 */
function hushMocha(durationS = 600) {
    wsSend({ type: 'mocha_mute', duration_s: durationS });
}
window.hushMocha = hushMocha;

// ============================================================================
//  WebSocket — /ws/live (audio + text communication)
// ============================================================================

function connectLive() {
    if (!bridgePort) return;
    if (wsLive && wsLive.readyState <= WebSocket.OPEN) return;

    const _tok = getJwt();
    const url = _bridgeWsUrl('/ws/live') + (_tok ? `?token=${encodeURIComponent(_tok)}` : '');
    wsLive = new WebSocket(url);
    wsLive.binaryType = 'arraybuffer';

    wsLive.onopen = () => {
        setStatus('connected', 'Connected');
        if (debugBar) debugBar.textContent = 'Connected to bridge';
        // Announce ourselves so the server can deliver any findings queued while
        // we were away, with our persistent session id + last-seen timestamp.
        wsSend({
            type: 'client_hello',
            session_id: getSessionId(),
            last_seen_at: getLastSeenIso(),
        });
        touchLastSeen();

        // Report the user's timezone / locale so the bridge can render time in
        // the user's local zone (not the server's). Optionally include
        // geolocation on HTTPS / localhost — fails silently on plain HTTP.
        try {
            const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
            const locale = navigator.language || 'en-US';
            wsSend({ type: 'user_context', tz, locale });
            if (navigator.geolocation && !localStorage.getItem('parrot.geo.disabled')) {
                navigator.geolocation.getCurrentPosition(
                    (p) => wsSend({
                        type: 'user_context',
                        tz, locale,
                        lat: p.coords.latitude,
                        lng: p.coords.longitude,
                        accuracy_m: p.coords.accuracy,
                    }),
                    () => {},  // silent on permission denied / HTTP
                    { timeout: 8000, maximumAge: 3600000 }
                );
            }
        } catch (e) { /* Intl unavailable on very old browsers */ }
    };

    wsLive.onmessage = (e) => {
        if (typeof e.data === 'string') {
            try {
                const msg = JSON.parse(e.data);
                handleLiveMessage(msg);
            } catch (err) {
                console.error('Live WS parse error:', err);
            }
        }
        // Binary messages from the bridge are not expected on /ws/live client side
    };

    wsLive.onclose = () => {
        setStatus('disconnected', 'Disconnected');
        scheduleLiveReconnect();
    };

    wsLive.onerror = () => {
        setStatus('disconnected', 'Connection error');
        scheduleLiveReconnect();
    };
}

function scheduleLiveReconnect() {
    if (reconnectLiveTimer) return;
    reconnectLiveTimer = setTimeout(() => {
        reconnectLiveTimer = null;
        connectLive();
    }, 3000);
}

// ============================================================================
//  WebSocket — /ws/monitor (pipeline status + config)
// ============================================================================

function connectMonitor() {
    if (!bridgePort) return;
    if (wsMonitor && wsMonitor.readyState <= WebSocket.OPEN) return;

    const url = _bridgeWsUrl('/ws/monitor');
    wsMonitor = new WebSocket(url);

    wsMonitor.onopen = () => {
        console.log('Monitor WS connected');
    };

    wsMonitor.onmessage = (e) => {
        if (typeof e.data === 'string') {
            try {
                const msg = JSON.parse(e.data);
                handleMonitorMessage(msg);
            } catch (err) {
                console.error('Monitor WS parse error:', err);
            }
        }
    };

    wsMonitor.onclose = () => {
        scheduleMonitorReconnect();
    };

    wsMonitor.onerror = () => {
        scheduleMonitorReconnect();
    };
}

function scheduleMonitorReconnect() {
    if (reconnectMonitorTimer) return;
    reconnectMonitorTimer = setTimeout(() => {
        reconnectMonitorTimer = null;
        connectMonitor();
    }, 3000);
}

// User-triggered reconnect — bound to the status dot. Cancels pending
// backoff timers and reopens both sockets immediately.
function forceReconnect() {
    if (reconnectLiveTimer)    { clearTimeout(reconnectLiveTimer);    reconnectLiveTimer = null; }
    if (reconnectMonitorTimer) { clearTimeout(reconnectMonitorTimer); reconnectMonitorTimer = null; }
    try { if (wsLive)    wsLive.close(); }    catch (e) {}
    try { if (wsMonitor) wsMonitor.close(); } catch (e) {}
    wsLive = null;
    wsMonitor = null;
    setStatus('disconnected', 'Reconnecting…');
    connectLive();
    connectMonitor();
}
window.forceReconnect = forceReconnect;

// ============================================================================
//  Message handling — /ws/live
// ============================================================================

function handleLiveMessage(msg) {
    // TEMP diag: log every Live WS message type so we can see whether
    // presence_active / presence_inactive arrive at all on the inactive device.
    if (msg && msg.type) {
        const t = msg.type;
        if (t === 'presence_active' || t === 'presence_inactive' ||
            t === 'quota_exceeded') {
            console.log('[ws]', t, msg);
        }
    }
    switch (msg.type) {
        case 'speech_segment': {
            // Dedup: bridge sends same segment twice (direct + broadcast)
            const segKey = `${msg.job_id}:${msg.index}`;
            if (_seenSegments.has(segKey)) break;
            _seenSegments.add(segKey);
            segmentQueue.push(msg);
            // Route chat bubble through orchestrator (sync text with audio)
            const extras = msg.autonomous ? { autonomous: true } : undefined;
            if (window.UIOrchestrator) {
                window.UIOrchestrator.interceptChat('mocha', msg.text, msg.emotion, extras);
            } else {
                appendChat('mocha', msg.text, msg.emotion, extras);
            }
            playNext();
            break;
        }

        case 'speech_end':
            // Clear dedup set for completed job
            for (const key of _seenSegments) {
                if (key.startsWith(msg.job_id + ':')) _seenSegments.delete(key);
            }
            break;

        case 'interrupt':
            cancelPlayback();
            break;

        case 'mic_mute':
            micMuted = msg.muted;
            break;

        case 'debug_state':
            updateDebug(msg);
            break;

        case 'echo_mode':
            console.log('Echo mode:', msg.mode);
            break;

        case 'ui_command':
            if (window.UIOrchestrator) {
                window.UIOrchestrator.intercept(msg);
            } else if (window._presentation) {
                window._presentation.handleUiCommand(msg);
            }
            break;

        // chat_entry handled only on /ws/monitor to avoid duplicates

        // ── Single-active-Mocha presence ──────────────────────────────────
        // Bridge sends these on the Live WS (because that's where the per-
        // session socket is registered). When this device is told it's
        // inactive, slide Mocha off + mute. presence_active swings it back.
        case 'presence_active':
            applyPresence(true);
            break;

        case 'presence_inactive':
            applyPresence(false);
            break;

        // ── Quota cap hit (anonymous users only) ──────────────────────────
        case 'quota_exceeded':
            handleQuotaExceeded(msg);
            break;

        default:
            // Unknown message type — ignore
            break;
    }
}

// ============================================================================
//  Message handling — /ws/monitor
// ============================================================================

function handleMonitorMessage(msg) {
    switch (msg.type) {
        case 'thread_status':
            updateThreadStatus(msg);
            break;

        case 'timeline_event':
            recordTimelineEvent(msg);
            break;

        case 'tool_activity':
            appendToolActivity(msg);
            break;

        case 'config_state':
            applyConfigState(msg);
            break;

        case 'echo_mode':
            updateEchoModeUI(msg.mode);
            break;

        case 'shiro_state':
            updateShiroUI(msg.running);
            break;

        case 'chat_entry':
            if (msg.entry) {
                handleChatEntry(msg.entry);
            }
            break;

        case 'memory_cleared':
            if (debugBar) debugBar.textContent = 'Memory cleared';
            break;

        case 'agent_thought':
            appendThinkingEntry(msg);
            break;

        // presence_active / presence_inactive / quota_exceeded are dispatched
        // on the Live WS, not Monitor — they're handled in handleLiveMessage.

        default:
            break;
    }
}

// ============================================================================
//  Single-active-Mocha presence — applyPresence + claim_active wake
// ============================================================================

let _isPresent = true;  // optimistic; flipped by presence_inactive

function applyPresence(active) {
    console.log('[presence] applyPresence(%s) — body class before:', active,
                document.body.className);
    _isPresent = !!active;
    if (active) {
        document.body.classList.remove('mocha-away');
        // Resume TTS playback context + unmute mic capture.
        try { if (typeof audioCtx !== 'undefined' && audioCtx) audioCtx.resume(); } catch (_) {}
        try { micMuted = false; } catch (_) {}
    } else {
        document.body.classList.add('mocha-away');
        try { if (typeof audioCtx !== 'undefined' && audioCtx) audioCtx.suspend(); } catch (_) {}
        try { micMuted = true; } catch (_) {}
        // Stop any in-flight TTS playback so we don't keep speaking on the
        // device the user just walked away from.
        try { if (typeof cancelPlayback === 'function') cancelPlayback(); } catch (_) {}
    }
    console.log('[presence] applyPresence(%s) — body class after:', active,
                document.body.className,
                '| #canvas3d display:',
                getComputedStyle(document.getElementById('canvas3d')).display);
}

/**
 * Wake-from-away handler. Any user gesture on this device while Mocha is
 * away should claim her back. We hook the existing presence "_reportActive"
 * fan-out plus a direct click on the away pill.
 */
function claimMochaActive() {
    if (_isPresent) return;
    // Optimistic: flip UI immediately so the response feels instant. If the
    // server overrides us (race with another device), it'll send presence_inactive.
    applyPresence(true);
    wsSend({ type: 'claim_active' });
}
window.claimMochaActive = claimMochaActive;

// Attach: clicks on the away pill, and any pointerdown on the canvas area
// while away. Initialized once after DOM is ready.
function initPresenceClaim() {
    const pill = document.getElementById('mochaAwayPill');
    if (pill) pill.addEventListener('click', claimMochaActive);
    // Catch-all: any pointerdown/keydown while away → claim.
    const onAnyInput = () => {
        if (document.body.classList.contains('mocha-away')) claimMochaActive();
    };
    window.addEventListener('pointerdown', onAnyInput, { passive: true, capture: true });
    window.addEventListener('keydown', onAnyInput, { passive: true, capture: true });
}

// ============================================================================
//  iOS keyboard tracking — keep Mocha visible when the soft keyboard opens
// ============================================================================
/*
 * iOS Safari shifts the entire layout viewport up when the soft keyboard
 * appears, hiding the top half of the page (Mocha's face). The CSS in
 * style.css locks html/body with position:fixed so iOS can't auto-scroll
 * us. This listener uses the visualViewport API to measure how tall the
 * keyboard is, and sets --kb-offset on the document root. The CSS bottom:
 * calc(... + var(--kb-offset)) on .phone-input-bar (and .chat-sidebar)
 * lifts only those elements above the keyboard while Mocha stays put.
 */
function initKeyboardTracker() {
    if (!window.visualViewport) return;  // Pre-iOS 13 / older browsers
    const vv = window.visualViewport;

    // Set --vvh = window.visualViewport.height (the actual visible area
    // including the keyboard subtraction on iOS). Used by .main-layout in
    // CSS to size itself precisely, so the flex column reflows correctly
    // when the keyboard opens. iOS Safari's dvh unit alone doesn't track
    // the keyboard — visualViewport does.
    function update() {
        const root = document.documentElement;
        root.style.setProperty('--vvh', vv.height + 'px');
        const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        root.style.setProperty('--kb-offset', kb + 'px');
        document.body.classList.toggle('keyboard-open', kb > 50);
    }

    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    window.addEventListener('resize', update);   // orientation change, etc.
    update();

    // Pin scroll if iOS tries to drift it on focus, and re-run sizing
    // afterwards in case visualViewport changed.
    function _pinScroll() {
        if (window.scrollY !== 0 || window.scrollX !== 0) {
            window.scrollTo(0, 0);
        }
        update();
    }
    window.addEventListener('scroll', _pinScroll, { passive: true });
    document.addEventListener('focusin', () => {
        // Defer past iOS's auto-scroll-into-view, then re-measure twice
        // (some iOS versions report stale values for one frame).
        requestAnimationFrame(() => requestAnimationFrame(() => {
            _pinScroll();
            requestAnimationFrame(update);
        }));
    });
    document.addEventListener('focusout', () => {
        // After the keyboard dismisses, iOS sometimes reports stale
        // visualViewport.height for ~100ms. Re-measure a few times.
        for (const ms of [50, 150, 300]) setTimeout(update, ms);
    });
}

// ============================================================================
//  Quota cap UX — refusal toast + Account tab nudge
// ============================================================================

function handleQuotaExceeded(msg) {
    const refusal = msg.message || 'hit my limit for guest chats today.';
    // Render the refusal as a Mocha message in the chat (no audio).
    try {
        if (typeof appendChat === 'function') {
            appendChat('mocha', refusal, 'thinking');
        }
    } catch (_) {}

    // Flash the Account tab badge.
    const badge = document.getElementById('accountBadge');
    if (badge) badge.style.display = 'inline-block';

    // Open Settings → Account so the sign-up form is right there.
    openAccountTab();

    // Refresh /me so the Account tab shows the maxed-out usage bar.
    refreshAccountState();
}

function openAccountTab() {
    const backdrop = document.getElementById('modalBackdrop');
    if (backdrop) backdrop.classList.add('open');
    document.querySelectorAll('.modal-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === 'account');
    });
    document.querySelectorAll('.modal-content').forEach(c => {
        c.classList.toggle('active', c.id === 'modal-account');
    });
    refreshAccountState();
}

/**
 * Pull /api/auth/me and reflect it in the Account tab — anon vs registered
 * view, and current quota usage. Cheap; safe to call on any modal open.
 */
async function refreshAccountState() {
    let me;
    try {
        const r = await fetch('/api/auth/me', { headers: authHeaders() });
        if (!r.ok) return;
        me = await r.json();
    } catch (_) {
        return;
    }
    const anonBox = document.getElementById('acctAnonSection');
    const regBox = document.getElementById('acctRegisteredSection');
    if (!anonBox || !regBox) return;

    if (me.account_type === 'anon') {
        anonBox.style.display = '';
        regBox.style.display = 'none';
        const used = me.tokens_used_today || 0;
        const cap = me.daily_token_cap || 0;
        const lbl = document.getElementById('acctUsageLabel');
        if (lbl) lbl.textContent = `${used.toLocaleString()} / ${cap.toLocaleString()}`;
        const fill = document.getElementById('acctUsageFill');
        if (fill) {
            const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
            fill.style.width = pct + '%';
        }
    } else {
        anonBox.style.display = 'none';
        regBox.style.display = '';
        const u = document.getElementById('acctUsername');
        if (u) u.textContent = me.username || me.email || '—';
    }
}

/**
 * Wire up the Account tab:
 *   - Sub-tab toggle (Sign Up / Sign In) inside the anon view
 *   - Sign-up form: anon → registered upgrade (single username field)
 *   - Sign-in form: claim an existing registered account from this browser
 *   - "Reset guest profile": drop browser_token + JWT, fresh anon on reload
 *   - Sign-out (registered view): clear JWT + browser_token, reload
 * Call once on init after the modal DOM is present.
 */
function setupAccountTab() {
    // ── Sub-tab toggle (Sign Up / Sign In) ────────────────────────────
    // Use event delegation on the document so this keeps working even if the
    // Account modal DOM gets re-rendered, and so iOS Safari's touch events
    // reliably reach the handler (direct .addEventListener on a <button>
    // inside a <form> can be missed if the form's submit fires first).
    document.addEventListener('click', (ev) => {
        const btn = ev.target.closest('.acct-subtab');
        if (!btn) return;
        ev.preventDefault();   // belt-and-suspenders against form submit
        const target = btn.dataset.acct;
        document.querySelectorAll('.acct-subtab').forEach(b =>
            b.classList.toggle('active', b === btn));
        document.querySelectorAll('.acct-pane').forEach(p =>
            p.style.display = (p.dataset.acctPane === target ? '' : 'none'));
        // Clear any stale error from the other pane
        ['acctSignupError', 'acctSigninError'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = 'none';
        });
    });

    // ── Sign Up: POST /api/auth/upgrade { username, password } ──────────
    const signupForm = document.getElementById('acctSignupForm');
    if (signupForm) {
        signupForm.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const username = document.getElementById('acctSignupUsername').value.trim();
            const password = document.getElementById('acctSignupPassword').value;
            const errBox = document.getElementById('acctSignupError');
            const btn = document.getElementById('acctSignupBtn');
            if (errBox) errBox.style.display = 'none';
            if (btn) { btn.disabled = true; btn.textContent = 'Creating account…'; }
            try {
                const r = await fetch('/api/auth/upgrade', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...authHeaders() },
                    body: JSON.stringify({ username, password }),
                });
                const j = await r.json().catch(() => ({}));
                if (!r.ok) throw new Error(j.detail || 'Signup failed');
                if (j.access_token) localStorage.setItem(JWT_KEY, j.access_token);
                await refreshAccountState();
                const badge = document.getElementById('accountBadge');
                if (badge) badge.style.display = 'none';
            } catch (e) {
                if (errBox) {
                    errBox.textContent = (e && e.message) || 'Signup failed';
                    errBox.style.display = '';
                }
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = 'Sign up & remove cap'; }
            }
        });
    }

    // ── Sign In: POST /api/auth/login { email_or_username, password } ───
    // The current anon profile gets abandoned (left in DB unowned). Browser
    // token gets relinked to the registered user_id on next page load via
    // anon endpoint's browser_token lookup — so we proactively call it here
    // by clearing the local browser_token before reload.
    const signinForm = document.getElementById('acctSigninForm');
    if (signinForm) {
        signinForm.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const username = document.getElementById('acctSigninUsername').value.trim();
            const password = document.getElementById('acctSigninPassword').value;
            const errBox = document.getElementById('acctSigninError');
            const btn = document.getElementById('acctSigninBtn');
            if (errBox) errBox.style.display = 'none';
            if (btn) { btn.disabled = true; btn.textContent = 'Signing in…'; }
            try {
                const r = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email_or_username: username, password }),
                });
                const j = await r.json().catch(() => ({}));
                if (!r.ok) throw new Error(j.detail || 'Sign in failed');
                if (j.access_token) localStorage.setItem(JWT_KEY, j.access_token);
                // Reload so the WS reconnects with the new JWT (different user_id
                // than the anon one — needs a fresh ConversationController).
                location.reload();
            } catch (e) {
                if (errBox) {
                    errBox.textContent = (e && e.message) || 'Sign in failed';
                    errBox.style.display = '';
                }
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = 'Sign in'; }
            }
        });
    }

    // ── Reset guest profile (anon "log out") ───────────────────────────
    const resetBtn = document.getElementById('acctResetGuestBtn');
    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            if (!confirm('Drop this guest profile and start fresh? Your conversation memory will be left behind.')) return;
            try { localStorage.removeItem(JWT_KEY); } catch (_) {}
            try { localStorage.removeItem(SESSION_ID_KEY); } catch (_) {}
            location.reload();
        });
    }

    // ── Sign out (registered view) ─────────────────────────────────────
    const out = document.getElementById('acctSignOutBtn');
    if (out) {
        out.addEventListener('click', () => {
            // Drop the JWT *and* the browser_token so the next visit is a
            // fresh anon profile (otherwise we'd just re-attach the existing
            // registered user via browser_token lookup).
            try { localStorage.removeItem(JWT_KEY); } catch (_) {}
            try { localStorage.removeItem(SESSION_ID_KEY); } catch (_) {}
            location.reload();
        });
    }
}

// ============================================================================
//  Thinking panel — live scrolling stream of agent activity
// ============================================================================

const _THINKING_MAX = 40;
const _THINKING_FADE_AFTER = 30;   // older than N entries get dimmed

function appendThinkingEntry(msg) {
    const panel = document.getElementById('thinkingPanel');
    if (!panel) return;

    const src = (msg.source || 'system').toLowerCase();
    const kind = (msg.kind || '').toLowerCase();
    const text = (msg.text || '').slice(0, 240);
    if (!text) return;

    const isErr = kind === 'tool_error' || kind === 'error';
    const entry = document.createElement('div');
    entry.className = 'thinking-entry ' + src + (isErr ? ' error' : '');
    const t = new Date((msg.at || Date.now() / 1000) * 1000);
    const hh = String(t.getHours()).padStart(2, '0');
    const mm = String(t.getMinutes()).padStart(2, '0');
    const ss = String(t.getSeconds()).padStart(2, '0');

    const timeSpan = document.createElement('span');
    timeSpan.className = 'tp-time';
    timeSpan.textContent = `${hh}:${mm}:${ss}`;
    const srcSpan = document.createElement('span');
    srcSpan.className = 'tp-src';
    srcSpan.textContent = src;
    const kindSpan = document.createElement('span');
    kindSpan.className = 'tp-kind';
    kindSpan.textContent = kind || '—';
    const txtSpan = document.createElement('span');
    txtSpan.textContent = text;

    entry.appendChild(timeSpan);
    entry.appendChild(srcSpan);
    entry.appendChild(kindSpan);
    entry.appendChild(txtSpan);
    panel.appendChild(entry);

    // Trim old entries
    while (panel.children.length > _THINKING_MAX) {
        panel.firstElementChild && panel.removeChild(panel.firstElementChild);
    }
    // Fade the oldest N entries
    const total = panel.children.length;
    for (let i = 0; i < total; i++) {
        const el = panel.children[i];
        if (!el) continue;
        if (i < total - _THINKING_FADE_AFTER) {
            el.classList.add('fade');
        } else {
            el.classList.remove('fade');
        }
    }
    panel.scrollTop = panel.scrollHeight;
}

/** Optional helper: clear the stream (exposed for dev tools). */
window.clearThinkingPanel = function () {
    const p = document.getElementById('thinkingPanel');
    if (p) p.innerHTML = '';
};

// ============================================================================
//  Gantt chart — timeline visualization
// ============================================================================

const _ganttJobs = {};      // job_id -> { events: [], maxMs: 0 }
const _ganttMaxJobs = 5;    // keep last N jobs
const _ganttPhaseColors = {
    stt: '#3b82f6', llm: '#f59e0b', llm_tool: '#d97706',
    tts: '#10b981', tool: '#e94560', animation: '#8b5cf6',
    cache_warm: '#6b7280', sent_to_unity: '#6366f1', unity_play: '#a78bfa', playback: '#a78bfa',
};

function recordTimelineEvent(msg) {
    const jid = msg.job_id;
    if (!_ganttJobs[jid]) {
        _ganttJobs[jid] = { events: [], bars: {}, maxMs: 0 };
        // Prune old jobs
        const keys = Object.keys(_ganttJobs).map(Number).sort((a, b) => a - b);
        while (keys.length > _ganttMaxJobs) {
            delete _ganttJobs[keys.shift()];
        }
    }
    const job = _ganttJobs[jid];
    job.events.push(msg);

    const key = `${msg.label}:${msg.segment}`;
    if (msg.action === 'start') {
        job.bars[key] = { label: msg.label, start: msg.offset_ms, end: null, segment: msg.segment };
    } else if (msg.action === 'end' && job.bars[key]) {
        job.bars[key].end = msg.offset_ms;
        job.maxMs = Math.max(job.maxMs, msg.offset_ms);
    }

    renderGantt();
}

function renderGantt() {
    const el = document.getElementById('ganttChart');
    if (!el) return;

    const jobIds = Object.keys(_ganttJobs).map(Number).sort((a, b) => a - b);
    if (!jobIds.length) {
        el.innerHTML = '<span style="color:var(--muted);font-size:12px;">No jobs yet</span>';
        return;
    }

    // Phase display order (each gets its own row)
    const phaseOrder = ['llm', 'tool', 'llm_tool', 'tts', 'animation', 'sent_to_unity', 'unity_play', 'stt', 'cache_warm'];

    let html = '';
    for (const jid of jobIds) {
        const job = _ganttJobs[jid];
        const totalMs = Math.max(job.maxMs, 100);
        const bars = Object.values(job.bars);

        // Group bars by phase
        const byPhase = {};
        for (const bar of bars) {
            const phase = bar.label;
            if (!byPhase[phase]) byPhase[phase] = [];
            byPhase[phase].push(bar);
        }

        // Only show phases that have bars
        const activePhases = phaseOrder.filter(p => byPhase[p]?.length);

        html += `<div class="gantt-job">`;
        html += `<div class="gantt-job-label">Job #${jid} &mdash; ${(totalMs / 1000).toFixed(1)}s</div>`;

        for (const phase of activePhases) {
            const phaseBars = byPhase[phase];
            const color = _ganttPhaseColors[phase] || '#666';
            html += `<div class="gantt-row">`;
            html += `<div class="gantt-row-label" style="color:${color}">${phase}</div>`;
            html += `<div class="gantt-track">`;
            for (const bar of phaseBars) {
                const start = bar.start || 0;
                const end = bar.end || totalMs;
                const leftPct = (start / totalMs * 100).toFixed(2);
                const widthPct = Math.max(0.3, ((end - start) / totalMs * 100)).toFixed(2);
                const seg = bar.segment >= 0 ? `:${bar.segment}` : '';
                const durMs = Math.round(end - start);
                html += `<div class="gantt-bar" data-phase="${phase}" `
                      + `style="left:${leftPct}%;width:${widthPct}%" `
                      + `title="${phase}${seg} ${durMs}ms (${start.toFixed(0)}-${end.toFixed(0)}ms)">`
                      + `</div>`;
            }
            html += `</div></div>`;
        }

        html += `</div>`;
    }

    // Legend
    html += '<div class="gantt-legend">';
    for (const [phase, color] of Object.entries(_ganttPhaseColors)) {
        html += `<div class="gantt-legend-item">`
              + `<div class="gantt-legend-dot" style="background:${color}"></div>`
              + `${phase}</div>`;
    }
    html += '</div>';

    el.innerHTML = html;
}

// ============================================================================
//  Activity tab — tool call log
// ============================================================================

function appendToolActivity(msg) {
    const log = document.getElementById('activityLog');
    if (!log) return;

    // Remove empty placeholder
    const empty = log.querySelector('.activity-empty');
    if (empty) empty.remove();

    const entry = document.createElement('div');
    entry.className = 'activity-entry';

    const isCall = msg.action === 'call';
    const badge = isCall
        ? '<span class="activity-badge activity-badge-call">CALL</span>'
        : '<span class="activity-badge activity-badge-result">RESULT</span>';
    const dur = msg.duration_ms ? `<span class="activity-duration">${(msg.duration_ms / 1000).toFixed(2)}s</span>` : '';

    entry.innerHTML = `
        <div class="activity-entry-header">
            <span>
                <span class="activity-tool-name">${_escHtml(msg.tool_name || '')}</span>
                ${badge}
                <span class="activity-round">R${msg.round || 1}</span>
            </span>
            ${dur}
        </div>
        <div class="activity-body">${_escHtml(isCall ? (msg.tool_args || '{}') : (msg.result_preview || ''))}</div>
    `;

    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
}

function _escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

// Wire up Activity clear button
document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('activityClear');
    if (btn) btn.addEventListener('click', () => {
        const log = document.getElementById('activityLog');
        if (log) log.innerHTML = '<div class="activity-empty">No tool activity yet.</div>';
    });
});

// ============================================================================
//  Status display
// ============================================================================

function setStatus(state, text) {
    if (statusDot) {
        statusDot.className = 'status-dot ' + (state === 'connected' ? 'connected' : '');
    }
    if (statusText) {
        statusText.textContent = text;
    }
}

function updateDebug(msg) {
    if (!debugBar) return;
    const parts = [];
    if (msg.phase) parts.push(msg.phase);
    if (msg.llm_ms) parts.push(`LLM ${Math.round(msg.llm_ms)}ms`);
    if (msg.tts_ms) parts.push(`TTS ${Math.round(msg.tts_ms)}ms`);
    if (msg.anim_ms) parts.push(`Anim ${Math.round(msg.anim_ms)}ms`);
    if (msg.segment_count) parts.push(`${msg.segment_count} segs`);
    debugBar.textContent = parts.join(' | ') || 'Ready';
}

// ============================================================================
//  Pipeline status lights (from /ws/monitor thread_status)
// ============================================================================

/**
 * Update a pipeline thread indicator in the UI.
 * Expected msg shape:
 *   { thread: "stt", status: "processing"|"idle", elapsed_ms, input_preview,
 *     segment, total_segments }
 */
function updateThreadStatus(msg) {
    const el    = document.getElementById('thread-' + msg.thread);
    const light = document.getElementById('light-' + msg.thread);

    if (el) {
        const statusEl  = el.querySelector('.thread-status');
        const timeEl    = el.querySelector('.thread-time');
        const previewEl = el.querySelector('.thread-preview');

        if (statusEl)  statusEl.textContent  = msg.status === 'processing' ? 'Processing' : 'Idle';
        if (timeEl)    timeEl.textContent     = msg.elapsed_ms ? (msg.elapsed_ms / 1000).toFixed(1) + 's' : '';
        if (previewEl) previewEl.textContent  = msg.input_preview || '';

        el.classList.toggle('active', msg.status === 'processing');
    }

    if (light) {
        const dot = light.querySelector('.light-dot');
        if (dot) dot.classList.toggle('active', msg.status === 'processing');
    }
}

// ============================================================================
//  Text input
// ============================================================================

function sendText() {
    if (!textInput) return;
    const text = textInput.value.trim();
    if (!text) return;
    if (!wsLive || wsLive.readyState !== WebSocket.OPEN) return;

    ensureAudioCtx();  // Unlock audio on first user interaction
    wsSend({ type: 'user_input', text: text });
    appendChat('user', text);
    textInput.value = '';
}

// Expose to global scope for inline onclick handlers
window.sendText = sendText;

// ============================================================================
//  Chat log
// ============================================================================

/**
 * Append a message to the chat log UI.
 * @param {string} sender - 'mocha' or 'user'
 * @param {string} text   - Message text
 * @param {string} [emotion] - Optional emotion badge
 * @param {Object} [extras]  - Reserved for future use (thinking blocks, etc.)
 */
function appendChat(sender, text, emotion, extras) {
    if (!chatLog) return;

    const div = document.createElement('div');
    div.className = 'chat-msg';
    if (extras && extras.autonomous) {
        div.classList.add('autonomous');
    }

    const badge       = emotion ? `<span class="emotion-badge">${escapeHtml(emotion)}</span>` : '';
    const autoTag     = (extras && extras.autonomous)
        ? `<span class="autonomous-tag" title="Mocha spoke up on her own">·auto</span>`
        : '';
    const senderClass = sender === 'mocha' ? 'mocha' : 'user';
    const name        = sender === 'mocha' ? 'Mocha' : 'You';

    div.innerHTML = `<span class="sender ${senderClass}">${name}${badge}${autoTag}</span>`
                  + `<div class="msg-text">${escapeHtml(text)}</div>`;

    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
}

// ============================================================================
//  Global Chat Popup — cross-platform conversation feed
//  Features: chat history, live updates, per-segment audio, thinking blocks,
//  tool status, expandable raw JSON detail, SSE streaming, source badges
// ============================================================================

const GCHAT_MAX = 50;
const GCHAT_CLIENT_ID = Math.random().toString(36).slice(2);
let gchatExchanges = [];
let gchatAudio = null;
let gchatLoaded = false;
let gchatStreamActive = false;

function handleChatEntry(entry) {
    // Skip our own SSE-streamed entries to avoid duplication
    if (entry._client_id === GCHAT_CLIENT_ID) return;
    gchatExchanges.push(entry);
    _gchatTrim();
    _gchatAppendExchange(entry, gchatExchanges.length - 1);
}

// -- Rendering helpers --

function _gchatSourceTag(src) {
    const s = escapeHtml(src || 'unknown');
    return `<span class="gchat-source ${s}">${s}</span>`;
}

function _gchatSegHtml(seg, exIdx, segIdx) {
    const emo = seg.emotion ? `<span class="gchat-emotion">${escapeHtml(seg.emotion)}</span>` : '';
    const play = seg.audio_base64
        ? ` <button class="gchat-seg-play" onclick="gchatPlaySeg(${exIdx},${segIdx})">&#x1f50a;</button>` : '';
    return `<div class="gchat-msg assistant"><div class="gchat-bubble">${emo}${escapeHtml(seg.text)}${play}</div></div>`;
}

function _gchatRenderAll() {
    const el = document.getElementById('gchatMessages');
    if (!el) return;
    if (gchatExchanges.length === 0) {
        el.innerHTML = '<div class="gchat-empty">No conversations yet</div>';
        return;
    }
    let h = '';
    gchatExchanges.forEach((ex, i) => {
        h += `<div class="gchat-exchange" id="gex-${i}">`;
        h += `<div class="gchat-msg user">${_gchatSourceTag(ex.source)}<div class="gchat-bubble">${escapeHtml(ex.user_text || '')}</div></div>`;
        h += `<div class="gchat-seg-group" id="gsg-${i}">`;
        const segs = ex.segments || [];
        if (segs.length > 0) {
            segs.forEach((seg, si) => { h += _gchatSegHtml(seg, i, si); });
            h += `<div class="gchat-actions">`;
            h += `<button class="gchat-action-btn" id="gplay-${i}" onclick="gchatPlayAll(${i})">&#x1f50a; Play</button>`;
            h += `<button class="gchat-action-btn" id="gdetail-btn-${i}" onclick="gchatToggleDetail(${i})">&#x25b6; Detail</button>`;
            h += `</div>`;
            h += `<div class="gchat-detail" id="gdetail-${i}"></div>`;
        } else if (ex.assistant_text) {
            h += `<div class="gchat-msg assistant"><div class="gchat-bubble">${escapeHtml(ex.assistant_text)}</div></div>`;
        }
        h += `</div></div>`;
    });
    el.innerHTML = h;
    el.scrollTop = el.scrollHeight;
}

function _gchatAppendExchange(ex, i) {
    const el = document.getElementById('gchatMessages');
    if (!el) return;
    const empty = el.querySelector('.gchat-empty');
    if (empty) empty.remove();

    const div = document.createElement('div');
    div.className = 'gchat-exchange';
    div.id = `gex-${i}`;

    let h = `<div class="gchat-msg user">${_gchatSourceTag(ex.source)}<div class="gchat-bubble">${escapeHtml(ex.user_text || '')}</div></div>`;
    h += `<div class="gchat-seg-group" id="gsg-${i}">`;
    const segs = ex.segments || [];
    segs.forEach((seg, si) => { h += _gchatSegHtml(seg, i, si); });
    if (segs.length > 0) {
        h += `<div class="gchat-actions">`;
        h += `<button class="gchat-action-btn" id="gplay-${i}" onclick="gchatPlayAll(${i})">&#x1f50a; Play</button>`;
        h += `<button class="gchat-action-btn" id="gdetail-btn-${i}" onclick="gchatToggleDetail(${i})">&#x25b6; Detail</button>`;
        h += `</div>`;
        h += `<div class="gchat-detail" id="gdetail-${i}"></div>`;
    }
    h += `</div>`;
    div.innerHTML = h;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
    _gchatTrimDOM();
}

function _gchatTrim() {
    while (gchatExchanges.length > GCHAT_MAX) gchatExchanges.shift();
}

function _gchatTrimDOM() {
    const el = document.getElementById('gchatMessages');
    if (!el) return;
    const items = el.querySelectorAll('.gchat-exchange');
    while (items.length > GCHAT_MAX) items[0].remove();
}

// -- Load history from bridge --

async function gchatLoadHistory() {
    if (!bridgePort || gchatLoaded) return;
    try {
        const resp = await fetch(_bridgeHttpUrl(`/api/conversation?limit=${GCHAT_MAX}`), { headers: authHeaders() });
        if (!resp.ok) return;
        const data = await resp.json();
        gchatExchanges = data.exchanges || [];
        gchatLoaded = true;
        _gchatRenderAll();
    } catch (e) {
        console.log('Global chat history load failed:', e.message);
    }
}

// -- SSE send: stream to /chat/stream --

async function gchatSend() {
    const input = document.getElementById('gchatInput');
    const text = (input?.value || '').trim();
    if (!text || gchatStreamActive) return;
    input.value = '';
    gchatStreamActive = true;

    gchatExchanges.push({ user_text: text, source: 'web', segments: [], tool_events: [] });
    _gchatTrim();
    const exIdx = gchatExchanges.length - 1;
    _gchatRenderAll();

    const groupEl = document.getElementById(`gsg-${exIdx}`);
    if (!groupEl) { gchatStreamActive = false; return; }

    try {
        const url = _bridgeHttpUrl(`/chat/stream?text=${encodeURIComponent(text)}&client_id=${GCHAT_CLIENT_ID}`);
        const resp = await fetch(url, { headers: authHeaders() });
        if (!resp.ok) {
            if (resp.status === 401) { location.href = '/login'; return; }
            throw new Error(`HTTP ${resp.status}`);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split('\n');
            buf = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                let evt;
                try { evt = JSON.parse(line.slice(6)); } catch { continue; }

                if (evt.type === 'thinking_delta') {
                    let thinkEl = groupEl.querySelector('.gchat-thinking');
                    if (!thinkEl) {
                        const typing = groupEl.querySelector('.gchat-typing');
                        if (typing) typing.remove();
                        thinkEl = document.createElement('div');
                        thinkEl.className = 'gchat-thinking';
                        thinkEl.innerHTML =
                            '<div class="gchat-thinking-label"><span class="gchat-thinking-dot"></span> thinking</div>' +
                            '<div class="gchat-thinking-body"></div>';
                        groupEl.appendChild(thinkEl);
                    }
                    const body = thinkEl.querySelector('.gchat-thinking-body');
                    body.textContent += evt.content;
                    body.scrollTop = body.scrollHeight;

                } else if (evt.type === 'thinking_done') {
                    const thinkEl = groupEl.querySelector('.gchat-thinking');
                    if (thinkEl) {
                        const label = thinkEl.querySelector('.gchat-thinking-label');
                        label.innerHTML = '\u{1F4AD} chain of thought';
                        thinkEl.addEventListener('click', () => thinkEl.classList.toggle('collapsed'));
                    }

                } else if (evt.type === 'tool_status') {
                    const div = document.createElement('div');
                    div.className = 'gchat-msg assistant';
                    const nameTag = `<span class="gchat-tool-name">${escapeHtml(evt.tool_name)}</span>`;
                    let body;
                    if (evt.action === 'call') {
                        body = `${nameTag} ${escapeHtml(evt.tool_args_preview || '')}`;
                    } else {
                        const dur = evt.duration_ms ? ` (${(evt.duration_ms / 1000).toFixed(1)}s)` : '';
                        const preview = (evt.result_preview || '').slice(0, 300);
                        body = `${nameTag} done${dur}<code>${escapeHtml(preview)}</code>`;
                    }
                    div.innerHTML = `<div class="gchat-bubble tool-status">${body}</div>`;
                    groupEl.appendChild(div);
                    if (!gchatExchanges[exIdx].tool_events) gchatExchanges[exIdx].tool_events = [];
                    gchatExchanges[exIdx].tool_events.push(evt);

                } else if (evt.type === 'emotion') {
                    // Fire-and-forget: apply emotion blendshape immediately.
                    if (window._applyEmotion) window._applyEmotion(evt.id);
                    // Stash latest emotion so subsequent speech_chunks can render it in-bubble.
                    gchatExchanges[exIdx]._lastEmotion = evt.id;

                } else if (evt.type === 'gesture') {
                    // Fire-and-forget: apply gesture immediately.
                    if (window._animController && evt.name) {
                        window._animController.setGesture(evt.name);
                    }
                    gchatExchanges[exIdx]._lastGesture = evt.name;

                } else if (evt.type === 'speech_chunk') {
                    const typing = groupEl.querySelector('.gchat-typing');
                    if (typing) typing.remove();
                    const thinkBlock = groupEl.querySelector('.gchat-thinking');
                    if (thinkBlock) thinkBlock.classList.add('collapsed');
                    const chunkIdx = gchatExchanges[exIdx].segments.length;
                    // Normalise chunk → segment-shape so existing renderers work.
                    const segShape = {
                        text: evt.text,
                        emotion: gchatExchanges[exIdx]._lastEmotion || 'neutral',
                        gesture: gchatExchanges[exIdx]._lastGesture || '',
                        audio_base64: evt.audio_base64,
                        viseme_b64: evt.viseme_b64,
                        viseme_fps: evt.viseme_fps,
                        viseme_frames: evt.viseme_frames,
                        chunk_idx: evt.chunk_idx,
                    };
                    gchatExchanges[exIdx].segments.push(segShape);
                    const sdiv = document.createElement('div');
                    sdiv.className = 'gchat-msg assistant';
                    const emo = segShape.emotion
                        ? `<span class="gchat-emotion">${escapeHtml(segShape.emotion)}</span>` : '';
                    const play = segShape.audio_base64
                        ? ` <button class="gchat-seg-play" onclick="gchatPlaySeg(${exIdx},${chunkIdx})">&#x1f50a;</button>` : '';
                    sdiv.innerHTML = `<div class="gchat-bubble">${emo}${escapeHtml(segShape.text)}${play}</div>`;
                    groupEl.appendChild(sdiv);

                    // Stream audio immediately for a live-conversational feel.
                    if (segShape.audio_base64) {
                        _gchatEnqueueChunk(exIdx, segShape);
                    }

                } else if (evt.type === 'speech_end') {
                    // No-op visually; audio queue drains on its own.

                } else if (evt.type === 'done') {
                    if (gchatExchanges[exIdx].segments.length > 0) {
                        const actions = document.createElement('div');
                        actions.className = 'gchat-actions';
                        actions.innerHTML =
                            `<button class="gchat-action-btn" id="gplay-${exIdx}" onclick="gchatPlayAll(${exIdx})">&#x1f50a; Play</button>` +
                            `<button class="gchat-action-btn" id="gdetail-btn-${exIdx}" onclick="gchatToggleDetail(${exIdx})">&#x25b6; Detail</button>`;
                        groupEl.appendChild(actions);
                        const detail = document.createElement('div');
                        detail.className = 'gchat-detail';
                        detail.id = `gdetail-${exIdx}`;
                        groupEl.appendChild(detail);
                    }

                } else if (evt.type === 'error') {
                    groupEl.innerHTML = `<div class="gchat-msg assistant"><div class="gchat-bubble">(Error: ${escapeHtml(evt.message)})</div></div>`;
                }
            }
        }
        const msgEl = document.getElementById('gchatMessages');
        if (msgEl) msgEl.scrollTop = msgEl.scrollHeight;
    } catch (e) {
        if (groupEl) groupEl.innerHTML = `<div class="gchat-msg assistant"><div class="gchat-bubble">(Error: ${escapeHtml(e.message)})</div></div>`;
    }
    gchatStreamActive = false;
}

window.gchatSend = gchatSend;

// -- Live chunk audio queue (continuous playback with 30ms crossfade) --
//
// Chunks arrive from /chat/stream as speech_chunk events. We schedule them on
// a shared AudioContext timeline so adjacent WAVs overlap ~30 ms and there is
// no audible seam. Each scheduled source also drives visemes for its chunk.

const _gchatChunkQueue = {
    ctx: null,
    nextStart: 0,            // absolute context time when next chunk should start
    lastSource: null,        // current source (for barge-in stop)
    activeChunks: 0,         // for tracking playback state
};

function _gchatEnsureAudioCtx() {
    if (!_gchatChunkQueue.ctx) {
        _gchatChunkQueue.ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (_gchatChunkQueue.ctx.state === 'suspended') {
        _gchatChunkQueue.ctx.resume();
    }
    return _gchatChunkQueue.ctx;
}

function _gchatEnqueueChunk(exIdx, seg) {
    const ctx = _gchatEnsureAudioCtx();
    const bytes = Uint8Array.from(atob(seg.audio_base64), c => c.charCodeAt(0));
    ctx.decodeAudioData(bytes.buffer.slice(0)).then((buffer) => {
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        const gain = ctx.createGain();
        source.connect(gain).connect(ctx.destination);

        const now = ctx.currentTime;
        const CROSSFADE = 0.030;  // 30 ms
        let startAt = Math.max(now, _gchatChunkQueue.nextStart - CROSSFADE);

        // Equal-power ramp in / ramp out at boundaries.
        gain.gain.setValueAtTime(0.0, startAt);
        gain.gain.linearRampToValueAtTime(1.0, startAt + CROSSFADE);
        const endAt = startAt + buffer.duration;
        gain.gain.setValueAtTime(1.0, Math.max(startAt + CROSSFADE, endAt - CROSSFADE));
        gain.gain.linearRampToValueAtTime(0.0, endAt);

        // Schedule visemes
        if (seg.viseme_b64 && window._applyVisemes) {
            _gchatScheduleVisemes(seg, startAt, ctx);
        }

        source.start(startAt);
        _gchatChunkQueue.lastSource = source;
        _gchatChunkQueue.nextStart = endAt;
        _gchatChunkQueue.activeChunks++;

        source.onended = () => {
            _gchatChunkQueue.activeChunks--;
            if (_gchatChunkQueue.activeChunks === 0) {
                if (window._clearLipSync) window._clearLipSync();
            }
        };
    }).catch((err) => {
        console.warn('_gchatEnqueueChunk decode failed:', err);
    });
}

function _gchatScheduleVisemes(seg, startAt, ctx) {
    // Decode base64 → Float32 LE per frame (5 channels)
    const bytes = Uint8Array.from(atob(seg.viseme_b64), c => c.charCodeAt(0));
    const totalFloats = bytes.byteLength / 4;
    const nFrames = Math.floor(totalFloats / 5);
    const f32 = new Float32Array(bytes.buffer, bytes.byteOffset, totalFloats);
    const fps = seg.viseme_fps || 30;
    const msPerFrame = 1000 / fps;
    for (let i = 0; i < nFrames; i++) {
        const delayMs = (startAt - ctx.currentTime) * 1000 + i * msPerFrame;
        setTimeout(() => {
            const frame = Array.from(f32.slice(i * 5, i * 5 + 5));
            if (window._applyVisemes) window._applyVisemes(frame);
        }, Math.max(0, delayMs));
    }
}

function _gchatBargeInStopChunks() {
    if (_gchatChunkQueue.lastSource) {
        try { _gchatChunkQueue.lastSource.stop(); } catch {}
        _gchatChunkQueue.lastSource = null;
    }
    _gchatChunkQueue.nextStart = 0;
    _gchatChunkQueue.activeChunks = 0;
}

// -- Audio playback --

function _gchatStopAudio() {
    _gchatBargeInStopChunks();
    if (gchatAudio) { gchatAudio.pause(); gchatAudio = null; }
    document.querySelectorAll('.gchat-action-btn.playing, .gchat-seg-play.playing').forEach(b => b.classList.remove('playing'));
}

function _gchatPlayB64(b64) {
    return new Promise((resolve, reject) => {
        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: 'audio/wav' });
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audio.onended = () => { URL.revokeObjectURL(url); resolve(); };
        audio.onerror = (e) => { URL.revokeObjectURL(url); reject(e); };
        gchatAudio = audio;
        audio.play();
    });
}

function gchatPlaySeg(exIdx, segIdx) {
    _gchatStopAudio();
    const seg = (gchatExchanges[exIdx] || {}).segments?.[segIdx];
    if (!seg?.audio_base64) return;
    _gchatPlayB64(seg.audio_base64).catch(e => console.error('Seg play fail:', e));
}
window.gchatPlaySeg = gchatPlaySeg;

async function gchatPlayAll(index) {
    const ex = gchatExchanges[index];
    if (!ex) return;
    const btn = document.getElementById(`gplay-${index}`);

    if (gchatAudio && gchatAudio._idx === index) {
        _gchatStopAudio();
        if (btn) btn.innerHTML = '&#x1f50a; Play';
        return;
    }
    _gchatStopAudio();

    const segs = (ex.segments || []).filter(s => s.audio_base64);
    if (segs.length > 0) {
        if (btn) { btn.innerHTML = '&#x23f9; Stop'; btn.classList.add('playing'); }
        try {
            for (const seg of segs) {
                if (gchatAudio) gchatAudio._idx = index;
                await _gchatPlayB64(seg.audio_base64);
            }
        } catch { /* stopped */ }
        if (btn) { btn.innerHTML = '&#x1f50a; Play'; btn.classList.remove('playing'); }
        return;
    }

    // Fallback: re-synthesize via /api/tts
    if (btn) { btn.innerHTML = '&#x23f3;'; btn.classList.add('loading'); }
    try {
        const fullText = ex.assistant_text || ex.segments?.map(s => s.text).join(' ') || '';
        const resp = await fetch(_bridgeHttpUrl('/api/tts'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: fullText }),
        });
        if (!resp.ok) throw new Error('TTS ' + resp.status);
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audio._idx = index;
        if (btn) { btn.innerHTML = '&#x23f9; Stop'; btn.classList.remove('loading'); btn.classList.add('playing'); }
        audio.onended = () => { gchatAudio = null; if (btn) { btn.innerHTML = '&#x1f50a; Play'; btn.classList.remove('playing'); } URL.revokeObjectURL(url); };
        audio.onerror = () => { gchatAudio = null; if (btn) { btn.innerHTML = '&#x1f50a; Play'; btn.classList.remove('playing'); } URL.revokeObjectURL(url); };
        gchatAudio = audio;
        audio.play();
    } catch (e) {
        if (btn) { btn.innerHTML = '&#x1f50a; Play'; btn.classList.remove('loading', 'playing'); }
    }
}
window.gchatPlayAll = gchatPlayAll;

// -- Detail toggle (raw JSON) --

function gchatToggleDetail(index) {
    const el = document.getElementById(`gdetail-${index}`);
    const btn = document.getElementById(`gdetail-btn-${index}`);
    if (!el) return;

    if (el.classList.contains('open')) {
        el.classList.remove('open');
        if (btn) btn.innerHTML = '&#x25b6; Detail';
    } else {
        const ex = gchatExchanges[index];
        // Strip audio_base64 to keep the JSON readable
        const show = {
            ...ex,
            segments: (ex.segments || []).map(s => {
                const { audio_base64, ...rest } = s;
                return rest;
            }),
        };
        el.textContent = JSON.stringify(show, null, 2);
        el.classList.add('open');
        if (btn) btn.innerHTML = '&#x25bc; Detail';
    }
}
window.gchatToggleDetail = gchatToggleDetail;

// -- Popup open/close + dragging --

function setupGlobalChat() {
    const popup = document.getElementById('gchatPopup');
    const btnOpen = document.getElementById('btnGlobalChat');
    const btnClose = document.getElementById('gchatClose');
    const btnMin = document.getElementById('gchatMinimize');
    const header = document.getElementById('gchatHeader');
    const inputEl = document.getElementById('gchatInput');
    const sendBtn = document.getElementById('gchatSend');

    if (btnOpen) btnOpen.addEventListener('click', () => {
        popup?.classList.toggle('open');
        if (popup?.classList.contains('open') && !gchatLoaded) gchatLoadHistory();
    });
    if (btnClose) btnClose.addEventListener('click', () => popup?.classList.remove('open'));
    if (btnMin) btnMin.addEventListener('click', () => popup?.classList.remove('open'));
    if (sendBtn) sendBtn.addEventListener('click', () => gchatSend());
    if (inputEl) inputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') gchatSend(); });

    // Draggable header
    if (header && popup) {
        let dragging = false, ox = 0, oy = 0;
        header.addEventListener('mousedown', (e) => {
            if (e.target.closest('.gchat-header-btn')) return;
            dragging = true;
            ox = e.clientX - popup.offsetLeft;
            oy = e.clientY - popup.offsetTop;
            document.body.style.userSelect = 'none';
        });
        document.addEventListener('mousemove', (e) => {
            if (!dragging) return;
            popup.style.left = Math.max(0, e.clientX - ox) + 'px';
            popup.style.top = Math.max(0, e.clientY - oy) + 'px';
            popup.style.bottom = 'auto';
        });
        document.addEventListener('mouseup', () => {
            dragging = false;
            document.body.style.userSelect = '';
        });
    }
}

// ============================================================================
//  Segment queue + sequential playback
//  (mirrors Unity's PlaySegmentQueue behaviour)
// ============================================================================

async function playNext() {
    if (isPlaying || segmentQueue.length === 0) return;
    isPlaying = true;
    window._isPlayingMotion = true;

    // Flush queued UI commands + chat bubbles now that speech starts
    if (window.UIOrchestrator) window.UIOrchestrator.onPlaybackStart();

    if (statusDot) statusDot.classList.add('speaking');

    while (segmentQueue.length > 0 && !interruptRequested) {
        const seg = segmentQueue.shift();

        // Apply emotion to VRM
        if (window._applyEmotion) window._applyEmotion(seg.emotion);

        // Decode viseme data
        const visemes = seg.viseme_b64
            ? decodeVisemes(seg.viseme_b64, seg.viseme_frames)
            : null;

        // Notify bridge: playback starting
        wsSend({
            type:   'segment_play_start',
            job_id: seg.job_id,
            index:  seg.index,
            total:  seg.total,
        });

        // Sync slides to speech: advance presentation as Mocha speaks
        if (window._presentation) {
            window._presentation.onSegmentPlay(seg.index, seg.total > 0 ? seg.total : segmentQueue.length + seg.index + 1);
        }

        // Tell animation controller which gesture to play
        // (duration is estimated from audio; actual playback is driven by controller.update())
        if (seg.gesture && window._animController) {
            window._animController.setGesture(seg.gesture, {
                duration: seg.audio_base64 ? estimateAudioDuration(seg.audio_base64) : 3,
            });
        }

        // Play audio with synchronized visemes (motion is handled by animation controller)
        if (seg.audio_base64) {
            await playSegment(
                seg.audio_base64,
                visemes,
                seg.viseme_fps || 30
            );
        }

        // Notify bridge: playback ended
        wsSend({
            type:   'segment_play_end',
            job_id: seg.job_id,
            index:  seg.index,
            total:  seg.total,
        });
    }

    if (statusDot) statusDot.classList.remove('speaking');
    isPlaying          = false;
    interruptRequested = false;

    // Tell animation controller speech is done → transition to idle
    if (window._animController) window._animController.endGesture();
    window._isPlayingMotion = false;

    if (window._applyEmotion) window._applyEmotion('neutral');

    // If new segments arrived during interrupt (e.g., stalling phrase while
    // previous playback was being cancelled), play them now.
    if (segmentQueue.length > 0) {
        playNext();
    }
}

/**
 * Estimate audio duration from base64 WAV data (reads header).
 */
function estimateAudioDuration(b64) {
    try {
        const raw = atob(b64.slice(0, 200));
        if (raw.length < 44 || raw.slice(0, 4) !== 'RIFF') return 3;
        const view = new DataView(new ArrayBuffer(44));
        for (let i = 0; i < 44 && i < raw.length; i++) view.setUint8(i, raw.charCodeAt(i));
        const sampleRate = view.getUint32(24, true);
        const channels = view.getUint16(22, true);
        const bps = view.getUint16(34, true);
        const totalBytes = b64.length * 3 / 4;
        const dataLen = Math.max(0, totalBytes - 44);
        return dataLen / Math.max(1, sampleRate * channels * (bps / 8));
    } catch { return 3; }
}

// ============================================================================
//  Interrupt handling
// ============================================================================

function cancelPlayback() {
    interruptRequested = true;
    segmentQueue.length = 0;

    // Clear any queued UI commands waiting for speech
    if (window.UIOrchestrator) window.UIOrchestrator.reset();

    if (currentAudioSource) {
        try { currentAudioSource.stop(); } catch (e) { /* already stopped */ }
        currentAudioSource = null;
    }

    if (window._resetBones)   window._resetBones();
    if (window._clearLipSync) window._clearLipSync();
}

// ============================================================================
//  Motion data decoding
//
// ============================================================================
//  Motion data decoding
//  Gesture service sends glTF-format quaternions (right-handed, same as three.js).
//  No coordinate flip needed.
// ============================================================================

// ============================================================================
//  Viseme data decoding
// ============================================================================

/**
 * Decode base64 viseme data into per-frame 5-channel weight arrays.
 * Wire format: T frames x 5 floats (aa, ih, ou, ee, oh), float32 LE.
 *
 * @param {string} b64       - Base64 encoded binary viseme data
 * @param {number} numFrames - Number of viseme frames
 * @returns {number[][]} Array of [aa, ih, ou, ee, oh] per frame
 */
function decodeVisemes(b64, numFrames) {
    const bytes  = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const floats = new Float32Array(bytes.buffer);
    const frames = [];

    for (let f = 0; f < numFrames; f++) {
        const idx = f * 5;
        frames.push([
            floats[idx],
            floats[idx + 1],
            floats[idx + 2],
            floats[idx + 3],
            floats[idx + 4]
        ]);
    }
    return frames;
}

// ============================================================================
//  Audio playback with analyser + synchronized viseme timer
// ============================================================================

/**
 * Play one audio segment with optional synchronized viseme data.
 * Returns a promise that resolves when the audio finishes playing.
 *
 * @param {string}     audioB64     - Base64-encoded audio (WAV/OGG)
 * @param {number[][]} visemeFrames - Decoded viseme frames, or null
 * @param {number}     visemeFps    - Viseme playback frame rate
 * @returns {Promise<void>}
 */
async function playSegment(audioB64, visemeFrames, visemeFps) {
    ensureAudioCtx();
    if (audioCtx.state === 'suspended') {
        await audioCtx.resume();
    }

    return new Promise((resolve) => {
        const bytes = Uint8Array.from(atob(audioB64), c => c.charCodeAt(0));

        audioCtx.decodeAudioData(bytes.buffer.slice(0), (buffer) => {
            const source   = audioCtx.createBufferSource();
            source.buffer  = buffer;
            currentAudioSource = source;

            // --- Audio analyser for browser-side lip sync fallback ---
            let analyser = null;
            let analyserData = null;
            if (window._setupAnalyser) {
                const a    = window._setupAnalyser(audioCtx, source);
                analyser     = a.analyser;
                analyserData = a.data;
            } else {
                source.connect(audioCtx.destination);
            }

            // --- Viseme playback timer (server-sent phoneme data) ---
            let vt = null;
            if (visemeFrames && visemeFrames.length > 0) {
                // Use server-provided viseme data
                let vf = 0;
                vt = setInterval(() => {
                    if (vf >= visemeFrames.length) { clearInterval(vt); return; }
                    if (window._applyVisemes) window._applyVisemes(visemeFrames[vf]);
                    vf++;
                }, 1000 / visemeFps);
            } else if (analyser) {
                // Fallback: audio-driven lip sync from frequency analysis
                const BANDS = [[0, 25], [25, 68], [68, 170], [170, 340]];
                vt = setInterval(() => {
                    if (!analyserData) return;
                    analyser.getByteFrequencyData(analyserData);
                    const w = [0, 0, 0, 0, 0];
                    let total = 0;
                    for (let b = 0; b < BANDS.length; b++) {
                        let sum = 0, count = 0;
                        for (let i = BANDS[b][0]; i < Math.min(BANDS[b][1], analyserData.length); i++) {
                            sum += analyserData[i];
                            count++;
                        }
                        w[b] = count > 0 ? sum / count / 255 : 0;
                        total += w[b];
                    }
                    const mx = Math.max(...w, 0.01);
                    for (let i = 0; i < 4; i++) w[i] = Math.pow(w[i] / mx, 1.5);
                    const gate = Math.min(1, total / 0.8);
                    for (let i = 0; i < 5; i++) w[i] *= gate;
                    if (window._applyVisemes) window._applyVisemes(w);
                }, 33); // ~30fps
            }

            // --- Cleanup on audio end ---
            source.onended = () => {
                currentAudioSource = null;
                if (vt) clearInterval(vt);
                if (window._clearLipSync) window._clearLipSync();
                resolve();
            };

            source.start(0);
        }, (err) => {
            console.error('Audio decode error:', err);
            resolve();
        });
    });
}

// ============================================================================
//  Mic capture (getUserMedia -> PCM16 16kHz -> binary WS frames)
// ============================================================================

async function toggleMic() {
    ensureAudioCtx();  // Unlock playback audio on mic toggle
    if (micActive) {
        stopMic();
    } else {
        await startMic();
    }
}

// Expose to global scope for inline onclick
window.toggleMic = toggleMic;

async function startMic() {
    if (!wsLive || wsLive.readyState !== WebSocket.OPEN) return;

    try {
        micStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                sampleRate: 16000,
                channelCount: 1,
                echoCancellation: true,
            }
        });

        micAudioCtx = new AudioContext({ sampleRate: 16000 });
        if (micAudioCtx.state === 'suspended') {
            await micAudioCtx.resume();
        }
        console.log('Mic AudioContext state:', micAudioCtx.state, 'sampleRate:', micAudioCtx.sampleRate);

        const source = micAudioCtx.createMediaStreamSource(micStream);

        // ScriptProcessor is deprecated but works everywhere;
        // buffer size 640 = 40ms at 16kHz (2 x 20ms frames)
        micProcessor = micAudioCtx.createScriptProcessor(640, 1, 1);
        let _micSentCount = 0;
        micProcessor.onaudioprocess = (e) => {
            if (micMuted || !wsLive || wsLive.readyState !== WebSocket.OPEN) return;
            const f32   = e.inputBuffer.getChannelData(0);
            const pcm16 = new Int16Array(f32.length);
            for (let i = 0; i < f32.length; i++) {
                pcm16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32768));
            }
            wsLive.send(pcm16.buffer);
            _micSentCount++;
            if (_micSentCount === 1) {
                console.log('Mic: first PCM frame sent — %d samples, %d bytes', f32.length, pcm16.buffer.byteLength);
            }
        };

        source.connect(micProcessor);
        micProcessor.connect(micAudioCtx.destination);

        micActive = true;
        if (btnMic) {
            btnMic.classList.add('active');
            btnMic.title = 'Stop mic';
        }
    } catch (e) {
        console.error('Mic error:', e);
        if (debugBar) debugBar.textContent = 'Mic access denied';
    }
}

function stopMic() {
    if (micProcessor) { micProcessor.disconnect(); micProcessor = null; }
    if (micAudioCtx)  { micAudioCtx.close();       micAudioCtx  = null; }
    if (micStream)    { micStream.getTracks().forEach(t => t.stop()); micStream = null; }

    micActive = false;
    if (btnMic) {
        btnMic.classList.remove('active');
        btnMic.title = 'Start mic';
    }
}

// ============================================================================
//  Tab switching
// ============================================================================

let _cronRefreshTimer = null;

function _startCronRefreshLoop() {
    refreshCronJobs();
    if (_cronRefreshTimer) clearInterval(_cronRefreshTimer);
    _cronRefreshTimer = setInterval(refreshCronJobs, 15000);
}

function _stopCronRefreshLoop() {
    if (_cronRefreshTimer) {
        clearInterval(_cronRefreshTimer);
        _cronRefreshTimer = null;
    }
}

function setupTabs() {
    // Modal tabs (inside settings modal)
    document.querySelectorAll('.modal-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.modal-tab').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.modal-content').forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            const panel = document.getElementById('modal-' + btn.dataset.tab);
            if (panel) panel.classList.add('active');

            // Tab-specific hooks
            if (btn.dataset.tab === 'crons') {
                _startCronRefreshLoop();
            } else {
                _stopCronRefreshLoop();
            }
            if (btn.dataset.tab === 'account') {
                refreshAccountState();
                // Visiting the Account tab clears the "you hit your cap" badge.
                const badge = document.getElementById('accountBadge');
                if (badge) badge.style.display = 'none';
            }
            if (btn.dataset.tab === 'avatar' && window.Avatar) {
                window.Avatar.refresh();
            }
        });
    });

    // Gear button opens modal
    const backdrop = document.getElementById('modalBackdrop');
    document.getElementById('btnGear')?.addEventListener('click', () => {
        backdrop?.classList.add('open');
        // If Crons tab is the active one when the modal opens, refresh it
        const activeTab = document.querySelector('.modal-tab.active');
        if (activeTab?.dataset.tab === 'crons') {
            _startCronRefreshLoop();
        }
        if (activeTab?.dataset.tab === 'account') {
            refreshAccountState();
        }
        if (activeTab?.dataset.tab === 'avatar' && window.Avatar) {
            window.Avatar.refresh();
        }
    });
    document.getElementById('btnCloseModal')?.addEventListener('click', () => {
        backdrop?.classList.remove('open');
        _stopCronRefreshLoop();
    });
    // Click backdrop to close
    backdrop?.addEventListener('click', (e) => {
        if (e.target === backdrop) {
            backdrop.classList.remove('open');
            _stopCronRefreshLoop();
        }
    });
    // Escape key closes modal
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            backdrop?.classList.remove('open');
            _stopCronRefreshLoop();
        }
    });

    // Manual refresh button on Crons tab
    document.getElementById('btnRefreshCrons')?.addEventListener('click', () => {
        refreshCronJobs();
    });
}

// ============================================================================
//  Cron jobs modal
// ============================================================================

function _fmtCronTime(iso) {
    if (!iso) return '<span style="color:#666;">—</span>';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '<span style="color:#666;">—</span>';
    const now = Date.now();
    const diffS = Math.round((d.getTime() - now) / 1000);
    const abs = Math.abs(diffS);
    let rel;
    if (abs < 60)            rel = diffS <= 0 ? `${abs}s ago` : `in ${abs}s`;
    else if (abs < 3600)     rel = diffS <= 0 ? `${Math.round(abs/60)}m ago`  : `in ${Math.round(abs/60)}m`;
    else if (abs < 86400)    rel = diffS <= 0 ? `${Math.round(abs/3600)}h ago`: `in ${Math.round(abs/3600)}h`;
    else                     rel = diffS <= 0 ? `${Math.round(abs/86400)}d ago`: `in ${Math.round(abs/86400)}d`;
    const local = d.toLocaleString(undefined, {
        month: 'short', day: '2-digit',
        hour: '2-digit', minute: '2-digit'
    });
    return `<span title="${d.toISOString()}">${local}<br><span style="color:#888;font-size:11px;">${rel}</span></span>`;
}

function _escHtml(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function refreshCronJobs() {
    const tbody = document.getElementById('cronJobsTbody');
    if (!tbody) return;
    try {
        const resp = await fetch('/api/cron_jobs', { cache: 'no-store' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const jobs = Array.isArray(data.jobs) ? data.jobs : [];
        if (jobs.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" style="padding:20px 10px;color:#888;text-align:center;">No scheduled jobs.</td></tr>`;
            return;
        }
        const rows = jobs.map(j => {
            const name = _escHtml(j.id || j.name || '(unnamed)');
            const desc = _escHtml(j.description || '');
            const enabled = j.enabled !== false;
            const statusBadge = enabled
                ? `<span style="color:#7ec87e;">● on</span>`
                : `<span style="color:#b88;">○ off</span>`;
            const cronHuman = _escHtml(j.cron_human || j.cron || '');
            const cronRaw = _escHtml(j.cron || '');
            const schedule = `${cronHuman}<br><code style="color:#888;font-size:11px;">${cronRaw}</code>`;
            const lastFire = _fmtCronTime(j.last_fire);
            const nextFire = _fmtCronTime(j.next_run_iso || j.next_run);
            return `<tr style="border-bottom:1px solid #222;">
                <td style="padding:8px 10px;vertical-align:top;"><b>${name}</b></td>
                <td style="padding:8px 10px;vertical-align:top;">${desc}</td>
                <td style="padding:8px 10px;vertical-align:top;">${statusBadge}</td>
                <td style="padding:8px 10px;vertical-align:top;">${schedule}</td>
                <td style="padding:8px 10px;vertical-align:top;">${lastFire}</td>
                <td style="padding:8px 10px;vertical-align:top;">${nextFire}</td>
            </tr>`;
        });
        tbody.innerHTML = rows.join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" style="padding:20px 10px;color:#b88;text-align:center;">Failed to load cron jobs: ${_escHtml(e.message)}</td></tr>`;
    }
}

// ============================================================================
//  Decorations: scene background image (drag-drop, persisted to localStorage)
// ============================================================================

const _BG_DEFAULT = null; // solid dark background by default
const _BG_KEY = 'parrot.bg';
const _BG_STYLES_KEY = 'parrot.bg_settings';
// 4 MB source cap: base64 inflates ~33 %, so ~5.3 MB in storage. Most browsers
// give localStorage ~5–10 MB per origin; leave headroom for other keys.
const _BG_MAX_BYTES = 4 * 1024 * 1024;

const _BG_STYLE_DEFAULTS = { fit: 'cover', posX: 50, posY: 50, blur: 0, brightness: 1 };

function _loadBgStyles() {
    try {
        const raw = localStorage.getItem(_BG_STYLES_KEY);
        if (raw) return { ..._BG_STYLE_DEFAULTS, ...JSON.parse(raw) };
    } catch { /* ignore */ }
    return { ..._BG_STYLE_DEFAULTS };
}

function _saveBgStyles(s) {
    try { localStorage.setItem(_BG_STYLES_KEY, JSON.stringify(s)); } catch { /* ignore */ }
}

function _applyBgStyles(s) {
    if (window._setSceneBackgroundStyles) window._setSceneBackgroundStyles(s);
}

function _humanBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(2) + ' MB';
}

function _updateBgPreview(src, label) {
    const row = document.getElementById('bgPreviewRow');
    const img = document.getElementById('bgPreview');
    const hint = document.getElementById('bgSizeHint');
    if (!row || !img) return;
    if (src) {
        img.src = src;
        img.style.display = '';
    } else {
        img.src = '';
        img.style.display = 'none';
    }
    if (hint) hint.textContent = label || '';
    row.style.display = '';
}

function _applySceneBg(src) {
    if (window._setSceneBackground) window._setSceneBackground(src);
}

async function _handleBgFile(file) {
    if (!file || !file.type.startsWith('image/')) {
        alert('Please drop an image file.');
        return;
    }
    if (file.size > _BG_MAX_BYTES) {
        alert(`Image is ${_humanBytes(file.size)}. Pick one under ${_humanBytes(_BG_MAX_BYTES)} so it fits in browser storage.`);
        return;
    }
    const dataUrl = await new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(r.result);
        r.onerror = () => reject(r.error);
        r.readAsDataURL(file);
    });
    try {
        localStorage.setItem(_BG_KEY, dataUrl);
    } catch (e) {
        alert('Could not save to browser storage: ' + e.message +
              '\nThe background will apply for this session but won\'t persist.');
    }
    _applySceneBg(dataUrl);
    _updateBgPreview(dataUrl, `${file.name} — ${_humanBytes(file.size)}`);
}

function setupDecorations() {
    const dropzone = document.getElementById('bgDropzone');
    const fileInput = document.getElementById('bgFile');
    const resetBtn = document.getElementById('btnBgReset');
    if (!dropzone || !fileInput) return;

    dropzone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', (e) => {
        if (e.target.files[0]) _handleBgFile(e.target.files[0]);
        fileInput.value = '';
    });

    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('drag-over');
    });
    dropzone.addEventListener('dragleave', (e) => {
        if (!dropzone.contains(e.relatedTarget)) dropzone.classList.remove('drag-over');
    });
    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('drag-over');
        const f = e.dataTransfer.files[0];
        if (f) _handleBgFile(f);
    });

    resetBtn?.addEventListener('click', () => {
        try {
            localStorage.removeItem(_BG_KEY);
            localStorage.removeItem(_BG_STYLES_KEY);
        } catch { /* ignore */ }
        _applySceneBg(null);
        _applyBgStyles(_BG_STYLE_DEFAULTS);
        _syncBgStyleControls(_BG_STYLE_DEFAULTS);
        _updateBgPreview(null, 'default (solid dark)');
    });

    let saved = null;
    try { saved = localStorage.getItem(_BG_KEY); } catch { /* ignore */ }
    if (saved) {
        _updateBgPreview(saved, 'custom (saved in browser)');
    } else {
        _applySceneBg(null);
        _updateBgPreview(null, 'default (solid dark)');
    }

    // --- Tuning knobs: fit, position, blur, brightness ---
    const styles = _loadBgStyles();
    _syncBgStyleControls(styles);
    _applyBgStyles(styles);

    const bgFit = document.getElementById('bgFit');
    const sX = document.getElementById('slider-bg-posX');
    const sY = document.getElementById('slider-bg-posY');
    const sBlur = document.getElementById('slider-bg-blur');
    const sBright = document.getElementById('slider-bg-bright');

    function _push(partial) {
        Object.assign(styles, partial);
        _applyBgStyles(styles);
        _saveBgStyles(styles);
    }

    bgFit?.addEventListener('change', () => _push({ fit: bgFit.value }));
    sX?.addEventListener('input', () => {
        document.getElementById('val-bg-posX').textContent = sX.value;
        _push({ posX: +sX.value });
    });
    sY?.addEventListener('input', () => {
        document.getElementById('val-bg-posY').textContent = sY.value;
        _push({ posY: +sY.value });
    });
    sBlur?.addEventListener('input', () => {
        document.getElementById('val-bg-blur').textContent = sBlur.value;
        _push({ blur: +sBlur.value });
    });
    sBright?.addEventListener('input', () => {
        document.getElementById('val-bg-bright').textContent = sBright.value;
        _push({ brightness: +sBright.value / 100 });
    });
}

function _syncBgStyleControls(s) {
    const bgFit = document.getElementById('bgFit');
    const sX = document.getElementById('slider-bg-posX');
    const sY = document.getElementById('slider-bg-posY');
    const sBlur = document.getElementById('slider-bg-blur');
    const sBright = document.getElementById('slider-bg-bright');
    if (bgFit) bgFit.value = s.fit;
    if (sX) sX.value = s.posX;
    if (sY) sY.value = s.posY;
    if (sBlur) sBlur.value = s.blur;
    if (sBright) sBright.value = Math.round(s.brightness * 100);
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set('val-bg-posX', s.posX);
    set('val-bg-posY', s.posY);
    set('val-bg-blur', s.blur);
    set('val-bg-bright', Math.round(s.brightness * 100));
}

// ============================================================================
//  Config sliders (send config_update to /ws/monitor)
// ============================================================================

function setupConfigSliders() {
    const sliders = {
        'slider-vad-final': {
            key: 'vad_final_ms',
            display: 'val-vad-final',
            format: v => Math.round(v),
        },
        'slider-vad-interim': {
            key: 'vad_interim_ms',
            display: 'val-vad-interim',
            format: v => Math.round(v),
        },
        'slider-llm-temp': {
            key: 'llm_temperature',
            display: 'val-llm-temp',
            format: v => parseFloat(v).toFixed(2),
        },
    };

    for (const [id, cfg] of Object.entries(sliders)) {
        const slider = document.getElementById(id);
        if (!slider) continue;

        // Live display update while dragging
        slider.addEventListener('input', () => {
            const displayEl = document.getElementById(cfg.display);
            if (displayEl) displayEl.textContent = cfg.format(slider.value);
        });

        // Send to bridge on release
        slider.addEventListener('change', () => {
            if (wsMonitor && wsMonitor.readyState === WebSocket.OPEN) {
                wsMonitor.send(JSON.stringify({
                    type:  'config_update',
                    key:   cfg.key,
                    value: parseFloat(slider.value),
                }));
            }
        });
    }
}

/**
 * Apply config_state message from /ws/monitor to slider elements.
 */
function applyConfigState(msg) {
    const mappings = {
        'slider-vad-final':   { value: msg.vad_final_ms,   display: 'val-vad-final',   format: v => Math.round(v) },
        'slider-vad-interim': { value: msg.vad_interim_ms,  display: 'val-vad-interim', format: v => Math.round(v) },
        'slider-llm-temp':    { value: msg.llm_temperature, display: 'val-llm-temp',    format: v => parseFloat(v).toFixed(2) },
    };

    for (const [sliderId, cfg] of Object.entries(mappings)) {
        if (cfg.value == null) continue;
        const slider = document.getElementById(sliderId);
        if (slider) slider.value = cfg.value;
        const display = document.getElementById(cfg.display);
        if (display) display.textContent = cfg.format(cfg.value);
    }
}

// ============================================================================
//  Echo mode toggle UI
// ============================================================================

function updateEchoModeUI(mode) {
    const el = document.getElementById('echoModeToggle');
    if (el) {
        el.textContent = mode === 'room' ? 'Room' : 'Headphone';
        el.dataset.mode = mode;
    }
}

function toggleEchoMode() {
    const el = document.getElementById('echoModeToggle');
    if (!el) return;
    const newMode = el.dataset.mode === 'room' ? 'headphone' : 'room';
    if (wsMonitor && wsMonitor.readyState === WebSocket.OPEN) {
        wsMonitor.send(JSON.stringify({
            type:  'config_update',
            key:   'echo_mode',
            value: newMode,
        }));
    }
}

window.toggleEchoMode = toggleEchoMode;

// ============================================================================
//  Shiro toggle UI
// ============================================================================

function updateShiroUI(running) {
    const el = document.getElementById('btnShiroToggle');
    if (el) {
        el.textContent = running ? 'Shiro: ON' : 'Shiro: OFF';
        el.classList.toggle('active', running);
    }
}

// ============================================================================
//  Admin action buttons (fetch to bridge admin endpoints)
// ============================================================================

function setupAdminButtons() {
    // Restart TTS
    const btnRestartTTS = document.getElementById('btnRestartTTS');
    if (btnRestartTTS) {
        btnRestartTTS.addEventListener('click', async () => {
            btnRestartTTS.disabled = true;
            try {
                await fetch(_bridgeHttpUrl('/admin/tts/restart'), { method: 'POST' });
                if (debugBar) debugBar.textContent = 'TTS restarted';
            } catch (e) {
                console.error('TTS restart failed:', e);
                if (debugBar) debugBar.textContent = 'TTS restart failed';
            }
            btnRestartTTS.disabled = false;
        });
    }

    // Process-level restart (for when STT/TTS have died and HTTP isn't an
    // option). Shells out to ./start.sh on the bridge. Expect a few seconds
    // for model load; health check confirms when it's back.
    async function _restartService(svc, statusEl) {
        const buttons = [
            document.getElementById('btnRestartSTT'),
            document.getElementById('btnRestartTTSProc'),
            document.getElementById('btnRestartBoth'),
        ].filter(Boolean);
        buttons.forEach(b => b.disabled = true);
        if (statusEl) statusEl.textContent = `Restarting ${svc}…`;

        try {
            const resp = await fetch(
                _bridgeHttpUrl(`/admin/services/restart?service=${encodeURIComponent(svc)}`),
                { method: 'POST' }
            );
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);

            // Poll /health for up to 30s so the status reflects reality
            if (statusEl) statusEl.textContent = `${svc} started, waiting for model load…`;
            const deadline = Date.now() + 30000;
            while (Date.now() < deadline) {
                await new Promise(r => setTimeout(r, 1500));
                try {
                    const h = await fetch(_bridgeHttpUrl('/health'));
                    const hj = await h.json();
                    const targets = svc === 'all' ? ['stt', 'tts'] : [svc];
                    const allOk = targets.every(t => (hj.services?.[t] || '').startsWith('ok'));
                    if (allOk) {
                        if (statusEl) statusEl.textContent = `${svc} ready.`;
                        if (debugBar) debugBar.textContent = `${svc} restarted OK`;
                        break;
                    }
                } catch { /* ignore transient failures */ }
            }
            if (statusEl && statusEl.textContent.includes('waiting')) {
                statusEl.textContent = `${svc} taking longer than usual — check logs.`;
            }
        } catch (e) {
            console.error(`restart ${svc} failed:`, e);
            if (statusEl) statusEl.textContent = `Restart failed: ${e.message || e}`;
            if (debugBar) debugBar.textContent = `Restart ${svc} failed`;
        } finally {
            buttons.forEach(b => b.disabled = false);
        }
    }

    const svcStatusEl = document.getElementById('svcRestartStatus');
    const btnSTT = document.getElementById('btnRestartSTT');
    const btnTTSProc = document.getElementById('btnRestartTTSProc');
    const btnBoth = document.getElementById('btnRestartBoth');
    if (btnSTT)     btnSTT.addEventListener('click',      () => _restartService('stt', svcStatusEl));
    if (btnTTSProc) btnTTSProc.addEventListener('click',  () => _restartService('tts', svcStatusEl));
    if (btnBoth)    btnBoth.addEventListener('click',     () => _restartService('all', svcStatusEl));

    // Hard reload — clears anything the page can clear from JS (service
    // workers, Cache Storage), then reloads with a cache-buster query so
    // the HTML and JS bundle bypass the disk cache. The browser's HTTP
    // cache itself is not clearable from JS (security) — the cache-buster
    // ?_=<timestamp> on the reload URL is what actually forces fresh JS.
    const btnHardReload = document.getElementById('btnHardReload');
    if (btnHardReload) {
        btnHardReload.addEventListener('click', async () => {
            const status = document.getElementById('hardReloadStatus');
            btnHardReload.disabled = true;
            if (status) status.textContent = 'Clearing caches…';
            try {
                if ('serviceWorker' in navigator) {
                    const regs = await navigator.serviceWorker.getRegistrations();
                    await Promise.all(regs.map(r => r.unregister()));
                }
                if ('caches' in window) {
                    const keys = await caches.keys();
                    await Promise.all(keys.map(k => caches.delete(k)));
                }
            } catch (e) {
                console.warn('hard-reload cleanup error (continuing):', e);
            }
            const sep = location.search ? '&' : '?';
            location.replace(location.pathname + location.search + sep + '_=' + Date.now() + location.hash);
        });
    }

    // Clear memory — windowed (1h / 3h / 1d / 1w / all). Always wipes
    // Mocha's short-term ring buffer; "all" also wipes mem0 facts + diary.
    // Local UI (chat panels, thinking log) clears on success.
    document.querySelectorAll('[data-clear-window]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const window_ = btn.dataset.clearWindow;
            if (window_ === 'all' && !confirm(
                'Wipe ALL memory for this account?\n\n' +
                'This clears short-term context, long-term facts, and the diary. ' +
                'The audit log is unaffected. Cannot be undone.'
            )) return;

            const status = document.getElementById('clearMemStatus');
            const allBtns = document.querySelectorAll('[data-clear-window]');
            allBtns.forEach(b => b.disabled = true);
            try {
                const url = _bridgeHttpUrl('/admin/clear-memory') + '?window=' + encodeURIComponent(window_);
                const r = await fetch(url, {
                    method: 'POST',
                    headers: { ...authHeaders() },
                });
                if (!r.ok) throw new Error('HTTP ' + r.status);

                // Clear visible chat surfaces locally so it feels real instantly.
                const chatLog = document.getElementById('chatLog');
                if (chatLog) chatLog.innerHTML = '';
                const gchatMsgs = document.getElementById('gchatMessages');
                if (gchatMsgs) gchatMsgs.innerHTML = '';
                const thinking = document.getElementById('thinkingPanel');
                if (thinking) thinking.innerHTML = '';

                const label = window_ === 'all' ? 'all memory wiped' : `memory cleared (${window_})`;
                if (status) {
                    status.textContent = label;
                    setTimeout(() => { if (status) status.textContent = ''; }, 4000);
                }
                if (debugBar) debugBar.textContent = label;
            } catch (e) {
                console.error('Memory clear failed:', e);
                if (status) status.textContent = 'clear failed: ' + (e.message || e);
                if (debugBar) debugBar.textContent = 'memory clear failed';
            } finally {
                allBtns.forEach(b => b.disabled = false);
            }
        });
    });

    // Shiro toggle
    const btnShiroToggle = document.getElementById('btnShiroToggle');
    if (btnShiroToggle) {
        btnShiroToggle.addEventListener('click', async () => {
            btnShiroToggle.disabled = true;
            try {
                await fetch(_bridgeHttpUrl('/admin/shiro/toggle'), { method: 'POST' });
            } catch (e) {
                console.error('Shiro toggle failed:', e);
            }
            btnShiroToggle.disabled = false;
        });
    }

    // Reload tools
    const btnReloadTools = document.getElementById('btnReloadTools');
    if (btnReloadTools) {
        btnReloadTools.addEventListener('click', async () => {
            btnReloadTools.disabled = true;
            try {
                const resp = await fetch(_bridgeHttpUrl('/admin/reload-tools'), { method: 'POST' });
                const data = await resp.json();
                if (debugBar) debugBar.textContent = `Tools reloaded: ${(data.tools || []).length}`;
            } catch (e) {
                console.error('Tool reload failed:', e);
                if (debugBar) debugBar.textContent = 'Tool reload failed';
            }
            btnReloadTools.disabled = false;
        });
    }

}

// ============================================================================
//  Chat history loading (from /api/conversation)
// ============================================================================

async function loadChatHistory(limit) {
    if (!bridgePort) return;
    try {
        const resp = await fetch(_bridgeHttpUrl(`/api/conversation?limit=${limit || 50}`), { headers: authHeaders() });
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.exchanges && Array.isArray(data.exchanges)) {
            // History load: show ALL entries regardless of source
            for (const entry of data.exchanges) {
                if (entry.user_text) appendChat('user', entry.user_text);
                if (entry.segments && entry.segments.length > 0) {
                    for (const seg of entry.segments) {
                        appendChat('mocha', seg.text, seg.emotion);
                    }
                } else if (entry.assistant_text) {
                    appendChat('mocha', entry.assistant_text);
                }
            }
        }
    } catch (e) {
        console.log('Could not load chat history:', e.message);
    }
}

// ============================================================================
//  Initialization
// ============================================================================

async function init() {
    // 0. Auth — try anonymous bootstrap on fresh visits.
    //    No JWT stored → POST /api/auth/anon with the persistent browser_token
    //    (= localStorage parrot.session_id). Backend resolves to an existing
    //    user (by browser_token, then by IP) or creates a fresh anon profile.
    //    Only fall back to /login if the bootstrap call itself fails.
    if (!getJwt()) {
        const ok = await tryAnonBootstrap();
        if (!ok) { location.href = '/login'; return; }
    }

    // 1. Fetch /api/config to get bridge port
    try {
        const resp = await fetch('/api/config');
        if (resp.ok) {
            const cfg = await resp.json();
            bridgePort = cfg.bridge_port || cfg.port || 8090;
            // Show the "Trading desk" button only if a dashboard URL is configured.
            const deskBtn = document.getElementById('enterDeskBtn');
            if (deskBtn && cfg.desk_url) {
                deskBtn.href = cfg.desk_url;
                deskBtn.style.display = '';
            }
        }
    } catch (e) {
        console.warn('/api/config not available, falling back to defaults');
    }

    // Fallback: use BRIDGE_PORT if set globally (from inline HTML template)
    if (!bridgePort && typeof BRIDGE_PORT !== 'undefined') {
        bridgePort = BRIDGE_PORT;
    }
    if (!bridgePort) {
        bridgePort = 8090;
    }

    // Bridge host defaults to the page hostname
    bridgeHost = location.hostname || '127.0.0.1';

    // 2. Connect to both WebSockets
    connectLive();
    connectMonitor();

    // 2a. Presence tracking — so Mocha can tell idle/visible/mute states.
    initPresence();

    // 3. Auto-load VRM from /api/default-model (handled by vrm-renderer.js)
    //    vrm-renderer.js calls autoLoadDefault() on init.

    // 4. Setup UI interactions
    setupTabs();
    setupConfigSliders();
    setupDecorations();
    setupAdminButtons();
    setupGlobalChat();
    setupAccountTab();
    initPresenceClaim();
    initKeyboardTracker();

    // Text input enter key
    if (textInput) {
        textInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') sendText();
        });
    }

    // Send button
    const btnSend = document.getElementById('btnSend');
    if (btnSend) {
        btnSend.addEventListener('click', () => sendText());
    }

    // Mic button
    if (btnMic) {
        btnMic.addEventListener('click', () => toggleMic());
    }

    // ── Phone-only floating input bar ───────────────────────────────────
    // Always-visible input + send + mic at the bottom of the screen on
    // phone, so the user can type/talk without expanding the chat history.
    // Mirrors the desktop sendText flow but reads from #phoneTextInput.
    const phoneInput = document.getElementById('phoneTextInput');
    const phoneBtnSend = document.getElementById('phoneBtnSend');
    const phoneBtnMic = document.getElementById('phoneBtnMic');

    function _phoneSend() {
        if (!phoneInput) return;
        const text = phoneInput.value.trim();
        if (!text) return;
        if (!wsLive || wsLive.readyState !== WebSocket.OPEN) {
            // Visible signal that the message couldn't be sent — flash the
            // status dot and prompt the user to click it to reconnect.
            if (statusDot) {
                statusDot.classList.add('disconnected-flash');
                setTimeout(() => statusDot.classList.remove('disconnected-flash'), 1500);
            }
            setStatus('disconnected', 'Disconnected — click the dot to reconnect');
            return;
        }
        ensureAudioCtx();
        wsSend({ type: 'user_input', text });
        appendChat('user', text);
        phoneInput.value = '';
    }

    if (phoneInput) {
        phoneInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                _phoneSend();
            }
        });
    }
    if (phoneBtnSend) phoneBtnSend.addEventListener('click', _phoneSend);
    if (phoneBtnMic) {
        phoneBtnMic.addEventListener('click', () => {
            toggleMic();
            // Reflect mic active state on the floating button.
            phoneBtnMic.classList.toggle('active', !!micActive);
        });
    }

    // 5. Make the status dot a one-click reconnect affordance. If the WS
    //    drops (bridge restart, tunnel hiccup, network blip), clicking the
    //    dot bypasses the 3 s backoff and tries to reconnect immediately.
    if (statusDot) {
        statusDot.title = 'Click to reconnect';
        statusDot.style.cursor = 'pointer';
        statusDot.addEventListener('click', forceReconnect);
    }

    // 6. Bootstrap active theme's non-CSS assets (background image is in
    //    active.css; audio/decor/html_mods are in active.json).
    bootstrapActiveTheme();

    // 6. Load chat history (only the last few, and mark as loaded so
    //    live speech_segments don't duplicate them)
    // Disabled for now — live messages populate the chat naturally.
    // await loadChatHistory(50);
}

// ============================================================================
//  Theme bootstrap — on page load, apply decor/html_mods from active.json
// ============================================================================

async function bootstrapActiveTheme() {
    if (!window.ThemeRuntime) return;
    let active = null;
    try {
        const resp = await fetch('/static/css/themes/active.json?v=' + Date.now(), { cache: 'no-store' });
        if (!resp.ok) return;
        active = await resp.json();
    } catch (e) {
        return;
    }
    const assets = (active && active.assets) || {};
    // CSS parts are already loaded via <link rel="stylesheet" ... active.css>.
    // Apply only the DOM-state parts here. Music is a separate concern —
    // the floating music_player is explicitly user-controlled, not auto-loaded.
    window.ThemeRuntime.applyFull({}, '', {
        html_decor: assets.html_decor || '',
        html_mods: assets.html_mods || [],
    });
}

// ============================================================================
//  Start
// ============================================================================

// Wait for DOM to be ready, then initialize
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
