"""Strava-Premium-style analytics built on top of stored activities.

Everything here is pure Python over the ORM objects so it is easy to unit-test
and does not require additional database tables (except goals in ``app_settings``).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Activity

MONTH_NAMES_UK = [
    "Січень",
    "Лютий",
    "Березень",
    "Квітень",
    "Травень",
    "Червень",
    "Липень",
    "Серпень",
    "Вересень",
    "Жовтень",
    "Листопад",
    "Грудень",
]
WEEKDAY_NAMES_UK = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

#: (код, назва, нижня межа, верхня межа) у відсотках від максимального пульсу
HR_ZONES = [
    ("Z1", "Відновлення", 0.50, 0.60),
    ("Z2", "Аеробна", 0.60, 0.70),
    ("Z3", "Темпова", 0.70, 0.80),
    ("Z4", "Порогова", 0.80, 0.90),
    ("Z5", "Максимальна", 0.90, 1.01),
]

DEFAULT_GOALS = {
    "goal_month_distance_km": 100.0,
    "goal_month_elevation_m": 1000.0,
    "goal_month_runs": 12.0,
}

DEFAULT_MAX_HR = 190
FITNESS_DAYS = 42


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _all_activities(db: Session) -> list[Activity]:
    return list(db.execute(select(Activity).order_by(Activity.date.asc())).scalars().all())


def _day(value: datetime) -> date:
    return value.date()


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _month_label(year: int, month: int) -> str:
    return f"{MONTH_NAMES_UK[month - 1]} {year}"


def _parse_point_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
#  streak
# --------------------------------------------------------------------------- #
def compute_streak(db: Session, today: date | None = None) -> dict:
    """Consecutive weeks (Mon–Sun) with at least one run + the current week grid."""
    today = today or date.today()
    activities = _all_activities(db)
    weeks_with_runs = {week_start(_day(activity.date)) for activity in activities}
    current_week = week_start(today)
    current_week_has_runs = current_week in weeks_with_runs

    streak = 0
    probe = current_week if current_week_has_runs else current_week - timedelta(days=7)
    while probe in weeks_with_runs:
        streak += 1
        probe -= timedelta(days=7)

    by_day: dict[date, list[Activity]] = {}
    for activity in activities:
        by_day.setdefault(_day(activity.date), []).append(activity)

    days = []
    for offset in range(7):
        day = current_week + timedelta(days=offset)
        day_activities = by_day.get(day, [])
        days.append(
            {
                "date": day.isoformat(),
                "label": WEEKDAY_NAMES_UK[day.weekday()],
                "day_number": day.day,
                "has_run": bool(day_activities),
                "distance_km": round(sum(a.distance_km for a in day_activities), 2),
                "is_today": day == today,
                "is_future": day > today,
            }
        )

    return {
        "weeks": streak,
        "current_week_has_runs": current_week_has_runs,
        "at_risk": streak > 0 and not current_week_has_runs,
        "days": days,
        "week_distance_km": round(sum(d["distance_km"] for d in days), 1),
    }


# --------------------------------------------------------------------------- #
#  monthly challenges + monthly report
# --------------------------------------------------------------------------- #
def compute_challenges(db: Session, goals: dict[str, float], today: date | None = None) -> list[dict]:
    """Progress for the current month's distance / elevation / count goals."""
    today = today or date.today()
    start, end = month_bounds(today.year, today.month)
    activities = [a for a in _all_activities(db) if start <= _day(a.date) < end]

    distance_km = sum(a.distance_km for a in activities)
    elevation_m = sum(a.elevation_gain for a in activities)
    runs = len(activities)

    specs = [
        ("distance", "Дистанція за місяць", "км", distance_km, goals.get("goal_month_distance_km", 100.0)),
        ("elevation", "Набір висоти за місяць", "м", elevation_m, goals.get("goal_month_elevation_m", 1000.0)),
        ("runs", "Кількість тренувань", "", runs, goals.get("goal_month_runs", 12.0)),
    ]
    challenges = []
    for key, label, unit, value, goal in specs:
        goal = float(goal or 0)
        percent = min(100.0, round(value / goal * 100, 1)) if goal > 0 else 0.0
        challenges.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "value": round(float(value), 1) if unit else int(value),
                "goal": round(goal, 1) if unit else int(goal),
                "percent": percent,
                "done": value >= goal > 0,
                "remaining": round(max(0.0, goal - float(value)), 1) if unit else int(max(0, goal - value)),
            }
        )
    return challenges


def _summary(activities: list[Activity]) -> dict:
    distance = sum(a.distance_km for a in activities)
    duration = sum(a.duration_sec for a in activities)
    elevation = sum(a.elevation_gain for a in activities)
    return {
        "distance_km": round(distance, 1),
        "count": len(activities),
        "duration_sec": duration,
        "elevation_m": round(elevation),
        "avg_pace_sec": round(duration / distance, 1) if distance > 0.01 else 0.0,
    }


def monthly_report(db: Session, year: int, month: int) -> dict:
    """Totals for a month plus percent comparison with the previous month."""
    start, end = month_bounds(year, month)
    previous_year, previous_month = _previous_month(year, month)
    previous_start, previous_end = month_bounds(previous_year, previous_month)

    activities = _all_activities(db)
    current = [a for a in activities if start <= _day(a.date) < end]
    previous = [a for a in activities if previous_start <= _day(a.date) < previous_end]

    current_summary = _summary(current)
    previous_summary = _summary(previous)

    def percent_change(current_value: float, previous_value: float) -> float | None:
        if previous_value == 0:
            return None
        return round((current_value - previous_value) / previous_value * 100, 1)

    delta = {
        "distance": percent_change(current_summary["distance_km"], previous_summary["distance_km"]),
        "count": percent_change(current_summary["count"], previous_summary["count"]),
        "duration": percent_change(current_summary["duration_sec"], previous_summary["duration_sec"]),
        "elevation": percent_change(current_summary["elevation_m"], previous_summary["elevation_m"]),
        "pace": (
            percent_change(current_summary["avg_pace_sec"], previous_summary["avg_pace_sec"])
            if previous_summary["avg_pace_sec"] and current_summary["avg_pace_sec"]
            else None
        ),
    }

    return {
        "year": year,
        "month": month,
        "label": _month_label(year, month),
        "previous_label": _month_label(previous_year, previous_month),
        "current": current_summary,
        "previous": previous_summary,
        "delta": delta,
        "has_data": current_summary["count"] > 0,
        "has_previous": previous_summary["count"] > 0,
    }


def available_report_months(db: Session, limit: int = 24) -> list[dict]:
    """Months that have activities (plus the current month), newest first."""
    today = date.today()
    months: set[tuple[int, int]] = {(today.year, today.month)}
    for activity in _all_activities(db):
        day = _day(activity.date)
        months.add((day.year, day.month))
    ordered = sorted(months, reverse=True)[:limit]
    return [{"value": f"{year:04d}-{month:02d}", "label": _month_label(year, month)} for year, month in ordered]


# --------------------------------------------------------------------------- #
#  heart-rate zones
# --------------------------------------------------------------------------- #
def hr_zone_analysis(db: Session) -> dict:
    """Time spent in Z1–Z5 based on the highest observed max HR."""
    activities = _all_activities(db)
    observed_max_hr = max((a.max_hr or 0 for a in activities), default=0)
    reference_max_hr = observed_max_hr or DEFAULT_MAX_HR
    # нижні межі зон у абсолютних ударах/хв: 50%, 60%, 70%, 80%, 90% від max HR
    lower_bounds = [reference_max_hr * zone[2] for zone in HR_ZONES]

    zone_seconds = [0.0] * len(HR_ZONES)
    for activity in activities:
        points = [p for p in activity.points if p.get("hr") and p.get("t")]
        for previous, current in zip(points, points[1:]):
            start_time = _parse_point_time(previous.get("t"))
            end_time = _parse_point_time(current.get("t"))
            if start_time is None or end_time is None:
                continue
            delta = (end_time - start_time).total_seconds()
            if delta <= 0 or delta > 120:
                continue
            hr = current["hr"]
            index = 0
            for position, lower in enumerate(lower_bounds):
                if hr >= lower:
                    index = position
            zone_seconds[index] += delta

    total_seconds = sum(zone_seconds)
    zones = []
    for position, (code, name, lower, upper) in enumerate(HR_ZONES):
        low_hr = int(reference_max_hr * lower)
        high_hr = int(reference_max_hr * upper)
        zones.append(
            {
                "code": code,
                "name": name,
                "range": f"{low_hr}–{high_hr}",
                "seconds": int(zone_seconds[position]),
                "percent": round(zone_seconds[position] / total_seconds * 100, 1) if total_seconds else 0.0,
            }
        )
    return {"max_hr": reference_max_hr, "total_seconds": int(total_seconds), "zones": zones}


# --------------------------------------------------------------------------- #
#  training load, fitness & fatigue, ACWR
# --------------------------------------------------------------------------- #
def _daily_loads(db: Session) -> dict[date, float]:
    """Simple TRIMP-like load: minutes × (avg HR / 140), min 60 bpm safety."""
    loads: dict[date, float] = {}
    for activity in _all_activities(db):
        minutes = (activity.duration_sec or 0) / 60
        if minutes <= 0:
            continue
        intensity = max(0.5, (activity.avg_hr or 140) / 140.0)
        loads[_day(activity.date)] = loads.get(_day(activity.date), 0.0) + minutes * intensity
    return loads


def _ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (span + 1)
    result = values[0]
    for value in values[1:]:
        result += alpha * (value - result)
    return result


def fitness_fatigue(db: Session, days: int = FITNESS_DAYS, today: date | None = None) -> dict:
    """CTL (42d) / ATL (7d) / TSB series for the last ``days`` days."""
    today = today or date.today()
    loads = _daily_loads(db)
    if not loads:
        return {"series": [], "ctl": 0.0, "atl": 0.0, "tsb": 0.0, "days": days}

    first_day = min(loads)
    ctl = 0.0
    atl = 0.0
    series: list[dict] = []
    window_start = today - timedelta(days=days - 1)
    day = first_day
    while day <= today:
        load = loads.get(day, 0.0)
        ctl += (load - ctl) / 42.0
        atl += (load - atl) / 7.0
        if day >= window_start:
            series.append(
                {
                    "date": day.isoformat(),
                    "label": day.strftime("%d.%m"),
                    "load": round(load, 1),
                    "ctl": round(ctl, 1),
                    "atl": round(atl, 1),
                    "tsb": round(ctl - atl, 1),
                }
            )
        day += timedelta(days=1)

    return {"series": series, "ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(ctl - atl, 1), "days": days}


def acwr(db: Session, today: date | None = None) -> dict:
    """Acute:chronic workload ratio (7 days vs 28-day weekly average)."""
    today = today or date.today()
    loads = _daily_loads(db)

    def total(start: date, end: date) -> float:
        return sum(value for day, value in loads.items() if start <= day <= end)

    acute = total(today - timedelta(days=6), today)
    chronic = total(today - timedelta(days=27), today)
    chronic_weekly = chronic / 4.0

    history_days = (today - min(loads)).days + 1 if loads else 0

    if chronic_weekly <= 0.01:
        ratio = None
        status = "Немає даних"
        advice = "Додай перші тренування цього тижня — і з'явиться оцінка навантаження."
        tone = "muted"
    elif history_days < 14:
        # ACWR статистично ненадійний, поки немає щонайменше двох тижнів історії
        ratio = round(acute / chronic_weekly, 2)
        status = "Мало даних"
        advice = "Історії тренувань менше двох тижнів — ACWR ще ненадійний. Поступово збільшуй обсяг і повернись до оцінки пізніше."
        tone = "muted"
    else:
        ratio = round(acute / chronic_weekly, 2)
        if ratio < 0.8:
            status = "Свіжий"
            advice = "Навантаження нижче звичного. Гарний момент для легкої відновлювальної пробіжки або спокійного темпу без ризику."
            tone = "fresh"
        elif ratio <= 1.3:
            status = "Оптимально"
            advice = "Ти в безпечній зоні. Тримай поточний обсяг: одна легка, одна темпова/інтервальна та довга пробіжка на тиждень."
            tone = "optimal"
        else:
            status = "Висока втома"
            advice = "Різке зростання навантаження. Зроби 1–2 дні відпочинку або дуже легку пробіжку, далі повертайся до обсягу поступово."
            tone = "high"

    return {
        "ratio": ratio,
        "acute": round(acute, 1),
        "chronic_weekly": round(chronic_weekly, 1),
        "status": status,
        "advice": advice,
        "tone": tone,
    }


# --------------------------------------------------------------------------- #
#  personal records
# --------------------------------------------------------------------------- #
PR_DISTANCES_KM = [1.0, 5.0, 10.0, 21.1, 42.2]


def _point_samples(activity: Activity) -> list[tuple[float, float]]:
    """(elapsed_seconds, cumulative_metres) from GPS points with timestamps."""
    points = []
    for point in activity.points:
        lat, lon = point.get("lat"), point.get("lon")
        when = _parse_point_time(point.get("t"))
        if lat is None or lon is None or when is None:
            continue
        points.append((lat, lon, when))
    if len(points) < 2:
        return []

    samples: list[tuple[float, float]] = []
    start_time = points[0][2]
    distance = 0.0
    previous = points[0]
    for current in points[1:]:
        distance += _haversine_m(previous[0], previous[1], current[0], current[1])
        elapsed = (current[2] - start_time).total_seconds()
        if samples and elapsed <= samples[-1][0]:
            elapsed = samples[-1][0] + 0.1
        samples.append((elapsed, distance))
        previous = current
    return samples


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _best_time_for_distance(samples: list[tuple[float, float]], target_m: float) -> float | None:
    """Fastest time to cover ``target_m`` metres (sliding window with interpolation)."""
    if not samples or samples[-1][1] < target_m:
        return None
    best: float | None = None
    count = len(samples)
    right = 0
    for left in range(count):
        if right < left:
            right = left
        start_elapsed, start_distance = samples[left]
        while right < count and samples[right][1] - start_distance < target_m:
            right += 1
        if right >= count:
            break
        previous_elapsed, previous_distance = samples[right - 1] if right - 1 >= left else samples[left]
        elapsed, distance = samples[right]
        span = distance - previous_distance
        if span <= 0:
            continue
        ratio = (start_distance + target_m - previous_distance) / span
        end_elapsed = previous_elapsed + ratio * (elapsed - previous_elapsed)
        candidate = end_elapsed - start_elapsed
        if candidate > 0 and (best is None or candidate < best):
            best = candidate
    return best


def personal_records(db: Session) -> list[dict]:
    """Best times for classic distances across all uploaded tracks."""
    activities = _all_activities(db)
    prepared = [(activity, _point_samples(activity)) for activity in activities]
    records: list[dict] = []
    for distance_km in PR_DISTANCES_KM:
        best: dict | None = None
        for activity, samples in prepared:
            if not samples:
                continue
            seconds = _best_time_for_distance(samples, distance_km * 1000)
            if seconds is None:
                continue
            if best is None or seconds < best["time_sec"]:
                best = {
                    "distance_km": distance_km,
                    "time_sec": int(round(seconds)),
                    "pace_sec": round(seconds / distance_km, 1),
                    "activity_id": activity.id,
                    "title": activity.title,
                    "date": activity.date,
                }
        records.append(best or {"distance_km": distance_km, "time_sec": None})
    return records


# --------------------------------------------------------------------------- #
#  heatmap data
# --------------------------------------------------------------------------- #
def heatmap_tracks(db: Session, year: int | None = None, month: int | None = None, max_points: int = 300) -> list[dict]:
    """Simplified GPS polylines for all activities matching the filter."""
    tracks = []
    for activity in _all_activities(db):
        if year and activity.date.year != year:
            continue
        if month and activity.date.month != month:
            continue
        points = [
            [round(p["lat"], 6), round(p["lon"], 6)]
            for p in activity.points
            if p.get("lat") is not None and p.get("lon") is not None
        ]
        if len(points) < 2:
            continue
        step = max(1, math.ceil(len(points) / max_points))
        simplified = points[::step]
        if simplified[-1] != points[-1]:
            simplified.append(points[-1])
        tracks.append(
            {
                "id": activity.id,
                "title": activity.title,
                "date": activity.date.strftime("%d.%m.%Y"),
                "year": activity.date.year,
                "month": activity.date.month,
                "distance_km": round(activity.distance_km, 2),
                "points": simplified,
            }
        )
    return tracks


def heatmap_years(db: Session) -> list[int]:
    years = {activity.date.year for activity in _all_activities(db)}
    if not years:
        years.add(date.today().year)
    return sorted(years, reverse=True)
