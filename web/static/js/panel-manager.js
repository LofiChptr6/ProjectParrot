/**
 * panel-manager.js — Layout orchestration for ProjectParrot
 *
 * Manages: chat sidebar toggle, panel drag/resize, responsive breakpoints,
 * dynamic camera computation, localStorage persistence.
 */

(function () {
    'use strict';

    // ========================================================================
    //  State
    // ========================================================================

    const state = {
        chatVisible: true,
        breakpointNarrow: false,
        panels: {},          // key -> { el, headerEl, defaultRect, savedRect, currentRect }
        listeners: {},       // event -> [callbacks]
    };

    const STORAGE_PREFIX = 'parrot.panel.';
    const MIN_W = 300;
    const MIN_H = 200;

    // ========================================================================
    //  Chat sidebar toggle
    // ========================================================================

    function toggleChatSidebar(force) {
        const layout = document.querySelector('.main-layout');
        if (!layout) return;
        const show = typeof force === 'boolean' ? force : !state.chatVisible;
        state.chatVisible = show;
        layout.classList.toggle('chat-collapsed', !show);
        _save('chat.visible', show);
        // Re-optimize panel positions after sidebar transition
        setTimeout(autoLayout, 350);
        _emit('layout-changed');
    }

    function isChatVisible() { return state.chatVisible; }

    // ========================================================================
    //  Responsive breakpoint
    // ========================================================================

    function isNarrow() { return state.breakpointNarrow; }

    function _checkNarrow() {
        // Narrow if viewport < 800px wide OR portrait aspect
        return window.innerWidth < 800 || window.innerHeight > window.innerWidth;
    }

    function _onBreakpointChange() {
        const narrow = _checkNarrow();
        if (narrow === state.breakpointNarrow) return;
        state.breakpointNarrow = narrow;
        const presPanel = state.panels.presentation?.el;

        if (narrow) {
            // Portrait/narrow: hide chat, presentation avoids Mocha's face
            toggleChatSidebar(false);
            if (presPanel) presPanel.classList.add('panel-narrow-mode');
        } else {
            // Landscape: restore chat state, remove narrow overrides
            const savedChat = _load('chat.visible');
            toggleChatSidebar(savedChat !== false);
            if (presPanel) presPanel.classList.remove('panel-narrow-mode');
            // Restore saved panel positions
            for (const [key, p] of Object.entries(state.panels)) {
                const saved = _load(key + '.rect');
                if (saved) _applyRect(p.el, saved);
            }
        }
        _emit('layout-changed');
    }

    // ========================================================================
    //  Panel registration + drag/resize
    // ========================================================================

    function registerPanel(key, el, defaultRect) {
        if (!el) return;
        const headerEl = el.querySelector('.pres-header');
        const savedRect = _load(key + '.rect');
        const p = {
            el, headerEl, defaultRect, savedRect, key,
            pinned: !!savedRect,  // pinned = user manually dragged before
        };
        state.panels[key] = p;

        el.classList.add('panel-draggable');

        // Drag via header
        if (headerEl) _attachDrag(p);

        // Resize grippers
        _attachGrippers(p);
    }

    function _attachDrag(p) {
        let startX, startY, startLeft, startTop, isDragging = false;

        p.headerEl.addEventListener('mousedown', (e) => {
            if (e.target.closest('.pres-nav-btn')) return; // don't drag on nav buttons
            e.preventDefault();
            isDragging = true;
            p.el.classList.add('panel-dragging');
            const rect = p.el.getBoundingClientRect();
            startX = e.clientX; startY = e.clientY;
            startLeft = rect.left; startTop = rect.top;
        });

        // Double-click to reset
        p.headerEl.addEventListener('dblclick', () => resetRect(p.key));

        document.addEventListener('mousemove', (e) => {
            if (!isDragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            let newLeft = startLeft + dx;
            let newTop = startTop + dy;
            // Constrain to viewport
            newLeft = Math.max(0, Math.min(window.innerWidth - p.el.offsetWidth, newLeft));
            newTop = Math.max(0, Math.min(window.innerHeight - p.el.offsetHeight, newTop));
            p.el.style.left = newLeft + 'px';
            p.el.style.top = newTop + 'px';
            p.el.style.right = 'auto';
            p.el.style.bottom = 'auto';
        });

        document.addEventListener('mouseup', () => {
            if (!isDragging) return;
            isDragging = false;
            p.el.classList.remove('panel-dragging');
            p.pinned = true;  // user manually positioned
            _saveCurrentRect(p);
            _emit('panel-moved', p.key);
        });
    }

    function _attachGrippers(p) {
        const directions = [
            { cls: 'panel-grip-edge panel-grip-edge--right',  dir: 'e' },
            { cls: 'panel-grip-edge panel-grip-edge--left',   dir: 'w' },
            { cls: 'panel-grip-edge panel-grip-edge--top',    dir: 'n' },
            { cls: 'panel-grip-edge panel-grip-edge--bottom', dir: 's' },
            { cls: 'panel-grip-corner panel-grip-corner--se', dir: 'se' },
            { cls: 'panel-grip-corner panel-grip-corner--sw', dir: 'sw' },
            { cls: 'panel-grip-corner panel-grip-corner--ne', dir: 'ne' },
            { cls: 'panel-grip-corner panel-grip-corner--nw', dir: 'nw' },
        ];

        for (const { cls, dir } of directions) {
            const grip = document.createElement('div');
            grip.className = cls;
            grip.dataset.resizeDir = dir;
            p.el.appendChild(grip);

            let startX, startY, startRect;

            grip.addEventListener('mousedown', (e) => {
                e.preventDefault(); e.stopPropagation();
                p.el.classList.add('panel-dragging');
                startX = e.clientX; startY = e.clientY;
                startRect = p.el.getBoundingClientRect();

                const onMove = (e2) => {
                    const dx = e2.clientX - startX;
                    const dy = e2.clientY - startY;
                    let { left, top, width, height } = startRect;

                    if (dir.includes('e')) width = Math.max(MIN_W, width + dx);
                    if (dir.includes('w')) { width = Math.max(MIN_W, width - dx); left = startRect.right - width; }
                    if (dir.includes('s')) height = Math.max(MIN_H, height + dy);
                    if (dir.includes('n')) { height = Math.max(MIN_H, height - dy); top = startRect.bottom - height; }

                    p.el.style.left = left + 'px';
                    p.el.style.top = top + 'px';
                    p.el.style.right = 'auto';
                    p.el.style.bottom = 'auto';
                    p.el.style.width = width + 'px';
                    p.el.style.height = height + 'px';
                };

                const onUp = () => {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    p.el.classList.remove('panel-dragging');
                    _saveCurrentRect(p);
                    _emit('panel-moved', p.key);
                };

                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            });
        }
    }

    function resetRect(key) {
        const p = state.panels[key];
        if (!p) return;
        localStorage.removeItem(STORAGE_PREFIX + key + '.rect');
        p.savedRect = null;
        p.pinned = false;  // back to auto-layout
        // Clear inline styles, let autoLayout handle it
        p.el.style.left = ''; p.el.style.top = '';
        p.el.style.right = ''; p.el.style.bottom = '';
        p.el.style.width = ''; p.el.style.height = '';
        autoLayout();
        _emit('panel-moved', key);
    }

    function getPanelRect(key) {
        const p = state.panels[key];
        return p?.el?.getBoundingClientRect() || null;
    }

    function _applyRect(el, rect) {
        if (rect.left != null) el.style.left = rect.left + 'px';
        if (rect.top != null) el.style.top = rect.top + 'px';
        if (rect.width != null) el.style.width = rect.width + 'px';
        if (rect.height != null) el.style.height = rect.height + 'px';
        el.style.right = 'auto';
        el.style.bottom = 'auto';
    }

    function _saveCurrentRect(p) {
        if (state.breakpointNarrow) return; // don't save narrow positions
        const rect = p.el.getBoundingClientRect();
        const data = { left: Math.round(rect.left), top: Math.round(rect.top),
                       width: Math.round(rect.width), height: Math.round(rect.height) };
        p.savedRect = data;
        _save(p.key + '.rect', data);
    }

    // ========================================================================
    //  Dynamic camera computation
    // ========================================================================

    // ========================================================================
    //  Auto-layout — dynamically optimize panel positions
    // ========================================================================

    function autoLayout() {
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const chatW = state.chatVisible ? 360 : 0;
        const narrow = state.breakpointNarrow;

        for (const [key, p] of Object.entries(state.panels)) {
            if (!p.el.classList.contains('active')) continue; // skip hidden panels

            if (p.pinned) {
                // Pinned panels: just constrain to viewport
                _constrainToViewport(p);
                continue;
            }

            // Auto-position based on available space
            if (key === 'presentation') {
                if (narrow) {
                    // Portrait: bottom-right, avoid Mocha's face (top-left)
                    p.el.style.top = Math.round(vh * 0.30) + 'px';
                    p.el.style.left = 'auto';
                    p.el.style.right = '8px';
                    p.el.style.bottom = '8px';
                    p.el.style.width = Math.round(vw * 0.65) + 'px';
                    p.el.style.height = '';
                } else {
                    // Landscape: right side, next to chat sidebar
                    const panelW = Math.round(Math.min(vw * 0.50, vw - chatW - 200));
                    const panelRight = chatW + 8;
                    p.el.style.top = '8px';
                    p.el.style.right = panelRight + 'px';
                    p.el.style.bottom = '8px';
                    p.el.style.left = 'auto';
                    p.el.style.width = Math.max(MIN_W, panelW) + 'px';
                    p.el.style.height = '';
                }
            }
        }
    }

    function _constrainToViewport(p) {
        const rect = p.el.getBoundingClientRect();
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        let changed = false;

        // If panel is off-screen right
        if (rect.right > vw) {
            p.el.style.left = Math.max(0, vw - rect.width) + 'px';
            p.el.style.right = 'auto';
            changed = true;
        }
        // If panel is off-screen bottom
        if (rect.bottom > vh) {
            p.el.style.top = Math.max(0, vh - rect.height) + 'px';
            p.el.style.bottom = 'auto';
            changed = true;
        }
        // If panel is off-screen left
        if (rect.left < 0) {
            p.el.style.left = '0px';
            changed = true;
        }
        // If panel is too wide for viewport
        if (rect.width > vw * 0.9) {
            p.el.style.width = Math.round(vw * 0.9) + 'px';
            changed = true;
        }
        if (changed) _saveCurrentRect(p);
    }

    // Run autoLayout on window resize (debounced)
    let _layoutTimer = null;
    window.addEventListener('resize', () => {
        if (_layoutTimer) clearTimeout(_layoutTimer);
        _layoutTimer = setTimeout(autoLayout, 150);
    });

    const CAM_CENTER = { camX: 0, camY: 1.25, camZ: 3.0, tgtX: 0, tgtY: 1.0, tgtZ: 0 };

    /**
     * Compute ideal camera position based on ALL visible panels/sidebars.
     * Called every frame by the render loop to smoothly drift Mocha
     * into the largest unoccupied region.
     */
    function getIdealCamera() {
        const canvas = document.getElementById('canvasArea');
        if (!canvas) return CAM_CENTER;
        const cRect = canvas.getBoundingClientRect();
        if (!cRect.width) return CAM_CENTER;

        // Collect all right-side obstructions as fractions of canvas width
        let rightOccupied = 0;  // how much of the right side is blocked (0-1)

        // Chat sidebar (now a floating overlay on the right)
        if (state.chatVisible) {
            const sidebar = document.querySelector('.chat-sidebar');
            if (sidebar && !sidebar.closest('.chat-collapsed')) {
                rightOccupied = Math.max(rightOccupied, sidebar.offsetWidth / cRect.width);
            }
        }

        // Presentation panel (if active and positioned on the right half)
        const presPanel = state.panels.presentation?.el;
        if (presPanel && presPanel.classList.contains('active')) {
            const pRect = presPanel.getBoundingClientRect();
            // How far from the right edge of canvas does the panel extend?
            const panelRightEdge = cRect.right - pRect.left;
            const panelFrac = panelRightEdge / cRect.width;
            // Only count it if panel is on the right half
            if (pRect.left > cRect.left + cRect.width * 0.3) {
                rightOccupied = Math.max(rightOccupied, panelFrac);
            }
        }

        // Narrow/portrait mode: don't shift horizontally
        if (state.breakpointNarrow) return CAM_CENTER;

        // If nothing blocking, center
        if (rightOccupied < 0.05) return CAM_CENTER;

        // Shift camera to put Mocha in center of free space
        // Free space is (1 - rightOccupied) of canvas width, on the left side
        // Map to camera X: more obstruction → more shift, capped at 0.6
        const camX = Math.min(0.6, rightOccupied * 0.55);
        const tgtX = camX + 0.15;  // slight rightward angle

        return { camX, camY: 1.25, camZ: 3.0, tgtX, tgtY: 1.0, tgtZ: 0 };
    }

    // Legacy method (still used as fallback by presentation.js)
    function computeCamPresent(panelRect, canvasRect) {
        return getIdealCamera();
    }

    // ========================================================================
    //  Simple event emitter
    // ========================================================================

    function on(event, cb) {
        if (!state.listeners[event]) state.listeners[event] = [];
        state.listeners[event].push(cb);
    }

    function _emit(event, data) {
        for (const cb of (state.listeners[event] || [])) cb(data);
        document.dispatchEvent(new CustomEvent('parrot:' + event, { detail: data }));
    }

    // ========================================================================
    //  LocalStorage helpers
    // ========================================================================

    function _save(key, val) {
        try { localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(val)); } catch (e) {}
    }
    function _load(key) {
        try { return JSON.parse(localStorage.getItem(STORAGE_PREFIX + key)); } catch (e) { return null; }
    }

    // ========================================================================
    //  Init on DOMContentLoaded
    // ========================================================================

    document.addEventListener('DOMContentLoaded', () => {
        // Chat toggle: status bar button re-opens, sidebar X button closes
        const toggleBtn = document.getElementById('btnChatToggle');
        if (toggleBtn) toggleBtn.addEventListener('click', () => toggleChatSidebar(true));
        const collapseBtn = document.getElementById('btnChatCollapse');
        if (collapseBtn) collapseBtn.addEventListener('click', () => toggleChatSidebar(false));

        // Restore saved chat state
        const savedChat = _load('chat.visible');
        if (savedChat === false) toggleChatSidebar(false);

        // Responsive breakpoint (width < 800px OR portrait aspect)
        state.breakpointNarrow = _checkNarrow();
        if (state.breakpointNarrow) {
            const tmp = state.breakpointNarrow;
            state.breakpointNarrow = !tmp; // force change detection
            _onBreakpointChange();
        }
        window.addEventListener('resize', _onBreakpointChange);

        // ── Auto-collapse chat after 2 min idle, re-open on keypress ──
        const IDLE_COLLAPSE_MS = 60 * 1000;
        let _idleTimer = null;
        const textInput = document.getElementById('textInput');

        function _resetIdleTimer() {
            if (_idleTimer) clearTimeout(_idleTimer);
            _idleTimer = setTimeout(() => {
                if (state.chatVisible) toggleChatSidebar(false);
            }, IDLE_COLLAPSE_MS);
        }

        // Any mouse/touch/click resets the idle timer (but doesn't re-open chat)
        for (const evt of ['mousemove', 'mousedown', 'touchstart', 'click']) {
            document.addEventListener(evt, _resetIdleTimer, { passive: true });
        }

        // Keyboard: re-open chat + focus text input + forward the key
        document.addEventListener('keydown', (e) => {
            _resetIdleTimer();
            // Don't hijack if user is in an input, modal, or calibration panel
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if (e.ctrlKey || e.altKey || e.metaKey) return;
            // Only printable characters (single char keys)
            if (e.key.length !== 1) return;

            // Re-open chat if collapsed
            if (!state.chatVisible) toggleChatSidebar(true);

            // Focus text input and let the character go there
            if (textInput) {
                textInput.focus();
                // The keydown event will naturally type into the now-focused input
            }
        });

        _resetIdleTimer();
    });

    // ========================================================================
    //  Public API
    // ========================================================================

    window.PanelManager = {
        registerPanel,
        toggleChatSidebar,
        isChatVisible,
        isNarrow,
        getIdealCamera,
        computeCamPresent,
        autoLayout,
        getPanelRect,
        resetRect,
        on,
    };

})();
