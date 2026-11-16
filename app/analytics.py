"""Strava-Premium-style analytics built on top of stored activities.

Everything here is pure Python over the ORM objects so it is easy to unit-test
and does not require additional database tables (except goals in ``app_settings``).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Activity, Shoe
from .parser import (
    _haversine_m,
    gap_pace_from_point_dicts,
    grade_adjusted_factor,
    samples_from_point_dicts,
)

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

#: Порогова швидкість за замовчуванням (сек/км) — 4:30/км, поки VDOT не оцінено
DEFAULT_THRESHOLD_PACE_SEC = 270.0
#: Частка VDOT, що відповідає пороговому (T) темпу за моделлю Деніелса
THRESHOLD_VDOT_SHARE = 0.88


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
def _threshold_pace_sec(db: Session) -> float:
    """Порогова швидкість (сек/км) з поточного VDOT, інакше дефолт 4:30/км."""
    vdot = vdot_analysis(db).get("vdot")
    if not vdot:
        return DEFAULT_THRESHOLD_PACE_SEC
    velocity = _velocity_for_vo2(vdot * THRESHOLD_VDOT_SHARE)
    if velocity <= 0:
        return DEFAULT_THRESHOLD_PACE_SEC
    return 60000.0 / velocity


def activity_load(activity: Activity, threshold_pace_sec: float) -> float:
    """Навантаження однієї сесії.

    * з пульсом — TRIMP-подібне: хвилини × (середній пульс / 140);
    * без пульсу — rTSS: години × IF² × 100, де ``IF = порогова / фактична (GAP) швидкість``;
    * якщо вказано sRPE (1–10) — об'єктивна оцінка усереднюється з суб'єктивною
      (``sRPE × години × 10``), тож важкість «на відчуття» коригує навантаження.
    """
    hours = (activity.duration_sec or 0) / 3600.0
    if hours <= 0:
        return 0.0

    base: float | None = None
    if activity.avg_hr:
        base = hours * 60.0 * max(0.5, activity.avg_hr / 140.0)
    else:
        gap_pace = gap_pace_from_point_dicts(activity.points) or activity.avg_pace_sec
        if gap_pace and gap_pace > 0 and threshold_pace_sec > 0:
            intensity_factor = threshold_pace_sec / gap_pace
            base = hours * intensity_factor**2 * 100.0

    if activity.srpe:
        srpe_load = max(1, min(10, int(activity.srpe))) * hours * 10.0
        base = srpe_load if base is None else (base + srpe_load) / 2.0

    return base or 0.0


def _daily_loads(db: Session) -> dict[date, float]:
    """Добове навантаження: TRIMP за пульсом або rTSS без пульсу (див. ``activity_load``)."""
    threshold_pace = _threshold_pace_sec(db)
    loads: dict[date, float] = {}
    for activity in _all_activities(db):
        load = activity_load(activity, threshold_pace)
        if load <= 0:
            continue
        loads[_day(activity.date)] = loads.get(_day(activity.date), 0.0) + load
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
        return {
            "series": [],
            "ctl": 0.0,
            "atl": 0.0,
            "tsb": 0.0,
            "days": days,
            "history_days": 0,
            "ramp_up": True,
            "warning": "Історія тренувань порожня — оцінка довгострокової форми недоступна.",
        }

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

    history_days = (today - first_day).days + 1
    # CTL розганяється приблизно за 42 дні: поки історії менше, крива занижена
    ramp_up = history_days < FITNESS_DAYS
    warning = (
        f"Період розгону бази: історія тренувань {history_days} дн. < {FITNESS_DAYS} дн. "
        "CTL/ATL ще не відображають реальну довгострокову форму — оцінка занижена."
        if ramp_up
        else None
    )

    return {
        "series": series,
        "ctl": round(ctl, 1),
        "atl": round(atl, 1),
        "tsb": round(ctl - atl, 1),
        "days": days,
        "history_days": history_days,
        "ramp_up": ramp_up,
        "warning": warning,
    }


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
#: Мінімальна тривалість безперервного відрізка для оцінки VDOT (сек) — 10 хвилин
VDOT_MIN_SEGMENT_SEC = 600.0
#: Вага зусилля: змагання — максимальні зусилля, тож отримують підвищений коефіцієнт
RACE_VDOT_WEIGHT = 1.04
#: Цільовий тижневий обсяг (км): за нього марафонський прогноз не коригується
MARATHON_REFERENCE_WEEKLY_KM = 50.0
#: Максимальна поправка (частка уповільнення) при повній відсутності обсягу
MARATHON_VOLUME_PENALTY = 0.15
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


def _best_segment(
    samples: list[tuple[float, float]], min_seconds: float = VDOT_MIN_SEGMENT_SEC
) -> tuple[float, float] | None:
    """Найшвидший безперервний відрізок тривалістю ``min_seconds``.

    Повертає ``(метри, секунди)`` або ``None``, якщо трек коротший за відрізок.
    Дистанція інтерполюється на межах вікна, тож оцінка не залежить від щільності
    GPS-точок.
    """
    if len(samples) < 2 or samples[-1][0] < min_seconds:
        return None
    best_distance = 0.0
    count = len(samples)
    right = 0
    for left in range(count):
        if right < left:
            right = left
        target = samples[left][0] + min_seconds
        while right < count and samples[right][0] < target:
            right += 1
        if right >= count:
            break
        previous = samples[right - 1] if right - 1 >= left else samples[left]
        span = samples[right][0] - previous[0]
        if span <= 0:
            continue
        ratio = (target - previous[0]) / span
        end_distance = previous[1] + ratio * (samples[right][1] - previous[1])
        distance = end_distance - samples[left][1]
        if distance > best_distance:
            best_distance = distance
    if best_distance <= 0:
        return None
    return best_distance, min_seconds


def _weekly_volume_km(db: Session, weeks: int = 6, today: date | None = None) -> float:
    """Середній тижневий обсяг (км) за останні ``weeks`` тижнів."""
    today = today or date.today()
    start = today - timedelta(days=weeks * 7)
    total = sum(a.distance_km for a in _all_activities(db) if _day(a.date) >= start)
    return round(total / weeks, 1)


def _marathon_forecast(db: Session, vdot: float, source: dict | None, today: date) -> dict:
    """Прогноз на 42.2 км з поправкою на тижневий обсяг і рівнем впевненості.

    Результат на марафоні сильно залежить від обсягу: якщо середній тижневий
    пробіг за 6 тижнів менший за опорний, прогноз VDOT уповільнюється.
    """
    base_seconds = _time_for_distance(vdot, 42_200.0)
    weekly_km = _weekly_volume_km(db, 6, today)
    if weekly_km < MARATHON_REFERENCE_WEEKLY_KM:
        shortfall = (MARATHON_REFERENCE_WEEKLY_KM - weekly_km) / MARATHON_REFERENCE_WEEKLY_KM
        factor = 1.0 + shortfall * MARATHON_VOLUME_PENALTY
    else:
        factor = 1.0
    seconds = base_seconds * factor

    activities = _all_activities(db)
    history_days = (today - _day(activities[0].date)).days + 1 if activities else 0
    is_race = bool(source and source.get("is_race"))
    if history_days < FITNESS_DAYS or weekly_km < 25:
        confidence, tone = "Низький", "low"
        note = "Мало історії або обсягу — прогноз орієнтовний. Нарощуй тижневий кілометраж і додай довгі пробіжки."
    elif is_race and weekly_km >= MARATHON_REFERENCE_WEEKLY_KM and history_days >= 2 * FITNESS_DAYS:
        confidence, tone = "Високий", "high"
        note = "Є змагальний результат і достатній обсяг — прогноз найнадійніший."
    else:
        confidence, tone = "Середній", "medium"
        note = "Обсяг або тривалість історії помірні — прогноз із поправкою на тижневий кілометраж."

    return {
        "distance_km": 42.2,
        "time_sec": int(round(seconds)),
        "pace_sec": round(seconds / 42.2, 1),
        "base_time_sec": int(round(base_seconds)),
        "weekly_km": weekly_km,
        "volume_factor": round(factor, 3),
        "confidence": confidence,
        "tone": tone,
        "note": note,
    }


def vdot_analysis(db: Session, weeks: int = 6, today: date | None = None) -> dict:
    """Поточний VDOT за найшвидшими безперервними відрізками (≥10 хв) останніх тижнів."""
    today = today or date.today()
    start = today - timedelta(days=weeks * 7)
    activities = [a for a in _all_activities(db) if _day(a.date) >= start]

    best_score = 0.0
    source: dict | None = None
    for activity in activities:
        samples = _point_samples(activity)
        if not samples:
            continue
        segment = _best_segment(samples)
        if segment is None:
            continue
        distance_m, seconds = segment
        raw_vdot = _vdot_from_performance(distance_m, seconds)
        if raw_vdot <= 0:
            continue
        # змагання — максимальне зусилля, тож має підвищений коефіцієнт ваги
        weight = RACE_VDOT_WEIGHT if activity.is_race else 1.0
        score = raw_vdot * weight
        if score > best_score:
            best_score = score
            source = {
                "distance_km": round(distance_m / 1000, 3),
                "time_sec": int(round(seconds)),
                "pace_sec": round(seconds / (distance_m / 1000), 1),
                "activity_id": activity.id,
                "title": activity.title,
                "date": activity.date,
                "is_race": bool(activity.is_race),
                "raw_vdot": round(raw_vdot, 1),
            }

    if best_score <= 0:
        return {
            "vdot": None,
            "predictions": [],
            "zones": [],
            "source": None,
            "marathon": None,
            "weeks": weeks,
        }

    marathon = _marathon_forecast(db, best_score, source, today)
    predictions = []
    for distance_km in RACE_DISTANCES_KM:
        if distance_km == 42.2:
            seconds = float(marathon["time_sec"])
        else:
            seconds = _time_for_distance(best_score, distance_km * 1000)
        predictions.append(
            {
                "distance_km": distance_km,
                "time_sec": int(round(seconds)),
                "pace_sec": round(seconds / distance_km, 1),
            }
        )
    zones = [
        {"code": code, "name": name, **_pace_band(best_score, low, high)}
        for code, name, low, high in TRAINING_ZONES
    ]
    return {
        "vdot": round(best_score, 1),
        "vo2max": round(best_score, 1),
        "predictions": predictions,
        "zones": zones,
        "source": source,
        "marathon": marathon,
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
#  темпові зони за VDOT та тренд GAP
# --------------------------------------------------------------------------- #
#: Темпові зони як частка від VDOT (VO₂max): (код, назва, нижня, верхня)
TEMPO_ZONES = [
    ("Z1", "Відновлення", 0.00, 0.65),
    ("Z2", "Аеробна", 0.65, 0.79),
    ("Z3", "Темпова", 0.79, 0.88),
    ("Z4", "Порогова", 0.88, 0.96),
    ("Z5", "Максимум", 0.96, 1.30),
]


def has_point_hr_data(db: Session) -> bool:
    """True, якщо хоч один трек має пер-точковий пульс (а не лише середній)."""
    return any(any(p.get("hr") for p in activity.points) for activity in _all_activities(db))


def tempo_zone_distribution(db: Session, vdot: float | None) -> dict:
    """Розподіл часу тренувань по темпових зонах Z1–Z5 на основі VDOT.

    Інтенсивність тренування = VO₂ від середньої швидкості ÷ VDOT, тобто
    частка від максимальної аеробної потужності.
    """
    if not vdot:
        return {"zones": [], "total_seconds": 0, "has_data": False}

    zone_seconds = [0.0] * len(TEMPO_ZONES)
    for activity in _all_activities(db):
        if not activity.duration_sec or not activity.distance_km:
            continue
        speed = activity.distance_km * 1000.0 / (activity.duration_sec / 60.0)
        if speed <= 0:
            continue
        share = _vo2_from_velocity(speed) / vdot
        index = len(TEMPO_ZONES) - 1
        for position, (_, _, _, upper) in enumerate(TEMPO_ZONES):
            if share < upper:
                index = position
                break
        zone_seconds[index] += float(activity.duration_sec)

    total = sum(zone_seconds)
    zones = [
        {
            "code": code,
            "name": name,
            "percent": round(zone_seconds[position] / total * 100, 1) if total else 0.0,
            "seconds": int(zone_seconds[position]),
            "range": f"{int(lower * 100)}–{int(upper * 100)}% VDOT",
        }
        for position, (code, name, lower, upper) in enumerate(TEMPO_ZONES)
    ]
    return {"zones": zones, "total_seconds": int(total), "has_data": total > 0}


def gap_trend(db: Session, days: int = 30, today: date | None = None) -> list[dict]:
    """Тренд середнього GAP (сек/км) за останні ``days`` днів."""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    series: list[dict] = []
    for activity in _all_activities(db):
        if _day(activity.date) < start:
            continue
        gap = gap_pace_from_point_dicts(activity.points)
        if gap is None:
            continue
        series.append(
            {
                "date": activity.date.isoformat(),
                "label": activity.date.strftime("%d.%m"),
                "gap": gap,
                "activity_id": activity.id,
                "title": activity.title,
            }
        )
    return series


# --------------------------------------------------------------------------- #
#  теплова карта тренувань (12 місяців, GitHub-стиль)
# --------------------------------------------------------------------------- #
def training_heatmap(db: Session, months: int = 12, today: date | None = None) -> dict:
    """Щоденна теплова карта за ``months`` місяців зі золотим градієнтом."""
    today = today or date.today()
    end = today
    start = end - timedelta(days=int(months * 30.5))
    start -= timedelta(days=start.weekday())  # вирівнюємо по понеділку

    daily: dict[date, dict[str, float]] = {}
    for activity in _all_activities(db):
        day = _day(activity.date)
        if day < start or day > end:
            continue
        entry = daily.setdefault(day, {"distance_km": 0.0, "count": 0})
        entry["distance_km"] += activity.distance_km
        entry["count"] += 1

    max_distance = max((value["distance_km"] for value in daily.values()), default=0.0)

    def level_for(distance_km: float) -> int:
        if max_distance <= 0:
            return 1
        ratio = distance_km / max_distance
        if ratio <= 0.25:
            return 1
        if ratio <= 0.5:
            return 2
        if ratio <= 0.75:
            return 3
        return 4

    weeks: list[list[dict]] = []
    cursor = start
    while cursor <= end:
        column = []
        for offset in range(7):
            day = cursor + timedelta(days=offset)
            info = daily.get(day)
            if day > end:
                cell_level = -1
            elif info is None:
                cell_level = 0
            else:
                cell_level = level_for(info["distance_km"])
            column.append(
                {
                    "date": day.isoformat(),
                    "level": cell_level,
                    "distance_km": round(info["distance_km"], 2) if info else 0.0,
                    "count": int(info["count"]) if info else 0,
                }
            )
        weeks.append(column)
        cursor += timedelta(days=7)

    month_labels: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for index, column in enumerate(weeks):
        month_start = date.fromisoformat(column[0]["date"]).replace(day=1)
        key = (month_start.year, month_start.month)
        if key not in seen:
            seen.add(key)
            month_labels.append({"column": index, "label": MONTH_NAMES_UK[month_start.month - 1][:3]})

    return {
        "weeks": weeks,
        "months": month_labels,
        "total_km": round(sum(value["distance_km"] for value in daily.values()), 1),
        "active_days": len(daily),
        "max_km": round(max_distance, 1),
    }


# --------------------------------------------------------------------------- #
#  схожі маршрути (Matched Runs)
# --------------------------------------------------------------------------- #
#: Допуск схожості маршруту: старт/фініш (м) і дистанція (частка)
MATCH_START_FINISH_M = 150.0
MATCH_DISTANCE_TOLERANCE = 0.05
#: Скільки точок лишається після ресемплінгу треку (порівняння форми маршруту)
ROUTE_SAMPLE_POINTS = 30
#: Точність geohash-клітинки для порівняння маршрутів (≈150 м)
ROUTE_GEOHASH_PRECISION = 7
#: Мінімальний коефіцієнт Жаккара, за якого маршрути вважаються однаковими
ROUTE_JACCARD_THRESHOLD = 0.75

_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


def _geohash(lat: float, lon: float, precision: int = ROUTE_GEOHASH_PRECISION) -> str:
    """Geohash-клітинка для координати (власна реалізація, без залежностей)."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    chars: list[str] = []
    bit = 0
    value = 0
    even = True
    while len(chars) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                value = (value << 1) | 1
                lon_lo = mid
            else:
                value <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                value = (value << 1) | 1
                lat_lo = mid
            else:
                value <<= 1
                lat_hi = mid
        even = not even
        bit += 1
        if bit == 5:
            chars.append(_GEOHASH_ALPHABET[value])
            bit = 0
            value = 0
    return "".join(chars)


def _resample_route(points: list[dict], count: int = ROUTE_SAMPLE_POINTS) -> list[tuple[float, float]]:
    """Рівномірно відібрати ``count`` GPS-точок треку (незалежно від довжини)."""
    coords = [(p["lat"], p["lon"]) for p in points]
    if len(coords) <= count:
        return coords
    last = len(coords) - 1
    step = last / (count - 1)
    indices = sorted({int(round(i * step)) for i in range(count)})
    return [coords[i] for i in indices]


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _route_signature(activity: Activity) -> dict | None:
    points = [p for p in activity.points if p.get("lat") is not None and p.get("lon") is not None]
    if len(points) < 2:
        return None
    resampled = _resample_route(points)
    return {
        "activity": activity,
        "start": resampled[0],
        "finish": resampled[-1],
        "cells": {_geohash(lat, lon) for lat, lon in resampled},
        "distance_km": activity.distance_km,
    }


def _same_route(left: dict, right: dict) -> bool:
    """Чи це той самий маршрут: та сама дистанція і збіг форми траси.

    Основний критерій — коефіцієнт Жаккара наборів geohash-клітинок (≥0.75).
    Як запасний варіант лишається збіг старту й фінішу в межах допуску.
    """
    if left["distance_km"] <= 0 or right["distance_km"] <= 0:
        return False
    if abs(left["distance_km"] - right["distance_km"]) / left["distance_km"] > MATCH_DISTANCE_TOLERANCE:
        return False
    if _jaccard(left["cells"], right["cells"]) >= ROUTE_JACCARD_THRESHOLD:
        return True
    start_gap = _haversine_m(*left["start"], *right["start"])
    finish_gap = _haversine_m(*left["finish"], *right["finish"])
    return start_gap <= MATCH_START_FINISH_M and finish_gap <= MATCH_START_FINISH_M


def matched_runs(db: Session, minimum: int = 2) -> list[dict]:
    """Групує тренування зі схожою трасою (geohash-Жаккар) і дистанцією (±5 %)."""
    signatures = [signature for signature in (_route_signature(a) for a in _all_activities(db)) if signature]
    used = [False] * len(signatures)
    groups: list[dict] = []
    for index, signature in enumerate(signatures):
        if used[index]:
            continue
        used[index] = True
        members = [signature]
        for other in range(index + 1, len(signatures)):
            if used[other]:
                continue
            if _same_route(signature, signatures[other]):
                used[other] = True
                members.append(signatures[other])
        if len(members) < minimum:
            continue
        members.sort(key=lambda item: item["activity"].date)
        runs = [
            {
                "id": member["activity"].id,
                "title": member["activity"].title,
                "date": member["activity"].date.strftime("%d.%m.%Y"),
                "distance_km": round(member["activity"].distance_km, 2),
                "pace_sec": round(member["activity"].avg_pace_sec, 1),
                "gap_sec": gap_pace_from_point_dicts(member["activity"].points),
            }
            for member in members
        ]
        paces = [run["pace_sec"] for run in runs if run["pace_sec"]]
        gaps = [run["gap_sec"] for run in runs if run["gap_sec"]]
        pace_delta = round(runs[-1]["pace_sec"] - runs[0]["pace_sec"], 1) if len(runs) >= 2 and paces else None
        gap_delta = (
            round(runs[-1]["gap_sec"] - runs[0]["gap_sec"], 1)
            if len(runs) >= 2 and runs[0]["gap_sec"] and runs[-1]["gap_sec"]
            else None
        )
        groups.append(
            {
                "count": len(runs),
                "distance_km": round(sum(run["distance_km"] for run in runs) / len(runs), 2),
                "avg_pace": round(sum(paces) / len(paces), 1) if paces else None,
                "avg_gap": round(sum(gaps) / len(gaps), 1) if gaps else None,
                "pace_delta": pace_delta,
                "gap_delta": gap_delta,
                "runs": runs,
            }
        )
    groups.sort(key=lambda group: group["count"], reverse=True)
    return groups


# --------------------------------------------------------------------------- #
#  ресурс взуття
# --------------------------------------------------------------------------- #
#: Загальний ліміт кілометражу за замовчуванням, якщо в кросівок він не заданий
SHOE_DEFAULT_LIMIT_KM = 750.0


def shoe_resource(db: Session, limit_default: float = SHOE_DEFAULT_LIMIT_KM) -> list[dict]:
    """Прогрес кілометражу активних кросівок (попередження при ≥80 %)."""
    shoes = db.execute(select(Shoe).where(Shoe.is_active.is_(True)).order_by(Shoe.id)).scalars().all()
    resource = []
    for shoe in shoes:
        limit = shoe.max_distance_km or limit_default
        current = shoe.current_distance_km or 0.0
        percent = round(min(100.0, current / limit * 100), 1) if limit > 0 else 0.0
        resource.append(
            {
                "id": shoe.id,
                "name": shoe.name,
                "brand": shoe.brand,
                "current_km": round(current, 1),
                "limit_km": round(limit, 1),
                "percent": percent,
                "remaining_km": round(max(0.0, limit - current), 1),
                "warning": percent >= 80.0,
            }
        )
    return resource


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

