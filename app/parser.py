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

#: Версія алгоритмів очищення GPS/висот. Записується в кожне нове тренування,
#: щоб було видно, яким парсером оброблено трек (і чи потрібна міграція).
PARSER_VERSION = 2

#: Cap on how many track points are persisted (keeps the JSON column light).
MAX_STORED_POINTS = 4000

#: Максимальна фізично можлива швидкість бігу (м/с ≈ 28.8 км/год). Сегменти
#: швидші за це — GPS-«телепорти» (мультипат у місті), які інакше завищують
#: дистанцію, «найкращі відрізки», GAP та прогноз VDOT.
MAX_POINT_SPEED_MS = 8.0

#: Поріг паузи: повільніше за це вважається стоянням (GPS-дрейф) і не входить
#: ні в дистанцію, ні в час руху.
MIN_MOVING_SPEED_MS = 0.5

#: Максимальне фізично можливе прискорення між сусідніми точками (м/с²).
#: Різкіші стрибки швидкості — артефакт GPS і відкидаються.
MAX_POINT_ACCEL_MS2 = 3.0

#: Вікно (непарне) медіанного + згладжуючого фільтра для масиву висот.
ELEVATION_SMOOTH_WINDOW = 5

#: Енерговитрати рівнинного бігу за Мінетті (J·кг⁻¹·м⁻¹), Cr(0).
MINETTI_FLAT_COST = 3.6
#: Практичне обмеження ухилу ±15 %, щоб GPS-піки висоти не спотворювали GAP.
MAX_GRADE = 0.15


def grade_adjusted_factor(grade: float) -> float:
    """Множник «еквівалентної рівнинної» дистанції для сегмента з ухилом.

    Повертає ``Cr(grade) / Cr(0)`` за поліномом Мінетті (2002): ``<1`` на спуску,
    ``>1`` на підйомі. Ухил обмежується ±15 %, щоб уникнути нереалістичних значень
    від GPS-шумів висоти.
    """
    i = max(-MAX_GRADE, min(MAX_GRADE, grade))
    cost = (
        155.4 * i**5
        - 30.4 * i**4
        - 43.3 * i**3
        + 46.3 * i**2
        + 19.5 * i
        + MINETTI_FLAT_COST
    )
    cost = max(cost, 0.6)  # запобіжник для дуже стрімких спусків
    return cost / MINETTI_FLAT_COST


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


def is_implausible_segment(
    distance_m: float,
    delta_sec: float | None,
    previous_speed_ms: float | None = None,
) -> bool:
    """True, якщо сегмент фізично неможливий для бігу (стрибок GPS).

    Відсікаються як «телепорти» (швидкість > :data:`MAX_POINT_SPEED_MS`), так і
    різкі стрибки швидкості між сусідніми точками (> :data:`MAX_POINT_ACCEL_MS2`).
    Без відомого часового кроку сегмент вважається прийнятним.
    """
    if delta_sec is None or delta_sec <= 0:
        return False
    speed = distance_m / delta_sec
    if speed > MAX_POINT_SPEED_MS:
        return True
    if previous_speed_ms is not None and abs(speed - previous_speed_ms) / delta_sec > MAX_POINT_ACCEL_MS2:
        return True
    return False


def is_pause_segment(distance_m: float, delta_sec: float | None) -> bool:
    """True, якщо сегмент — пауза/стояння (швидкість < :data:`MIN_MOVING_SPEED_MS`).

    Такі сегменти не додаються ні в дистанцію, ні в час руху.
    """
    return delta_sec is not None and delta_sec > 0 and distance_m / delta_sec < MIN_MOVING_SPEED_MS


def smooth_elevations(values: list[float | None], window: int = ELEVATION_SMOOTH_WINDOW) -> list[float | None]:
    """Медіанний фільтр + ковзне середнє для масиву висот (прибирає GPS-піки).

    ``None``-значення лишаються ``None``; фільтр працює лише по сусідніх відомих
    висотах. Повертається новий список тієї ж довжини.
    """
    if window < 3:
        window = 3
    if window % 2 == 0:
        window += 1
    half = window // 2

    known = [index for index, value in enumerate(values) if value is not None]
    if len(known) < 3:
        return list(values)

    def neighbours(position: int) -> list[float]:
        lo = max(0, position - half)
        hi = min(len(known), position + half + 1)
        return [values[known[step]] for step in range(lo, hi)]  # type: ignore[misc]

    median_pass: list[float | None] = list(values)
    for position, index in enumerate(known):
        chunk = sorted(neighbours(position))
        median_pass[index] = chunk[len(chunk) // 2]

    smoothed: list[float | None] = list(median_pass)
    for position, index in enumerate(known):
        lo = max(0, position - half)
        hi = min(len(known), position + half + 1)
        chunk = [median_pass[known[step]] for step in range(lo, hi)]
        smoothed[index] = sum(chunk) / len(chunk)  # type: ignore[arg-type]
    return smoothed


def _to_local(dt: datetime | None) -> datetime | None:
    """Normalize a timestamp to naive local time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().replace(tzinfo=None)


def _parse_iso_time(raw: Any) -> datetime | None:
    """Parse a stored ISO timestamp (naive local time) into a ``datetime``."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _assign_distances(points: list[TrackPoint]) -> None:
    """Fill ``distance_m`` with the cumulative 2D distance along the track.

    Паузи (швидкість < ``MIN_MOVING_SPEED_MS``) та GPS-«телепорти»/стрибки
    прискорення не додаються в дистанцію.
    """
    total = 0.0
    prev: TrackPoint | None = None
    previous_speed: float | None = None
    for point in points:
        if prev is not None and None not in (prev.lat, prev.lon, point.lat, point.lon):
            segment = _haversine_m(prev.lat, prev.lon, point.lat, point.lon)  # type: ignore[arg-type]
            delta = (point.time - prev.time).total_seconds() if prev.time and point.time else None
            if delta is None or delta <= 0:
                total += segment  # без часу не можемо відрізнити паузу — лишаємо дистанцію
            elif is_pause_segment(segment, delta):
                pass  # стояння: ані дистанції, ані швидкості
            elif is_implausible_segment(segment, delta, previous_speed):
                pass  # GPS-стрибок: пропускаємо сегмент
            else:
                total += segment
                previous_speed = segment / delta
        point.distance_m = total
        prev = point


def _raw_distance_m(points: list[TrackPoint]) -> float:
    """Дистанція треку без жодних фільтрів (паузи та GPS-«телепорти» враховані).

    Потрібна лише для порівняння «було -> стало»: зберігається в
    ``distance_km_raw`` поряд з очищеною ``distance_km_clean``.
    """
    total = 0.0
    previous: TrackPoint | None = None
    for point in points:
        if previous is not None and None not in (previous.lat, previous.lon, point.lat, point.lon):
            total += _haversine_m(previous.lat, previous.lon, point.lat, point.lon)  # type: ignore[arg-type]
        previous = point
    return total


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
def _splits_from_samples(
    samples: list[tuple[float, float, int | None, float | None]],
    total_distance: float,
) -> list[dict[str, Any]]:
    """1-km splits from ``(distance_m, elapsed_sec, hr, elevation)`` samples.

    Crossing times are linearly interpolated; each split reports pace, average
    HR and the elevation gain/loss inside its distance window.
    """
    if len(samples) < 2 or total_distance < 100:  # too short for meaningful splits
        return []

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

    elevation_profile = [(s[0], s[3]) for s in samples if s[3] is not None]

    def elevation_at(distance_m: float) -> float | None:
        """Interpolated elevation at any distance along the track."""
        if not elevation_profile:
            return None
        previous = elevation_profile[0]
        for current in elevation_profile:
            if current[0] >= distance_m:
                if current[0] == previous[0]:
                    return current[1]
                ratio = (distance_m - previous[0]) / (current[0] - previous[0])
                return previous[1] + ratio * (current[1] - previous[1])
            previous = current
        return elevation_profile[-1][1]

    def segment_stats(lo: float, hi: float) -> tuple[int | None, float, float]:
        """Average HR and elevation gain/loss for the ``(lo, hi]`` window.

        Boundary elevations are interpolated so gain/loss stays correct even for
        sparsely sampled tracks (points landing exactly on kilometre marks).
        """
        hrs = [s[2] for s in samples if lo < s[0] <= hi and s[2]]
        avg_hr = int(round(sum(hrs) / len(hrs))) if hrs else None

        profile: list[float] = []
        start_ele = elevation_at(lo)
        if start_ele is not None:
            profile.append(start_ele)
        profile.extend(s[3] for s in samples if lo < s[0] < hi and s[3] is not None)
        end_ele = elevation_at(hi)
        if end_ele is not None:
            profile.append(end_ele)

        gain = 0.0
        loss = 0.0
        for previous, current in zip(profile, profile[1:]):
            if current > previous:
                gain += current - previous
            else:
                loss += previous - current
        return avg_hr, round(gain, 1), round(loss, 1)

    def segment_gap(lo: float, hi: float) -> float | None:
        """GAP (хв/км у секундах) для вікна ``(lo, hi]`` або ``None`` без висот.

        Кожен під-сегмент зважується на енерговитрату Мінетті: час ділиться на
        «еквівалентну рівнинну» дистанцію.
        """
        flat_m = 0.0
        seconds = 0.0
        has_elevation = False
        for (d1, t1, _, e1), (d2, t2, _, e2) in zip(samples, samples[1:]):
            if not lo < (d1 + d2) / 2 <= hi:
                continue
            delta_d, delta_t = d2 - d1, t2 - t1
            if delta_d <= 0 or delta_t <= 0:
                continue
            seconds += delta_t
            if e1 is not None and e2 is not None:
                has_elevation = True
                flat_m += delta_d * grade_adjusted_factor((e2 - e1) / delta_d)
            else:
                flat_m += delta_d
        if not has_elevation or flat_m <= 0 or seconds <= 0:
            return None
        return round(seconds / (flat_m / 1000.0), 1)

    previous_boundary = 0.0
    previous_time = 0.0
    boundary = 1000.0
    km = 1
    while boundary <= total_distance + 1:
        split_time = elapsed_at(boundary)
        avg_hr, gain, loss = segment_stats(previous_boundary, boundary)
        splits.append(
            {
                "km": km,
                "distance_km": 1.0,
                "duration_sec": int(round(split_time - previous_time)),
                "pace_sec": round((split_time - previous_time) / 1.0, 1),
                "gap_sec": segment_gap(previous_boundary, boundary),
                "avg_hr": avg_hr,
                "elevation_m": gain,
                "descent_m": loss,
                "partial": False,
            }
        )
        previous_boundary, previous_time = boundary, split_time
        boundary += 1000.0
        km += 1

    remaining = total_distance - previous_boundary
    if remaining > 50:  # keep the final partial kilometre
        split_time = samples[-1][1]
        avg_hr, gain, loss = segment_stats(previous_boundary, total_distance + 1)
        duration = max(0.0, split_time - previous_time)
        splits.append(
            {
                "km": km,
                "distance_km": round(remaining / 1000, 2),
                "duration_sec": int(round(duration)),
                "pace_sec": round(duration / (remaining / 1000), 1),
                "gap_sec": segment_gap(previous_boundary, total_distance + 1),
                "avg_hr": avg_hr,
                "elevation_m": gain,
                "descent_m": loss,
                "partial": True,
            }
        )
    return splits


def _gap_pace_from_samples(
    samples: list[tuple[float, float, int | None, float | None]],
) -> float | None:
    """Середній GAP (сек/км) для всього треку або ``None``, якщо немає висот."""
    flat_m = 0.0
    seconds = 0.0
    has_elevation = False
    for (d1, t1, _, e1), (d2, t2, _, e2) in zip(samples, samples[1:]):
        delta_d, delta_t = d2 - d1, t2 - t1
        if delta_d <= 0 or delta_t <= 0:
            continue
        seconds += delta_t
        if e1 is not None and e2 is not None:
            has_elevation = True
            flat_m += delta_d * grade_adjusted_factor((e2 - e1) / delta_d)
        else:
            flat_m += delta_d
    if not has_elevation or flat_m <= 0 or seconds <= 0:
        return None
    return round(seconds / (flat_m / 1000.0), 1)


def _build_samples(
    records: list[tuple[float, float, datetime, int | None, float | None]],
) -> list[tuple[float, float, int | None, float | None]]:
    """Спільне ядро: ``(lat, lon, time, hr, ele)`` → ``(distance_m, moving_sec, hr, ele)``.

    Паузи та GPS-стрибки (швидкість/прискорення) не додаються ні в дистанцію, ні в
    час, тож ``moving_sec`` — це саме час руху без зупинок. Масив висот заздалегідь
    згладжується медіанним + ковзним фільтром, щоб ухил для формули Мінетті (GAP)
    не стрибав на GPS-піках висоти.
    """
    samples: list[tuple[float, float, int | None, float | None]] = []
    smoothed_elevations = smooth_elevations([record[4] for record in records])
    previous: tuple[float, float, datetime] | None = None
    previous_speed: float | None = None
    distance = 0.0
    moving = 0.0

    for index, (lat, lon, when, hr, _) in enumerate(records):
        elevation = smoothed_elevations[index]
        if previous is not None:
            segment = _haversine_m(previous[0], previous[1], lat, lon)
            delta = (when - previous[2]).total_seconds()
            if delta <= 0:
                distance += segment
            elif is_pause_segment(segment, delta):
                pass  # стояння не рухає ні дистанцію, ні час
            elif is_implausible_segment(segment, delta, previous_speed):
                pass  # GPS-стрибок пропускаємо
            else:
                distance += segment
                moving += delta
                previous_speed = segment / delta
        previous = (lat, lon, when)
        samples.append((distance, moving, hr, elevation))

    return samples


def _samples_from_points(points: list[TrackPoint]) -> list[tuple[float, float, int | None, float | None]]:
    """(distance_m, moving_sec, hr, elevation) з щойно розібраних точок треку.

    Дистанція береться з ``point.distance_m`` (уже очищена в :func:`_assign_distances`),
    а час — рухомий, без пауз.
    """
    records = [
        (point.lat, point.lon, point.time, point.hr, point.elevation)  # type: ignore[arg-type]
        for point in points
        if point.time is not None and point.lat is not None and point.lon is not None
    ]
    return _build_samples(records)


def _moving_time_sec(points: list[TrackPoint]) -> float | None:
    """Час руху (без пауз) у секундах або ``None`` без часових міток."""
    samples = _samples_from_points(points)
    if len(samples) < 2:
        return None
    return samples[-1][1]


def _compute_splits(points: list[TrackPoint]) -> list[dict[str, Any]]:
    """1-km splits straight from freshly parsed track points."""
    samples = _samples_from_points(points)
    if not samples:
        return []
    return _splits_from_samples(samples, points[-1].distance_m)


def _compute_gap(points: list[TrackPoint]) -> float | None:
    """Середній GAP тренування з щойно розібраних точок треку."""
    return _gap_pace_from_samples(_samples_from_points(points))


def samples_from_point_dicts(
    points: list[dict[str, Any]],
) -> list[tuple[float, float, int | None, float | None]]:
    """(distance_m, moving_sec, hr, elevation) зі збережених серіалізованих точок.

    Паузи, GPS-«телепорти» та стрибки прискорення не додаються в дистанцію/час,
    тож спліти, GAP та ефективність лишаються фізично коректними навіть для
    «брудних» треків.
    """
    records: list[tuple[float, float, datetime, int | None, float | None]] = []
    for point in points:
        lat, lon = point.get("lat"), point.get("lon")
        when = _parse_iso_time(point.get("t"))
        if lat is None or lon is None or when is None:
            continue
        records.append((lat, lon, when, point.get("hr"), point.get("ele")))
    return _build_samples(records)


def splits_from_point_dicts(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recompute 1-km splits (gain **and** loss) from stored serialized points.

    Used for activities saved before descent was tracked, so the detail page and
    charts stay consistent for already-uploaded tracks.
    """
    samples = samples_from_point_dicts(points)
    if not samples:
        return []
    return _splits_from_samples(samples, samples[-1][0])


def gap_pace_from_point_dicts(points: list[dict[str, Any]]) -> float | None:
    """Середній GAP тренування зі збережених точок (для старих треків теж)."""
    samples = samples_from_point_dicts(points)
    if len(samples) < 2:
        return None
    return _gap_pace_from_samples(samples)


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

    # час руху (без пауз) — з виключенням відрізків зі швидкістю < MIN_MOVING_SPEED_MS
    duration = _moving_time_sec(points)
    if duration is None:
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

    raw_m = _raw_distance_m(points)
    if raw_m <= 0:
        raw_m = distance_m
    distance_km_raw = round(raw_m / 1000, 3)

    timed = [p for p in points if p.time is not None]
    activity_date = timed[0].time if timed else datetime.now()

    splits = _compute_splits(points)

    return {
        "title": raw.title or "Тренування",
        "date": activity_date,
        "distance_km": distance_km,
        "distance_km_raw": distance_km_raw,
        "distance_km_clean": distance_km,
        "duration_sec": int(duration),
        "avg_pace_sec": avg_pace,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "elevation_gain": ascent,
        "points": _serialize_points(points),
        "splits": splits,
        "parser_version": PARSER_VERSION,
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

