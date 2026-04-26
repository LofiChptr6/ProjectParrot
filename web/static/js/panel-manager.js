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

    // Phone / portrait detection — used to default the chat sidebar to
    // collapsed on phone (the bottom-sheet covers Mocha + iOS keyboard makes
    // it worse). matchMedia stays live, so orientation flips are handled too.
    function _isPhone() {
        return window.matchMedia(
            '(orientation: portrait) and (max-width: 900px), (max-width: 600px)'
        ).matches;
    }

    // Read either mouse or touch coords from an event in one place.
    function _ptr(e) {
        if (e.touches && e.touches[0])
            return { x: e.touches[0].clientX, y: e.touches[0].clientY };
        if (e.changedTouches && e.changedTouches[0])
            return { x: e.changedTouches[0].clientX, y: e.changedTouches[0].clientY };
        return { x: e.clientX, y: e.clientY };
    }

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

        // Single drag-start handler — used by both mousedown and touchstart so
        // the desktop and phone code paths stay aligned.
        const _onStart = (e) => {
            if (e.target.closest('.pres-nav-btn')) return; // don't drag on nav buttons
            e.preventDefault();
            const pt = _ptr(e);
            isDragging = true;
            p.el.classList.add('panel-dragging');
            const rect = p.el.getBoundingClientRect();
            startX = pt.x; startY = pt.y;
            startLeft = rect.left; startTop = rect.top;
        };

        p.headerEl.addEventListener('mousedown', _onStart);
        // passive:false so we can preventDefault and stop the page from
        // pan-scrolling under the finger.
        p.headerEl.addEventListener('touchstart', _onStart, { passive: false });

        // Double-click to reset (mouse only — touch users get a long-press
        // alternative below or just hit the X button).
        p.headerEl.addEventListener('dblclick', () => resetRect(p.key));

        const _onMove = (e) => {
            if (!isDragging) return;
            if (e.touches) e.preventDefault();  // block page scroll while dragging
            const pt = _ptr(e);
            const dx = pt.x - startX;
            const dy = pt.y - startY;
            let newLeft = startLeft + dx;
            let newTop = startTop + dy;
            // Constrain to viewport
            newLeft = Math.max(0, Math.min(window.innerWidth - p.el.offsetWidth, newLeft));
            newTop = Math.max(0, Math.min(window.innerHeight - p.el.offsetHeight, newTop));
            p.el.style.left = newLeft + 'px';
            p.el.style.top = newTop + 'px';
            p.el.style.right = 'auto';
            p.el.style.bottom = 'auto';
        };

        const _onEnd = () => {
            if (!isDragging) return;
            isDragging = false;
            p.el.classList.remove('panel-dragging');
            p.pinned = true;  // user manually positioned
            _saveCurrentRect(p);
            _emit('panel-moved', p.key);
        };

        document.addEventListener('mousemove', _onMove);
        document.addEventListener('mouseup', _onEnd);
        document.addEventListener('touchmove', _onMove, { passive: false });
        document.addEventListener('touchend', _onEnd);
        document.addEventListener('touchcancel', _onEnd);
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

            const _onGripStart = (e) => {
                const pt = _ptr(e);
                // Corner grippers extend 8px inside the panel and sit above
                // the header (z-index 12), so they overlap the close/prev/next
                // nav buttons in the top-right. If a button sits under the
                // pointer, defer to it — install a one-shot up handler that
                // fires a click on the button if the user didn't drag away.
                const below = document.elementsFromPoint(pt.x, pt.y);
                const btn = below
                    .find(el => el !== grip && el.closest?.('.pres-nav-btn, .video-player-btn, button'))
                    ?.closest('.pres-nav-btn, .video-player-btn, button');
                if (btn) {
                    const downX = pt.x, downY = pt.y;
                    const onUp = (e2) => {
                        document.removeEventListener('mouseup', onUp, true);
                        document.removeEventListener('touchend', onUp, true);
                        const upPt = _ptr(e2);
                        const dx = upPt.x - downX, dy = upPt.y - downY;
                        if (dx * dx + dy * dy < 25) btn.click();  // <5px = click
                    };
                    document.addEventListener('mouseup', onUp, true);
                    document.addEventListener('touchend', onUp, true);
                    return;  // don't initiate a resize drag
                }

                e.preventDefault(); e.stopPropagation();
                p.el.classList.add('panel-dragging');
                startX = pt.x; startY = pt.y;
                startRect = p.el.getBoundingClientRect();

                const onMove = (e2) => {
                    if (e2.touches) e2.preventDefault();
                    const mp = _ptr(e2);
                    const dx = mp.x - startX;
                    const dy = mp.y - startY;
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
                    document.removeEventListener('touchmove', onMove);
                    document.removeEventListener('touchend', onUp);
                    document.removeEventListener('touchcancel', onUp);
                    p.el.classList.remove('panel-dragging');
                    _saveCurrentRect(p);
                    _emit('panel-moved', p.key);
                };

                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
                document.addEventListener('touchmove', onMove, { passive: false });
                document.addEventListener('touchend', onUp);
                document.addEventListener('touchcancel', onUp);
            };

            grip.addEventListener('mousedown', _onGripStart);
            grip.addEventListener('touchstart', _onGripStart, { passive: false });
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
        // For panels in the "mini" state (e.g. the collapsed video player
        // pill), CSS owns the width/height — restoring the saved rect's
        // full-state dimensions here would blow the pill out to 480+ px,
        // overflowing its text-overflow ellipsis. Only restore position.
        const isMini = el.dataset.state === 'mini';
        if (rect.left != null) el.style.left = rect.left + 'px';
        if (rect.top != null) el.style.top = rect.top + 'px';
        if (!isMini) {
            if (rect.width != null) el.style.width = rect.width + 'px';
            if (rect.height != null) el.style.height = rect.height + 'px';
        }
        el.style.right = 'auto';
        el.style.bottom = 'auto';
    }

    function _saveCurrentRect(p) {
        if (state.breakpointNarrow) return; // don't save narrow positions
        const isMini = p.el.dataset.state === 'mini';
        const rect = p.el.getBoundingClientRect();
        const data = { left: Math.round(rect.left), top: Math.round(rect.top) };
        if (isMini) {
            // In mini state, the pill's width/height come from CSS. Preserve
            // any previously-saved full-state dimensions instead of overwriting
            // with the mini size — otherwise next time the panel expands to
            // full, it snaps to the tiny pill shape.
            if (p.savedRect?.width != null)  data.width  = p.savedRect.width;
            if (p.savedRect?.height != null) data.height = p.savedRect.height;
        } else {
            data.width  = Math.round(rect.width);
            data.height = Math.round(rect.height);
        }
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

    // Any visible element matching this selector will be treated as an obstruction
    // for Mocha's placement. Future UI components should add the `.mocha-avoid`
    // class or `data-mocha-avoid="true"` to opt in without touching this list.
    // NOTE: settings modal is deliberately NOT in this list — it's allowed to cover Mocha.
    const AVOID_SELECTORS = [
        '.mocha-avoid',
        '[data-mocha-avoid="true"]',
        '.chat-sidebar',
        '.presentation-panel.active',
        '.video-player[data-state="full"]',
        '.gchat-popup',
        '.card-container',
    ].join(', ');

    // Minimum rect size to bother treating as an obstruction (px).
    const OBSTRUCTION_MIN_SIZE = 24;

    function _collectObstructions() {
        const vw = window.innerWidth, vh = window.innerHeight;
        const portrait = vh > vw;
        // On portrait, anything anchored in the bottom half of the screen
        // (chat sheet, floating input bar, future bottom toolbars) sits BELOW
        // Mocha — it shouldn't shift her horizontally. Skip it at collection
        // time so the band algo never sees it. Threshold = 45% so a panel
        // exactly at 50vh top-edge still doesn't count even mid-animation.
        const skipBelow = portrait ? vh * 0.45 : Infinity;
        const out = [];
        for (const el of document.querySelectorAll(AVOID_SELECTORS)) {
            if (el.dataset && el.dataset.mochaAvoid === 'false') continue;
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) < 0.05) continue;
            const r = el.getBoundingClientRect();
            if (r.width < OBSTRUCTION_MIN_SIZE || r.height < OBSTRUCTION_MIN_SIZE) continue;
            if (r.right <= 0 || r.bottom <= 0 || r.left >= vw || r.top >= vh) continue;
            // card-container: skip if no visible children
            if (el.classList.contains('card-container') && el.children.length === 0) continue;
            // Portrait: bottom-anchored panels are below Mocha, not next to her.
            if (r.top >= skipBelow) continue;
            out.push({
                left:   Math.max(0, r.left),
                top:    Math.max(0, r.top),
                right:  Math.min(vw, r.right),
                bottom: Math.min(vh, r.bottom),
            });
        }
        return out;
    }

    // Mocha is a standing character: she needs horizontal clearance at her
    // body height, and Y stays fixed (no vertical sliding). So we solve this
    // as a 1D problem: take a horizontal cross-section at Mocha's body band,
    // project every obstruction that crosses that band onto the X axis, then
    // pick the widest free interval.
    //
    // Mocha's body band covers shoulders-to-feet in screen coords. In
    // portrait mode the chat sidebar reflows to a 50vh bottom sheet — if
    // we kept the wide landscape band, the sheet would briefly count as a
    // partial-width X obstruction during its slide-in animation and shove
    // Mocha left for ~300ms. Narrowing the band to the upper half on
    // portrait makes her ignore anything anchored to the bottom.
    const MOCHA_BAND_TOP_LANDSCAPE    = 0.20;
    const MOCHA_BAND_BOTTOM_LANDSCAPE = 0.95;
    const MOCHA_BAND_TOP_PORTRAIT     = 0.05;
    const MOCHA_BAND_BOTTOM_PORTRAIT  = 0.50;

    function _findBestXBand(vw, vh, rects) {
        const portrait = vh > vw;
        const bandTop    = vh * (portrait ? MOCHA_BAND_TOP_PORTRAIT    : MOCHA_BAND_TOP_LANDSCAPE);
        const bandBottom = vh * (portrait ? MOCHA_BAND_BOTTOM_PORTRAIT : MOCHA_BAND_BOTTOM_LANDSCAPE);

        // Keep only obstructions whose vertical extent crosses Mocha's band.
        // A rect that sits entirely above or below the band doesn't block her.
        const spans = [];
        for (const r of rects) {
            if (r.bottom < bandTop || r.top > bandBottom) continue;
            spans.push([r.left, r.right]);
        }

        // Merge overlapping X spans.
        spans.sort((a, b) => a[0] - b[0]);
        const merged = [];
        for (const [l, r] of spans) {
            if (merged.length && l <= merged[merged.length - 1][1]) {
                merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], r);
            } else {
                merged.push([l, r]);
            }
        }

        // Build free intervals over [0, vw].
        const free = [];
        let cursor = 0;
        for (const [l, r] of merged) {
            if (l > cursor) free.push([cursor, l]);
            cursor = Math.max(cursor, r);
        }
        if (cursor < vw) free.push([cursor, vw]);

        if (free.length === 0) return null;

        // Widest wins. Ties broken by proximity to viewport center so Mocha
        // doesn't hug an edge when two equal-sized gaps exist.
        let best = free[0];
        let bestW = best[1] - best[0];
        let bestDistToCenter = Math.abs((best[0] + best[1]) / 2 - vw / 2);
        for (let i = 1; i < free.length; i++) {
            const iv = free[i];
            const w = iv[1] - iv[0];
            const d = Math.abs((iv[0] + iv[1]) / 2 - vw / 2);
            if (w > bestW + 1 || (Math.abs(w - bestW) <= 1 && d < bestDistToCenter)) {
                best = iv; bestW = w; bestDistToCenter = d;
            }
        }

        return { left: best[0], right: best[1], width: bestW };
    }

    // Assumed VRM camera FOV (matches the default in vrm-renderer.js).
    const FOV_DEG = 25;
    const TAN_HALF_FOV = Math.tan((FOV_DEG / 2) * Math.PI / 180);

    /**
     * Compute ideal camera position by finding the viewport's largest
     * whitespace region and projecting its center into world-space
     * camera offsets. Called every frame by the render loop.
     */
    function getIdealCamera() {
        const vw = window.innerWidth, vh = window.innerHeight;
        // NOTE: intentionally do NOT bail in state.breakpointNarrow — the 1D
        // band algo works just as well in portrait. The earlier bailout was
        // why portrait viewports pinned Mocha behind whatever modal was open.

        const rects = _collectObstructions();
        if (rects.length === 0) return CAM_CENTER;

        const band = _findBestXBand(vw, vh, rects);
        if (!band) return CAM_CENTER;

        // Mocha sits at the horizontal center of the widest free X interval.
        // y target is fixed — she's a standing character, vertical drift
        // only makes her look like she's hovering.
        const centerPx = (band.left + band.right) / 2;
        const nx = (centerPx / vw) - 0.5;

        // Zoom out if the free band is too narrow to comfortably contain
        // Mocha's silhouette. Her on-screen pixel width is driven by vh
        // (perspective FOV is vertical), so portrait needs its own threshold;
        // taking max(vw*0.22, vh*0.20) covers both orientations.
        const minComfortablePx = Math.max(vw * 0.22, vh * 0.20);
        const zoomFactor = band.width >= minComfortablePx
            ? 1
            : Math.min(2.0, minComfortablePx / Math.max(1, band.width));
        const camZ = 3.0 * zoomFactor;  // up to ~6.0

        // Perspective-correct X mapping: to render Mocha (at world x=0) at
        // screen fraction nx, shift the camera target to -nx in world X.
        const aspect = vw / vh;
        const tanH   = TAN_HALF_FOV * aspect;
        const tgtX = -nx * 2 * camZ * tanH;

        // Y stays fixed — same default head-height framing as CAM_CENTER.
        return { camX: tgtX, camY: 1.25, camZ, tgtX, tgtY: 1.0, tgtZ: 0 };
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

        // Restore saved chat state — but on phone, default to collapsed.
        // Explicit user preference (saved=true|false) always wins; if unset
        // (savedChat===null|undefined), phones start collapsed and desktops
        // start visible.
        const savedChat = _load('chat.visible');
        const initialChatVisible = (savedChat === true || savedChat === false)
            ? savedChat
            : !_isPhone();
        if (!initialChatVisible) toggleChatSidebar(false);

        // Thinking log collapse toggle
        const thinkToggle = document.getElementById('btnThinkingToggle');
        const thinkWrap = document.getElementById('thinkingPanelWrap');
        if (thinkToggle && thinkWrap) {
            const applyThinking = (collapsed) => {
                thinkWrap.classList.toggle('collapsed', collapsed);
                thinkToggle.textContent = collapsed ? '+' : '\u2212';
                thinkToggle.title = collapsed ? 'Show activity log' : 'Hide activity log';
            };
            thinkToggle.addEventListener('click', () => {
                const next = !thinkWrap.classList.contains('collapsed');
                applyThinking(next);
                _save('thinking.collapsed', next);
            });
            if (_load('thinking.collapsed') === true) applyThinking(true);
        }

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
