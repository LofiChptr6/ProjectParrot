/**
 * ui-orchestrator.js — Timing synchronization for ProjectParrot
 *
 * Holds ui_command messages (show_slides, show_card, etc.) in a staging
 * queue until Mocha actually starts speaking. This prevents presentations
 * from popping up 10-20s early while Nori is still researching.
 *
 * Also holds Mocha's chat bubbles so text + audio appear in sync.
 */

(function () {
    'use strict';

    const state = {
        uiQueue: [],         // queued ui_command messages
        chatQueue: [],       // queued { sender, text, emotion } for chat bubbles
        armed: false,        // true = waiting for speech to start
        flushTimer: null,    // safety timeout
    };

    const SAFETY_TIMEOUT_MS = 8000; // auto-flush if no speech arrives

    /**
     * Intercept a ui_command message.
     * If speech is already playing, pass through immediately.
     * Otherwise, queue it until playback starts.
     */
    // Actions that are interactive / user-initiated and shouldn't be held
    // back for speech-sync. show_diary is one — user said "open my diary",
    // they want the modal now, not after an 8s safety timeout.
    const _IMMEDIATE_ACTIONS = new Set(['show_diary', 'show_weather']);

    function intercept(msg) {
        // Bypass the queue entirely for interactive commands.
        if (msg && _IMMEDIATE_ACTIONS.has(msg.action)) {
            _deliverUiCommand(msg);
            return;
        }
        // If already playing audio, deliver immediately (late-arriving UI update)
        if (_isSpeechActive()) {
            _deliverUiCommand(msg);
            return;
        }

        // Queue for later delivery
        state.uiQueue.push(msg);
        _arm();
    }

    /**
     * Intercept a Mocha chat bubble.
     * If armed (waiting for speech), hold it so text appears with audio.
     * Otherwise pass through immediately.
     */
    function interceptChat(sender, text, emotion, extras) {
        if (state.armed && sender === 'mocha') {
            state.chatQueue.push({ sender, text, emotion, extras });
            return;
        }
        // Pass through
        _appendChat(sender, text, emotion, extras);
    }

    /**
     * Called when playNext() starts playback (isPlaying becomes true).
     * Flushes all queued commands and chat bubbles.
     */
    function onPlaybackStart() {
        if (!state.armed) return;
        _flush();
    }

    /**
     * Force-flush everything (e.g., if segments arrive without audio).
     */
    function flush() { _flush(); }

    /**
     * Reset on interrupt — clear all queued items.
     */
    function reset() {
        state.uiQueue.length = 0;
        state.chatQueue.length = 0;
        state.armed = false;
        if (state.flushTimer) {
            clearTimeout(state.flushTimer);
            state.flushTimer = null;
        }
    }

    // ── Internal ──────────────────────────────────────────────────────────

    function _arm() {
        if (state.armed) return;
        state.armed = true;
        // Safety: auto-flush after timeout in case speech never comes
        state.flushTimer = setTimeout(() => {
            if (state.armed) _flush();
        }, SAFETY_TIMEOUT_MS);
    }

    function _flush() {
        state.armed = false;
        if (state.flushTimer) {
            clearTimeout(state.flushTimer);
            state.flushTimer = null;
        }

        // Deliver all queued UI commands
        for (const msg of state.uiQueue) {
            _deliverUiCommand(msg);
        }
        state.uiQueue.length = 0;

        // Deliver all queued chat bubbles
        for (const { sender, text, emotion, extras } of state.chatQueue) {
            _appendChat(sender, text, emotion, extras);
        }
        state.chatQueue.length = 0;
    }

    function _deliverUiCommand(msg) {
        // Diary modal has its own dedicated handler — route show_diary there
        // before the presentation fallback so we don't stuff diary state into
        // the slides panel.
        if (msg && msg.action === 'show_diary') {
            if (!window.Diary) {
                console.error('[UIOrchestrator] show_diary received but window.Diary is undefined — diary.js may not have loaded');
                return;
            }
            console.log('[UIOrchestrator] dispatching show_diary', msg);
            window.Diary.open(msg.date || null);
            return;
        }
        // Same dedicated-handler pattern for the weather modal.
        if (msg && msg.action === 'show_weather') {
            if (!window.Weather) {
                console.error('[UIOrchestrator] show_weather received but window.Weather is undefined — weather.js may not have loaded');
                return;
            }
            console.log('[UIOrchestrator] dispatching show_weather', msg);
            window.Weather.open(msg.payload || {});
            return;
        }
        if (window._presentation) {
            window._presentation.handleUiCommand(msg);
        }
    }

    function _appendChat(sender, text, emotion, extras) {
        // appendChat is defined in app.js (loaded after this file)
        if (typeof appendChat === 'function') {
            appendChat(sender, text, emotion, extras);
        }
    }

    function _isSpeechActive() {
        // isPlaying is a global in app.js
        return typeof isPlaying !== 'undefined' && isPlaying;
    }

    // ── Public API ────────────────────────────────────────────────────────

    window.UIOrchestrator = {
        intercept,
        interceptChat,
        onPlaybackStart,
        flush,
        reset,
    };

})();
