/**
 * presentation.js — Mocha's HTML Toolbox UI
 *
 * Handles: presentations/slides, info cards, toast notifications.
 * Receives commands via WebSocket ui_command messages from custom tools.
 */

(function () {
    'use strict';

    // ========================================================================
    //  State
    // ========================================================================

    const state = {
        // Presentation
        presentation: null,   // { id, title, slides[], auto_advance_sec }
        currentSlide: 0,
        autoTimer: null,
        chartInstance: null,

        // Cards
        cards: [],            // DOM elements
        maxCards: 3,

        // Notifications
        notifications: [],

        // Camera transition
        presenting: false,
        camTween: null,
    };

    // ========================================================================
    //  Camera transition — Mocha steps left when presenting
    // ========================================================================

    // Camera poses
    const CAM_CENTER = { camX: 0,     camY: 1.25, camZ: 3.0, tgtX: 0,     tgtY: 1.0, tgtZ: 0 };
    const CAM_PRESENT = { camX: 0.6, camY: 1.25, camZ: 3.0, tgtX: 0.8, tgtY: 1.0, tgtZ: 0 };
    const CAM_TRANSITION_MS = 500;

    function _animateCamera(to) {
        const cam = window._getCamera?.();
        const ctrl = window._getControls?.();
        if (!cam || !ctrl) return;

        // Cancel any in-flight tween
        if (state.camTween) { cancelAnimationFrame(state.camTween); state.camTween = null; }

        const from = {
            camX: cam.position.x, camY: cam.position.y, camZ: cam.position.z,
            tgtX: ctrl.target.x,  tgtY: ctrl.target.y,  tgtZ: ctrl.target.z,
        };
        const start = performance.now();

        function tick(now) {
            const t = Math.min((now - start) / CAM_TRANSITION_MS, 1);
            // Ease out cubic
            const e = 1 - Math.pow(1 - t, 3);

            cam.position.x = from.camX + (to.camX - from.camX) * e;
            cam.position.y = from.camY + (to.camY - from.camY) * e;
            cam.position.z = from.camZ + (to.camZ - from.camZ) * e;
            ctrl.target.x  = from.tgtX + (to.tgtX - from.tgtX) * e;
            ctrl.target.y  = from.tgtY + (to.tgtY - from.tgtY) * e;
            ctrl.target.z  = from.tgtZ + (to.tgtZ - from.tgtZ) * e;
            ctrl.update();

            if (t < 1) {
                state.camTween = requestAnimationFrame(tick);
            } else {
                state.camTween = null;
            }
        }
        state.camTween = requestAnimationFrame(tick);
    }

    function enterPresentMode() {
        // Camera movement is now handled by the render loop's continuous lerp
        // via PanelManager.getIdealCamera(). Just set the flag.
        state.presenting = true;
    }

    function exitPresentMode() {
        state.presenting = false;
    }

    // ========================================================================
    //  Slide sync — advance slides as Mocha speaks
    // ========================================================================

    function onSegmentPlay(segIndex, totalSegments) {
        if (!state.presentation || !state.presenting) return;
        const slideCount = state.presentation.slides.length;
        if (slideCount <= 1) return;

        // The panel may open mid-stream — e.g. tool calls take a few seconds
        // and meanwhile Mocha has already spoken several "let me check"
        // segments. Without an offset, the first onSegmentPlay after the
        // panel opens would compute a high segIndex/totalSegments ratio
        // and jump straight to the last slide. Lock in the segment index
        // at panel-open time and map progress relative to it.
        if (state.presentStartSegIndex == null) {
            state.presentStartSegIndex = segIndex;
        }
        const segOffset    = Math.max(0, segIndex - state.presentStartSegIndex);
        const segRemaining = Math.max(1, totalSegments - state.presentStartSegIndex);
        const targetSlide = Math.min(
            Math.floor((segOffset / segRemaining) * slideCount),
            slideCount - 1
        );
        if (targetSlide !== state.currentSlide) {
            renderSlide(targetSlide);
        }
    }

    // ========================================================================
    //  DOM refs (resolved lazily after DOMContentLoaded)
    // ========================================================================

    let panel, presTitle, presCounter, presSlide, presPrev, presNext, presClose;
    let cardContainer, notifContainer;

    function ensureDOM() {
        if (panel) return;
        panel        = document.getElementById('presentationPanel');
        presTitle    = document.getElementById('presTitle');
        presCounter  = document.getElementById('presCounter');
        presSlide    = document.getElementById('presSlide');
        presPrev     = document.getElementById('presPrev');
        presNext     = document.getElementById('presNext');
        presClose    = document.getElementById('presClose');
        cardContainer  = document.getElementById('cardContainer');
        notifContainer = document.getElementById('notifContainer');

        if (presPrev)  presPrev.addEventListener('click', () => navigateSlide(-1));
        if (presNext)  presNext.addEventListener('click', () => navigateSlide(1));
        if (presClose) presClose.addEventListener('click', clearPresentation);

        document.addEventListener('keydown', (e) => {
            if (!state.presentation || !panel?.classList.contains('active')) return;
            if (e.key === 'ArrowLeft')  { e.preventDefault(); navigateSlide(-1); }
            if (e.key === 'ArrowRight') { e.preventDefault(); navigateSlide(1); }
            if (e.key === 'Escape')     { e.preventDefault(); clearPresentation(); }
        });

        // Register with PanelManager for drag/resize
        if (window.PanelManager && panel) {
            window.PanelManager.registerPanel('presentation', panel, {
                top: 8, right: 8, bottom: 8, width: '64%'
            });
        }

        // Camera tracking is now continuous via the render loop
        // (PanelManager.getIdealCamera() called every frame)
    }

    // ========================================================================
    //  Command dispatcher
    // ========================================================================

    function handleUiCommand(msg) {
        ensureDOM();
        switch (msg.action) {
            case 'create_presentation':
                createPresentation(msg.presentation);
                break;
            case 'presentation_next':
                navigateSlide(1);
                break;
            case 'presentation_prev':
                navigateSlide(-1);
                break;
            case 'presentation_goto':
                gotoSlide(msg.slide_index || 0);
                break;
            case 'presentation_clear':
                clearPresentation();
                break;
            case 'show_card':
                showCard(msg.card);
                break;
            case 'show_notification':
                showNotification(msg.message, msg.level);
                break;
            case 'clear_ui':
                clearUI(msg.target || 'all');
                break;

            // ── Theme toolbox ──────────────────────────────────────────────
            case 'apply_theme_preview':
                applyThemePreview(msg.variables, msg.css, {
                    background_image_url: msg.background_image_url,
                    background_overlay_rgba: msg.background_overlay_rgba,
                    html_decor: msg.html_decor,
                    html_mods: msg.html_mods,
                });
                break;
            case 'clear_theme_preview':
                clearThemePreview();
                break;
            case 'revert_theme':
                // Full revert — used when Ika rejects the preview. Drops the
                // preview style element AND the decor/html_mods that came
                // with it. To restore a saved theme's assets, call theme_load.
                if (window.ThemeRuntime) window.ThemeRuntime.clearAll();
                break;

            // ── Video player (unified: music + regular videos) ──────────────
            case 'video_player':
                handleVideoPlayer(msg);
                break;
            case 'capture_screenshot':
                // Delegated to theme-capture.js (see window.ThemeCapture)
                if (window.ThemeCapture) {
                    window.ThemeCapture.captureAndReply(msg.request_id);
                }
                break;
            case 'reload_stylesheets':
                reloadStylesheets(msg.v);
                break;
            case 'show_theme_preview_modal':
                showThemePreviewModal(msg);
                break;
            case 'close_theme_preview_modal':
                closeThemePreviewModal();
                break;
        }
    }

    // ========================================================================
    //  Theme preview modal — palette swatches + mockup + Apply/Cancel
    // ========================================================================

    const _SWATCH_ORDER = ['bg', 'surface', 'card', 'border', 'accent', 'accent-dim', 'text', 'muted'];

    function _wsSendUserInput(text) {
        // app.js owns wsSend but it's global — guard for safety
        if (typeof wsSend === 'function') {
            wsSend({ type: 'user_input', text });
        }
    }

    function _escape(s) {
        return String(s || '').replace(/[&<>"']/g, (c) => ({
            '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;',
        }[c]));
    }

    function closeThemePreviewModal() {
        const bd = document.getElementById('themePreviewBackdrop');
        if (bd) bd.remove();
    }

    function showThemePreviewModal(msg) {
        // If one is already open, replace it.
        closeThemePreviewModal();

        const name = String(msg.name || 'theme');
        const description = String(msg.description || '');
        const palette = msg.palette || {};
        const mood = String(msg.mood || '');
        const score = (typeof msg.critique_score === 'number') ? msg.critique_score : null;
        const screenshot = msg.screenshot_b64 || '';
        const bgImageURL = String(msg.background_image_url || '');
        const hasDecor = !!msg.has_decor;
        const htmlModsCount = Number(msg.html_mods_count || 0);

        const backdrop = document.createElement('div');
        backdrop.id = 'themePreviewBackdrop';
        backdrop.className = 'theme-modal-backdrop open';

        const modal = document.createElement('div');
        modal.className = 'theme-modal';

        // ── Header ───────────────────────────────────────────────────────
        const header = document.createElement('div');
        header.className = 'theme-modal-header';
        const scoreHtml = (score !== null)
            ? `<span class="theme-modal-score ${score >= 7 ? 'good' : ''}">Hana · ${score}/10</span>`
            : '';
        header.innerHTML = `
            <div class="theme-modal-title">
                Preview
                <span class="theme-name-tag">${_escape(name)}</span>
                ${scoreHtml}
            </div>
            <button class="theme-modal-close" id="themePreviewCloseBtn" title="Close">&times;</button>
        `;

        // ── Body ─────────────────────────────────────────────────────────
        const body = document.createElement('div');
        body.className = 'theme-modal-body';

        if (screenshot) {
            const img = document.createElement('img');
            img.className = 'theme-modal-screenshot';
            img.alt = 'Live preview';
            img.src = 'data:image/png;base64,' + screenshot;
            body.appendChild(img);
        }

        if (description) {
            const desc = document.createElement('div');
            desc.className = 'theme-modal-mood';
            desc.textContent = description;
            body.appendChild(desc);
        }
        if (mood && mood !== description) {
            const m = document.createElement('div');
            m.className = 'theme-modal-mood';
            m.textContent = mood;
            body.appendChild(m);
        }

        // Full-package extras: background image thumbnail + decor/html_mods
        // indicators. Shown only when present. Music is a separate concern —
        // handled by the floating music_player, not baked into themes.
        if (bgImageURL || hasDecor || htmlModsCount > 0) {
            const extras = document.createElement('div');
            extras.className = 'theme-modal-extras';
            if (bgImageURL) {
                const row = document.createElement('div');
                row.className = 'theme-extra-row';
                row.innerHTML = `
                    <img class="theme-extra-bg-thumb" src="${_escape(bgImageURL)}" alt="bg">
                    <div class="theme-extra-label">Background image</div>
                `;
                extras.appendChild(row);
            }
            if (hasDecor || htmlModsCount > 0) {
                const row = document.createElement('div');
                row.className = 'theme-extra-row';
                const bits = [];
                if (hasDecor) bits.push('+ HTML decor');
                if (htmlModsCount > 0) bits.push(`+ ${htmlModsCount} element mod${htmlModsCount > 1 ? 's' : ''}`);
                row.innerHTML = `<div class="theme-extra-label">${_escape(bits.join(' · '))}</div>`;
                extras.appendChild(row);
            }
            body.appendChild(extras);
        }

        // Palette — swatches in a grid
        const grid = document.createElement('div');
        grid.className = 'theme-modal-palette';
        const keys = _SWATCH_ORDER.filter((k) => palette[k]).concat(
            Object.keys(palette).filter((k) => !_SWATCH_ORDER.includes(k))
        );
        for (const key of keys) {
            const val = palette[key];
            if (!val) continue;
            const sw = document.createElement('div');
            sw.className = 'theme-swatch';
            sw.innerHTML = `
                <div class="theme-swatch-chip" style="background:${_escape(val)}"></div>
                <div class="theme-swatch-meta">
                    <span class="swatch-name">${_escape(key)}</span>
                    <span>${_escape(val)}</span>
                </div>
            `;
            grid.appendChild(sw);
        }
        body.appendChild(grid);

        // Mock components — shown with the proposed palette so the user sees
        // how buttons, bubbles, and text will look once committed.
        const mock = document.createElement('div');
        mock.className = 'theme-modal-mock';
        const mp = palette;
        mock.innerHTML = `
            <div class="theme-mock-label">MOCK COMPONENTS</div>
            <div class="theme-mock-bubble" style="
                background:${_escape(mp['card'] || mp['surface'] || '#1a1e28')};
                color:${_escape(mp['text'] || '#d4d8e4')};
                border:1px solid ${_escape(mp['border'] || '#252a36')};
            ">Hey — this is what a chat bubble looks like.</div>
            <button class="theme-mock-btn" style="
                background:${_escape(mp['accent'] || '#e94560')};
                color:#fff;
            ">Primary action</button>
            <div style="
                color:${_escape(mp['muted'] || '#6b7280')};
                font-size:11px;
            ">… and muted text reads like this.</div>
        `;
        body.appendChild(mock);

        // ── Footer ──────────────────────────────────────────────────────
        const footer = document.createElement('div');
        footer.className = 'theme-modal-footer';
        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'theme-modal-btn';
        cancelBtn.id = 'themePreviewCancelBtn';
        cancelBtn.textContent = 'Try something else';
        const saveBtn = document.createElement('button');
        saveBtn.className = 'theme-modal-btn primary';
        saveBtn.id = 'themePreviewSaveBtn';
        saveBtn.textContent = `Save as “${name}”`;
        footer.appendChild(cancelBtn);
        footer.appendChild(saveBtn);

        modal.appendChild(header);
        modal.appendChild(body);
        modal.appendChild(footer);
        backdrop.appendChild(modal);
        document.body.appendChild(backdrop);

        // ── Events ──────────────────────────────────────────────────────
        header.querySelector('#themePreviewCloseBtn').addEventListener('click', () => {
            closeThemePreviewModal();
        });
        backdrop.addEventListener('click', (e) => {
            if (e.target === backdrop) closeThemePreviewModal();
        });
        cancelBtn.addEventListener('click', () => {
            closeThemePreviewModal();
            _wsSendUserInput(`Nah, revert that — let's try something else.`);
        });
        saveBtn.addEventListener('click', () => {
            closeThemePreviewModal();
            _wsSendUserInput(`Yes, save it as ${name}.`);
        });
    }

    // ========================================================================
    //  Theme preview — live CSS staging (does NOT touch style.css on disk)
    // ========================================================================

    const DEFAULT_OVERLAY_RGBA = '10,12,16,0.55';
    // Track html_mods we've applied so revert/reset can undo them.
    let _appliedHtmlMods = [];

    function _escAttrURL(s) {
        // Very cheap URL escape — strip quotes/backslash to prevent breaking out of url('...').
        return String(s || '').replace(/['"\\]/g, '');
    }

    function _stripScripts(html) {
        // Decor HTML is written by Mocha, but strip <script> as a cheap belt-and-braces.
        return String(html || '')
            .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
            .replace(/\son\w+\s*=\s*"[^"]*"/gi, '')
            .replace(/\son\w+\s*=\s*'[^']*'/gi, '');
    }

    function _backgroundImageCSS(url, rgba) {
        const safeURL = _escAttrURL(url);
        const safeRGBA = rgba || DEFAULT_OVERLAY_RGBA;
        return (
            'body {\n' +
            `  background-image: url('${safeURL}');\n` +
            '  background-size: cover;\n' +
            '  background-position: center;\n' +
            '  background-attachment: fixed;\n' +
            '}\n' +
            'body::before {\n' +
            '  content: \'\'; position: fixed; inset: 0;\n' +
            `  background: rgba(${safeRGBA}); pointer-events: none; z-index: -1;\n` +
            '}'
        );
    }

    function _undoHtmlMods() {
        for (const mod of _appliedHtmlMods) {
            try {
                document.querySelectorAll(mod.selector).forEach((el) => {
                    if (mod.add_class) el.classList.remove(mod.add_class);
                    if (mod.remove_class) el.classList.add(mod.remove_class);
                    if (mod.set_attr) {
                        for (const k of Object.keys(mod.set_attr)) {
                            el.removeAttribute(k);
                        }
                    }
                });
            } catch (e) { console.warn('theme: undo html_mod failed', mod, e); }
        }
        _appliedHtmlMods = [];
    }

    function _applyHtmlMods(mods) {
        if (!Array.isArray(mods) || mods.length === 0) return;
        for (const mod of mods) {
            if (!mod || !mod.selector) continue;
            try {
                document.querySelectorAll(mod.selector).forEach((el) => {
                    if (mod.add_class) el.classList.add(mod.add_class);
                    if (mod.remove_class) el.classList.remove(mod.remove_class);
                    if (mod.set_attr && typeof mod.set_attr === 'object') {
                        for (const [k, v] of Object.entries(mod.set_attr)) {
                            el.setAttribute(k, String(v));
                        }
                    }
                });
                _appliedHtmlMods.push(mod);
            } catch (e) { console.warn('theme: bad selector', mod, e); }
        }
    }

    function _applyDecor(html) {
        const slot = document.getElementById('theme-decor');
        if (!slot) return;
        slot.innerHTML = _stripScripts(html);
    }

    function applyThemePreview(variables, css, extras) {
        let el = document.getElementById('theme-preview');
        if (!el) {
            el = document.createElement('style');
            el.id = 'theme-preview';
            // Append to <head> last so cascade order gives the preview priority.
            document.head.appendChild(el);
        }
        const parts = [];
        if (variables && typeof variables === 'object') {
            const lines = Object.entries(variables)
                .filter(([k, v]) => k && v !== undefined && v !== null)
                .map(([k, v]) => {
                    // Accept keys with or without the leading "--".
                    const name = k.startsWith('--') ? k : '--' + k;
                    return `  ${name}: ${v};`;
                })
                .join('\n');
            if (lines) parts.push(`:root {\n${lines}\n}`);
        }

        const ex = extras || {};
        if (ex.background_image_url) {
            parts.push(_backgroundImageCSS(ex.background_image_url, ex.background_overlay_rgba));
        }
        if (typeof css === 'string' && css.trim()) {
            parts.push(css);
        }
        el.textContent = parts.join('\n\n');

        // Decor and html_mods — DOM-level state, survives reload_stylesheets.
        _applyDecor(ex.html_decor || '');
        _undoHtmlMods();
        _applyHtmlMods(ex.html_mods || []);
    }

    // Exposed so app.js bootstrap can apply active.json on load.
    window.ThemeRuntime = {
        applyFull: applyThemePreview,
        clearPreviewStyle: function () {
            const el = document.getElementById('theme-preview');
            if (el) el.remove();
        },
        clearAll: function () {
            const el = document.getElementById('theme-preview');
            if (el) el.remove();
            _applyDecor('');
            _undoHtmlMods();
        },
    };

    function clearThemePreview() {
        // theme_apply persists to active.css; the preview <style> element
        // duplicates what's now in active.css so it can come down. Audio,
        // decor, and html_mods are DOM state — leave them; they already
        // match the new active theme.
        const el = document.getElementById('theme-preview');
        if (el) el.remove();
    }

    function reloadStylesheets(version) {
        const v = version || Date.now();
        document.querySelectorAll('link[rel="stylesheet"]').forEach((link) => {
            const base = link.getAttribute('href').split('?')[0];
            link.setAttribute('href', base + '?v=' + v);
        });
    }

    // ========================================================================
    //  Video player — persistent floating YouTube player (full/mini/closed)
    //  Handles both music and general videos. Full state is user-draggable
    //  + resizable via PanelManager. Mini state is a bottom-right pill.
    // ========================================================================

    // YouTube IFrame Player API loader — lets the video player catch playback
    // errors (removed / embed-disabled / region-locked / dead live stream) via
    // onError and show a friendly message instead of YouTube's raw error frame.
    // Resolves to the YT namespace, or null if the API can't load — in which case
    // the player falls back to a plain iframe, identical to the old behavior.
    let _ytApiPromise = null;
    function loadYT() {
        if (window.YT && window.YT.Player) return Promise.resolve(window.YT);
        if (_ytApiPromise) return _ytApiPromise;
        _ytApiPromise = new Promise((resolve) => {
            let done = false;
            const finish = (v) => { if (!done) { done = true; resolve(v); } };
            const prev = window.onYouTubeIframeAPIReady;
            window.onYouTubeIframeAPIReady = function () {
                if (typeof prev === 'function') { try { prev(); } catch (_) {} }
                finish(window.YT);
            };
            const tag = document.createElement('script');
            tag.src = 'https://www.youtube.com/iframe_api';
            tag.onerror = () => finish(null);
            document.head.appendChild(tag);
            // Safety net: don't hold the player blank forever if the API stalls.
            setTimeout(() => finish(window.YT && window.YT.Player ? window.YT : null), 4000);
        });
        return _ytApiPromise;
    }

    const VideoPlayer = {
        _state: 'hidden',  // 'hidden' | 'full' | 'mini'
        _videoId: '',
        _title: '',
        _yt: null,         // active YT.Player instance (null when raw-iframe path)
        _registered: false,
        // Inline-style snapshot captured right before minimizing, so the full
        // state can restore the exact same rect (drag / resize position).
        _savedInline: null,

        _el() { return document.getElementById('video-player'); },
        _iframeWrap() { return document.getElementById('videoPlayerIframeWrap'); },
        _titleEl() { return document.getElementById('videoPlayerTitle'); },
        _thumbTitleEl() { return document.getElementById('videoPlayerThumbTitle'); },

        _ensureRegistered() {
            if (this._registered || !window.PanelManager) return;
            const el = this._el();
            if (!el) return;
            // Default rect — used on first open and double-click-to-reset.
            // Top-left corner, out of the way of the character + right-side chat.
            window.PanelManager.registerPanel('video', el, {
                top: 60, left: 24, width: 520, height: 320,
            });
            this._registered = true;
        },

        _captureInline() {
            const el = this._el();
            if (!el) return null;
            return {
                left: el.style.left, top: el.style.top,
                right: el.style.right, bottom: el.style.bottom,
                width: el.style.width, height: el.style.height,
            };
        },
        _restoreInline(snap) {
            const el = this._el();
            if (!el || !snap) return;
            el.style.left = snap.left; el.style.top = snap.top;
            el.style.right = snap.right; el.style.bottom = snap.bottom;
            el.style.width = snap.width; el.style.height = snap.height;
        },
        _clearInline() {
            const el = this._el();
            if (!el) return;
            ['left','top','right','bottom','width','height'].forEach((k) => {
                el.style[k] = '';
            });
        },

        _setState(next) {
            const el = this._el();
            if (!el) return;
            const prev = this._state;
            this._state = next;
            if (next === 'hidden') {
                el.classList.add('hidden');
                el.removeAttribute('data-state');
                clearTimeout(this._loadTimer);
                clearTimeout(this._graceTimer);
                try { if (this._yt && this._yt.destroy) this._yt.destroy(); } catch (_) {}
                this._yt = null;
                this._iframeWrap().innerHTML = '';  // stop audio/video
                this._clearInline();
                this._savedInline = null;
                return;
            }
            el.classList.remove('hidden');
            if (next === 'mini') {
                // Capture the dragged/resized full-state rect so expand can restore it.
                if (prev === 'full') this._savedInline = this._captureInline();
                this._clearInline();  // let data-state=mini CSS apply bottom-right corner
            } else if (next === 'full') {
                this._ensureRegistered();
                // Restore prior drag/resize rect if we have one.
                if (this._savedInline) {
                    this._restoreInline(this._savedInline);
                }
            }
            el.setAttribute('data-state', next);
        },

        open(videoId, title, candidates, playId) {
            // candidates: optional [{id,title}] fallback list the player cycles
            // through if an embed is refused at play time (oEmbed can pass while
            // the IFrame still throws 101/150). playId: correlation token used to
            // report the REAL outcome (played / all-failed) back to the bridge.
            this._candidates = (Array.isArray(candidates) && candidates.length)
                ? candidates.slice()
                : (videoId ? [{ id: videoId, title: title }] : []);
            if (!this._candidates.length) return;
            this._candIdx = 0;
            this._playId = playId || '';
            this._reported = false;
            const first = this._candidates[0];
            this._videoId = first.id;
            this._title = first.title || title || 'Now playing';
            this._titleEl().textContent = this._title;
            this._thumbTitleEl().textContent = this._title;
            this._mountPlayer(this._videoId);
            this._setState('full');
        },

        // Plain autoplay iframe — the original embed path, used as the fallback
        // when the IFrame Player API can't load.
        //   autoplay=1  — start immediately (user gesture provided by the
        //                 conversation that triggered this open).
        //   rel=0       — suppress recommended videos at the end.
        //   playsinline=1 — don't force fullscreen on iOS.
        //   origin      — required for some embeds to dispatch API events.
        _rawIframe(videoId) {
            const origin = encodeURIComponent(location.origin);
            const src = `https://www.youtube.com/embed/${encodeURIComponent(videoId)}`
                + `?autoplay=1&rel=0&playsinline=1&origin=${origin}`;
            this._iframeWrap().innerHTML =
                `<iframe src="${src}" title="${_escape(this._title)}" ` +
                `allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>`;
        },

        _showError(msg) {
            this._iframeWrap().innerHTML =
                '<div style="display:flex;align-items:center;justify-content:center;'
                + 'height:100%;padding:18px;text-align:center;font-size:13px;'
                + `line-height:1.4;opacity:.85;">${_escape(msg)}</div>`;
        },

        // Mount via the IFrame Player API so playback errors surface a friendly
        // message instead of YouTube's raw "video unavailable" frame. Falls back
        // to a plain iframe whenever the API is unavailable.
        _mountPlayer(videoId) {
            const self = this;
            try { if (this._yt && this._yt.destroy) this._yt.destroy(); } catch (_) {}
            this._yt = null;
            clearTimeout(this._loadTimer);
            clearTimeout(this._graceTimer);
            loadYT().then((YT) => {
                // A newer open() may have superseded this one while the API loaded.
                if (self._videoId !== videoId) return;
                if (!YT || !YT.Player) {
                    // No IFrame API → can't detect play/fail; show it, assume ok.
                    self._rawIframe(videoId);
                    self._reportOk(videoId);
                    return;
                }
                self._iframeWrap().innerHTML = '<div id="ytPlayerHost"></div>';
                self._yt = new YT.Player('ytPlayerHost', {
                    width: '100%', height: '100%', videoId: videoId,
                    playerVars: { autoplay: 1, rel: 0, playsinline: 1, origin: location.origin },
                    events: {
                        onReady: () => {
                            if (self._videoId !== videoId) return;
                            // Loaded, no error. Autoplay may be blocked (stays CUED
                            // not PLAYING) — give it a beat, then count "loaded with
                            // no error" as success (the user can press play).
                            clearTimeout(self._graceTimer);
                            self._graceTimer = setTimeout(() => {
                                if (self._videoId === videoId) self._reportOk(videoId);
                            }, 2500);
                        },
                        onStateChange: (e) => {
                            // 1 === YT.PlayerState.PLAYING → unambiguous success.
                            if (self._videoId === videoId && e && e.data === 1) {
                                self._reportOk(videoId);
                            }
                        },
                        onError: () => {
                            if (self._videoId !== videoId) return;
                            self._onPlayFailure();   // embed refused → next candidate
                        },
                    },
                });
                // Never loaded at all (no onReady, no onError) → treat as failure.
                clearTimeout(self._loadTimer);
                self._loadTimer = setTimeout(() => {
                    if (self._videoId === videoId && !self._reported) self._onPlayFailure();
                }, 9000);
            }).catch(() => { self._rawIframe(videoId); self._reportOk(videoId); });
        },

        // An embed was refused (or never loaded): advance to the next fallback
        // candidate; if they're exhausted, show the message and report failure so
        // the bridge knows the video did NOT play.
        _onPlayFailure() {
            clearTimeout(this._loadTimer);
            clearTimeout(this._graceTimer);
            this._candIdx = (this._candIdx || 0) + 1;
            if (this._candidates && this._candIdx < this._candidates.length) {
                const next = this._candidates[this._candIdx];
                this._videoId = next.id;
                this._title = next.title || this._title;
                this._titleEl().textContent = this._title;
                this._thumbTitleEl().textContent = this._title;
                this._mountPlayer(next.id);
                return;
            }
            this._showError(
                "Couldn't play that one — it may have ended, gone private, or "
                + 'blocked embedding. Ask me to find another.'
            );
            this._reportFailed();
        },

        _reportOk(videoId) {
            clearTimeout(this._loadTimer);
            clearTimeout(this._graceTimer);
            if (this._reported) return;
            this._reported = true;
            if (this._playId && window.wsSend) {
                window.wsSend({
                    type: 'video_play_result', play_id: this._playId,
                    ok: true, video_id: videoId, title: this._title,
                });
            }
        },

        _reportFailed() {
            clearTimeout(this._loadTimer);
            clearTimeout(this._graceTimer);
            if (this._reported) return;
            this._reported = true;
            if (this._playId && window.wsSend) {
                window.wsSend({ type: 'video_play_result', play_id: this._playId, ok: false });
            }
        },

        minimize() {
            if (this._state === 'hidden') return;
            this._setState('mini');
        },
        expand() {
            if (this._state === 'hidden') return;
            this._setState('full');
        },
        close() { this._setState('hidden'); },

        setTitle(title) {
            this._title = title || 'Now playing';
            this._titleEl().textContent = this._title;
            this._thumbTitleEl().textContent = this._title;
        },
    };
    window.VideoPlayer = VideoPlayer;

    function handleVideoPlayer(msg) {
        const op = String(msg.op || '').toLowerCase();
        switch (op) {
            case 'open':     VideoPlayer.open(msg.video_id, msg.title, msg.candidates, msg.play_id); break;
            case 'close':    VideoPlayer.close(); break;
            case 'set_title': VideoPlayer.setTitle(msg.title); break;
            // minimize/expand kept in case someone drives from JS directly,
            // but NOT exposed via the tool schema — the user clicks buttons.
            case 'minimize': VideoPlayer.minimize(); break;
            case 'expand':   VideoPlayer.expand();   break;
            default: console.warn('video_player: unknown op', op);
        }
    }

    // Wire the header/thumbnail buttons once the DOM is ready.
    function _initVideoPlayerControls() {
        const minBtn = document.getElementById('videoPlayerMinBtn');
        const expandBtn = document.getElementById('videoPlayerExpandBtn');
        const closeBtn = document.getElementById('videoPlayerCloseBtn');
        const thumb = document.getElementById('videoPlayerThumbControls');
        if (minBtn) minBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            VideoPlayer.minimize();
        });
        if (expandBtn) expandBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            VideoPlayer.expand();
        });
        if (closeBtn) closeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            VideoPlayer.close();
        });
        // Click anywhere on the thumbnail (outside the expand button) to expand.
        if (thumb) thumb.addEventListener('click', () => VideoPlayer.expand());
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _initVideoPlayerControls);
    } else {
        _initVideoPlayerControls();
    }

    // ========================================================================
    //  Presentation
    // ========================================================================

    function createPresentation(data) {
        if (!panel || !data || !data.slides?.length) return;

        // Clean up previous
        destroyChart();
        clearAutoAdvance();

        state.presentation = data;
        state.currentSlide = 0;
        state.presentStartSegIndex = null;  // locked on first onSegmentPlay

        presTitle.textContent = data.title || 'Presentation';
        panel.classList.add('active');
        enterPresentMode();

        // Let panel manager optimize the position
        if (window.PanelManager) requestAnimationFrame(() => window.PanelManager.autoLayout?.());

        renderSlide(0);

        // Auto-advance
        if (data.auto_advance_sec > 0) {
            state.autoTimer = setInterval(() => {
                if (state.currentSlide < state.presentation.slides.length - 1) {
                    navigateSlide(1);
                } else {
                    clearAutoAdvance();
                }
            }, data.auto_advance_sec * 1000);
        }
    }

    function renderSlide(index) {
        const slides = state.presentation?.slides;
        if (!slides || index < 0 || index >= slides.length) return;

        destroyChart();
        state.currentSlide = index;
        presCounter.textContent = `${index + 1} / ${slides.length}`;

        const slide = slides[index];
        presSlide.innerHTML = '';
        presSlide.className = 'pres-slide';

        switch (slide.type) {
            case 'title':
                renderTitleSlide(slide);
                break;
            case 'bullets':
                renderBulletsSlide(slide);
                break;
            case 'table':
                renderTableSlide(slide);
                break;
            case 'chart':
                renderChartSlide(slide);
                break;
            case 'image':
                renderImageSlide(slide);
                break;
            case 'video':
                renderVideoSlide(slide);
                break;
            case 'candlestick':
                renderCandlestickSlide(slide);
                break;
            case 'multi_chart':
                renderMultiChartSlide(slide);
                break;
            case 'news_feed':
                renderNewsFeedSlide(slide);
                break;
            case 'stat_row':
                renderStatRowSlide(slide);
                break;
            case 'markdown':
                renderMarkdownSlide(slide);
                break;
            default:
                presSlide.innerHTML = `<p class="pres-text">${escHtml(slide.title || 'Untitled')}</p>`;
        }
    }

    // Renders a list of {label, value, delta?} rows. delta auto-colored
    // by leading sign character ("+" green, "-" red). Used by trading-desk
    // briefings, P&L summaries, etc.
    function renderStatRowSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-stat-row-slide';

        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }

        const list = document.createElement('div');
        list.className = 'stat-list';
        const stats = Array.isArray(slide.stats) ? slide.stats : [];
        for (const s of stats) {
            const row = document.createElement('div');
            row.className = 'stat-row';
            const label = document.createElement('span');
            label.className = 'stat-label';
            label.textContent = s.label || '';
            const value = document.createElement('span');
            value.className = 'stat-value';
            value.textContent = s.value != null ? String(s.value) : '';
            row.appendChild(label);
            row.appendChild(value);
            if (s.delta != null && s.delta !== '') {
                const delta = document.createElement('span');
                const dStr = String(s.delta).trim();
                const dir = dStr.startsWith('-') ? 'down' : (dStr.startsWith('+') ? 'up' : 'flat');
                delta.className = 'stat-delta ' + dir;
                // Direction glyph so the up/down read is instant, not color-only.
                const arrow = dir === 'up' ? '▲ ' : (dir === 'down' ? '▼ ' : '');
                delta.textContent = arrow + dStr;
                row.appendChild(delta);
            }
            list.appendChild(row);
        }
        wrap.appendChild(list);
        presSlide.appendChild(wrap);
    }

    // Renders a freeform markdown body. Uses marked.js if present, falls
    // back to a minimal converter (paragraphs + tables + bold/italic) so
    // briefing markdown still readable when the lib isn't loaded.
    function renderMarkdownSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-markdown-slide';

        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }

        const body = document.createElement('div');
        body.className = 'markdown-body';
        const md = String(slide.content || '');
        if (window.marked && typeof window.marked.parse === 'function') {
            body.innerHTML = window.marked.parse(md);
        } else {
            body.innerHTML = _miniMarkdown(md);
        }
        wrap.appendChild(body);
        presSlide.appendChild(wrap);
    }

    // Minimal markdown subset: GFM tables, **bold**, *italic*, and
    // paragraph breaks on blank lines. Enough for briefing copy.
    function _miniMarkdown(md) {
        const escape = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const lines = md.split('\n');
        let html = '';
        let i = 0;
        while (i < lines.length) {
            const line = lines[i];
            // GFM table: header | sep | rows
            if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
                const headerCells = line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
                let rowHtml = '<table class="md-table"><thead><tr>'
                    + headerCells.map(c => `<th>${escape(c)}</th>`).join('') + '</tr></thead><tbody>';
                i += 2;
                while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
                    const cells = lines[i].trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
                    rowHtml += '<tr>' + cells.map(c => `<td>${escape(c)}</td>`).join('') + '</tr>';
                    i++;
                }
                rowHtml += '</tbody></table>';
                html += rowHtml;
                continue;
            }
            if (/^\s*$/.test(line)) {
                html += '';
                i++;
                continue;
            }
            // Paragraph: collect contiguous non-empty lines
            const para = [];
            while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*\|.*\|\s*$/.test(lines[i])) {
                para.push(lines[i]);
                i++;
            }
            const text = escape(para.join(' '))
                .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
                .replace(/\*(.+?)\*/g, '<em>$1</em>');
            html += `<p>${text}</p>`;
        }
        return html;
    }

    function renderTitleSlide(slide) {
        const h = document.createElement('div');
        h.className = 'pres-title-slide';
        h.innerHTML = `
            <h2 class="pres-slide-title pres-title-big">${escHtml(slide.title || '')}</h2>
            ${slide.subtitle ? `<p class="pres-subtitle">${escHtml(slide.subtitle)}</p>` : ''}
        `;
        presSlide.appendChild(h);
    }

    function renderBulletsSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-bullets-slide';
        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }
        if (slide.items?.length) {
            const ul = document.createElement('ul');
            ul.className = 'pres-bullet-list';
            for (const item of slide.items) {
                const li = document.createElement('li');
                li.textContent = item;
                ul.appendChild(li);
            }
            wrap.appendChild(ul);
        }
        presSlide.appendChild(wrap);
    }

    function renderTableSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-table-slide';
        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }
        const tableWrap = document.createElement('div');
        tableWrap.className = 'pres-table-wrap';
        const table = document.createElement('table');
        table.className = 'pres-table';

        if (slide.headers?.length) {
            const thead = document.createElement('thead');
            const tr = document.createElement('tr');
            for (const h of slide.headers) {
                const th = document.createElement('th');
                th.textContent = h;
                tr.appendChild(th);
            }
            thead.appendChild(tr);
            table.appendChild(thead);
        }

        if (slide.rows?.length) {
            const tbody = document.createElement('tbody');
            for (const row of slide.rows) {
                const tr = document.createElement('tr');
                for (const cell of row) {
                    const td = document.createElement('td');
                    td.textContent = String(cell);
                    tr.appendChild(td);
                }
                tbody.appendChild(tr);
            }
            table.appendChild(tbody);
        }

        tableWrap.appendChild(table);
        wrap.appendChild(tableWrap);
        presSlide.appendChild(wrap);
    }

    function renderChartSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-chart-slide';
        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }

        const canvasWrap = document.createElement('div');
        canvasWrap.className = 'pres-chart-wrap';
        const canvas = document.createElement('canvas');
        canvasWrap.appendChild(canvas);
        wrap.appendChild(canvasWrap);
        presSlide.appendChild(wrap);

        // Chart.js needs the canvas to be in the DOM
        requestAnimationFrame(() => {
            if (typeof Chart === 'undefined') {
                canvasWrap.innerHTML = '<p class="pres-text" style="color:var(--muted)">Chart.js not loaded</p>';
                return;
            }

            const palette = [
                '#e94560', '#4ade80', '#60a5fa', '#facc15',
                '#c084fc', '#fb923c', '#22d3ee', '#f472b6',
            ];

            const datasets = (slide.datasets || []).map((ds, i) => ({
                label: ds.label || `Series ${i + 1}`,
                data: ds.data || [],
                borderColor: palette[i % palette.length],
                backgroundColor: (slide.chart_type === 'line')
                    ? palette[i % palette.length] + '22'
                    : palette.slice(0, (ds.data || []).length),
                borderWidth: 2,
                tension: 0.3,
                fill: slide.chart_type === 'line',
            }));

            state.chartInstance = new Chart(canvas, {
                type: slide.chart_type || 'bar',
                data: {
                    labels: slide.labels || [],
                    datasets,
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            labels: { color: '#d4d8e4', font: { size: 12 } },
                        },
                    },
                    scales: (['line', 'bar'].includes(slide.chart_type)) ? {
                        x: {
                            ticks: { color: '#6b7280' },
                            grid: { color: '#1a1e2833' },
                        },
                        y: {
                            ticks: { color: '#6b7280' },
                            grid: { color: '#1a1e2844' },
                        },
                    } : undefined,
                },
            });
        });
    }

    function renderImageSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-image-slide';
        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }
        if (slide.url) {
            const img = document.createElement('img');
            img.className = 'pres-image';
            img.src = slide.url;
            img.alt = slide.caption || slide.title || '';
            wrap.appendChild(img);
        }
        if (slide.caption) {
            const cap = document.createElement('p');
            cap.className = 'pres-caption';
            cap.textContent = slide.caption;
            wrap.appendChild(cap);
        }
        presSlide.appendChild(wrap);
    }

    function renderVideoSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-video-slide';
        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }
        if (slide.video_id) {
            const vWrap = document.createElement('div');
            vWrap.className = 'pres-video-wrap';
            const iframe = document.createElement('iframe');
            iframe.className = 'pres-video-iframe';
            iframe.src = `https://www.youtube.com/embed/${encodeURIComponent(slide.video_id)}`;
            iframe.title = slide.title || 'Video';
            iframe.setAttribute('allow', 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture');
            iframe.setAttribute('allowfullscreen', '');
            iframe.setAttribute('frameborder', '0');
            vWrap.appendChild(iframe);
            wrap.appendChild(vWrap);
        }
        if (slide.caption) {
            const cap = document.createElement('p');
            cap.className = 'pres-caption';
            cap.textContent = slide.caption;
            wrap.appendChild(cap);
        }
        presSlide.appendChild(wrap);
    }

    // ----- Candlestick chart (lightweight-charts) -----

    const _TIMEFRAMES = [
        { label: '1D', period: '1d',  interval: '5m'  },
        { label: '1W', period: '5d',  interval: '15m' },
        { label: '1M', period: '1mo', interval: '1d'  },
        { label: '3M', period: '3mo', interval: '1d'  },
        { label: '1Y', period: '1y',  interval: '1wk' },
        { label: '5Y', period: '5y',  interval: '1mo' },
    ];

    function renderCandlestickSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-candle-slide';

        // Header: symbol + price info
        const header = document.createElement('div');
        header.className = 'candle-header';
        header.innerHTML = `
            <span class="candle-symbol">${escHtml(slide.symbol || '')}</span>
            ${slide.price != null ? `<span class="candle-price">${_fmtPrice(slide.price)}</span>` : ''}
            ${slide.change != null ? `<span class="candle-change ${_changeDir(slide.change)}">${_fmtChange(slide.change)}</span>` : ''}
            ${slide.title && slide.title !== slide.symbol ? `<span class="candle-name">${escHtml(slide.title)}</span>` : ''}
        `;
        wrap.appendChild(header);

        // Timeframe buttons
        const btnBar = document.createElement('div');
        btnBar.className = 'candle-timeframes';
        const defaultTf = slide.default_period || '1d';
        for (const tf of _TIMEFRAMES) {
            const btn = document.createElement('button');
            btn.className = 'candle-tf-btn' + (tf.period === defaultTf ? ' active' : '');
            btn.textContent = tf.label;
            btn.addEventListener('click', () => {
                btnBar.querySelectorAll('.candle-tf-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                _loadCandleData(chartEl, slide.symbol, tf.period, tf.interval);
            });
            btnBar.appendChild(btn);
        }
        wrap.appendChild(btnBar);

        // Chart container
        const chartEl = document.createElement('div');
        chartEl.className = 'candle-chart-container';
        wrap.appendChild(chartEl);

        // Caption
        if (slide.caption) {
            const cap = document.createElement('p');
            cap.className = 'pres-caption';
            cap.textContent = slide.caption;
            wrap.appendChild(cap);
        }

        presSlide.appendChild(wrap);

        // Find matching default timeframe
        const defTf = _TIMEFRAMES.find(t => t.period === defaultTf) || _TIMEFRAMES[2];
        requestAnimationFrame(() => _loadCandleData(chartEl, slide.symbol, defTf.period, defTf.interval));
    }

    function _bridgeUrl(path) {
        // Always go through the page host. The webapp proxies /api/* to
        // the bridge, so this works for direct LAN, Cloudflare tunnel,
        // and any reverse proxy.
        return `${location.protocol}//${location.host}${path}`;
    }

    async function _loadCandleData(container, symbol, period, interval) {
        if (!symbol) return;
        container.innerHTML = '<div class="candle-loading">Loading...</div>';

        try {
            const resp = await fetch(_bridgeUrl(`/api/stock-chart?symbol=${encodeURIComponent(symbol)}&period=${period}&interval=${interval}`));
            const json = await resp.json();
            if (json.error || !json.data?.length) {
                container.innerHTML = `<div class="candle-loading">${escHtml(json.error || 'No data')}</div>`;
                return;
            }
            _renderLWChart(container, json.data, symbol);
        } catch (e) {
            container.innerHTML = `<div class="candle-loading">Error: ${escHtml(String(e))}</div>`;
        }
    }

    function _renderLWChart(container, data, symbol) {
        container.innerHTML = '';
        if (typeof LightweightCharts === 'undefined') {
            container.innerHTML = '<div class="candle-loading">Chart library not loaded</div>';
            return;
        }

        // Clean up previous
        // Remove previous chart in this container if any
        if (container._lwChart) { container._lwChart.remove(); container._lwChart = null; }

        const chart = LightweightCharts.createChart(container, {
            width: container.clientWidth,
            height: container.clientHeight || 260,
            layout: {
                background: { type: 'solid', color: 'transparent' },
                textColor: '#6b7280',
                fontSize: 11,
            },
            grid: {
                vertLines: { color: '#1a1e2833' },
                horzLines: { color: '#1a1e2844' },
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            rightPriceScale: { borderColor: '#252a36' },
            timeScale: {
                borderColor: '#252a36',
                timeVisible: data.length > 0 && (data[data.length - 1].time - data[0].time) < 86400 * 7,
            },
        });

        const candleSeries = chart.addCandlestickSeries({
            upColor: '#4ade80',
            downColor: '#ef4444',
            borderUpColor: '#4ade80',
            borderDownColor: '#ef4444',
            wickUpColor: '#4ade80',
            wickDownColor: '#ef4444',
        });
        candleSeries.setData(data);

        // Volume as histogram
        const hasVolume = data.some(d => d.volume > 0);
        if (hasVolume) {
            const volSeries = chart.addHistogramSeries({
                priceFormat: { type: 'volume' },
                priceScaleId: 'vol',
            });
            chart.priceScale('vol').applyOptions({
                scaleMargins: { top: 0.8, bottom: 0 },
            });
            volSeries.setData(data.map(d => ({
                time: d.time,
                value: d.volume,
                color: d.close >= d.open ? '#4ade8033' : '#ef444433',
            })));
        }

        chart.timeScale().fitContent();
        container._lwChart = chart;
        if (!state.lwCharts) state.lwCharts = [];
        state.lwCharts.push(chart);

        // Resize observer
        const ro = new ResizeObserver(() => {
            if (container._lwChart === chart) {
                chart.applyOptions({ width: container.clientWidth });
            }
        });
        ro.observe(container);
    }

    // ----- Multi-chart: multiple candlesticks on one scrollable slide -----

    function renderMultiChartSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'pres-multi-chart-slide';

        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }

        // Shared timeframe bar
        const btnBar = document.createElement('div');
        btnBar.className = 'candle-timeframes';
        const defaultTf = slide.default_period || '1d';
        let activePeriod = _TIMEFRAMES.find(t => t.period === defaultTf) || _TIMEFRAMES[0];

        const chartEls = []; // { el, symbol }

        for (const tf of _TIMEFRAMES) {
            const btn = document.createElement('button');
            btn.className = 'candle-tf-btn' + (tf.period === defaultTf ? ' active' : '');
            btn.textContent = tf.label;
            btn.addEventListener('click', () => {
                btnBar.querySelectorAll('.candle-tf-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                activePeriod = tf;
                for (const c of chartEls) _loadCandleData(c.el, c.symbol, tf.period, tf.interval);
            });
            btnBar.appendChild(btn);
        }
        wrap.appendChild(btnBar);

        // One mini chart per symbol
        const symbols = slide.symbols || [];
        for (const item of symbols) {
            const sym = typeof item === 'string' ? item : item.symbol;
            const price = typeof item === 'object' ? item.price : null;
            const change = typeof item === 'object' ? item.change : null;

            const row = document.createElement('div');
            row.className = 'multi-chart-row';

            const header = document.createElement('div');
            header.className = 'multi-chart-header';
            header.innerHTML = `<span class="candle-symbol">${escHtml(sym)}</span>`
                + (price  != null ? ` <span class="candle-price">${_fmtPrice(price)}</span>` : '')
                + (change != null ? ` <span class="candle-change ${_changeDir(change)}">${_fmtChange(change)}</span>` : '');
            row.appendChild(header);

            const chartEl = document.createElement('div');
            chartEl.className = 'multi-chart-container';
            row.appendChild(chartEl);
            wrap.appendChild(row);

            chartEls.push({ el: chartEl, symbol: sym });
        }

        presSlide.appendChild(wrap);

        // Load all charts
        requestAnimationFrame(() => {
            for (const c of chartEls) _loadCandleData(c.el, c.symbol, activePeriod.period, activePeriod.interval);
        });
    }

    // ----- News feed: card-based article layout (Apple News / Economist style) -----

    function renderNewsFeedSlide(slide) {
        const wrap = document.createElement('div');
        wrap.className = 'news-feed-slide';

        if (slide.title) {
            const t = document.createElement('h3');
            t.className = 'pres-slide-title';
            t.textContent = slide.title;
            wrap.appendChild(t);
        }

        const articles = slide.articles || [];
        for (const art of articles) {
            const card = document.createElement('a');
            card.className = 'news-card';
            card.href = art.url || '#';
            card.target = '_blank';
            card.rel = 'noopener';

            // Thumbnail (left side)
            if (art.thumbnail) {
                const imgWrap = document.createElement('div');
                imgWrap.className = 'news-card-thumb';
                const img = document.createElement('img');
                img.src = art.thumbnail;
                img.alt = '';
                img.loading = 'lazy';
                img.onerror = () => { imgWrap.style.display = 'none'; };
                imgWrap.appendChild(img);
                card.appendChild(imgWrap);
            }

            // Content (right side)
            const body = document.createElement('div');
            body.className = 'news-card-body';

            const headline = document.createElement('div');
            headline.className = 'news-card-headline';
            headline.textContent = art.title || '';
            body.appendChild(headline);

            if (art.snippet) {
                const snippet = document.createElement('div');
                snippet.className = 'news-card-snippet';
                snippet.textContent = art.snippet;
                body.appendChild(snippet);
            }

            const meta = document.createElement('div');
            meta.className = 'news-card-meta';
            const parts = [];
            if (art.source) parts.push(art.source);
            if (art.date) parts.push(art.date);
            meta.textContent = parts.join(' · ');
            body.appendChild(meta);

            card.appendChild(body);
            wrap.appendChild(card);
        }

        presSlide.appendChild(wrap);
    }

    function navigateSlide(delta) {
        if (!state.presentation) return;
        const next = state.currentSlide + delta;
        if (next >= 0 && next < state.presentation.slides.length) {
            renderSlide(next);
        }
    }

    function gotoSlide(index) {
        if (!state.presentation) return;
        if (index >= 0 && index < state.presentation.slides.length) {
            renderSlide(index);
        }
    }

    function clearPresentation() {
        destroyChart();
        clearAutoAdvance();
        state.presentation = null;
        state.currentSlide = 0;
        if (panel) {
            panel.classList.remove('active');
            if (presSlide) presSlide.innerHTML = '';
        }
        exitPresentMode();
    }

    function destroyChart() {
        if (state.chartInstance) {
            state.chartInstance.destroy();
            state.chartInstance = null;
        }
        if (state.lwCharts) {
            for (const c of state.lwCharts) c.remove();
        }
        state.lwCharts = [];
    }

    function clearAutoAdvance() {
        if (state.autoTimer) {
            clearInterval(state.autoTimer);
            state.autoTimer = null;
        }
    }

    // ========================================================================
    //  Cards
    // ========================================================================

    function showCard(data) {
        if (!cardContainer || !data) return;

        const card = document.createElement('div');
        card.className = `ui-card ui-card-${data.card_type || 'info'}`;
        card.dataset.cardId = data.id || '';

        let inner = `<div class="ui-card-header">
            <span class="ui-card-title">${escHtml(data.title || '')}</span>
            <button class="ui-card-close" title="Dismiss">&times;</button>
        </div>`;

        if (data.card_type === 'stat' && data.value) {
            inner += `<div class="ui-card-stat">${escHtml(data.value)}</div>`;
        }

        if (data.content) {
            inner += `<p class="ui-card-content">${escHtml(data.content)}</p>`;
        }

        if (data.card_type === 'quote' && data.content) {
            inner = `<div class="ui-card-header">
                <span class="ui-card-title">${escHtml(data.title || '')}</span>
                <button class="ui-card-close" title="Dismiss">&times;</button>
            </div>
            <blockquote class="ui-card-quote">${escHtml(data.content)}</blockquote>`;
        }

        if (data.image_url) {
            inner += `<img class="ui-card-image" src="${escAttr(data.image_url)}" alt="">`;
        }

        if (data.fields?.length) {
            inner += '<div class="ui-card-fields">';
            for (const f of data.fields) {
                inner += `<div class="ui-card-field">
                    <span class="ui-card-field-label">${escHtml(f.label || '')}</span>
                    <span class="ui-card-field-value">${escHtml(f.value || '')}</span>
                </div>`;
            }
            inner += '</div>';
        }

        card.innerHTML = inner;

        // Close button
        card.querySelector('.ui-card-close')?.addEventListener('click', () => removeCard(card));

        // Add to container
        cardContainer.appendChild(card);
        state.cards.push(card);

        // Enforce max cards
        while (state.cards.length > state.maxCards) {
            removeCard(state.cards[0]);
        }

        // Animate in
        requestAnimationFrame(() => card.classList.add('visible'));

        // Auto-dismiss
        if (data.duration_sec > 0) {
            setTimeout(() => removeCard(card), data.duration_sec * 1000);
        }
    }

    function removeCard(card) {
        card.classList.remove('visible');
        card.classList.add('dismissing');
        setTimeout(() => {
            card.remove();
            state.cards = state.cards.filter(c => c !== card);
        }, 300);
    }

    function clearCards() {
        for (const card of [...state.cards]) {
            removeCard(card);
        }
    }

    // ========================================================================
    //  Notifications
    // ========================================================================

    function showNotification(message, level) {
        if (!notifContainer || !message) return;

        const toast = document.createElement('div');
        toast.className = `ui-toast ui-toast-${level || 'info'}`;
        toast.textContent = message;

        notifContainer.appendChild(toast);
        state.notifications.push(toast);

        requestAnimationFrame(() => toast.classList.add('visible'));

        // Auto-dismiss after 5s
        setTimeout(() => dismissToast(toast), 5000);
    }

    function dismissToast(toast) {
        toast.classList.remove('visible');
        toast.classList.add('dismissing');
        setTimeout(() => {
            toast.remove();
            state.notifications = state.notifications.filter(n => n !== toast);
        }, 400);
    }

    function clearNotifications() {
        for (const toast of [...state.notifications]) {
            dismissToast(toast);
        }
    }

    // ========================================================================
    //  Clear all
    // ========================================================================

    function clearUI(target) {
        if (target === 'all' || target === 'presentation') clearPresentation();
        if (target === 'all' || target === 'cards')         clearCards();
        if (target === 'all' || target === 'notifications') clearNotifications();
    }

    // ========================================================================
    //  Utilities
    // ========================================================================

    function escHtml(str) {
        const d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }

    function escAttr(str) {
        return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;')
                  .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // Price/change values may arrive as raw numbers (875.32, 3.1) OR as
    // already-formatted strings (e.g. "$875.32", "+3.1%", or "-1.23%")
    // when the upstream tool resolved a num: handle. Don't double-prefix.
    function _fmtPrice(v) {
        if (typeof v === 'string') {
            const s = v.trim();
            return escHtml(s.startsWith('$') || s.startsWith('-$') || s.startsWith('+$') ? s : '$' + s);
        }
        return '$' + v;
    }
    function _fmtChange(v) {
        if (typeof v === 'string') {
            const s = v.trim();
            return escHtml(s.endsWith('%') ? s : s + '%');
        }
        return (v >= 0 ? '+' : '') + v + '%';
    }
    function _changeDir(v) {
        if (typeof v === 'number') return v >= 0 ? 'up' : 'down';
        if (typeof v === 'string') return v.trim().startsWith('-') ? 'down' : 'up';
        return 'up';
    }

    // ========================================================================
    //  Expose API
    // ========================================================================

    window._presentation = { handleUiCommand, onSegmentPlay };

})();
