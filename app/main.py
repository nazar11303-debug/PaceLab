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
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload

from .db import BASE_DIR, get_db, init_db
from .models import Activity, Shoe
from .parser import SUPPORTED_EXTENSIONS, ParseError, parse_activity_file

UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(title="RunTracker", description="Персональний аналіз бігових тренувань", lifespan=lifespan)
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


templates.env.filters.update(
    {
        "duration": fmt_duration,
        "pace": fmt_pace,
        "pace_km": fmt_pace_km,
        "km": fmt_km,
        "date": fmt_date,
        "datetime_local": fmt_datetime_local,
    }
)


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
    return {"status": "ok", "app": "runtracker"}


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    error: str | None = None,
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
        },
    )


@app.post("/upload")
async def upload_activity(
    file: UploadFile = File(...),
    shoe_id: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    original_name = file.filename or ""
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return _error("Підтримуються лише файли .gpx та .fit")

    content = await file.read()
    if not content:
        return _error("Файл порожній — завантажте справжній .gpx або .fit")

    file_hash = hashlib.sha256(content).hexdigest()

    # 1) same file already imported?
    existing = db.execute(select(Activity).where(Activity.file_hash == file_hash)).scalars().first()
    if existing is not None:
        return _error(_duplicate_message(existing))

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original_name).name)[:80] or f"activity{suffix}"
    stored_name = f"{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:8]}_{safe_name}"
    destination = UPLOAD_DIR / stored_name
    destination.write_bytes(content)

    try:
        payload = parse_activity_file(destination, filename_hint=original_name)
    except ParseError as exc:
        destination.unlink(missing_ok=True)
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 - keep the UI alive on odd files
        destination.unlink(missing_ok=True)
        return _error(f"Не вдалося розібрати файл: {exc}")

    # 2) same start time + distance already imported (different file bytes)?
    existing = _find_duplicate(db, file_hash, payload)
    if existing is not None:
        destination.unlink(missing_ok=True)
        return _error(_duplicate_message(existing))

    selected_shoe: Shoe | None = None
    shoe_id = (shoe_id or "").strip()
    if shoe_id.isdigit():
        selected_shoe = db.get(Shoe, int(shoe_id))

    activity = Activity(
        title=payload["title"][:200],
        date=payload["date"],
        distance_km=payload["distance_km"],
        duration_sec=payload["duration_sec"],
        avg_pace_sec=payload["avg_pace_sec"],
        avg_hr=payload["avg_hr"],
        max_hr=payload["max_hr"],
        elevation_gain=payload["elevation_gain"],
        shoe_id=selected_shoe.id if selected_shoe else None,
        file_hash=file_hash,
        raw_points_json=json.dumps(
            {
                "points": payload["points"],
                "splits": payload["splits"],
                "source_file": stored_name,
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

    summary = (
        f"Додано «{activity.title}» — {fmt_km(activity.distance_km)} км, "
        f"{fmt_duration(activity.duration_sec)}"
    )
    return _message(summary)


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

    return templates.TemplateResponse(
        request=request,
        name="activity.html",
        context={
            "activity": activity,
            "points": activity.points,
            "splits": activity.splits,
            "message": message,
            "error": error,
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

    filename = f"runtracker-activities-{datetime.now():%Y%m%d-%H%M}.csv"
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
        "app": "runtracker",
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

    filename = f"runtracker-backup-{datetime.now():%Y%m%d-%H%M}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
