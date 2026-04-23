/**
 * app.js — Main application JavaScript for Mocha web app.
 *
 * Handles:
 *  - WebSocket connections to bridge (/ws/live and /ws/monitor)
 *  - Message handling (speech_segment, speech_end, interrupt, mic_mute, etc.)
 *  - Segment queue + sequential playback (mirrors Unity PlaySegmentQueue)
 *  - Audio decoding + playback via Web Audio API
 *  - Motion data decoding (motion_b64 -> per-frame bone quaternions)
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

// ============================================================================
//  Presence — session id, activity tracking, page visibility
// ============================================================================

const SESSION_ID_KEY = 'parrot.session_id';
const LAST_SEEN_KEY  = 'parrot.last_seen_at';
const IDLE_MS        = 60 * 1000;   // report idle after 60s of no interaction

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

    const url = `ws://${bridgeHost}:${bridgePort}/ws/live`;
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

    const url = `ws://${bridgeHost}:${bridgePort}/ws/monitor`;
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

// ============================================================================
//  Message handling — /ws/live
// ============================================================================

function handleLiveMessage(msg) {
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

        default:
            break;
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
        const resp = await fetch(`http://${bridgeHost}:${bridgePort}/api/conversation?limit=${GCHAT_MAX}`);
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
        const url = `http://${bridgeHost}:${bridgePort}/chat/stream?text=${encodeURIComponent(text)}&client_id=${GCHAT_CLIENT_ID}`;
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

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

                } else if (evt.type === 'segment') {
                    const typing = groupEl.querySelector('.gchat-typing');
                    if (typing) typing.remove();
                    const thinkBlock = groupEl.querySelector('.gchat-thinking');
                    if (thinkBlock) thinkBlock.classList.add('collapsed');
                    const segIdx = gchatExchanges[exIdx].segments.length;
                    gchatExchanges[exIdx].segments.push(evt);
                    const sdiv = document.createElement('div');
                    sdiv.className = 'gchat-msg assistant';
                    const emo = evt.emotion ? `<span class="gchat-emotion">${escapeHtml(evt.emotion)}</span>` : '';
                    const play = evt.audio_base64
                        ? ` <button class="gchat-seg-play" onclick="gchatPlaySeg(${exIdx},${segIdx})">&#x1f50a;</button>` : '';
                    sdiv.innerHTML = `<div class="gchat-bubble">${emo}${escapeHtml(evt.text)}${play}</div>`;
                    groupEl.appendChild(sdiv);

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

// -- Audio playback --

function _gchatStopAudio() {
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
        const resp = await fetch(`http://${bridgeHost}:${bridgePort}/api/tts`, {
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

        // Play audio with synchronized visemes (motion is now handled by animation controller)
        if (seg.audio_base64) {
            await playSegment(
                seg.audio_base64,
                null,    // no more per-segment motion_b64
                visemes,
                30,
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
//  Audio playback with analyser + synchronized motion/viseme timers
// ============================================================================

/**
 * Play one audio segment with optional synchronized motion + viseme data.
 * Returns a promise that resolves when the audio finishes playing.
 *
 * @param {string}   audioB64     - Base64-encoded audio (WAV/OGG)
 * @param {Object[]} motionFrames - Decoded motion frames, or null
 * @param {number[][]} visemeFrames - Decoded viseme frames, or null
 * @param {number}   motionFps    - Motion playback frame rate
 * @param {number}   visemeFps    - Viseme playback frame rate
 * @returns {Promise<void>}
 */
async function playSegment(audioB64, motionFrames, visemeFrames, motionFps, visemeFps) {
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

            // --- Motion playback timer ---
            let mf = 0;
            let mt = null;
            if (motionFrames && motionFrames.length > 0) {
                mt = setInterval(() => {
                    if (mf >= motionFrames.length) { clearInterval(mt); return; }
                    if (window._applyBones) window._applyBones(motionFrames[mf]);
                    mf++;
                }, 1000 / motionFps);
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
                if (mt) clearInterval(mt);
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

function setupTabs() {
    // Modal tabs (inside settings modal)
    document.querySelectorAll('.modal-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.modal-tab').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.modal-content').forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            const panel = document.getElementById('modal-' + btn.dataset.tab);
            if (panel) panel.classList.add('active');
        });
    });

    // Gear button opens modal
    const backdrop = document.getElementById('modalBackdrop');
    document.getElementById('btnGear')?.addEventListener('click', () => {
        backdrop?.classList.add('open');
    });
    document.getElementById('btnCloseModal')?.addEventListener('click', () => {
        backdrop?.classList.remove('open');
    });
    // Click backdrop to close
    backdrop?.addEventListener('click', (e) => {
        if (e.target === backdrop) backdrop.classList.remove('open');
    });
    // Escape key closes modal
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') backdrop?.classList.remove('open');
    });
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
                await fetch(`http://${bridgeHost}:${bridgePort}/admin/tts/restart`, { method: 'POST' });
                if (debugBar) debugBar.textContent = 'TTS restarted';
            } catch (e) {
                console.error('TTS restart failed:', e);
                if (debugBar) debugBar.textContent = 'TTS restart failed';
            }
            btnRestartTTS.disabled = false;
        });
    }

    // Clear memory
    const btnClearMemory = document.getElementById('btnClearMemory');
    if (btnClearMemory) {
        btnClearMemory.addEventListener('click', async () => {
            if (!confirm('Clear all memory? This cannot be undone.')) return;
            btnClearMemory.disabled = true;
            try {
                await fetch(`http://${bridgeHost}:${bridgePort}/admin/clear-memory`, { method: 'POST' });
                if (debugBar) debugBar.textContent = 'Memory cleared';
            } catch (e) {
                console.error('Memory clear failed:', e);
                if (debugBar) debugBar.textContent = 'Memory clear failed';
            }
            btnClearMemory.disabled = false;
        });
    }

    // Shiro toggle
    const btnShiroToggle = document.getElementById('btnShiroToggle');
    if (btnShiroToggle) {
        btnShiroToggle.addEventListener('click', async () => {
            btnShiroToggle.disabled = true;
            try {
                await fetch(`http://${bridgeHost}:${bridgePort}/admin/shiro/toggle`, { method: 'POST' });
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
                const resp = await fetch(`http://${bridgeHost}:${bridgePort}/admin/reload-tools`, { method: 'POST' });
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
        const resp = await fetch(`http://${bridgeHost}:${bridgePort}/api/conversation?limit=${limit || 50}`);
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
    // 1. Fetch /api/config to get bridge port
    try {
        const resp = await fetch('/api/config');
        if (resp.ok) {
            const cfg = await resp.json();
            bridgePort = cfg.bridge_port || cfg.port || 8000;
        }
    } catch (e) {
        console.warn('/api/config not available, falling back to defaults');
    }

    // Fallback: use BRIDGE_PORT if set globally (from inline HTML template)
    if (!bridgePort && typeof BRIDGE_PORT !== 'undefined') {
        bridgePort = BRIDGE_PORT;
    }
    if (!bridgePort) {
        bridgePort = 8000;
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
    setupAdminButtons();
    setupGlobalChat();

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

    // 5. Bootstrap active theme's non-CSS assets (background image is in
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
