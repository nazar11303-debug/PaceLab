"""End-to-end smoke test for PaceLab (standard library only).

Checks (fresh database, running server on 127.0.0.1:8000):

* homepage renders and the app starts empty (no demo data);
* duplicate upload protection — same file (SHA-256) and same start+distance;
* pace formatting ``MM:SS /км`` and the inverted pace axis on the chart;
* activities CRUD with shoe mileage recalculation;
* shoes CRUD (create with initial mileage, edit, archive, delete);
* CSV history export and JSON database backup.

Usage::

    .venv\\Scripts\\python -m uvicorn app.main:app
    .venv\\Scripts\\python tools\\smoke_test.py
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
#: тимчасові файли тесту — тільки всередині проєкту (див. AGENTS.md)
WORK_DIR = PROJECT / "_tmp"
WORK_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
#  tiny HTTP helpers
# --------------------------------------------------------------------------- #
def get(path: str) -> tuple[int, str, dict[str, str]]:
    request = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)


def post_form(path: str, fields: dict[str, str]) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post_file(path: str, fields: dict[str, str], file_path: pathlib.Path) -> tuple[int, str]:
    boundary = "----PaceLabBoundary" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
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


def activity_count() -> int:
    return db_rows("SELECT COUNT(*) AS n FROM activities")[0]["n"]


def shoe(shoe_id: int) -> dict:
    rows = db_rows("SELECT * FROM shoes WHERE id = ?", (shoe_id,))
    return rows[0] if rows else {}


results: list[bool] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("PASS " if condition else "FAIL ") + label + ((" — " + detail) if detail else ""))
    results.append(condition)
    return condition


# --------------------------------------------------------------------------- #
#  0. preconditions
# --------------------------------------------------------------------------- #
print("=== PaceLab smoke test (етап 2) ===\n")
status, html, _ = get("/")
check("сервер відповідає на / (200)", status == 200, f"HTTP {status}")
check("застосунок стартує порожнім (без демо-даних)", activity_count() == 0, f"activities={activity_count()}")
check("стартові демо-кросівки відсутні", len(db_rows("SELECT * FROM shoes")) == 0)
check("кнопки експорту в UI", "/export/activities.csv" in html and "/export/backup.json" in html)
check("порожній стан стрічки", "Поки що порожньо" in html)
check("start.bat існує", (PROJECT / "start.bat").is_file())
check("start.bat містить --reload", "--reload" in (PROJECT / "start.bat").read_text(encoding="utf-8"))

# --------------------------------------------------------------------------- #
#  1. create shoes (one with initial mileage)
# --------------------------------------------------------------------------- #
status, _ = post_form("/shoes", {"name": "Test Shoes", "brand": "TestBrand", "max_distance_km": "500", "current_distance_km": "100"})
first_shoe = db_rows("SELECT * FROM shoes ORDER BY id")[0]
check("create shoe: створено пару", status in (200, 303) and len(db_rows("SELECT * FROM shoes")) == 1, f"HTTP {status}")
check("create shoe: початковий пробіг 100 км", abs(first_shoe["current_distance_km"] - 100.0) < 0.01, str(first_shoe["current_distance_km"]))

status, _ = post_form("/shoes", {"name": "Second Pair", "brand": "X", "max_distance_km": "300", "current_distance_km": "0"})
second_shoe = db_rows("SELECT * FROM shoes ORDER BY id")[1]
check("create shoe: друга пара створена", len(db_rows("SELECT * FROM shoes")) == 2)

# --------------------------------------------------------------------------- #
#  2. upload + duplicate protection
# --------------------------------------------------------------------------- #
gpx_path = SAMPLES / "sample_run.gpx"
status, html = post_file("/upload", {"shoe_id": str(first_shoe["id"])}, gpx_path)
check("upload .gpx: 200 + «Додано»", status == 200 and "Додано" in html, f"HTTP {status}")
check("upload .gpx: 1 тренування в БД", activity_count() == 1, f"activities={activity_count()}")
check("upload .gpx: пробіг кросівок 104.99 км", abs(shoe(first_shoe["id"])["current_distance_km"] - 104.989) < 0.02, str(shoe(first_shoe["id"])["current_distance_km"]))
files_after_first_upload = len(list((PROJECT / "uploads").glob("*.gpx")))

# 2a. exact same file -> rejected by SHA-256
status, html = post_file("/upload", {"shoe_id": ""}, gpx_path)
check("дубль (SHA-256): відхилено з повідомленням", "вже є в історії" in html and "Дубль не додано" in html)
check("дубль (SHA-256): кількість тренувань не змінилась", activity_count() == 1, f"activities={activity_count()}")
check("дубль (SHA-256): пробіг кросівок не змінився", abs(shoe(first_shoe["id"])["current_distance_km"] - 104.989) < 0.02)
check("дубль (SHA-256): файл не лишився в uploads", len(list((PROJECT / "uploads").glob("*.gpx"))) == files_after_first_upload)

# 2b. same start time + distance but different bytes -> rejected by fallback check
duplicate_variant = WORK_DIR / "sample_run_variant.gpx"
original = gpx_path.read_text(encoding="utf-8")
duplicate_variant.write_text(original.replace("<gpx ", "<!-- duplicate variant -->\n<gpx ", 1), encoding="utf-8")
check("варіант того ж треку має інший хеш", __import__("hashlib").sha256(duplicate_variant.read_bytes()).hexdigest() != __import__("hashlib").sha256(gpx_path.read_bytes()).hexdigest())
status, html = post_file("/upload", {"shoe_id": ""}, duplicate_variant)
check("дубль (час+дистанція): відхилено", "вже є в історії" in html)
check("дубль (час+дистанція): кількість тренувань не змінилась", activity_count() == 1)

# 2c. the same workout in another format (.fit) is also a duplicate (start+distance)
status, html = post_file("/upload", {"shoe_id": ""}, SAMPLES / "sample_run.fit")
check("дубль (той самий трек у .fit): відхилено", "вже є в історії" in html)
check("дубль (.fit): кількість тренувань не змінилась", activity_count() == 1, f"activities={activity_count()}")

# 2d. a genuinely different workout is accepted
shifted_gpx = WORK_DIR / "sample_run_next_day.gpx"
shifted_gpx.write_text(original.replace("2026-11-12T", "2026-11-13T"), encoding="utf-8")
status, html = post_file("/upload", {"shoe_id": ""}, shifted_gpx)
check("upload іншого .gpx: 200 + «Додано»", status == 200 and "Додано" in html, f"HTTP {status}")
check("upload іншого .gpx: 2 тренування в БД", activity_count() == 2, f"activities={activity_count()}")

# --------------------------------------------------------------------------- #
#  3. pace formatting + inverted chart axis
# --------------------------------------------------------------------------- #
status, html, _ = get("/activities/1")
check("сторінка тренування: 200", status == 200, f"HTTP {status}")
check("темп у форматі MM:SS /км (06:01 /км)", "06:01 /км" in html)
check("спліти теж у форматі MM:SS /км", html.count("06:01 /км") >= 3, f"знайдено {html.count('06:01 /км')}")
check("вісь темпу інвертована (reverse: true)", "reverse: true" in html)
check("тултіп форматує темп", "Темп: \" + fmtPace" in html)

status, html, _ = get("/activities/1/edit")
check("сторінка редагування тренування: 200", status == 200 and 'name="title"' in html, f"HTTP {status}")
check("форма редагування має datetime-local", 'type="datetime-local"' in html)

# --------------------------------------------------------------------------- #
#  4. activity edit: title/date/shoe switching with mileage recalculation
# --------------------------------------------------------------------------- #
activity_one = db_rows("SELECT * FROM activities ORDER BY id")[0]
status, _ = post_form(
    "/activities/1/edit",
    {"title": "Оновлений забіг", "date": "2026-11-13T07:15", "shoe_id": str(second_shoe["id"])},
)
activity_one_after = db_rows("SELECT * FROM activities WHERE id = 1")[0]
check("edit activity: назва оновлена", activity_one_after["title"] == "Оновлений забіг")
check("edit activity: дата оновлена", activity_one_after["date"].startswith("2026-11-13 07:15"), activity_one_after["date"])
check("edit activity: стара пара -4.99 км (100.0)", abs(shoe(first_shoe["id"])["current_distance_km"] - 100.0) < 0.02, str(shoe(first_shoe["id"])["current_distance_km"]))
check("edit activity: нова пара +4.99 км", abs(shoe(second_shoe["id"])["current_distance_km"] - 4.989) < 0.02, str(shoe(second_shoe["id"])["current_distance_km"]))

# shoe edit page + update
status, html, _ = get(f"/shoes/{second_shoe['id']}/edit")
check("сторінка редагування кросівок: 200", status == 200 and 'name="max_distance_km"' in html, f"HTTP {status}")
post_form(
    f"/shoes/{second_shoe['id']}/edit",
    {"name": "Second Pair v2", "brand": "X", "max_distance_km": "400", "current_distance_km": "4.99"},
)
second_after = shoe(second_shoe["id"])
check("edit shoe: назва/ресурс оновлені", second_after["name"] == "Second Pair v2" and abs(second_after["max_distance_km"] - 400) < 0.01, second_after["name"])

# archive toggle
post_form(f"/shoes/{first_shoe['id']}/toggle", {})
archived = shoe(first_shoe["id"])["is_active"] == 0
post_form(f"/shoes/{first_shoe['id']}/toggle", {})
check("archive toggle: архів/активація працює", archived and shoe(first_shoe["id"])["is_active"] == 1)

# --------------------------------------------------------------------------- #
#  5. delete activity -> mileage recalculated
# --------------------------------------------------------------------------- #
post_form("/activities/1/delete", {})
remaining = db_rows("SELECT * FROM activities ORDER BY id")
check("delete activity: тренування видалено", len(remaining) == 1, f"activities={len(remaining)}")
check("delete activity: пробіг другої пари повернувся до 0", abs(shoe(second_shoe["id"])["current_distance_km"]) < 0.01, str(shoe(second_shoe["id"])["current_distance_km"]))

# --------------------------------------------------------------------------- #
#  6. shoes delete -> activities keep working without shoe
# --------------------------------------------------------------------------- #
second_activity = remaining[0]
post_form(
    f"/activities/{second_activity['id']}/edit",
    {"title": "Другий забіг", "date": second_activity["date"][:16].replace(" ", "T"), "shoe_id": str(second_shoe["id"])},
)
check("edit activity: тренування прив'язане до другої пари", shoe(second_shoe["id"])["current_distance_km"] > 4.9)
post_form(f"/shoes/{second_shoe['id']}/delete", {})
activity_after = db_rows("SELECT * FROM activities WHERE id = ?", (second_activity["id"],))[0]
check("delete shoe: тренування лишилось без прив'язки", activity_after["shoe_id"] is None)
check("delete shoe: пара видалена", len(db_rows("SELECT * FROM shoes")) == 1)

# --------------------------------------------------------------------------- #
#  7. exports
# --------------------------------------------------------------------------- #
request = urllib.request.Request(BASE + "/export/activities.csv")
with urllib.request.urlopen(request, timeout=30) as response:
    csv_status = response.status
    csv_headers = {key.lower(): value for key, value in dict(response.headers).items()}
    csv_body = response.read().decode("utf-8-sig")
check("export CSV: 200", csv_status == 200, f"HTTP {csv_status}")
check("export CSV: content-type text/csv", "text/csv" in csv_headers.get("content-type", ""), csv_headers.get("content-type", ""))
check("export CSV: attachment", "attachment" in csv_headers.get("content-disposition", ""))
check("export CSV: заголовки та рядок даних", "distance_km" in csv_body and "Другий забіг" in csv_body)

request = urllib.request.Request(BASE + "/export/backup.json")
with urllib.request.urlopen(request, timeout=30) as response:
    json_status = response.status
    json_headers = {key.lower(): value for key, value in dict(response.headers).items()}
    backup = json.loads(response.read().decode("utf-8"))
check("export JSON: 200", json_status == 200, f"HTTP {json_status}")
check("export JSON: content-type application/json", "application/json" in json_headers.get("content-type", ""))
check("export JSON: є shoes та activities", isinstance(backup.get("shoes"), list) and isinstance(backup.get("activities"), list))
check("export JSON: тренування містить points і splits", bool(backup["activities"][0].get("points")) and bool(backup["activities"][0].get("splits")))

# 404 for a missing activity
status, _, _ = get("/activities/999")
check("неіснуюче тренування → 404", status == 404, f"HTTP {status}")

passed = sum(1 for item in results if item)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)

