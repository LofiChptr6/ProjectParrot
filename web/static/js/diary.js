/**
 * diary.js — Mocha's Diary reader (book-flip modal).
 *
 * Fetches pages from /api/diary/pages, lazy-loads the full content per page
 * on demand via /api/diary/page/:date. Navigation: left/right arrows + keys.
 *
 * Exposes:
 *   window.Diary.open(date?)   — open at a specific page (or most-recent)
 *   window.Diary.close()
 *
 * UI markup lives in index.html under #diaryModal.
 */

(function () {
    'use strict';

    const els = {};
    const state = {
        pages: [],       // [{date, summary, status, activity_count, ...}] — index only
        idx: 0,          // current page index into `pages` (0 = newest)
        fullCache: {},   // date → full page JSON (lazy-loaded)
        open: false,
    };

    function _q(id) { return document.getElementById(id); }

    function _init() {
        els.modal        = _q('diaryModal');
        els.date         = _q('diaryDate');
        els.counter      = _q('diaryCounter');
        els.prev         = _q('diaryPrev');
        els.next         = _q('diaryNext');
        els.close        = _q('diaryClose');
        els.status       = _q('diaryStatus');
        els.summary      = _q('diarySummary');
        els.highlights   = _q('diaryHighlights');
        els.activities   = _q('diaryActivities');
        els.delete       = _q('diaryDelete');
        if (!els.modal) return; // DOM not ready / not on this page

        els.prev.addEventListener('click', () => _flip(+1));   // prev = older
        els.next.addEventListener('click', () => _flip(-1));   // next = newer
        els.close.addEventListener('click', close);
        els.delete.addEventListener('click', _deleteCurrent);
        document.addEventListener('keydown', (e) => {
            if (!state.open) return;
            if (e.key === 'Escape')      close();
            else if (e.key === 'ArrowLeft')  _flip(+1);
            else if (e.key === 'ArrowRight') _flip(-1);
        });
    }

    function _escape(s) {
        return String(s || '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    async function _fetchIndex() {
        const r = await fetch('/api/diary/pages');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        return Array.isArray(data.pages) ? data.pages : [];
    }

    async function _fetchPage(date) {
        if (state.fullCache[date]) return state.fullCache[date];
        const r = await fetch('/api/diary/page/' + encodeURIComponent(date));
        if (!r.ok) return null;
        const page = await r.json();
        state.fullCache[date] = page;
        return page;
    }

    function _renderEmpty(msg) {
        els.date.textContent = '—';
        els.counter.textContent = '—';
        els.status.textContent = '';
        els.summary.textContent = msg || 'No diary pages yet.';
        els.highlights.innerHTML = '';
        els.activities.innerHTML = '';
    }

    async function _renderCurrent() {
        if (state.pages.length === 0) {
            _renderEmpty();
            return;
        }
        const stub = state.pages[state.idx];
        const page = await _fetchPage(stub.date) || stub;

        els.date.textContent    = page.date || '—';
        els.counter.textContent = (state.idx + 1) + ' / ' + state.pages.length;
        els.status.textContent  = page.status === 'draft' ? '(today, still writing)' : '';
        els.summary.textContent = page.summary || '(nothing written yet)';

        const highlights = Array.isArray(page.highlights) ? page.highlights : [];
        if (highlights.length) {
            els.highlights.innerHTML =
                '<ul>' + highlights.map(h => '<li>' + _escape(h) + '</li>').join('') + '</ul>';
        } else {
            els.highlights.innerHTML = '';
        }

        const activities = Array.isArray(page.activities) ? page.activities : [];
        if (activities.length) {
            els.activities.innerHTML = activities.map((a) =>
                '<div class="diary-activity">' +
                '<span class="diary-activity-time">' + _escape(a.t || '') + '</span> ' +
                '<span class="diary-activity-tool">' + _escape(a.tool || '') + '</span>' +
                (a.brief ? ' — <span class="diary-activity-brief">' + _escape(a.brief) + '</span>' : '') +
                '</div>'
            ).join('');
        } else {
            els.activities.innerHTML = '<div class="diary-activity-empty">no tool activity this day</div>';
        }

        // Draft pages can't be deleted safely (would leave data/activities
        // orphaned today); only finalized pages expose the delete action.
        els.delete.disabled = (page.status !== 'final');
        els.delete.style.opacity = els.delete.disabled ? '0.4' : '1';
    }

    function _flip(delta) {
        if (state.pages.length === 0) return;
        const next = state.idx + delta;
        if (next < 0 || next >= state.pages.length) return;
        state.idx = next;
        _renderCurrent().catch(console.error);
    }

    async function _deleteCurrent() {
        if (state.pages.length === 0) return;
        const page = state.pages[state.idx];
        if (!page || page.status !== 'final') return;
        if (!confirm('Delete diary page for ' + page.date + '? This cannot be undone.')) return;
        try {
            const r = await fetch('/api/diary/page/' + encodeURIComponent(page.date), {
                method: 'DELETE',
            });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            delete state.fullCache[page.date];
            state.pages.splice(state.idx, 1);
            if (state.idx >= state.pages.length) state.idx = Math.max(0, state.pages.length - 1);
            await _renderCurrent();
        } catch (exc) {
            alert('Delete failed: ' + exc.message);
        }
    }

    async function open(date) {
        console.log('[Diary] open() called', { date });
        if (!els.modal) _init();
        if (!els.modal) {
            console.error('[Diary] #diaryModal not found in DOM — check HTML');
            return;
        }
        try {
            state.pages = await _fetchIndex();
            console.log('[Diary] fetched pages', state.pages.length);
        } catch (exc) {
            console.error('[Diary] index fetch failed', exc);
            state.pages = [];
        }
        if (date) {
            const i = state.pages.findIndex(p => p.date === date);
            state.idx = i >= 0 ? i : 0;
        } else {
            state.idx = 0;   // newest
        }
        state.open = true;
        els.modal.classList.remove('hidden');
        els.modal.setAttribute('aria-hidden', 'false');
        console.log('[Diary] modal class list after open:', els.modal.className);
        await _renderCurrent();
    }

    function close() {
        state.open = false;
        if (els.modal) {
            els.modal.classList.add('hidden');
            els.modal.setAttribute('aria-hidden', 'true');
        }
    }

    // Expose API + init
    window.Diary = { open, close };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _init);
    } else {
        _init();
    }
})();
