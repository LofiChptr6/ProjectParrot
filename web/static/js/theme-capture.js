/**
 * theme-capture.js — capture the current viewport as a PNG and ship it back
 * to the bridge. Used by Hana (vision sub-agent) for design critique.
 *
 * Triggered by a ui_command from the bridge:
 *   { type:"ui_command", action:"capture_screenshot", request_id:"..." }
 *
 * Posts the result to POST /admin/theme/screenshot-reply:
 *   { request_id, image_b64 }  — success
 *   { request_id, error }       — failure
 *
 * The VRM canvas is WebGL; html2canvas historically renders WebGL as black
 * unless `preserveDrawingBuffer: true` is set on the context. We fall back
 * to compositing the raw canvas contents via canvas.toDataURL() if the
 * html2canvas capture comes back blank over the 3D area.
 */

(function () {
    'use strict';

    let _html2canvasPromise = null;

    function loadHtml2Canvas() {
        if (window.html2canvas) return Promise.resolve(window.html2canvas);
        if (_html2canvasPromise) return _html2canvasPromise;
        _html2canvasPromise = new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = '/static/vendor/html2canvas.min.js';
            s.onload = () => resolve(window.html2canvas);
            s.onerror = () => reject(new Error('Failed to load html2canvas'));
            document.head.appendChild(s);
        });
        return _html2canvasPromise;
    }

    /** Strip the "data:image/png;base64," prefix. */
    function dataUrlToB64(dataUrl) {
        const idx = dataUrl.indexOf('base64,');
        return idx >= 0 ? dataUrl.slice(idx + 'base64,'.length) : '';
    }

    async function capturePage() {
        const html2canvas = await loadHtml2Canvas();
        // Give the preview a beat to apply.
        await new Promise(r => setTimeout(r, 80));

        // Scale 0.5 — Hana reads the layout + colors just fine at half res, and
        // html2canvas is ~4x faster. Also ignoreElements skips the WebGL canvas
        // (html2canvas can't render it anyway, and excluding it removes the
        // single slowest node to traverse).
        const canvas = await html2canvas(document.body, {
            backgroundColor: null,
            scale: 0.5,
            useCORS: true,
            logging: false,
            foreignObjectRendering: false,
            allowTaint: false,
            imageTimeout: 3000,
            ignoreElements: (el) => el && el.id === 'canvas3d',
        });

        // Composite the VRM canvas on top via toDataURL (fast — single draw).
        // Best-effort: if the WebGL context wasn't created with
        // preserveDrawingBuffer this returns blank and we keep what we have.
        try {
            const threeCanvas = document.getElementById('canvas3d');
            if (threeCanvas) {
                const rect = threeCanvas.getBoundingClientRect();
                const ctx = canvas.getContext('2d');
                if (ctx && rect.width > 0 && rect.height > 0) {
                    try {
                        const glPng = threeCanvas.toDataURL('image/png');
                        if (glPng && glPng.length > 256) {
                            await new Promise((resolve) => {
                                const img = new Image();
                                const t = setTimeout(resolve, 800);   // hard cap
                                img.onload = () => {
                                    clearTimeout(t);
                                    ctx.drawImage(
                                        img,
                                        rect.left * 0.5, rect.top * 0.5,
                                        rect.width * 0.5, rect.height * 0.5,
                                    );
                                    resolve();
                                };
                                img.onerror = () => { clearTimeout(t); resolve(); };
                                img.src = glPng;
                            });
                        }
                    } catch (_) { /* tainted / empty */ }
                }
            }
        } catch (_) { /* best-effort */ }

        return dataUrlToB64(canvas.toDataURL('image/png'));
    }

    async function captureAndReply(requestId) {
        if (!requestId) return;
        // Webapp proxies /admin/* to the bridge — use page host so this
        // works on direct LAN, Cloudflare tunnel, and any reverse proxy.
        const bridge = `${location.protocol}//${location.host}`;
        try {
            const b64 = await capturePage();
            await fetch(bridge + '/admin/theme/screenshot-reply', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ request_id: requestId, image_b64: b64 }),
            });
        } catch (err) {
            console.warn('theme-capture failed:', err);
            try {
                await fetch(bridge + '/admin/theme/screenshot-reply', {
                    method: 'POST',
                    headers: { 'content-type': 'application/json' },
                    body: JSON.stringify({ request_id: requestId, error: String(err) }),
                });
            } catch (_) { /* swallow */ }
        }
    }

    window.ThemeCapture = { captureAndReply, capturePage };
})();
