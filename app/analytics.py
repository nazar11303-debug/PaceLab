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
from .parser import grade_adjusted_factor, samples_from_point_dicts

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
def _zone_index(hr: int, lower_bounds: list[float]) -> int:
    """Index of the HR zone whose lower bound the sample reaches (0 = lowest)."""
    index = 0
    for position, lower in enumerate(lower_bounds):
        if hr >= lower:
            index = position
    return index


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
        if not points:
            # немає пер-точкового пульсу — зараховуємо всю тривалість за середнім пульсом
            if activity.avg_hr:
                index = _zone_index(activity.avg_hr, lower_bounds)
                zone_seconds[index] += float(activity.duration_sec or 0)
            continue
        for previous, current in zip(points, points[1:]):
            start_time = _parse_point_time(previous.get("t"))
            end_time = _parse_point_time(current.get("t"))
            if start_time is None or end_time is None:
                continue
            delta = (end_time - start_time).total_seconds()
            if delta <= 0 or delta > 120:
                continue
            index = _zone_index(current["hr"], lower_bounds)
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
PR_DISTANCES_KM = [1.0, 3.0, 5.0, 10.0, 21.1]


def _point_samples(activity: Activity) -> list[tuple[float, float]]:
    """(elapsed_seconds, cumulative_metres) для пошуку «найкращих відрізків».

    Використовує спільний розбір точок, який відсіює GPS-«телепорти», тож
    найкращі відрізки та VDOT лишаються фізично реалістичними.
    """
    return [
        (elapsed, distance)
        for distance, elapsed, _, _ in samples_from_point_dicts(activity.points)
    ]


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
#  форма / відновлення (інтерпретація CTL-ATL-TSB)
# --------------------------------------------------------------------------- #
def form_status(fitness: dict) -> dict:
    """Людиночитний статус форми за значенням TSB (Form = CTL − ATL)."""
    tsb = fitness.get("tsb", 0.0) or 0.0
    if tsb >= 20:
        label, tone = "Пік форми", "peak"
        advice = "Свіжість на максимумі — ідеальний момент для старту або контрольного тесту на результат."
    elif tsb >= 5:
        label, tone = "Оптимальне навантаження", "optimal"
        advice = "Баланс форми й втоми — можна працювати в цільовому темпі без ризику."
    elif tsb >= -10:
        label, tone = "Продуктивне тренування", "building"
        advice = "Ти в роботі: форма накопичується, тіло під помірним навантаженням."
    elif tsb >= -30:
        label, tone = "Висока втома", "fatigued"
        advice = "Втома переважає — додай відновлювальні дні, сон і легкі пробіжки."
    else:
        label, tone = "Ризик перетренування", "overreaching"
        advice = "Дуже глибока втома — знизь обсяг і інтенсивність, дай тілу відпочити 2–3 дні."
    return {"tsb": round(tsb, 1), "label": label, "tone": tone, "advice": advice}


def recovery_estimate(db: Session, today: date | None = None) -> dict:
    """Приблизна оцінка годин до відновлення після останнього тренування."""
    activities = _all_activities(db)
    if not activities:
        return {"hours": None, "remaining": None, "since": None, "ready": True, "activity": None}
    last = activities[-1]
    duration_h = (last.duration_sec or 0) / 3600
    # інтенсивність за середнім пульсом (140 уд/хв ≈ рівень аеробного порогу)
    intensity = max(0.5, (last.avg_hr or 140) / 140.0)
    hours = max(6.0, min(72.0, duration_h * 12.0 * intensity))
    hours_since = max(0.0, (datetime.now() - last.date).total_seconds() / 3600)
    remaining = max(0.0, hours - hours_since)
    return {
        "hours": round(hours, 1),
        "remaining": round(remaining, 1),
        "since": round(hours_since, 1),
        "ready": remaining <= 0.5,
        "activity": {"id": last.id, "title": last.title, "date": last.date},
    }


# --------------------------------------------------------------------------- #
#  GAP-ефективність та розкладка
# --------------------------------------------------------------------------- #
def _interp_elapsed(
    samples: list[tuple[float, float, int | None, float | None]], distance_m: float
) -> float | None:
    """Лінійна інтерполяція часу досягнення заданої дистанції."""
    if not samples:
        return None
    previous = samples[0]
    for sample in samples:
        if sample[0] >= distance_m:
            if sample[0] == previous[0]:
                return sample[1]
            ratio = (distance_m - previous[0]) / (sample[0] - previous[0])
            return previous[1] + ratio * (sample[1] - previous[1])
        previous = sample
    return samples[-1][1]


def _graded_speed_and_hr(
    samples: list[tuple[float, float, int | None, float | None]],
) -> tuple[float, float] | None:
    """(швидкість за GAP, м/хв; середній пульс) для фрагмента треку."""
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
    hrs = [s[2] for s in samples if s[2]]
    if not has_elevation or flat_m <= 0 or seconds <= 0 or not hrs:
        return None
    return flat_m / (seconds / 60.0), sum(hrs) / len(hrs)


def efficiency_metrics(
    samples: list[tuple[float, float, int | None, float | None]],
) -> dict | None:
    """EF (GAP-швидкість / пульс) та аеробне розʼєднання (decoupling)."""
    if len(samples) < 4:
        return None
    total = _graded_speed_and_hr(samples)
    if total is None:
        return None
    graded_speed, avg_hr = total
    if avg_hr <= 0:
        return None
    ef = graded_speed / avg_hr

    half_distance = samples[-1][0] / 2
    split_index = next((i for i, s in enumerate(samples) if s[0] >= half_distance), len(samples) - 1)
    first = _graded_speed_and_hr(samples[: split_index + 1])
    second = _graded_speed_and_hr(samples[split_index:])
    decoupling = None
    if first and second:
        ef_first = first[0] / first[1]
        ef_second = second[0] / second[1]
        if ef_first > 0:
            decoupling = round((ef_first - ef_second) / ef_first * 100, 1)

    return {
        "ef": round(ef, 2),
        "graded_speed_m_min": round(graded_speed, 1),
        "avg_hr": int(round(avg_hr)),
        "decoupling_pct": decoupling,
    }


def ef_trend(db: Session, days: int = 30, today: date | None = None) -> list[dict]:
    """Тренд Efficiency Factor за останні ``days`` днів (лише треки з пульсом)."""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    series: list[dict] = []
    for activity in _all_activities(db):
        if _day(activity.date) < start:
            continue
        metrics = efficiency_metrics(samples_from_point_dicts(activity.points))
        if not metrics:
            continue
        series.append(
            {
                "date": activity.date.isoformat(),
                "label": activity.date.strftime("%d.%m"),
                "ef": metrics["ef"],
                "activity_id": activity.id,
                "title": activity.title,
            }
        )
    return series


def pacing_analysis(
    samples: list[tuple[float, float, int | None, float | None]],
) -> dict | None:
    """Тип розкладки забігу: Negative / Even / Positive split."""
    if len(samples) < 4:
        return None
    total_distance = samples[-1][0]
    total_time = samples[-1][1]
    if total_distance < 1000 or total_time <= 0:
        return None
    half = total_distance / 2
    half_time = _interp_elapsed(samples, half)
    if half_time is None:
        return None
    first_pace = half_time / (half / 1000.0)
    second_pace = (total_time - half_time) / ((total_distance - half) / 1000.0)
    if first_pace <= 0 or second_pace <= 0:
        return None
    delta = (second_pace - first_pace) / first_pace * 100
    if delta < -0.5:
        kind, label = "negative", "Negative Split"
        note = "Друга половина швидша за першу — зразкова розкладка."
    elif delta > 0.5:
        kind, label = "positive", "Positive Split"
        note = "Друга половина повільніша — старт був занадто швидким."
    else:
        kind, label = "even", "Even Pace"
        note = "Рівний темп протягом усієї пробіжки."
    return {
        "type": kind,
        "label": label,
        "note": note,
        "delta_pct": round(delta, 1),
        "first_pace": round(first_pace, 1),
        "second_pace": round(second_pace, 1),
    }


# --------------------------------------------------------------------------- #
#  VDOT / VO2max та прогноз на дистанції (Джек Деніелс)
# --------------------------------------------------------------------------- #
#: Дистанції, за якими шукаємо найкращий відрізок для оцінки VDOT
VDOT_SOURCE_DISTANCES_KM = [1.609, 3.0, 5.0, 10.0, 21.1]
#: Класичні змагальні дистанції для прогнозу
RACE_DISTANCES_KM = [5.0, 10.0, 21.1, 42.2]
#: Тренувальні зони (частка від VDOT/VO2max за Деніелсом)
TRAINING_ZONES = [
    ("R", "Відновлювальний", 0.59, 0.65),
    ("E", "Аеробний", 0.65, 0.79),
    ("T", "Темповий", 0.83, 0.88),
    ("I", "Інтервальний", 0.96, 1.00),
]


def _vo2_from_velocity(velocity_m_min: float) -> float:
    return -4.60 + 0.182258 * velocity_m_min + 0.000104 * velocity_m_min**2


def _pct_vo2max(duration_min: float) -> float:
    return (
        0.8
        + 0.1894393 * math.exp(-0.012778 * duration_min)
        + 0.2989558 * math.exp(-0.1932605 * duration_min)
    )


def _vdot_from_performance(distance_m: float, time_sec: float) -> float:
    """VDOT за формулами Деніелса: VO2(max) на тривалість зусилля."""
    if distance_m <= 0 or time_sec <= 0:
        return 0.0
    duration_min = time_sec / 60.0
    velocity = distance_m / duration_min
    percent = _pct_vo2max(duration_min)
    if percent <= 0:
        return 0.0
    return _vo2_from_velocity(velocity) / percent


def _time_for_distance(vdot: float, distance_m: float) -> float:
    """Обернена задача: час (с) на дистанцію за відомого VDOT (бісекція)."""
    low, high = 1.0, 900.0  # хвилини
    for _ in range(90):
        mid = (low + high) / 2
        if _vdot_from_performance(distance_m, mid * 60.0) > vdot:
            low = mid
        else:
            high = mid
    return (low + high) / 2 * 60.0


def _velocity_for_vo2(vo2: float) -> float:
    """Швидкість (м/хв), за якої споживання кисню дорівнює ``vo2``."""
    a, b, c = 0.000104, 0.182258, -(vo2 + 4.60)
    discriminant = b * b - 4 * a * c
    if discriminant <= 0:
        return 0.0
    return (-b + math.sqrt(discriminant)) / (2 * a)


def _pace_band(vdot: float, low_pct: float, high_pct: float) -> dict:
    """Діапазон темпу (сек/км): від швидкого до повільного краю зони."""
    fast = _velocity_for_vo2(vdot * high_pct)
    slow = _velocity_for_vo2(vdot * low_pct)
    return {
        "pace_fast": round(60000.0 / fast, 1) if fast > 0 else None,
        "pace_slow": round(60000.0 / slow, 1) if slow > 0 else None,
    }


def vdot_analysis(db: Session, weeks: int = 6, today: date | None = None) -> dict:
    """Поточний VDOT за найкращими відрізками останніх ``weeks`` тижнів."""
    today = today or date.today()
    start = today - timedelta(days=weeks * 7)
    activities = [a for a in _all_activities(db) if _day(a.date) >= start]

    best_vdot = 0.0
    source: dict | None = None
    for activity in activities:
        samples = _point_samples(activity)
        if not samples:
            continue
        for distance_km in VDOT_SOURCE_DISTANCES_KM:
            seconds = _best_time_for_distance(samples, distance_km * 1000)
            if seconds is None:
                continue
            vdot = _vdot_from_performance(distance_km * 1000, seconds)
            if vdot > best_vdot:
                best_vdot = vdot
                source = {
                    "distance_km": distance_km,
                    "time_sec": int(round(seconds)),
                    "pace_sec": round(seconds / distance_km, 1),
                    "activity_id": activity.id,
                    "title": activity.title,
                    "date": activity.date,
                }

    if best_vdot <= 0:
        return {"vdot": None, "predictions": [], "zones": [], "source": None, "weeks": weeks}

    predictions = []
    for distance_km in RACE_DISTANCES_KM:
        seconds = _time_for_distance(best_vdot, distance_km * 1000)
        predictions.append(
            {
                "distance_km": distance_km,
                "time_sec": int(round(seconds)),
                "pace_sec": round(seconds / distance_km, 1),
            }
        )
    zones = [
        {"code": code, "name": name, **_pace_band(best_vdot, low, high)}
        for code, name, low, high in TRAINING_ZONES
    ]
    return {
        "vdot": round(best_vdot, 1),
        "vo2max": round(best_vdot, 1),
        "predictions": predictions,
        "zones": zones,
        "source": source,
        "weeks": weeks,
    }


# --------------------------------------------------------------------------- #
#  досягнення та нагороди
# --------------------------------------------------------------------------- #
def achievements(db: Session, today: date | None = None) -> list[dict]:
    """Автоматичні нагороди за мотивами тренувальної історії."""
    today = today or date.today()
    activities = _all_activities(db)

    negative_runs = 0
    for activity in activities:
        if activity.distance_km < 5:
            continue
        pacing = pacing_analysis(samples_from_point_dicts(activity.points))
        if pacing and pacing["type"] == "negative":
            negative_runs += 1

    max_ascent = max((a.elevation_gain or 0.0 for a in activities), default=0.0)
    streak = compute_streak(db, today)["weeks"]

    series = fitness_fatigue(db, days=90, today=today)["series"]
    ctl_now = series[-1]["ctl"] if series else 0.0
    ctl_prev = series[-15]["ctl"] if len(series) >= 15 else (series[0]["ctl"] if series else 0.0)
    ctl_delta = round(ctl_now - ctl_prev, 1)

    start, end = month_bounds(today.year, today.month)
    month_km = round(sum(a.distance_km for a in activities if start <= _day(a.date) < end), 1)

    return [
        {
            "key": "negative_split",
            "icon": "⚡",
            "title": "Negative Split майстер",
            "description": "Забіг на 5+ км, де друга половина швидша за першу",
            "unlocked": negative_runs >= 1,
            "progress": f"{negative_runs}",
        },
        {
            "key": "mountain_goat",
            "icon": "⛰️",
            "title": "Гірський козел",
            "description": "Набір висоти 300+ м в одному тренуванні",
            "unlocked": max_ascent >= 300,
            "progress": f"{round(max_ascent)} м",
        },
        {
            "key": "steel_week",
            "icon": "🛡️",
            "title": "Сталевий тиждень",
            "description": "4+ тижні поспіль із пробіжками",
            "unlocked": streak >= 4,
            "progress": f"{streak} тиж.",
        },
        {
            "key": "form_progress",
            "icon": "📈",
            "title": "Прогрес форми",
            "description": "Форма (CTL) зросла за останні два тижні",
            "unlocked": ctl_delta > 0,
            "progress": f"{ctl_delta:+.1f}",
        },
        {
            "key": "marathon_volume",
            "icon": "🏅",
            "title": "Марафонський обсяг місяця",
            "description": "42.2 км і більше за календарний місяць",
            "unlocked": month_km >= 42.2,
            "progress": f"{month_km} км",
        },
    ]


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

