"""Custom tool: get_weather — fetch weather via Open-Meteo (free, no key).

Numeric values (temps, humidity, wind, precipitation) are wrapped with the
``num:xxxxxxxx`` handle system so the LLM never sees raw numbers — it must
quote handles verbatim, and the bridge resolves them to formatted values
before TTS.
"""

import json

from tools.handle_registry import get_registry


def _h_temp(val) -> str | None:
    if val is None:
        return None
    return get_registry().issue("num", f"{val:.0f}°C")


def _h_pct(val) -> str | None:
    if val is None:
        return None
    return get_registry().issue("num", f"{val:.0f}%")


def _h_wind(val) -> str | None:
    if val is None:
        return None
    return get_registry().issue("num", f"{val:.0f} km/h")


def _h_mm(val) -> str | None:
    if val is None:
        return None
    return get_registry().issue("num", f"{val:.1f} mm")

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Fetch current weather and forecast for a location. "
            "Returns temperature, conditions, and daily forecast."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name or place (e.g. 'Tokyo', 'New York')",
                },
                "days": {
                    "type": "integer",
                    "description": "Forecast days (1-7, default 3)",
                },
            },
            "required": ["location"],
        },
    },
}


async def execute(arguments: dict) -> str:
    """Geocode location, then fetch weather from Open-Meteo."""
    import httpx

    location = arguments.get("location", "")
    days = min(max(arguments.get("days", 3), 1), 7)

    if not location:
        return "Error: no location provided."

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Geocode
            geo_resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "en"},
            )
            geo = geo_resp.json()
            results = geo.get("results")
            if not results:
                return f"Could not find location: {location}"

            place = results[0]
            lat, lon = place["latitude"], place["longitude"]
            name = place.get("name", location)
            country = place.get("country", "")

            # Fetch weather
            wx_resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_sum",
                    "forecast_days": days,
                    "timezone": "auto",
                },
            )
            wx = wx_resp.json()

            # WMO weather codes to descriptions
            wmo = {
                0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
                45: "Fog", 48: "Rime fog",
                51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
                61: "Light rain", 63: "Rain", 65: "Heavy rain",
                71: "Light snow", 73: "Snow", 75: "Heavy snow",
                80: "Light showers", 81: "Showers", 82: "Heavy showers",
                95: "Thunderstorm", 96: "Thunderstorm w/ hail",
            }

            current = wx.get("current", {})
            daily = wx.get("daily", {})

            result = {
                "location": f"{name}, {country}",
                "current": {
                    "temp_c": _h_temp(current.get("temperature_2m")),
                    "humidity": _h_pct(current.get("relative_humidity_2m")),
                    "wind_kph": _h_wind(current.get("wind_speed_10m")),
                    "condition": wmo.get(current.get("weather_code", -1), "Unknown"),
                },
                "forecast": [],
            }

            dates = daily.get("time", [])
            highs = daily.get("temperature_2m_max", [])
            lows = daily.get("temperature_2m_min", [])
            codes = daily.get("weather_code", [])
            precip = daily.get("precipitation_sum", [])

            for i in range(min(days, len(dates))):
                result["forecast"].append({
                    "date": dates[i],
                    "high_c": _h_temp(highs[i]) if i < len(highs) else None,
                    "low_c": _h_temp(lows[i]) if i < len(lows) else None,
                    "condition": wmo.get(codes[i] if i < len(codes) else -1, "Unknown"),
                    "precip_mm": _h_mm(precip[i]) if i < len(precip) else None,
                })

            return json.dumps(result, separators=(",", ":"))
    except Exception as e:
        return f"Error fetching weather: {e}"
