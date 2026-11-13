"""Parsing of ``.gpx`` / ``.fit`` activity files and running-metric calculation.

Both formats are normalized into a single internal representation
(:class:`RawTrack`) which is then turned into the plain dict that gets stored
in ``activity.raw_points_json`` and the numeric columns of ``activities``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EARTH_RADIUS_M = 6_371_000.0

#: Cap on how many track points are persisted (keeps the JSON column light).
MAX_STORED_POINTS = 4000


class ParseError(Exception):
    """Raised when an activity file cannot be parsed at all."""


@dataclass
class TrackPoint:
    lat: float | None = None
    lon: float | None = None
    elevation: float | None = None
    time: datetime | None = None  # local naive
    hr: int | None = None
    distance_m: float = 0.0


@dataclass
class RawTrack:
    points: list[TrackPoint]
    title: str = "Тренування"
    fallback_distance_m: float | None = None
    fallback_duration_sec: int | None = None
    fallback_avg_hr: int | None = None
    fallback_max_hr: int | None = None
    fallback_ascent_m: float | None = None


# --------------------------------------------------------------------------- #
#  small helpers
# --------------------------------------------------------------------------- #
def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _to_local(dt: datetime | None) -> datetime | None:
    """Normalize a timestamp to naive local time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().replace(tzinfo=None)


def _assign_distances(points: list[TrackPoint]) -> None:
    """Fill ``distance_m`` with the cumulative 2D distance along the track."""
    total = 0.0
    prev: TrackPoint | None = None
    for point in points:
        if prev is not None and None not in (prev.lat, prev.lon, point.lat, point.lon):
            total += _haversine_m(prev.lat, prev.lon, point.lat, point.lon)  # type: ignore[arg-type]
        point.distance_m = total
        prev = point


def _hr_from_gpx_extensions(trackpoint: Any) -> int | None:
    """Extract ``gpxtpx:hr`` (or similar) from a gpxpy track point extension."""
    extensions = getattr(trackpoint, "extensions", None) or []
    for extension in extensions:
        try:
            elements = extension.iter()
        except AttributeError:  # plain string extension
            continue
        for element in elements:
            tag = str(element.tag).split("}")[-1].lower()
            if tag in {"hr", "heartrate", "heartratebpm", "heart_rate"}:
                text = (element.text or "").strip()
                if not text:
                    continue
                try:
                    return int(round(float(text)))
                except ValueError:
                    continue
    return None


def _elevation_gain(points: list[TrackPoint]) -> float:
    gain = 0.0
    prev: float | None = None
    for point in points:
        if point.elevation is None:
            continue
        if prev is not None and point.elevation > prev:
            gain += point.elevation - prev
        prev = point.elevation
    return round(gain, 1)


def _hr_stats(points: list[TrackPoint]) -> tuple[int | None, int | None]:
    values = [p.hr for p in points if p.hr]
    if not values:
        return None, None
    return int(round(sum(values) / len(values))), int(max(values))


def _duration_sec(points: list[TrackPoint]) -> int | None:
    timed = [p for p in points if p.time is not None]
    if len(timed) < 2:
        return None
    return max(0, int((timed[-1].time - timed[0].time).total_seconds()))  # type: ignore[operator]


# --------------------------------------------------------------------------- #
#  GPX
# --------------------------------------------------------------------------- #
def _parse_gpx(path: Path, fallback_title: str | None = None) -> RawTrack:
    import gpxpy

    try:
        with path.open("rb") as handle:
            gpx = gpxpy.parse(handle)
    except Exception as exc:  # noqa: BLE001 - surfaced as ParseError
        raise ParseError(f"Не вдалося прочитати GPX-файл: {exc}") from exc

    points: list[TrackPoint] = []
    title = ""

    def add_point(tp: Any) -> None:
        elevation = tp.elevation
        if elevation is not None and math.isnan(elevation):
            elevation = None
        points.append(
            TrackPoint(
                lat=float(tp.latitude) if tp.latitude is not None else None,
                lon=float(tp.longitude) if tp.longitude is not None else None,
                elevation=float(elevation) if elevation is not None else None,
                time=_to_local(tp.time),
                hr=_hr_from_gpx_extensions(tp),
            )
        )

    for track in gpx.tracks:
        if not title and track.name:
            title = track.name.strip()
        for segment in track.segments:
            for tp in segment.points:
                add_point(tp)

    if not points:  # some devices export routes instead of tracks
        for route in gpx.routes:
            if not title and route.name:
                title = route.name.strip()
            for tp in route.points:
                add_point(tp)

    if not points:
        raise ParseError("У GPX-файлі не знайдено точок треку — файл порожній?")

    _assign_distances(points)
    if not title:
        title = (fallback_title or path.stem).replace("_", " ").strip() or "Тренування"
    return RawTrack(points=points, title=title)


# --------------------------------------------------------------------------- #
#  FIT
# --------------------------------------------------------------------------- #
def _fit_semicircles_to_degrees(value: float) -> float:
    """FIT stores lat/lon as sint32 semicircles; tolerate already-converted deg."""
    if abs(value) > 180:
        return value * 180.0 / 2**31
    return value


def _fit_get(frame: Any, *names: str) -> Any:
    for name in names:
        try:
            value = frame.get_value(name)
        except (KeyError, ValueError):
            continue
        if value is not None:
            return value
    return None


def _fit_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _to_local(value)
    if isinstance(value, (int, float)):
        return _to_local(datetime.fromtimestamp(float(value), tz=timezone.utc))
    return None


def _parse_fit(path: Path, fallback_title: str | None = None) -> RawTrack:
    import fitdecode

    points: list[TrackPoint] = []
    session: dict[str, Any] = {}
    sport = ""

    try:
        with fitdecode.FitReader(str(path)) as fit:
            for frame in fit:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                if frame.name == "record":
                    lat = _fit_get(frame, "position_lat")
                    lon = _fit_get(frame, "position_long")
                    altitude = _fit_get(frame, "enhanced_altitude", "altitude")
                    hr = _fit_get(frame, "heart_rate")
                    points.append(
                        TrackPoint(
                            lat=_fit_semicircles_to_degrees(float(lat)) if lat is not None else None,
                            lon=_fit_semicircles_to_degrees(float(lon)) if lon is not None else None,
                            elevation=float(altitude) if altitude is not None else None,
                            time=_fit_timestamp(_fit_get(frame, "timestamp")),
                            hr=int(hr) if hr is not None else None,
                        )
                    )
                elif frame.name in {"session", "sport", "activity"}:
                    if frame.name == "session":
                        session = {
                            "distance_m": _fit_get(frame, "total_distance"),
                            "duration_sec": _fit_get(frame, "total_elapsed_time", "total_timer_time"),
                            "avg_hr": _fit_get(frame, "avg_heart_rate"),
                            "max_hr": _fit_get(frame, "max_heart_rate"),
                            "ascent_m": _fit_get(frame, "total_ascent"),
                            "sport": _fit_get(frame, "sport"),
                        }
                    elif frame.name == "sport":
                        sport = str(_fit_get(frame, "name") or "")
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as ParseError
        raise ParseError(f"Не вдалося прочитати FIT-файл: {exc}") from exc

    if not points:
        raise ParseError("У FIT-файлі не знайдено записів треку (record)")

    _assign_distances(points)

    title = (fallback_title or path.stem).replace("_", " ").strip() or "Тренування"
    if sport and sport.lower() not in {"running", "generic"}:
        title = f"{title} · {sport}"
    elif not title or title == "Тренування":
        title = "Тренування"

    return RawTrack(
        points=points,
        title=title,
        fallback_distance_m=float(session["distance_m"]) if session.get("distance_m") else None,
        fallback_duration_sec=int(session["duration_sec"]) if session.get("duration_sec") else None,
        fallback_avg_hr=int(session["avg_hr"]) if session.get("avg_hr") else None,
        fallback_max_hr=int(session["max_hr"]) if session.get("max_hr") else None,
        fallback_ascent_m=float(session["ascent_m"]) if session.get("ascent_m") else None,
    )


# --------------------------------------------------------------------------- #
#  splits & metrics
# --------------------------------------------------------------------------- #
def _compute_splits(points: list[TrackPoint]) -> list[dict[str, Any]]:
    """1-km splits with interpolated crossing times and per-split heart rate."""
    timed = [p for p in points if p.time is not None and p.distance_m is not None]
    if len(timed) < 2:
        return []

    start_time = timed[0].time
    total_distance = points[-1].distance_m
    if total_distance < 100:  # too short for meaningful splits
        return []

    # (distance_m, elapsed_sec, hr, elevation) samples
    samples: list[tuple[float, float, int | None, float | None]] = []
    for point in timed:
        elapsed = (point.time - start_time).total_seconds()  # type: ignore[operator]
        samples.append((point.distance_m, elapsed, point.hr, point.elevation))

    splits: list[dict[str, Any]] = []

    def elapsed_at(distance_m: float) -> float:
        """Linear interpolation of elapsed seconds at a given distance."""
        previous = samples[0]
        for sample in samples:
            if sample[0] >= distance_m:
                if sample[0] == previous[0]:
                    return sample[1]
                ratio = (distance_m - previous[0]) / (sample[0] - previous[0])
                return previous[1] + ratio * (sample[1] - previous[1])
            previous = sample
        return samples[-1][1]

    def segment_stats(lo: float, hi: float) -> tuple[int | None, float, int | None]:
        """Average/max HR and elevation gain for the (lo, hi] distance window."""
        hrs = [s[2] for s in samples if lo < s[0] <= hi and s[2]]
        avg_hr = int(round(sum(hrs) / len(hrs))) if hrs else None
        gain = 0.0
        prev_ele: float | None = None
        for sample in samples:
            if not lo < sample[0] <= hi or sample[3] is None:
                continue
            if prev_ele is not None and sample[3] > prev_ele:
                gain += sample[3] - prev_ele
            prev_ele = sample[3]
        return avg_hr, round(gain, 1), len(hrs)

    previous_boundary = 0.0
    previous_time = 0.0
    boundary = 1000.0
    km = 1
    while boundary <= total_distance + 1:
        split_time = elapsed_at(boundary)
        avg_hr, gain, _ = segment_stats(previous_boundary, boundary)
        splits.append(
            {
                "km": km,
                "distance_km": 1.0,
                "duration_sec": int(round(split_time - previous_time)),
                "pace_sec": round((split_time - previous_time) / 1.0, 1),
                "avg_hr": avg_hr,
                "elevation_m": gain,
                "partial": False,
            }
        )
        previous_boundary, previous_time = boundary, split_time
        boundary += 1000.0
        km += 1

    remaining = total_distance - previous_boundary
    if remaining > 50:  # keep the final partial kilometre
        split_time = samples[-1][1]
        avg_hr, gain, _ = segment_stats(previous_boundary, total_distance + 1)
        duration = max(0.0, split_time - previous_time)
        splits.append(
            {
                "km": km,
                "distance_km": round(remaining / 1000, 2),
                "duration_sec": int(round(duration)),
                "pace_sec": round(duration / (remaining / 1000), 1),
                "avg_hr": avg_hr,
                "elevation_m": gain,
                "partial": True,
            }
        )
    return splits


def _downsample(points: list[TrackPoint], limit: int = MAX_STORED_POINTS) -> list[TrackPoint]:
    if len(points) <= limit:
        return points
    step = math.ceil(len(points) / limit)
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled


def _serialize_points(points: list[TrackPoint]) -> list[dict[str, Any]]:
    serialized = []
    for point in _downsample(points):
        serialized.append(
            {
                "lat": round(point.lat, 6) if point.lat is not None else None,
                "lon": round(point.lon, 6) if point.lon is not None else None,
                "ele": round(point.elevation, 1) if point.elevation is not None else None,
                "t": point.time.isoformat() if point.time else None,
                "hr": point.hr,
            }
        )
    return serialized


def build_activity(raw: RawTrack) -> dict[str, Any]:
    """Turn a :class:`RawTrack` into the normalized activity payload."""
    points = raw.points

    distance_m = points[-1].distance_m if points else 0.0
    if distance_m < 1 and raw.fallback_distance_m:
        distance_m = raw.fallback_distance_m

    duration = _duration_sec(points)
    if duration is None:
        duration = raw.fallback_duration_sec or 0

    avg_hr, max_hr = _hr_stats(points)
    if not points or avg_hr is None:
        avg_hr = raw.fallback_avg_hr
        max_hr = raw.fallback_max_hr

    ascent = _elevation_gain(points)
    if ascent <= 0 and raw.fallback_ascent_m:
        ascent = round(raw.fallback_ascent_m, 1)

    distance_km = round(distance_m / 1000, 3)
    avg_pace = round(duration / distance_km, 1) if distance_km > 0 else 0.0

    timed = [p for p in points if p.time is not None]
    activity_date = timed[0].time if timed else datetime.now()

    splits = _compute_splits(points)

    return {
        "title": raw.title or "Тренування",
        "date": activity_date,
        "distance_km": distance_km,
        "duration_sec": int(duration),
        "avg_pace_sec": avg_pace,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "elevation_gain": ascent,
        "points": _serialize_points(points),
        "splits": splits,
    }


# --------------------------------------------------------------------------- #
#  public entry point
# --------------------------------------------------------------------------- #
SUPPORTED_EXTENSIONS = {".gpx", ".fit"}


def parse_activity_file(path: str | Path, filename_hint: str | None = None) -> dict[str, Any]:
    """Parse a ``.gpx``/``.fit`` file into a normalized activity payload.

    ``filename_hint`` is the original (human readable) file name shown to the
    user; it is used as a fallback title when the file has no own name.
    """
    path = Path(path)
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ParseError(f"Непідтримуваний формат «{extension or 'без розширення'}». Дозволено .gpx та .fit")

    fallback_title = Path(filename_hint).stem if filename_hint else path.stem
    raw = _parse_gpx(path, fallback_title) if extension == ".gpx" else _parse_fit(path, fallback_title)
    payload = build_activity(raw)
    if payload["distance_km"] <= 0 and payload["duration_sec"] <= 0:
        raise ParseError("Не вдалося визначити дистанцію чи тривалість тренування")
    return payload
