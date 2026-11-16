"""Безпечна міграція історичних дистанцій PaceLab під парсер v2.

Що робить скрипт:

1. Створює резервну копію БД через ``VACUUM INTO '_backup/runtracker_pre_migration.db'``.
2. Додає нові колонки ``distance_km_raw``, ``distance_km_clean``, ``parser_version``,
   ``srpe`` та ``is_race``, якщо їх ще немає.
3. Перераховує очищену дистанцію кожного треку (паузи < 0.5 м/с та GPS-«телепорти»
   відкидаються), зберігає старе значення в ``distance_km_raw``, пише очищене в
   ``distance_km_clean`` і оновлює ``distance_km`` та похідний ``avg_pace_sec`` —
   усе в одній транзакції.
4. Перераховує залежні спліти (зберігає назад у ``raw_points_json``), а також
   сумарний пробіг кожної пари кросівок. PR та CTL/ATL рахуються на льоту з
   оновлених даних, тож вони узгоджуються автоматично.

За замовчуванням — dry-run: лише звіт ``id | назва | було -> стало`` без запису.

Запуск із фактичним записом::

    .venv\\Scripts\\python.exe tools\\migrate_distances.py --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from app.parser import PARSER_VERSION, samples_from_point_dicts, splits_from_point_dicts  # noqa: E402

DB_PATH = PROJECT / "PaceLab.db"
BACKUP_DIR = PROJECT / "_backup"
BACKUP_PATH = BACKUP_DIR / "runtracker_pre_migration.db"

#: нові колонки активностей (назва, SQL-тип) — узгоджено з app/db.py
NEW_COLUMNS = [
    ("distance_km_raw", "FLOAT"),
    ("distance_km_clean", "FLOAT"),
    ("parser_version", "INTEGER"),
    ("srpe", "INTEGER"),
    ("is_race", "BOOLEAN DEFAULT 0"),
]


def backup_database() -> Path:
    """Створити копію БД через ``VACUUM INTO`` (нічого не перезаписуючи)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_PATH
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = BACKUP_DIR / f"runtracker_pre_migration_{stamp}.db"
        print(f"! {BACKUP_PATH.name} вже існує — бекап у {target.name}")
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.execute(f"VACUUM INTO '{target.as_posix()}'")
    finally:
        connection.close()
    print(f"✓ Резервна копія БД: {target.relative_to(PROJECT)} ({target.stat().st_size} байт)")
    return target


def ensure_columns(connection: sqlite3.Connection) -> list[str]:
    """Додати відсутні колонки; повернути перелік щойно створених."""
    existing = {row[1] for row in connection.execute("PRAGMA table_info('activities')")}
    added: list[str] = []
    for column, sql_type in NEW_COLUMNS:
        if column in existing:
            continue
        connection.execute(f"ALTER TABLE activities ADD COLUMN {column} {sql_type}")
        added.append(column)
    return added


def _clean_metrics(points: list[dict]) -> tuple[float, int] | None:
    """Очищені ``(дистанція_км, час_руху_сек)`` треку або ``None`` без GPS.

    Час руху береться без пауз — інакше темп, спліти та PR/RSS не зійдуться з
    новою дистанцією.
    """
    samples = samples_from_point_dicts(points)
    if not samples or samples[-1][0] <= 0:
        return None
    return round(samples[-1][0] / 1000, 3), int(round(samples[-1][1]))


def build_plan(connection: sqlite3.Connection) -> list[dict]:
    """Порахувати, що саме зміниться (без запису в БД)."""
    plan: list[dict] = []
    rows = connection.execute(
        "SELECT id, title, distance_km, duration_sec, shoe_id, raw_points_json "
        "FROM activities ORDER BY id"
    ).fetchall()
    for activity_id, title, distance_km, duration_sec, shoe_id, raw_points_json in rows:
        try:
            payload = json.loads(raw_points_json or "{}")
        except ValueError:
            payload = {}
        points = payload.get("points") or []

        metrics = _clean_metrics(points)
        if metrics is None:
            clean_km = float(distance_km or 0.0)
            moving_sec = int(duration_sec or 0)
        else:
            clean_km, moving_sec = metrics
            if moving_sec <= 0:
                moving_sec = int(duration_sec or 0)

        new_pace = round(moving_sec / clean_km, 1) if clean_km > 0 and moving_sec else None
        changed = abs(clean_km - float(distance_km or 0.0)) >= 0.005
        plan.append(
            {
                "id": activity_id,
                "title": title,
                "old_km": float(distance_km or 0.0),
                "new_km": clean_km,
                "new_duration": moving_sec,
                "new_pace": new_pace,
                "shoe_id": shoe_id,
                "points": points,
                "payload": payload,
                "changed": changed,
            }
        )
    return plan


def print_report(plan: list[dict]) -> None:
    print("\nid | назва | було -> стало")
    print("-" * 68)
    for item in plan:
        mark = "  " if item["changed"] else "= "
        print(f"{mark}{item['id']:>3} | {item['title'][:32]:<32} | "
              f"{item['old_km']:.3f} -> {item['new_km']:.3f} км")
    changed = [item for item in plan if item["changed"]]
    print("-" * 68)
    print(f"Всього тренувань: {len(plan)} · змінять дистанцію: {len(changed)}")


def apply_plan(connection: sqlite3.Connection, plan: list[dict]) -> None:
    """Записати зміни в одній транзакції та перерахувати кросівки."""
    connection.execute("BEGIN")
    try:
        for item in plan:
            payload = item["payload"]
            payload["splits"] = splits_from_point_dicts(item["points"])
            payload["parser_version"] = PARSER_VERSION
            connection.execute(
                "UPDATE activities SET distance_km_raw = ?, distance_km_clean = ?, "
                "distance_km = ?, duration_sec = ?, "
                "avg_pace_sec = COALESCE(?, avg_pace_sec), "
                "parser_version = ?, raw_points_json = ? WHERE id = ?",
                (
                    item["old_km"],
                    item["new_km"],
                    item["new_km"],
                    item["new_duration"],
                    item["new_pace"],
                    PARSER_VERSION,
                    json.dumps(payload, ensure_ascii=False),
                    item["id"],
                ),
            )

        # сумарний пробіг кожної пари кросівок — з очищених дистанцій
        connection.execute(
            "UPDATE shoes SET current_distance_km = COALESCE(("
            "  SELECT ROUND(SUM(a.distance_km), 2) FROM activities a WHERE a.shoe_id = shoes.id"
            "), 0)"
        )
    except Exception:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def verify_migration() -> None:
    """Переконатися, що зміни справді на диску (окреме з'єднання)."""
    connection = sqlite3.connect(DB_PATH)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info('activities')")}
        migrated = connection.execute(
            "SELECT COUNT(*) FROM activities WHERE parser_version IS NOT NULL"
        ).fetchone()[0]
    finally:
        connection.close()
    missing = [column for column, _ in NEW_COLUMNS if column not in columns]
    if missing:
        raise SystemExit(f"✗ Міграція не збереглася — бракує колонок: {', '.join(missing)}")
    print(f"✓ Перевірка на диску: колонки на місці, parser_version у {migrated} тренувань.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Міграція історичних дистанцій PaceLab")
    parser.add_argument("--apply", action="store_true", help="записати зміни (без прапорця — dry-run)")
    parser.add_argument("--no-backup", action="store_true", help="не робити резервну копію БД")
    args = parser.parse_args()

    if not DB_PATH.is_file():
        print(f"✗ БД не знайдено: {DB_PATH}")
        return 1

    print(f"=== PaceLab · міграція дистанцій (parser v{PARSER_VERSION}) ===")
    if args.apply:
        if not args.no_backup:
            backup_database()
    else:
        print("(режим dry-run — зміни не записуються; додай --apply для запису)")

    connection = sqlite3.connect(DB_PATH)
    connection.isolation_level = None  # авто-commit: керуємо транзакцією явно
    # журнал у пам'яті: інакше SQLite створює поряд файл ``*.db-journal``, без
    # якого транзакція «проходить», але на диск нічого не записується
    connection.execute("PRAGMA journal_mode=MEMORY")
    try:
        if args.apply:
            added = ensure_columns(connection)
            if added:
                print(f"✓ Додано колонки: {', '.join(added)}")

        plan = build_plan(connection)
        print_report(plan)

        if args.apply:
            apply_plan(connection, plan)
            verify_migration()
            shoes = connection.execute(
                "SELECT name, current_distance_km FROM shoes ORDER BY id"
            ).fetchall()
            print("\n✓ Пробіг кросівок перераховано:")
            for name, distance in shoes:
                print(f"    {name}: {round(float(distance or 0.0), 2)} км")
            print("\n✓ Міграцію завершено (спліти, PR, CTL/ATL узгоджено з новими дистанціями).")
        else:
            print("\n(dry-run) Запусти з --apply, щоб застосувати зміни.")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
