"""Route building through the public OSRM foot router + Open-Meteo elevation.

Both APIs are free and require no key. The module is testable in isolation:
network calls go through :func:`_request_json`, which tests may monkeypatch.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/foot"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
TIMEOUT_SEC = 12
MAX_WAYPOINTS = 25
MAX_ELEVATION_SAMPLES = 100
GPX_PACE_SEC_PER_KM = 360.0  # орієнтовний темп для часових міток у GPX


class RouteError(Exception):
    """Raised when a route cannot be built (network, OSRM status, bad input)."""


class RouteInputError(RouteError):
    """Bad input supplied by the client (waypoint count/format)."""


def _request_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "PaceLab/3.0 (+local)"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
        return json.loads(response.read().decode("utf-8"))


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _normalize_waypoints(waypoints: list) -> list[tuple[float, float]]:
    if not isinstance(waypoints, list) or len(waypoints) < 2:
        raise RouteInputError("Потрібно щонайменше 2 точки маршруту")
    if len(waypoints) > MAX_WAYPOINTS:
        raise RouteInputError(f"Максимум {MAX_WAYPOINTS} точок маршруту")
    normalized: list[tuple[float, float]] = []
    for item in waypoints:
        try:
            if isinstance(item, dict):
                lat, lon = float(item["lat"]), float(item["lon"])
            else:
                lat, lon = float(item[0]), float(item[1])
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise RouteInputError("Некоректні координати точки маршруту") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise RouteInputError("Координати поза допустимими межами")
        normalized.append((lat, lon))
    return normalized


def fetch_route(waypoints: list[tuple[float, float]]) -> dict:
    """Ask OSRM for a foot route. Returns ``{"coords": [[lat, lon], ...], "distance_m": float}``."""
    coordinates = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    params = urllib.parse.urlencode({"overview": "full", "geometries": "geojson", "steps": "false"})
    url = f"{OSRM_ROUTE_URL}/{coordinates}?{params}"
    try:
        payload = _request_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
        raise RouteError("Сервіс OSRM недоступний — спробуй пізніше") from exc

    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RouteError(f"OSRM не зміг прокласти маршрут: {payload.get('message', 'невідома причина')}")
    route = payload["routes"][0]
    geometry = route.get("geometry", {}).get("coordinates") or []
    coords = [[round(lat, 6), round(lon, 6)] for lon, lat in geometry]
    if len(coords) < 2:
        raise RouteError("OSRM повернув порожню геометрію маршруту")
    return {"coords": coords, "distance_m": float(route.get("distance") or 0.0)}


def fetch_elevations(coords: list[list[float]]) -> list[float] | None:
    """Fetch elevation for up to 100 sampled points (Open-Meteo elevation API)."""
    if not coords:
        return None
    step = max(1, math.ceil(len(coords) / MAX_ELEVATION_SAMPLES))
    sampled = coords[::step]
    if sampled[-1] is not coords[-1]:
        sampled = sampled + [coords[-1]]
    latitude = ",".join(f"{lat:.5f}" for lat, _ in sampled)
    longitude = ",".join(f"{lon:.5f}" for _, lon in sampled)
    url = f"{ELEVATION_URL}?latitude={latitude}&longitude={longitude}"
    try:
        payload = _request_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None
    elevations = payload.get("elevation") or []
    if not elevations:
        return None
    if len(elevations) == len(sampled):
        return [round(float(value), 1) for value in elevations]
    return None


def elevation_gain(elevations: list[float] | None) -> float:
    """Total positive ascent in metres."""
    if not elevations or len(elevations) < 2:
        return 0.0
    gain = 0.0
    previous = elevations[0]
    for value in elevations[1:]:
        if value > previous:
            gain += value - previous
        previous = value
    return round(gain, 1)


def _distance_m(coords: list[list[float]]) -> float:
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(coords, coords[1:]):
        total += _haversine_m(lat1, lon1, lat2, lon2)
    return total


def build_route(waypoints: list) -> dict:
    """Full pipeline: OSRM geometry -> elevations -> summary + ready-to-export GPX."""
    normalized = _normalize_waypoints(waypoints)
    route = fetch_route(normalized)
    coords = route["coords"]
    distance_m = route["distance_m"] or _distance_m(coords)

    elevations = fetch_elevations(coords)
    gain = elevation_gain(elevations)

    # прив'язуємо висоти до точок (лінійна інтерполяція від семплів)
    coords_with_ele: list[list[float]] = []
    if elevations:
        step = max(1, math.ceil(len(coords) / max(1, len(elevations))))
        for index, (lat, lon) in enumerate(coords):
            sampled_index = min(len(elevations) - 1, index // step)
            coords_with_ele.append([lat, lon, elevations[sampled_index]])
    else:
        coords_with_ele = [[lat, lon, 0.0] for lat, lon in coords]

    gpx_text = route_to_gpx(coords_with_ele, name="PaceLab route")
    return {
        "coords": coords,
        "coords_with_elevation": coords_with_ele,
        "distance_m": round(distance_m, 1),
        "distance_km": round(distance_m / 1000, 2),
        "elevation_gain_m": gain,
        "estimated_duration_sec": int(distance_m / 1000 * GPX_PACE_SEC_PER_KM) if distance_m else 0,
        "gpx": gpx_text,
    }


def route_to_gpx(coords_with_elevation: list[list[float]], name: str = "PaceLab route") -> str:
    """Serialize ``[[lat, lon, ele], ...]`` into a GPX 1.1 track."""
    start = datetime.now().replace(microsecond=0)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="PaceLab route builder" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{escape(name)}</name>",
        "  </metadata>",
        "  <trk>",
        f"    <name>{escape(name)}</name>",
        "    <type>running</type>",
        "    <trkseg>",
    ]
    cumulative_m = 0.0
    previous: list[float] | None = None
    for point in coords_with_elevation:
        lat, lon, ele = float(point[0]), float(point[1]), float(point[2]) if len(point) > 2 else 0.0
        if previous is not None:
            cumulative_m += _haversine_m(previous[0], previous[1], lat, lon)
        previous = point
        timestamp = start + timedelta(seconds=cumulative_m / 1000 * GPX_PACE_SEC_PER_KM)
        lines.append(
            f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">'
            f"<ele>{ele:.1f}</ele><time>{timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')}</time></trkpt>"
        )
    lines += ["    </trkseg>", "  </trk>", "</gpx>"]
    return "\n".join(lines)

