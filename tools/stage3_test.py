"""Stage 3 autotests: PWA/mobile, new pages, migration, route generation, analytics.

Run against a running server with a fresh database:

    .venv\\Scripts\\python.exe -m uvicorn app.main:app
    .venv\\Scripts\\python.exe tools\\stage3_test.py

The test never deletes files: temporary artifacts go to ``_tmp/``.
"""
from __future__ import annotations

import json
import mimetypes
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"
PROJECT = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = PROJECT / "PaceLab.db"
SAMPLES = PROJECT / "sample_data"
WORK_DIR = PROJECT / "_tmp"
WORK_DIR.mkdir(exist_ok=True)

# щоб `import app.routing` працював незалежно від місця запуску скрипта
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

results: list[bool] = []
notes: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("PASS " if condition else "FAIL ") + label + ((" — " + detail) if detail else ""))
    results.append(bool(condition))
    return condition


def note(message: str) -> None:
    print("NOTE " + message)
    notes.append(message)


# --------------------------------------------------------------------------- #
#  HTTP helpers
# --------------------------------------------------------------------------- #
def get(path: str) -> tuple[int, str, dict[str, str]]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=25) as response:
            headers = {key.lower(): value for key, value in dict(response.headers).items()}
            return response.status, response.read().decode("utf-8", "replace"), headers
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in dict(exc.headers).items()}
        return exc.code, exc.read().decode("utf-8", "replace"), headers


def post_form(path: str, fields: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(
        BASE + path,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            body = {"ok": False, "error": "non-json error"}
        return exc.code, body


def post_file(path: str, fields: dict[str, str], file_path: pathlib.Path) -> tuple[int, str]:
    boundary = "----Stage3Boundary" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        BASE + path,
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
#  DB helpers
# --------------------------------------------------------------------------- #
def db_rows(sql: str, params: tuple = ()) -> list[dict]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def table_columns(table: str) -> list[str]:
    return [row["name"] for row in db_rows(f"PRAGMA table_info('{table}')")]


print("=== PaceLab Stage 3 autotests ===\n")

# --------------------------------------------------------------------------- #
#  1. базові сторінки та PWA
# --------------------------------------------------------------------------- #
status, html, _ = get("/healthz")
check("healthz 200", status == 200, f"HTTP {status}")

initial_activities = db_rows("SELECT COUNT(*) AS n FROM activities")[0]["n"]
initial_shoes = db_rows("SELECT COUNT(*) AS n FROM shoes")[0]["n"]
check("стартова БД порожня (без демо-даних)", initial_activities == 0 and initial_shoes == 0,
      f"activities={initial_activities}, shoes={initial_shoes}")

for path, marker in [
    ("/", "Стрік"),
    ("/analytics", "Пульсові зони"),
    ("/heatmap", "Мапа тренувань"),
    ("/routes/builder", "Конструктор маршрутів"),
    ("/manifest.json", "start_url"),
    ("/sw.js", "CACHE_NAME"),
    ("/static/style.css", "#fc4c02"),
    ("/static/logo.png", ""),
]:
    status, body, headers = get(path)
    expected_marker = marker in body if marker else True
    check(f"GET {path} → 200", status == 200 and expected_marker, f"HTTP {status}, marker={marker!r}")

# PWA / мобільна верстка
manifest = json.loads(get("/manifest.json")[1])
check("manifest: name/short_name/start_url/display",
      manifest.get("name") and manifest.get("short_name") and manifest.get("start_url") == "/"
      and manifest.get("display") == "standalone")
icons = manifest.get("icons") or []
sizes = {icon.get("sizes") for icon in icons}
check("manifest: іконки 192 та 512", "192x192" in sizes and "512x512" in sizes, str(sizes))

status, home_html, _ = get("/")
check("мобільна навігація (bottom-nav) у HTML", "bottom-nav" in home_html and 'href="/analytics"' in home_html)
check("PWA meta-теги", 'name="theme-color"' in home_html and 'rel="manifest"' in home_html
      and "apple-mobile-web-app-capable" in home_html)
_, css, _ = get("/static/style.css")
check("темна палітра в CSS", "#0d1117" in css and "#161b22" in css and "#fc4c02" in css)
check("адаптивні медіа-запити", "@media (max-width: 760px)" in css and ".bottom-nav" in css)

# start.bat
start_bat = (PROJECT / "start.bat").read_text(encoding="utf-8")
check("start.bat: host 0.0.0.0 і --reload", "--host 0.0.0.0" in start_bat and "--reload" in start_bat)
banner_text = (PROJECT / "tools" / "console_banner.ps1").read_text(encoding="utf-8-sig")
check("LAN-підказка для телефона (банер-хелпер)",
      "console_banner.ps1" in start_bat and "Для відкриття з телефона у Wi-Fi" in banner_text)
check("start.bat: активна перевірка /healthz (до 15 с)",
      "wait_for_server.ps1" in start_bat and "/healthz" in (PROJECT / "tools" / "wait_for_server.ps1").read_text(encoding="utf-8"))
check("start.bat: ExecutionPolicy Bypass для обох .ps1", start_bat.count("-ExecutionPolicy Bypass") >= 2)
check("start.bat: не закриває вікно при збої (if errorlevel 1 + pause)",
      "if errorlevel 1" in start_bat and "pause" in start_bat)
check("lan_ip.ps1 існує", (PROJECT / "tools" / "lan_ip.ps1").is_file())
check("wait_for_server.ps1 існує", (PROJECT / "tools" / "wait_for_server.ps1").is_file())

# --------------------------------------------------------------------------- #
#  2. імпорт + погода
# --------------------------------------------------------------------------- #
gpx_path = SAMPLES / "sample_run.gpx"
status, html = post_file("/upload", {}, gpx_path)
check("upload sample .gpx → 200 «Додано»", status == 200 and "Додано" in html, f"HTTP {status}")
count_after_first = db_rows("SELECT COUNT(*) AS n FROM activities")[0]["n"]
check("у БД 1 тренування", count_after_first == 1, f"activities={count_after_first}")

status, html = post_file("/upload", {}, gpx_path)
check("дубль відхилено", "вже є в історії" in html)

original_gpx = gpx_path.read_text(encoding="utf-8")
shifted = WORK_DIR / "sample_run_stage3_next_day.gpx"
shifted.write_text(original_gpx.replace("2026-11-12T", "2026-11-13T"), encoding="utf-8")
status, html = post_file("/upload", {}, shifted)
count_after_second = db_rows("SELECT COUNT(*) AS n FROM activities")[0]["n"]
check("друге (інше) тренування додано", status == 200 and count_after_second == 2, f"activities={count_after_second}")

activity_columns = table_columns("activities")
check("міграція: колонка weather_json існує", "weather_json" in activity_columns, str(activity_columns))
check("міграція: колонка file_hash існує", "file_hash" in activity_columns)

rows = db_rows("SELECT weather_json FROM activities")
weather_present = [row["weather_json"] for row in rows if row["weather_json"]]
if weather_present:
    weather = json.loads(weather_present[0])
    check("погода: є температура/вітер/опади",
          "temp_c" in weather and "wind_kmh" in weather and "precipitation_mm" in weather,
          str(weather))
    check("погода: бейдж у стрічці", "weather-chip" in get("/")[1])
else:
    note("погода не отримана (зовнішній API недоступний у цій мережі) — імпорт не зламався")

# --------------------------------------------------------------------------- #
#  3. стрік, челенджі, звіт за місяць
# --------------------------------------------------------------------------- #
status, home_html, _ = get("/")
check("стрік: блок на головній", "🔥 Стрік" in home_html and "week-grid" in home_html)
check("стрік: поточний тиждень має пробіжку", "week-day done" in home_html)
check("челенджі: блок + 3 прогрес-бари", home_html.count("challenge") >= 3 and "Челенджі місяця" in home_html)
check("звіт за місяць: блок і селектор", "Звіт за місяць" in home_html and "report_month" in home_html)

status, _ = post_form("/challenges/goals", {
    "goal_month_distance_km": "42",
    "goal_month_elevation_m": "300",
    "goal_month_runs": "5",
})
goals_rows = {row["key"]: row["value"] for row in db_rows("SELECT key, value FROM app_settings")}
check("цілі збережено в app_settings",
      goals_rows.get("goal_month_distance_km") == "42" and goals_rows.get("goal_month_runs") == "5",
      str(goals_rows))
_, home_html, _ = get("/")
check("нові цілі відображаються в UI", "ціль: 42" in home_html and "ціль: 5" in home_html)

current_month = db_rows("SELECT date FROM activities ORDER BY date DESC LIMIT 1")[0]["date"][:7]
status, report_html, _ = get(f"/?report_month={current_month}")
check("звіт за обраний місяць рендериться", status == 200 and "Звіт за місяць" in report_html
      and "Середній темп" in report_html, f"HTTP {status}")

# --------------------------------------------------------------------------- #
#  4. аналітика
# --------------------------------------------------------------------------- #
status, analytics_html, _ = get("/analytics")
check("аналітика: сторінка 200", status == 200, f"HTTP {status}")
for marker in ["Пульсові зони", "Форма і втома", "Особисті рекорди", "Відновлення та рекомендація", "ACWR", "Z1", "Z5"]:
    check(f"аналітика: блок «{marker}»", marker in analytics_html)
check("аналітика: рекорд 1 км пораховано", "1.0 км" in analytics_html and "record-empty" not in analytics_html.split("1.0 км")[1][:200])
check("аналітика: CTL/ATL/TSB у HTML", "CTL:" in analytics_html and "ATL:" in analytics_html and "TSB:" in analytics_html)

# --------------------------------------------------------------------------- #
#  5. heatmap
# --------------------------------------------------------------------------- #
status, heatmap_html, _ = get("/heatmap")
check("heatmap 200 + треки", status == 200 and "heatmap" in heatmap_html and "треків на карті" in heatmap_html)
check("heatmap: JSON треків вбудовано", "const tracks = [" in heatmap_html)
year = db_rows("SELECT date FROM activities LIMIT 1")[0]["date"][:4]
status, filtered_html, _ = get(f"/heatmap?year={year}&month=11")
check("heatmap фільтр за роком/місяцем", status == 200 and "треків на карті" in filtered_html)

# --------------------------------------------------------------------------- #
#  6. конструктор маршрутів: API + модуль
# --------------------------------------------------------------------------- #
status, body = post_json("/api/route", {"waypoints": [[50.517, 30.605]]})
check("/api/route: 1 точка → 400", status == 400 and body.get("ok") is False, f"HTTP {status}")

status, body = post_json("/api/route", {"waypoints": "bad"})
check("/api/route: некоректний формат → 400", status == 400)

status, body = post_json("/api/route", {"waypoints": [[50.517, 30.605], [50.5205, 30.6105], [50.5232, 30.5985]]})
if status == 200 and body.get("ok"):
    check("/api/route: маршрут згенеровано", body.get("distance_km", 0) > 0.1 and len(body.get("coords", [])) >= 2)
    check("/api/route: GPX у відповіді", "<gpx" in body.get("gpx", "") and "<trkpt" in body.get("gpx", ""))
    check("/api/route: набір висоти обчислено", isinstance(body.get("elevation_gain_m"), (int, float)))
else:
    note(f"/api/route: зовнішній OSRM недоступний (HTTP {status}: {body.get('error')}) — перевіряю генерацію на stub")

# модульний тест генерації маршруту з підміною мережі (детермінований)
import app.routing as routing  # noqa: E402

original_request_json = routing._request_json
stub_route = {
    "code": "Ok",
    "routes": [{
        "distance": 1234.5,
        "geometry": {"coordinates": [[30.605, 50.517], [30.6105, 50.5205], [30.5985, 50.5232]]},
    }],
}
stub_elevation = {"elevation": [120.0, 130.0, 128.0]}
routing._request_json = lambda url: stub_route if "/route/v1/" in url else stub_elevation  # type: ignore[assignment]
try:
    built = routing.build_route([[50.517, 30.605], [50.5232, 30.5985]])
    check("routing stub: дистанція з OSRM", abs(built["distance_m"] - 1234.5) < 1, str(built["distance_m"]))
    check("routing stub: геометрія ≥ 2 точок", len(built["coords"]) >= 2, str(len(built["coords"])))
    check("routing stub: набір висоти", built["elevation_gain_m"] == 10.0, str(built["elevation_gain_m"]))
    check("routing stub: валідний GPX", built["gpx"].startswith("<?xml") and built["gpx"].count("<trkpt") >= 2)
    check("routing stub: оцінка часу", built["estimated_duration_sec"] > 0)
finally:
    routing._request_json = original_request_json  # type: ignore[assignment]

for bad_waypoints, label in [
    ([[50.5, 30.6]], "1 точка"),
    ([[50.5, 30.6]] * 26, "26 точок"),
    ([[99.0, 30.6], [50.5, 30.6]], "широта 99°"),
]:
    try:
        routing.build_route(bad_waypoints)
        check(f"routing: відмова для {label}", False)
    except routing.RouteInputError:
        check(f"routing: відмова для {label}", True)

# --------------------------------------------------------------------------- #
#  7. безпечна міграція старої БД
# --------------------------------------------------------------------------- #
import sqlite3 as _sqlite3  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402

import app.db as db_module  # noqa: E402

legacy_path = WORK_DIR / f"legacy_migration_{uuid.uuid4().hex[:8]}.db"
legacy = _sqlite3.connect(legacy_path)
legacy.executescript(
    """
    CREATE TABLE activities (
        id INTEGER PRIMARY KEY, title TEXT, date DATETIME, distance_km FLOAT,
        duration_sec INTEGER, avg_pace_sec FLOAT, avg_hr INTEGER, max_hr INTEGER,
        elevation_gain FLOAT, shoe_id INTEGER, raw_points_json TEXT
    );
    CREATE TABLE shoes (
        id INTEGER PRIMARY KEY, name TEXT, brand TEXT, max_distance_km FLOAT,
        current_distance_km FLOAT, is_active BOOLEAN
    );
    INSERT INTO activities (title, date, distance_km, duration_sec, avg_pace_sec, raw_points_json)
        VALUES ('legacy run', '2026-01-01 10:00:00', 5.0, 1800, 360, '{}');
    """
)
legacy.commit()
legacy.close()

legacy_engine = create_engine(f"sqlite:///{legacy_path}")
original_engine = db_module.engine
db_module.engine = legacy_engine
try:
    db_module.init_db()
finally:
    db_module.engine = original_engine

legacy_check = _sqlite3.connect(legacy_path)
legacy_columns = {row[1] for row in legacy_check.execute("PRAGMA table_info(activities)")}
legacy_tables = {row[0] for row in legacy_check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
legacy_rows = legacy_check.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
legacy_check.close()

check("міграція: додано file_hash у стару БД", "file_hash" in legacy_columns)
check("міграція: додано weather_json у стару БД", "weather_json" in legacy_columns)
check("міграція: створено app_settings", "app_settings" in legacy_tables)
check("міграція: старі дані не втрачено", legacy_rows == 1)

status, _, _ = get("/activities/99999")
check("404 для неіснуючого тренування", status == 404, f"HTTP {status}")

passed = sum(1 for item in results if item)
print(f"\n{passed}/{len(results)} checks passed")
if notes:
    print("Примітки:")
    for item in notes:
        print(" ·", item)
sys.exit(0 if passed == len(results) else 1)

