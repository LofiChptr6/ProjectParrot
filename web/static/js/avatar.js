/**
 * avatar.js — Settings → Avatar tab.
 *
 * Per-user library of VRMs + reference voices, plus inline editors for
 * soul.md and behaviors.yaml. Talks to the per-user library endpoints in
 * bridge/server.py (all auth-gated via the JWT in localStorage).
 *
 * Exposes window.Avatar.{ refresh, init }. init() is called by app.js after
 * DOM ready; refresh() is called by setupTabs() when the user clicks the
 * Avatar tab so the data is always fresh.
 */

(function () {
    'use strict';

    const els = {};

    function _q(id) { return document.getElementById(id); }

    function _init() {
        els.vrmGrid     = _q('avatarVrmGrid');
        els.vrmDrop     = _q('avatarVrmDrop');
        els.vrmFile     = _q('avatarVrmFile');
        els.voiceList   = _q('avatarVoiceList');
        els.voiceDrop   = _q('avatarVoiceDrop');
        els.voiceFile   = _q('avatarVoiceFile');
        els.soulEditor  = _q('avatarSoulEditor');
        els.soulSave    = _q('avatarSoulSave');
        els.soulReset   = _q('avatarSoulReset');
        els.soulFile    = _q('avatarSoulFile');
        els.soulSaved   = _q('avatarSoulSaved');
        els.behavEditor = _q('avatarBehaviorsEditor');
        els.behavSave   = _q('avatarBehaviorsSave');
        els.behavReset  = _q('avatarBehaviorsReset');
        els.behavFile   = _q('avatarBehaviorsFile');
        els.behavSaved  = _q('avatarBehaviorsSaved');
        if (!els.vrmGrid) return;  // Avatar tab markup missing — no-op.

        // ── VRM dropzone ────────────────────────────────────────────────
        _wireDropzone(els.vrmDrop, els.vrmFile, ['.vrm'], (f) => _upload('vrm', f));
        // ── Voice dropzone ──────────────────────────────────────────────
        _wireDropzone(els.voiceDrop, els.voiceFile, ['.wav', '.mp3', '.ogg', '.m4a'],
                      (f) => _upload('voice', f));

        // ── Soul / Behaviors editors ────────────────────────────────────
        if (els.soulSave)  els.soulSave.addEventListener('click', _saveSoul);
        if (els.behavSave) els.behavSave.addEventListener('click', _saveBehaviors);
        if (els.soulReset) els.soulReset.addEventListener('click', () => _resetEditor('soul'));
        if (els.behavReset) els.behavReset.addEventListener('click', () => _resetEditor('behaviors'));

        // Drop-file-into-editor handlers — read the file as text and stuff
        // it into the textarea. User still has to click Save.
        if (els.soulFile) els.soulFile.addEventListener('change', (e) => {
            const f = e.target.files && e.target.files[0];
            if (f) _readTextInto(f, els.soulEditor);
            e.target.value = '';
        });
        if (els.behavFile) els.behavFile.addEventListener('change', (e) => {
            const f = e.target.files && e.target.files[0];
            if (f) _readTextInto(f, els.behavEditor);
            e.target.value = '';
        });
    }

    function _wireDropzone(zone, fileInput, acceptExts, onFile) {
        if (!zone || !fileInput) return;
        zone.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', (e) => {
            const f = e.target.files && e.target.files[0];
            if (f) onFile(f);
            e.target.value = '';
        });
        zone.addEventListener('dragover', (e) => {
            e.preventDefault();
            zone.classList.add('drag-over');
        });
        zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            zone.classList.remove('drag-over');
            const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (!f) return;
            const ok = acceptExts.some(ext => f.name.toLowerCase().endsWith(ext));
            if (!ok) {
                _toast(zone, 'Wrong file type — expected ' + acceptExts.join('/'));
                return;
            }
            onFile(f);
        });
    }

    function _readTextInto(file, textarea) {
        const r = new FileReader();
        r.onload = () => { textarea.value = r.result || ''; };
        r.onerror = () => { console.error('read failed', r.error); };
        r.readAsText(file);
    }

    function _toast(target, msg) {
        if (!target) return;
        const old = target.dataset.originalText || target.textContent;
        target.dataset.originalText = old;
        target.textContent = msg;
        setTimeout(() => { target.textContent = old; }, 2200);
    }

    function _flashSaved(pillEl) {
        if (!pillEl) return;
        pillEl.textContent = 'saved';
        pillEl.style.opacity = '1';
        setTimeout(() => { pillEl.style.opacity = '0'; }, 1800);
    }

    // ── API calls ───────────────────────────────────────────────────────

    function _hdrs(extra) {
        const h = (typeof authHeaders === 'function') ? authHeaders() : {};
        return Object.assign({}, h, extra || {});
    }

    async function _fetchLibrary() {
        try {
            const r = await fetch('/api/user/library', { headers: _hdrs() });
            if (!r.ok) return null;
            return await r.json();
        } catch (e) { return null; }
    }

    async function _activate(kind, name) {
        const r = await fetch('/api/user/active', {
            method: 'POST',
            headers: _hdrs({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ kind, name }),
        });
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            throw new Error(j.detail || 'activate failed');
        }
        return r.json();
    }

    async function _delete(kind, name) {
        const url = `/api/user/asset?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(name)}`;
        const r = await fetch(url, { method: 'DELETE', headers: _hdrs() });
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            throw new Error(j.detail || 'delete failed');
        }
        return r.json();
    }

    async function _upload(kind, file) {
        const form = new FormData();
        form.append('file', file);
        const url = `/api/user/upload/${kind}`;
        const r = await fetch(url, { method: 'POST', headers: _hdrs(), body: form });
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            console.error('upload failed', j);
            return;
        }
        await refresh();
        // If the user uploaded their first VRM, auto-active kicks in server-side
        // and the canvas should refresh too. Trigger that now.
        if (kind === 'vrm' && typeof loadVRMFromUrl === 'function') {
            try { await loadVRMFromUrl('/api/user/vrm?bust=' + Date.now()); } catch (_) {}
        }
    }

    // ── Render ──────────────────────────────────────────────────────────

    async function refresh() {
        const lib = await _fetchLibrary();
        if (!lib) return;
        _renderVrms(lib.vrms || [], lib.active_vrm);
        _renderVoices(lib.voices || [], lib.active_voice);
        await _loadCharacter();
    }

    function _renderVrms(vrms, active) {
        if (!els.vrmGrid) return;
        if (!vrms.length) {
            els.vrmGrid.innerHTML = '<div class="avatar-empty">No VRMs yet — drop one below.</div>';
            return;
        }
        els.vrmGrid.innerHTML = vrms.map(v => `
            <div class="avatar-card${v.name === active ? ' active' : ''}" data-name="${_esc(v.name)}">
                <div class="avatar-card-icon">🧍</div>
                <div class="avatar-card-name" title="${_esc(v.name)}">${_esc(v.name)}</div>
                ${v.name === active ? '<div class="avatar-card-badge">Active</div>' : ''}
                <button class="avatar-card-del" title="Delete" data-name="${_esc(v.name)}">×</button>
            </div>
        `).join('');
        els.vrmGrid.querySelectorAll('.avatar-card').forEach(card => {
            card.addEventListener('click', async (e) => {
                if (e.target.classList.contains('avatar-card-del')) return;
                const name = card.dataset.name;
                if (name === active) return;
                try {
                    await _activate('vrm', name);
                    await refresh();
                    if (typeof loadVRMFromUrl === 'function') {
                        await loadVRMFromUrl('/api/user/vrm?bust=' + Date.now());
                    }
                } catch (err) {
                    alert(err.message);
                }
            });
        });
        els.vrmGrid.querySelectorAll('.avatar-card-del').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const name = btn.dataset.name;
                if (!confirm(`Delete ${name}?`)) return;
                try { await _delete('vrm', name); await refresh(); }
                catch (err) { alert(err.message); }
            });
        });
    }

    function _renderVoices(voices, active) {
        if (!els.voiceList) return;
        if (!voices.length) {
            els.voiceList.innerHTML = '<div class="avatar-empty">No voices yet — drop a clip below.</div>';
            return;
        }
        els.voiceList.innerHTML = voices.map(v => `
            <div class="avatar-list-row${v.name === active ? ' active' : ''}" data-name="${_esc(v.name)}">
                <span class="avatar-list-name" title="${_esc(v.name)}">${_esc(v.name)}</span>
                <audio class="avatar-list-audio" controls preload="none"
                       src="/api/user/voice-file?name=${encodeURIComponent(v.name)}"></audio>
                ${v.name === active ? '<span class="avatar-card-badge">Active</span>' : ''}
                <button class="avatar-list-del" title="Delete" data-name="${_esc(v.name)}">×</button>
            </div>
        `).join('');
        els.voiceList.querySelectorAll('.avatar-list-row').forEach(row => {
            row.addEventListener('click', async (e) => {
                if (e.target.tagName === 'BUTTON' || e.target.tagName === 'AUDIO') return;
                const name = row.dataset.name;
                if (name === active) return;
                try { await _activate('voice', name); await refresh(); }
                catch (err) { alert(err.message); }
            });
        });
        els.voiceList.querySelectorAll('.avatar-list-del').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const name = btn.dataset.name;
                if (!confirm(`Delete ${name}?`)) return;
                try { await _delete('voice', name); await refresh(); }
                catch (err) { alert(err.message); }
            });
        });
    }

    // ── Soul / Behaviors editors ────────────────────────────────────────

    async function _loadCharacter() {
        try {
            const r = await fetch('/api/user/character', { headers: _hdrs() });
            if (!r.ok) return;
            const j = await r.json();
            if (els.soulEditor) els.soulEditor.value = j.soul || '';
            if (els.behavEditor) els.behavEditor.value = j.behaviors || '';
        } catch (_) {}
    }

    async function _saveSoul() {
        try {
            const r = await fetch('/api/user/soul', {
                method: 'PUT',
                headers: _hdrs({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ content: els.soulEditor.value }),
            });
            if (r.ok) _flashSaved(els.soulSaved);
            else alert('save failed');
        } catch (e) { alert(e.message); }
    }

    async function _saveBehaviors() {
        try {
            const r = await fetch('/api/user/behaviors', {
                method: 'PUT',
                headers: _hdrs({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ content: els.behavEditor.value }),
            });
            if (r.ok) _flashSaved(els.behavSaved);
            else alert('save failed');
        } catch (e) { alert(e.message); }
    }

    async function _resetEditor(kind) {
        // Loads the GLOBAL default into the editor without saving — user
        // still has to click Save to commit. Endpoint:
        // /api/character-default?kind=soul|behaviors
        try {
            const r = await fetch(`/api/character-default?kind=${encodeURIComponent(kind)}`);
            if (!r.ok) {
                alert(`Couldn't load default ${kind}`);
                return;
            }
            const txt = await r.text();
            if (kind === 'soul') els.soulEditor.value = txt;
            else els.behavEditor.value = txt;
        } catch (e) {
            alert(e.message);
        }
    }

    function _esc(s) {
        return String(s || '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    document.addEventListener('DOMContentLoaded', _init);

    window.Avatar = { init: _init, refresh };
})();
