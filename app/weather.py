"""Weather enrichment via the free Open-Meteo API (no API key required).

The module is deliberately failure-tolerant: any network/API problem simply
returns ``None`` so that importing an activity never breaks.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_FIELDS = "temperature_2m,precipitation,wind_speed_10m,weather_code"
TIMEOUT_SEC = 8
MAX_PAST_DAYS = 92

#: WMO weather codes -> (український опис, іконка)
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Ясно", "☀️"),
    1: ("Переважно ясно", "🌤️"),
    2: ("Мінлива хмарність", "⛅"),
    3: ("Похмуро", "☁️"),
    45: ("Туман", "🌫️"),
    48: ("Туман з інеєм", "🌫️"),
    51: ("Легка мряка", "🌦️"),
    53: ("Мряка", "🌦️"),
    55: ("Сильна мряка", "🌧️"),
    56: ("Крижана мряка", "🌧️"),
    57: ("Сильна крижана мряка", "🌧️"),
    61: ("Невеликий дощ", "🌦️"),
    63: ("Дощ", "🌧️"),
    65: ("Сильний дощ", "🌧️"),
    66: ("Крижаний дощ", "🌧️"),
    67: ("Сильний крижаний дощ", "🌧️"),
    71: ("Невеликий сніг", "🌨️"),
    73: ("Сніг", "🌨️"),
    75: ("Сильний сніг", "❄️"),
    77: ("Снігові зерна", "🌨️"),
    80: ("Невеликі зливи", "🌦️"),
    81: ("Зливи", "🌧️"),
    82: ("Сильні зливи", "⛈️"),
    85: ("Снігові зливи", "🌨️"),
    86: ("Сильні снігові зливи", "❄️"),
    95: ("Гроза", "⛈️"),
    96: ("Гроза з градом", "⛈️"),
    99: ("Сильна гроза з градом", "⛈️"),
}


def describe_code(code: int | None) -> tuple[str, str]:
    """Return a human readable description and emoji for a WMO weather code."""
    if code is None:
        return ("—", "🌡️")
    return WMO_CODES.get(int(code), ("Мінлива погода", "🌡️"))


def _get_json(url: str) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": "PaceLab/3.0 (+local)"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def _nearest_hour_index(times: list[str], when: datetime) -> int | None:
    """Find the index of the hourly slot closest to ``when`` (local time)."""
    best_index: int | None = None
    best_delta: float | None = None
    for index, raw in enumerate(times):
        try:
            candidate = datetime.fromisoformat(raw)
        except ValueError:
            continue
        delta = abs((candidate - when).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta, best_index = delta, index
    return best_index


def fetch_weather(latitude: float, longitude: float, when: datetime) -> dict | None:
    """Fetch weather at a location/hour. Returns a compact dict or ``None``."""
    target_day = when.date()
    days_back = (date.today() - target_day).days

    params = {
        "latitude": f"{latitude:.5f}",
        "longitude": f"{longitude:.5f}",
        "hourly": HOURLY_FIELDS,
        "timezone": "auto",
        "wind_speed_unit": "kmh",
    }
    if days_back <= MAX_PAST_DAYS:
        params["past_days"] = max(1, min(MAX_PAST_DAYS, days_back + 1))
        params["forecast_days"] = 1
        url = f"{FORECAST_URL}?{urllib.parse.urlencode(params)}"
    else:
        params["start_date"] = target_day.isoformat()
        params["end_date"] = target_day.isoformat()
        url = f"{ARCHIVE_URL}?{urllib.parse.urlencode(params)}"

    payload = _get_json(url)
    if not payload:
        return None

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    index = _nearest_hour_index(times, when)
    if index is None:
        return None

    def pick(field: str) -> float | None:
        values = hourly.get(field) or []
        if index >= len(values):
            return None
        value = values[index]
        return float(value) if value is not None else None

    temperature = pick("temperature_2m")
    wind = pick("wind_speed_10m")
    precipitation = pick("precipitation")
    code_raw = pick("weather_code")
    code = int(code_raw) if code_raw is not None else None
    description, icon = describe_code(code)

    if temperature is None and wind is None and precipitation is None:
        return None

    return {
        "temp_c": round(temperature, 1) if temperature is not None else None,
        "wind_kmh": round(wind, 1) if wind is not None else None,
        "precipitation_mm": round(precipitation, 2) if precipitation is not None else None,
        "code": code,
        "description": description,
        "icon": icon,
    }


def badge_from_json(raw: str | None) -> dict | None:
    """Turn the stored ``weather_json`` string into a template-friendly dict."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data:
        return None
    data.setdefault("icon", "🌡️")
    data.setdefault("description", "")
    return data

