"""FastAPI app: activity upload, statistics, shoes, CRUD, exports and dedupe."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload

from .analytics import (
    DEFAULT_GOALS,
    achievements,
    acwr,
    available_report_months,
    compute_challenges,
    compute_streak,
    ef_trend,
    efficiency_metrics,
    fade_analysis,
    fitness_fatigue,
    form_status,
    foster_load,
    gap_trend,
    has_point_hr_data,
    heatmap_tracks,
    heatmap_years,
    hr_zone_analysis,
    matched_runs,
    monthly_report,
    pacing_analysis,
    personal_records,
    recovery_estimate,
    shoe_resource,
    tempo_zone_distribution,
    training_heatmap,
    vdot_analysis,
)
from .db import BASE_DIR, auto_backup, get_db, init_db
from .models import Activity, AppSettings, Shoe
from .parser import (
    PARSER_VERSION,
    SUPPORTED_EXTENSIONS,
    ParseError,
    _parse_gpx,
    gap_pace_from_point_dicts,
    parse_activity_file,
    plan_race,
    samples_from_point_dicts,
    splits_from_point_dicts,
)
from .routing import RouteError, RouteInputError, build_route
from .weather import badge_from_json, fetch_weather

UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    auto_backup()
    yield


app = FastAPI(title="PaceLab", description="Персональний аналіз бігових тренувань", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --------------------------------------------------------------------------- #
#  formatting helpers (exposed to Jinja2 as filters)
# --------------------------------------------------------------------------- #
def fmt_duration(total_seconds: float | int | None) -> str:
    if not total_seconds:
        return "—"
    total = int(round(float(total_seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _pace_str(sec_per_km: float | None) -> str:
    """Seconds per kilometre -> ``MM:SS`` (zero padded)."""
    if not sec_per_km:
        return "—"
    total = int(round(float(sec_per_km)))
    minutes, seconds = divmod(total, 60)
    return f"{minutes:02d}:{seconds:02d}"


def fmt_pace(sec_per_km: float | None) -> str:
    return _pace_str(sec_per_km)


def fmt_pace_km(sec_per_km: float | None) -> str:
    """Seconds per kilometre -> ``MM:SS /км``."""
    value = _pace_str(sec_per_km)
    return "—" if value == "—" else f"{value} /км"


def fmt_km(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def fmt_date(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y · %H:%M")


def fmt_datetime_local(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%dT%H:%M")


def weather_badge(activity: Activity) -> dict | None:
    """Jinja filter: weather dict for a training (``None`` when unavailable)."""
    return badge_from_json(activity.weather_json)


templates.env.filters.update(
    {
        "duration": fmt_duration,
        "pace": fmt_pace,
        "pace_km": fmt_pace_km,
        "km": fmt_km,
        "date": fmt_date,
        "datetime_local": fmt_datetime_local,
        "weather": weather_badge,
    }
)


# --------------------------------------------------------------------------- #
#  app settings (monthly challenge goals)
# --------------------------------------------------------------------------- #
GOAL_KEYS = ("goal_month_distance_km", "goal_month_elevation_m", "goal_month_runs")


def _get_goals(db: Session) -> dict[str, float]:
    goals = dict(DEFAULT_GOALS)
    rows = db.execute(select(AppSettings).where(AppSettings.key.in_(GOAL_KEYS))).scalars().all()
    for row in rows:
        try:
            goals[row.key] = float(row.value)
        except (TypeError, ValueError):
            continue
    return goals


def _save_goals(db: Session, values: dict[str, str]) -> None:
    for key in GOAL_KEYS:
        raw = (values.get(key) or "").replace(",", ".").strip()
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = DEFAULT_GOALS[key]
        if value <= 0:
            value = DEFAULT_GOALS[key]
        row = db.get(AppSettings, key)
        if row is None:
            db.add(AppSettings(key=key, value=f"{value:g}"))
        else:
            row.value = f"{value:g}"
    db.commit()


# --------------------------------------------------------------------------- #
#  data helpers
# --------------------------------------------------------------------------- #
def _period_stats(db: Session, start: datetime) -> dict:
    count, distance, duration, elevation = db.execute(
        select(
            func.count(Activity.id),
            func.coalesce(func.sum(Activity.distance_km), 0.0),
            func.coalesce(func.sum(Activity.duration_sec), 0),
            func.coalesce(func.sum(Activity.elevation_gain), 0.0),
        ).where(Activity.date >= start)
    ).one()
    distance = float(distance or 0)
    duration = int(duration or 0)
    return {
        "count": int(count or 0),
        "distance_km": round(distance, 1),
        "duration_sec": duration,
        "elevation_m": round(float(elevation or 0)),
        "avg_pace_sec": (duration / distance) if distance > 0.01 else 0,
    }


def _period_starts(now: datetime) -> dict[str, datetime]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "week": midnight - timedelta(days=now.weekday()),
        "month": midnight.replace(day=1),
        "year": midnight.replace(month=1, day=1),
    }


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _error(message: str) -> RedirectResponse:
    return _redirect(f"/?error={quote(message)}")


def _message(message: str) -> RedirectResponse:
    return _redirect(f"/?message={quote(message)}")


def _parse_local_datetime(value: str) -> datetime:
    """Parse ``datetime-local`` input (``YYYY-MM-DDTHH:MM``) into naive datetime."""
    cleaned = (value or "").strip().replace(" ", "T")
    for pattern in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
    raise ValueError("Не вдалося розпізнати дату. Формат: РРРР-ММ-ДД ГГ:ХХ")


def _parse_float(value: str, default: float) -> float:
    try:
        return float((value or "").replace(",", "."))
    except (ValueError, AttributeError):
        return default


def _parse_hr(value: str) -> int | None:
    """Parse the optional manual average-HR field; ``None`` if empty/invalid."""
    cleaned = (value or "").strip()
    if not cleaned.isdigit():
        return None
    hr = int(cleaned)
    return hr if 30 <= hr <= 230 else None


def _parse_time_sec(value: str) -> int | None:
    """Розібрати цільовий час фінішу (``H:MM:SS``, ``HH:MM`` або ``MM:SS``).

    Повертає секунди або ``None``, якщо формат некоректний чи значення не додатне.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    parts = cleaned.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.strip().isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    else:  # «години:хвилини» — типовий запис часу на півмарафон і довші гонки
        hours, minutes = numbers
        seconds = 0
    if minutes >= 60 or seconds >= 60:
        return None
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def _parse_pace_sec(value: str) -> int | None:
    """Розібрати базовий темп ``MM:SS`` на кілометр; ``None`` поза межами 2:00–20:00."""
    cleaned = (value or "").strip().split(" ")[0]
    if not cleaned:
        return None
    parts = cleaned.split(":")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        return None
    minutes, seconds = (int(part) for part in parts)
    if seconds >= 60:
        return None
    total = minutes * 60 + seconds
    return total if 120 <= total <= 1200 else None


def _has_point_hr(activity: Activity) -> bool:
    """Whether the source track carries per-point pulse data."""
    try:
        points = json.loads(activity.raw_points_json or "{}").get("points") or []
    except (ValueError, AttributeError):
        return False
    return any(point.get("hr") for point in points)


def _duplicate_message(activity: Activity) -> str:
    return (
        f"Це тренування вже є в історії: «{activity.title}» від {fmt_date(activity.date)} "
        f"({fmt_km(activity.distance_km)} км). Дубль не додано."
    )


def _find_duplicate(db: Session, file_hash: str, payload: dict | None = None) -> Activity | None:
    """Find an existing activity with the same file hash or same start + distance."""
    existing = db.execute(select(Activity).where(Activity.file_hash == file_hash)).scalars().first()
    if existing is not None:
        return existing
    if not payload:
        return None

    start = payload["date"]
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = (
        db.execute(
            select(Activity).where(
                Activity.date >= day_start,
                Activity.date < day_start + timedelta(days=1),
            )
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        same_start = abs((candidate.date - start).total_seconds()) <= 60
        same_distance = abs(candidate.distance_km - payload["distance_km"]) <= 0.02
        if same_start and same_distance:
            return candidate
    return None


# --------------------------------------------------------------------------- #
#  routes
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "app": "PaceLab"}


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    error: str | None = None,
    report_month: str | None = None,
) -> HTMLResponse:
    now = datetime.now()
    starts = _period_starts(now)
    stats = {name: _period_stats(db, start) for name, start in starts.items()}

    activities = (
        db.execute(
            select(Activity)
            .options(joinedload(Activity.shoe))
            .order_by(Activity.date.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    shoes = db.execute(select(Shoe).order_by(Shoe.is_active.desc(), Shoe.id)).scalars().all()

    streak = compute_streak(db)
    goals = _get_goals(db)
    challenges = compute_challenges(db, goals)

    months = available_report_months(db)
    month_values = {entry["value"] for entry in months}
    default_month = f"{now.year:04d}-{now.month:02d}"
    selected_month = report_month if report_month in month_values else default_month
    report_year, report_month_number = (int(part) for part in selected_month.split("-"))
    report = monthly_report(db, report_year, report_month_number)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "stats": stats,
            "activities": activities,
            "shoes": shoes,
            "active_shoes": [shoe for shoe in shoes if shoe.is_active],
            "message": message,
            "error": error,
            "now": now,
            "nav_active": "home",
            "streak": streak,
            "challenges": challenges,
            "goals": goals,
            "report": report,
            "report_months": months,
            "report_month": selected_month,
        },
    )


def _import_activity(
    db: Session,
    original_name: str,
    content: bytes,
    shoe_id: str = "",
    avg_hr: str = "",
) -> tuple[Activity | None, str | None]:
    """Зберегти один трек у БД. Повертає ``(activity, error_message)``.

    ``error_message`` встановлено, коли нічого нового не створено (невірний формат,
    порожній файл, дубль). У разі дубля повертається наявне тренування, тож виклик
    може перенаправити користувача на нього.
    """
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return None, "Підтримуються лише файли .gpx та .fit"
    if not content:
        return None, "Файл порожній — завантажте справжній .gpx або .fit"

    file_hash = hashlib.sha256(content).hexdigest()

    # 1) same file already imported?
    existing = db.execute(select(Activity).where(Activity.file_hash == file_hash)).scalars().first()
    if existing is not None:
        return existing, _duplicate_message(existing)

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original_name).name)[:80] or f"activity{suffix}"
    stored_name = f"{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:8]}_{safe_name}"
    destination = UPLOAD_DIR / stored_name
    destination.write_bytes(content)

    try:
        payload = parse_activity_file(destination, filename_hint=original_name)
    except ParseError as exc:
        destination.unlink(missing_ok=True)
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - keep the UI alive on odd files
        destination.unlink(missing_ok=True)
        return None, f"Не вдалося розібрати файл: {exc}"

    # 2) same start time + distance already imported (different file bytes)?
    existing = _find_duplicate(db, file_hash, payload)
    if existing is not None:
        destination.unlink(missing_ok=True)
        return existing, _duplicate_message(existing)

    # 3) погода на годину старту (Open-Meteo, без ключа; помилки не валять імпорт)
    weather: dict | None = None
    gps_point = next(
        (point for point in payload["points"] if point.get("lat") is not None and point.get("lon") is not None),
        None,
    )
    if gps_point is not None:
        weather = fetch_weather(gps_point["lat"], gps_point["lon"], payload["date"])

    selected_shoe: Shoe | None = None
    shoe_id = (shoe_id or "").strip()
    if shoe_id.isdigit():
        selected_shoe = db.get(Shoe, int(shoe_id))

    # пульс, введений вручну, використовується лише коли у файлі його немає
    manual_hr = _parse_hr(avg_hr)

    activity = Activity(
        title=payload["title"][:200],
        date=payload["date"],
        distance_km=payload["distance_km"],
        distance_km_raw=payload.get("distance_km_raw"),
        distance_km_clean=payload.get("distance_km_clean"),
        parser_version=PARSER_VERSION,
        duration_sec=payload["duration_sec"],
        avg_pace_sec=payload["avg_pace_sec"],
        avg_hr=payload["avg_hr"] if payload["avg_hr"] else manual_hr,
        max_hr=payload["max_hr"],
        elevation_gain=payload["elevation_gain"],
        shoe_id=selected_shoe.id if selected_shoe else None,
        file_hash=file_hash,
        weather_json=json.dumps(weather, ensure_ascii=False) if weather else None,
        raw_points_json=json.dumps(
            {
                "points": payload["points"],
                "splits": payload["splits"],
                "source_file": stored_name,
                "parser_version": PARSER_VERSION,
            },
            ensure_ascii=False,
        ),
    )
    db.add(activity)

    if selected_shoe is not None:
        selected_shoe.current_distance_km = round(
            selected_shoe.current_distance_km + payload["distance_km"], 2
        )

    db.commit()
    db.refresh(activity)
    return activity, None


@app.post("/upload")
async def upload_activity(
    file: UploadFile = File(...),
    shoe_id: str = Form(""),
    avg_hr: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    content = await file.read()
    activity, error = _import_activity(db, file.filename or "", content, shoe_id, avg_hr)
    if error or activity is None:
        return _error(error or "Не вдалося зберегти тренування")
    summary = (
        f"Додано «{activity.title}» — {fmt_km(activity.distance_km)} км, "
        f"{fmt_duration(activity.duration_sec)}"
    )
    return _message(summary)


@app.post("/share")
async def share_target(
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """PWA Web Share Target: приймає .gpx/.fit із системного меню «Поділитися».

    Android-застосунки надсилають файл POST-запитом ``multipart/form-data`` на
    ``/share``; трек одразу парситься в базу, а користувач потрапляє на сторінку
    нової активності.
    """
    if file is None or not file.filename:
        return _error("Файл не отримано — поділись .gpx або .fit треком із застосунку.")
    content = await file.read()
    activity, error = _import_activity(db, file.filename, content)
    if activity is not None:
        return _redirect(f"/activities/{activity.id}")
    return _error(error or "Не вдалося імпортувати файл")


# --------------------------------------------------------------------------- #
#  activities: read / edit / delete
# --------------------------------------------------------------------------- #
@app.get("/activities/{activity_id}", response_class=HTMLResponse)
def activity_detail(
    activity_id: int,
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    activity = db.execute(
        select(Activity).options(joinedload(Activity.shoe)).where(Activity.id == activity_id)
    ).scalar_one_or_none()
    if activity is None:
        raise HTTPException(status_code=404, detail="Тренування не знайдено")

    points = activity.points
    # перераховуємо спліти зі збережених точок, щоб мати набір/втрату висоти
    # також для треків, завантажених до появи цієї метрики
    recomputed = splits_from_point_dicts(points) if activity.has_gps else []
    splits = recomputed or activity.splits
    has_elevation = activity.has_gps and sum(1 for p in points if p.get("ele") is not None) > 1

    # GAP, ефективність та розкладка рахуються зі збережених точок треку
    samples = samples_from_point_dicts(points)
    gap_sec = gap_pace_from_point_dicts(points)
    efficiency = efficiency_metrics(samples)
    pacing = pacing_analysis(samples)
    # стійкість темпу в останній третині (Fade % за GAP) — лише для довгих треків
    fade = fade_analysis(samples)

    return templates.TemplateResponse(
        request=request,
        name="activity.html",
        context={
            "activity": activity,
            "points": points,
            "splits": splits,
            "has_elevation": has_elevation,
            "gap_sec": gap_sec,
            "efficiency": efficiency,
            "pacing": pacing,
            "fade": fade,
            "message": message,
            "error": error,
            "nav_active": "",
        },
    )


@app.get("/activities/{activity_id}/edit", response_class=HTMLResponse)
def activity_edit_form(
    activity_id: int,
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    activity = db.execute(
        select(Activity).options(joinedload(Activity.shoe)).where(Activity.id == activity_id)
    ).scalar_one_or_none()
    if activity is None:
        raise HTTPException(status_code=404, detail="Тренування не знайдено")

    shoes = db.execute(select(Shoe).order_by(Shoe.is_active.desc(), Shoe.id)).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="activity_edit.html",
        context={
            "activity": activity,
            "shoes": shoes,
            "message": message,
            "error": error,
        },
    )


@app.post("/activities/{activity_id}/edit")
def activity_update(
    activity_id: int,
    title: str = Form(...),
    date: str = Form(...),
    shoe_id: str = Form(""),
    avg_hr: str = Form(""),
    srpe: str = Form(""),
    is_race: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    activity = db.execute(
        select(Activity).options(joinedload(Activity.shoe)).where(Activity.id == activity_id)
    ).scalar_one_or_none()
    if activity is None:
        return _error("Тренування не знайдено")

    try:
        new_date = _parse_local_datetime(date)
    except ValueError as exc:
        return _redirect(f"/activities/{activity_id}/edit?error={quote(str(exc))}")

    old_shoe = activity.shoe
    new_shoe: Shoe | None = None
    shoe_id = (shoe_id or "").strip()
    if shoe_id.isdigit():
        new_shoe = db.get(Shoe, int(shoe_id))

    old_shoe_id = old_shoe.id if old_shoe else None
    new_shoe_id = new_shoe.id if new_shoe else None
    if old_shoe_id != new_shoe_id:
        if old_shoe is not None:
            old_shoe.current_distance_km = max(
                0.0, round(old_shoe.current_distance_km - activity.distance_km, 2)
            )
        if new_shoe is not None:
            new_shoe.current_distance_km = round(
                new_shoe.current_distance_km + activity.distance_km, 2
            )

    activity.title = title.strip()[:200] or activity.title
    activity.date = new_date
    activity.shoe_id = new_shoe_id
    # ручний пульс застосовується лише коли у вихідному треку немає пер-точкових даних
    if not _has_point_hr(activity):
        activity.avg_hr = _parse_hr(avg_hr)

    # суб'єктивна важкість (sRPE, 1–10) коригує навантаження; порожнє значення — скидання
    srpe = (srpe or "").strip()
    activity.srpe = int(srpe) if srpe.isdigit() and 1 <= int(srpe) <= 10 else None
    activity.is_race = (is_race or "").strip().lower() in {"1", "true", "on", "yes"}
    db.commit()

    return _message(f"Тренування «{activity.title}» оновлено")


@app.post("/activities/{activity_id}/delete")
def delete_activity(activity_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    activity = db.execute(
        select(Activity).options(joinedload(Activity.shoe)).where(Activity.id == activity_id)
    ).scalar_one_or_none()
    if activity is None:
        return _error("Тренування не знайдено")

    if activity.shoe is not None:
        activity.shoe.current_distance_km = max(
            0.0, round(activity.shoe.current_distance_km - activity.distance_km, 2)
        )

    source_file = ""
    try:
        source_file = json.loads(activity.raw_points_json or "{}").get("source_file", "")
    except ValueError:
        source_file = ""
    if source_file:
        candidate = UPLOAD_DIR / Path(source_file).name
        if candidate.is_file():
            candidate.unlink(missing_ok=True)

    title = activity.title
    db.delete(activity)
    db.commit()
    return _message(f"Тренування «{title}» видалено, пробіг кросівок перераховано")


# --------------------------------------------------------------------------- #
#  shoes: create / update / archive / delete
# --------------------------------------------------------------------------- #
@app.post("/shoes")
def create_shoe(
    name: str = Form(...),
    brand: str = Form(""),
    max_distance_km: str = Form("700"),
    current_distance_km: str = Form("0"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    name = name.strip()
    if not name:
        return _error("Назва кросівок не може бути порожньою")

    max_distance = _parse_float(max_distance_km, 700.0)
    if max_distance <= 0:
        max_distance = 700.0
    initial = max(0.0, _parse_float(current_distance_km, 0.0))

    db.add(
        Shoe(
            name=name[:120],
            brand=brand.strip()[:120],
            max_distance_km=round(max_distance, 1),
            current_distance_km=round(initial, 2),
            is_active=True,
        )
    )
    db.commit()
    return _message(f"Кросівки «{name}» додано")


@app.get("/shoes/{shoe_id}/edit", response_class=HTMLResponse)
def shoe_edit_form(
    shoe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    shoe = db.get(Shoe, shoe_id)
    if shoe is None:
        raise HTTPException(status_code=404, detail="Кросівки не знайдено")
    return templates.TemplateResponse(
        request=request,
        name="shoe_edit.html",
        context={"shoe": shoe, "message": message, "error": error},
    )


@app.post("/shoes/{shoe_id}/edit")
def shoe_update(
    shoe_id: int,
    name: str = Form(...),
    brand: str = Form(""),
    max_distance_km: str = Form("700"),
    current_distance_km: str = Form("0"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shoe = db.get(Shoe, shoe_id)
    if shoe is None:
        return _error("Кросівки не знайдено")

    name = name.strip()
    if not name:
        return _redirect(f"/shoes/{shoe_id}/edit?error={quote('Назва не може бути порожньою')}")

    max_distance = _parse_float(max_distance_km, 700.0)
    if max_distance <= 0:
        max_distance = 700.0

    shoe.name = name[:120]
    shoe.brand = brand.strip()[:120]
    shoe.max_distance_km = round(max_distance, 1)
    shoe.current_distance_km = round(max(0.0, _parse_float(current_distance_km, 0.0)), 2)
    db.commit()
    return _message(f"Кросівки «{shoe.name}» оновлено")


@app.post("/shoes/{shoe_id}/toggle")
def toggle_shoe(shoe_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    shoe = db.get(Shoe, shoe_id)
    if shoe is None:
        return _error("Кросівки не знайдено")
    shoe.is_active = not shoe.is_active
    db.commit()
    state = "активовано" if shoe.is_active else "архівовано"
    return _message(f"Кросівки «{shoe.name}» {state}")


@app.post("/shoes/{shoe_id}/delete")
def delete_shoe(shoe_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    shoe = db.get(Shoe, shoe_id)
    if shoe is None:
        return _error("Кросівки не знайдено")

    db.execute(update(Activity).where(Activity.shoe_id == shoe_id).values(shoe_id=None))
    name = shoe.name
    db.delete(shoe)
    db.commit()
    return _message(f"Кросівки «{name}» видалено (тренування лишились без прив'язки)")


# --------------------------------------------------------------------------- #
#  exports
# --------------------------------------------------------------------------- #
@app.get("/export/activities.csv")
def export_activities_csv(db: Session = Depends(get_db)) -> Response:
    activities = (
        db.execute(
            select(Activity).options(joinedload(Activity.shoe)).order_by(Activity.date.asc())
        )
        .scalars()
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "id",
            "title",
            "date",
            "distance_km",
            "duration_sec",
            "duration_hms",
            "avg_pace_sec_per_km",
            "avg_pace_mmss",
            "avg_hr",
            "max_hr",
            "elevation_gain_m",
            "shoe",
        ]
    )
    for activity in activities:
        writer.writerow(
            [
                activity.id,
                activity.title,
                activity.date.strftime("%Y-%m-%d %H:%M"),
                f"{activity.distance_km:.3f}",
                activity.duration_sec,
                fmt_duration(activity.duration_sec),
                f"{activity.avg_pace_sec:.1f}",
                fmt_pace(activity.avg_pace_sec),
                activity.avg_hr or "",
                activity.max_hr or "",
                f"{activity.elevation_gain:.1f}",
                activity.shoe.name if activity.shoe else "",
            ]
        )

    filename = f"PaceLab-activities-{datetime.now():%Y%m%d-%H%M}.csv"
    content = ("\ufeff" + buffer.getvalue()).encode("utf-8")  # BOM keeps Excel happy
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/backup.json")
def export_backup_json(db: Session = Depends(get_db)) -> Response:
    shoes = db.execute(select(Shoe).order_by(Shoe.id)).scalars().all()
    activities = (
        db.execute(select(Activity).options(joinedload(Activity.shoe)).order_by(Activity.id))
        .scalars()
        .all()
    )

    payload = {
        "app": "PaceLab",
        "version": 2,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "shoes": [
            {
                "id": shoe.id,
                "name": shoe.name,
                "brand": shoe.brand,
                "max_distance_km": shoe.max_distance_km,
                "current_distance_km": shoe.current_distance_km,
                "is_active": shoe.is_active,
            }
            for shoe in shoes
        ],
        "activities": [
            {
                "id": activity.id,
                "title": activity.title,
                "date": activity.date.isoformat(sep=" "),
                "distance_km": activity.distance_km,
                "duration_sec": activity.duration_sec,
                "avg_pace_sec": activity.avg_pace_sec,
                "avg_hr": activity.avg_hr,
                "max_hr": activity.max_hr,
                "elevation_gain": activity.elevation_gain,
                "shoe_id": activity.shoe_id,
                "shoe_name": activity.shoe.name if activity.shoe else None,
                "file_hash": activity.file_hash,
                "points": activity.points,
                "splits": activity.splits,
            }
            for activity in activities
        ],
    }

    filename = f"PaceLab-backup-{datetime.now():%Y%m%d-%H%M}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
#  analytics, heatmap, route builder, challenge goals
# --------------------------------------------------------------------------- #
#: Київ (центр) — точка за замовчуванням для всіх карт без треку
KIEV_CENTER = (50.4501, 30.5234)


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    zones = hr_zone_analysis(db)
    fitness = fitness_fatigue(db, days=90)
    form = form_status(fitness)
    records = personal_records(db)
    recovery = acwr(db)
    rest = recovery_estimate(db)
    ef_series = ef_trend(db, days=30)
    vdot = vdot_analysis(db)
    badges = achievements(db)
    # коли в базі немає пер-точкового пульсу — замінюємо HR-блоки темповими зонами й GAP
    has_point_hr = has_point_hr_data(db)
    tempo = tempo_zone_distribution(db, vdot["vdot"])
    gap_series = gap_trend(db, days=30)
    heatmap = training_heatmap(db, months=12)
    routes = matched_runs(db)
    shoe_wear = shoe_resource(db)
    foster = foster_load(db)
    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "message": message,
            "error": error,
            "nav_active": "analytics",
            "zones": zones,
            "has_point_hr": has_point_hr,
            "tempo": tempo,
            "fitness": fitness,
            "form": form,
            "records": records,
            "recovery": recovery,
            "rest": rest,
            "ef_series": ef_series,
            "gap_series": gap_series,
            "vdot": vdot,
            "badges": badges,
            "heatmap": heatmap,
            "routes": routes,
            "shoe_wear": shoe_wear,
            "foster": foster,
        },
    )


@app.get("/heatmap", response_class=HTMLResponse)
def heatmap_page(
    request: Request,
    db: Session = Depends(get_db),
    year: int | None = None,
    month: int | None = None,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    years = heatmap_years(db)
    selected_year = year if year in years else None
    selected_month = month if month and 1 <= month <= 12 else None
    tracks = heatmap_tracks(db, selected_year, selected_month)
    return templates.TemplateResponse(
        request=request,
        name="heatmap.html",
        context={
            "message": message,
            "error": error,
            "nav_active": "heatmap",
            "tracks": tracks,
            "years": years,
            "selected_year": selected_year,
            "selected_month": selected_month,
            "default_center": list(KIEV_CENTER),
        },
    )


@app.get("/routes/builder", response_class=HTMLResponse)
def route_builder_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="routes_builder.html",
        context={
            "message": message,
            "error": error,
            "nav_active": "routes",
            "default_center": list(KIEV_CENTER),
        },
    )


@app.post("/api/route")
async def api_route(request: Request) -> JSONResponse:
    """Build a foot route via OSRM (server-side proxy keeps CORS/keys simple)."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body from the client
        return JSONResponse({"ok": False, "error": "Некоректний JSON запиту"}, status_code=400)

    waypoints = payload.get("waypoints") if isinstance(payload, dict) else None
    try:
        result = build_route(waypoints)
    except RouteInputError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RouteError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    except Exception as exc:  # noqa: BLE001 - never break the page
        return JSONResponse({"ok": False, "error": f"Не вдалося побудувати маршрут: {exc}"}, status_code=500)

    return JSONResponse({"ok": True, **result})


# --------------------------------------------------------------------------- #
#  планувальник гонки (GPX-траса → розкладка по кілометрах)
# --------------------------------------------------------------------------- #
@app.get("/race-planner", response_class=HTMLResponse)
def race_planner_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="race_planner.html",
        context={
            "message": message,
            "error": error,
            "nav_active": "planner",
            "plan": None,
            "form": {"negative_split": 2},
        },
    )


@app.post("/race-planner", response_class=HTMLResponse)
async def race_planner_submit(
    request: Request,
    file: UploadFile = File(...),
    target_time: str = Form(""),
    base_pace: str = Form(""),
    negative_split: str = Form("2"),
) -> HTMLResponse:
    """Побудувати розкладку гонки за GPX-трасою, цільовим часом або базовим темпом."""
    name = file.filename or "course.gpx"
    context: dict = {
        "nav_active": "planner",
        "plan": None,
        "message": None,
        "error": None,
        "form": {
            "filename": name,
            "target_time": target_time,
            "base_pace": base_pace,
            "negative_split": negative_split,
        },
    }

    def render() -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="race_planner.html", context=context)

    content = await file.read()
    if not content:
        context["error"] = "Файл порожній — завантаж .gpx трасу майбутньої гонки."
        return render()
    if Path(name).suffix.lower() != ".gpx":
        context["error"] = "Планувальник приймає лише .gpx (експорт траси з карти або годинника)."
        return render()

    target_sec = _parse_time_sec(target_time)
    base_gap = _parse_pace_sec(base_pace)
    if target_sec is None and base_gap is None:
        context["error"] = "Вкажи цільовий час фінішу (напр. 3:30:00) або базовий темп (напр. 5:00)."
        return render()

    try:
        course = _parse_gpx(io.BytesIO(content), Path(name).stem)
        plan = plan_race(
            course.points,
            target_time_sec=target_sec,
            base_gap_sec=base_gap,
            negative_split_pct=_parse_float(negative_split, 2.0),
        )
    except ParseError as exc:
        context["error"] = str(exc)
        return render()
    except Exception as exc:  # noqa: BLE001 - keep the page alive on odd files
        context["error"] = f"Не вдалося побудувати розкладку: {exc}"
        return render()

    context["plan"] = plan
    context["form"]["course_name"] = course.title
    context["form"]["mode"] = "time" if target_sec else "pace"
    return render()


@app.post("/challenges/goals")
def update_challenge_goals(
    goal_month_distance_km: str = Form("100"),
    goal_month_elevation_m: str = Form("1000"),
    goal_month_runs: str = Form("12"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _save_goals(
        db,
        {
            "goal_month_distance_km": goal_month_distance_km,
            "goal_month_elevation_m": goal_month_elevation_m,
            "goal_month_runs": goal_month_runs,
        },
    )
    return _message("Цілі челенджів оновлено")


@app.get("/manifest.json")
def pwa_manifest() -> Response:
    """Serve the PWA manifest from the app root (nicer scope than /static)."""
    manifest = (STATIC_DIR / "manifest.json").read_text(encoding="utf-8")
    return Response(content=manifest, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> Response:
    """Service worker must be served from the root scope to control the app."""
    script = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    return Response(content=script, media_type="application/javascript")

