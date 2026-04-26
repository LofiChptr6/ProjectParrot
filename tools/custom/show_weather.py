"""Custom tool: show_weather — open an animated weather modal.

Fetches conditions + 7-day forecast from Open-Meteo (free, no API key) and
broadcasts a ui_command that opens the weather modal in the frontend, with an
animated background matching the current condition (sunny / cloudy / rain /
snow / fog / thunderstorm).

Mirrors the show_diary pattern: ui_command broadcast, frontend handles the
rest. Not registered with _OPEN_MODALS — the modal is user-dismissable and
re-opening is cheap+idempotent.
"""

from __future__ import annotations

from typing import Any


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "show_weather",
        "description": (
            "Open an iPhone-Weather-style modal on the user's screen with current "
            "conditions, hourly temps, and a 7-day forecast — with an animated "
            "background that matches the weather (rain, sun, clouds, snow). Use "
            "when the user asks 'what's the weather', 'is it raining in X', "
            "'show me the forecast', or anything weather-shaped. Don't narrate "
            "the data afterward — the modal IS the answer; just react in one "
            "short sentence ('huh, it's pouring there')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City or place (e.g. 'Tokyo', 'San Francisco').",
                },
            },
            "required": ["location"],
        },
    },
}


# WMO weather code → animation class (matches CSS [data-condition] selectors).
_WMO_TO_BG = {
    0: "sunny", 1: "sunny",
    2: "cloudy", 3: "cloudy",
    45: "fog", 48: "fog",
    51: "rain", 53: "rain", 55: "rain",
    56: "rain", 57: "rain",
    61: "rain", 63: "rain", 65: "rain",
    66: "rain", 67: "rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow",
    80: "rain", 81: "rain", 82: "rain",
    85: "snow", 86: "snow",
    95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
}

_WMO_LABEL = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
}


async def execute(arguments: dict[str, Any]) -> str:
    import httpx

    location = (arguments.get("location") or "").strip()
    if not location:
        return "show_weather: location required"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Geocode
            geo = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "en"},
            )
            results = (geo.json() or {}).get("results") or []
            if not results:
                return f"show_weather: couldn't find {location}"

            place = results[0]
            lat, lon = place["latitude"], place["longitude"]
            display_name = ", ".join(
                p for p in (place.get("name"), place.get("admin1"), place.get("country"))
                if p
            )

            # Fetch current + hourly + daily with everything we need for the modal.
            wx = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                                "weather_code,wind_speed_10m",
                    "hourly": "temperature_2m,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code,"
                              "sunrise,sunset,precipitation_sum",
                    "forecast_days": 7,
                    "timezone": "auto",
                },
            )
            data = wx.json() or {}
    except Exception as exc:
        return f"show_weather: fetch failed: {exc}"

    cur = data.get("current") or {}
    hourly = data.get("hourly") or {}
    daily = data.get("daily") or {}
    cur_code = int(cur.get("weather_code") or 0)
    bg_class = _WMO_TO_BG.get(cur_code, "cloudy")

    # Trim hourly to next 24h starting from "now" — Open-Meteo returns 168h.
    now_iso = (cur.get("time") or "")[:13]  # "YYYY-MM-DDTHH"
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    start = 0
    for i, t in enumerate(times):
        if t[:13] >= now_iso:
            start = i
            break
    hourly_strip = []
    for i in range(start, min(start + 24, len(times))):
        hourly_strip.append({
            "time": times[i][11:16],  # "HH:MM"
            "temp_c": round(temps[i]) if i < len(temps) and temps[i] is not None else None,
            "wmo": int(codes[i]) if i < len(codes) and codes[i] is not None else 0,
        })

    forecast = []
    for i, d in enumerate(daily.get("time") or []):
        forecast.append({
            "date": d,
            "high_c": round((daily.get("temperature_2m_max") or [None])[i] or 0),
            "low_c": round((daily.get("temperature_2m_min") or [None])[i] or 0),
            "wmo": int((daily.get("weather_code") or [0])[i] or 0),
            "label": _WMO_LABEL.get(int((daily.get("weather_code") or [0])[i] or 0), ""),
            "precip_mm": round((daily.get("precipitation_sum") or [None])[i] or 0, 1),
        })

    payload = {
        "location": display_name,
        "current": {
            "temp_c": round(cur.get("temperature_2m") or 0),
            "feels_c": round(cur.get("apparent_temperature") or 0),
            "humidity": round(cur.get("relative_humidity_2m") or 0),
            "wind_kph": round(cur.get("wind_speed_10m") or 0),
            "wmo": cur_code,
            "label": _WMO_LABEL.get(cur_code, ""),
        },
        "hourly": hourly_strip,
        "forecast": forecast[:7],
        "sunrise": ((daily.get("sunrise") or [""])[0] or "")[11:16],
        "sunset": ((daily.get("sunset") or [""])[0] or "")[11:16],
        "bg_class": bg_class,
    }

    from bridge.server import _broadcast_clients
    try:
        await _broadcast_clients({
            "type": "ui_command",
            "action": "show_weather",
            "payload": payload,
        })
    except Exception as exc:
        return f"show_weather: broadcast failed: {exc}"

    return f"Weather modal opened for {display_name} ({payload['current']['label']})."
