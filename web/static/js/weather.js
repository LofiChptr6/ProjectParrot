/**
 * weather.js — iPhone-Weather-style modal with animated background.
 *
 * Opened via the `show_weather` ui_command (routed in presentation.js).
 * Payload arrives pre-shaped from tools/custom/show_weather.py and includes:
 *   { location, current:{temp_c, feels_c, humidity, wind_kph, wmo, label},
 *     hourly:[{time, temp_c, wmo}, ...24],
 *     forecast:[{date, high_c, low_c, wmo, label}, ...7],
 *     sunrise, sunset, bg_class }
 *
 * Exposes:
 *   window.Weather.open(payload)
 *   window.Weather.close()
 */

(function () {
    'use strict';

    const els = {};
    const state = { open: false };

    function _q(id) { return document.getElementById(id); }

    function _init() {
        els.modal     = _q('weatherModal');
        if (!els.modal) return;
        els.bg        = _q('weatherBg');
        els.close     = _q('weatherClose');
        els.location  = _q('weatherLocation');
        els.temp      = _q('weatherTemp');
        els.condition = _q('weatherCondition');
        els.feels     = _q('weatherFeels');
        els.hourly    = _q('weatherHourly');
        els.forecast  = _q('weatherForecast');
        els.sunrise   = _q('weatherSunrise');
        els.sunset    = _q('weatherSunset');
        els.humidity  = _q('weatherHumidity');
        els.wind      = _q('weatherWind');

        els.close.addEventListener('click', close);
        document.addEventListener('keydown', (e) => {
            if (state.open && e.key === 'Escape') close();
        });

        // Register with PanelManager so the user can drag/resize it
        // exactly like the presentation panel. autoLayout will reposition
        // on viewport resize / chat sidebar toggle.
        if (window.PanelManager && typeof window.PanelManager.registerPanel === 'function') {
            window.PanelManager.registerPanel('weather', els.modal, {
                top: 8, right: 8, bottom: 8, width: '45%',
            });
        }
    }

    // Open-Meteo WMO weather code → emoji glyph (cheap, language-free icon).
    const _ICON = (wmo) => {
        if (wmo === 0 || wmo === 1) return '☀️';
        if (wmo === 2) return '⛅';
        if (wmo === 3) return '☁️';
        if (wmo === 45 || wmo === 48) return '🌫️';
        if (wmo >= 51 && wmo <= 67) return '🌧️';
        if ((wmo >= 71 && wmo <= 77) || wmo === 85 || wmo === 86) return '❄️';
        if (wmo >= 80 && wmo <= 82) return '🌧️';
        if (wmo === 95 || wmo === 96 || wmo === 99) return '⛈️';
        return '🌡️';
    };

    function _renderHourly(hours) {
        if (!hours || !hours.length) {
            els.hourly.innerHTML = '<div class="wx-empty">no hourly data</div>';
            return;
        }
        // First entry shows "Now" instead of HH:MM so the strip reads at a glance.
        els.hourly.innerHTML = hours.map((h, i) => `
            <div class="wx-hour">
                <div class="wx-hour-time">${i === 0 ? 'Now' : (h.time || '')}</div>
                <div class="wx-hour-icon">${_ICON(h.wmo || 0)}</div>
                <div class="wx-hour-temp">${h.temp_c == null ? '—' : h.temp_c + '°'}</div>
            </div>
        `).join('');
    }

    function _renderForecast(days) {
        if (!days || !days.length) {
            els.forecast.innerHTML = '<div class="wx-empty">no forecast</div>';
            return;
        }
        const dayName = (iso, i) => {
            if (i === 0) return 'Today';
            try {
                const d = new Date(iso + 'T00:00:00');
                return d.toLocaleDateString(undefined, { weekday: 'short' });
            } catch (_) { return iso; }
        };
        els.forecast.innerHTML = days.map((d, i) => `
            <div class="wx-day">
                <div class="wx-day-name">${dayName(d.date, i)}</div>
                <div class="wx-day-icon">${_ICON(d.wmo || 0)}</div>
                <div class="wx-day-low">${d.low_c == null ? '—' : d.low_c + '°'}</div>
                <div class="wx-day-bar"><span></span></div>
                <div class="wx-day-high">${d.high_c == null ? '—' : d.high_c + '°'}</div>
            </div>
        `).join('');
    }

    function open(payload) {
        if (!els.modal) _init();
        if (!els.modal) return;
        if (!payload || typeof payload !== 'object') return;

        const cur = payload.current || {};
        els.location.textContent  = payload.location || '—';
        els.temp.textContent      = (cur.temp_c == null ? '—' : cur.temp_c) + '°';
        els.condition.textContent = cur.label || '—';
        els.feels.textContent     = 'Feels like ' + (cur.feels_c == null ? '—' : cur.feels_c) + '°';

        _renderHourly(payload.hourly || []);
        _renderForecast(payload.forecast || []);

        els.sunrise.textContent  = payload.sunrise || '—';
        els.sunset.textContent   = payload.sunset || '—';
        els.humidity.textContent = (cur.humidity == null ? '—' : cur.humidity + '%');
        els.wind.textContent     = (cur.wind_kph == null ? '—' : cur.wind_kph + ' km/h');

        // Drive the animated background. CSS [data-condition=X] selectors
        // toggle which .wx-fx-* layer is visible.
        els.bg.dataset.condition = payload.bg_class || 'cloudy';

        els.modal.classList.remove('hidden');
        els.modal.setAttribute('aria-hidden', 'false');
        state.open = true;
    }

    function close() {
        if (!els.modal) return;
        els.modal.classList.add('hidden');
        els.modal.setAttribute('aria-hidden', 'true');
        state.open = false;
    }

    document.addEventListener('DOMContentLoaded', _init);

    window.Weather = { open, close };
})();
