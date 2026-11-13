"""Database bootstrap: engine, session factory, declarative base, migrations."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "runtracker.db"

logger = logging.getLogger("runtracker.db")

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


#: колонки, які могли з'явитися пізніше за першу версію БД
_LATE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "activities": [
        ("file_hash", "VARCHAR(64)"),
        ("weather_json", "TEXT"),
    ],
    "shoes": [],
    "app_settings": [],
}


def _table_columns(connection, table: str) -> set[str]:
    try:
        rows = connection.execute(text(f"PRAGMA table_info('{table}')")).fetchall()
    except Exception:  # noqa: BLE001 - таблиці може ще не існувати
        return set()
    return {row[1] for row in rows}


def _apply_column_migrations() -> None:
    """Add late columns with independent try/except so an old DB never breaks startup."""
    with engine.begin() as connection:
        for table, columns in _LATE_COLUMNS.items():
            if not columns:
                continue
            existing = _table_columns(connection, table)
            if not existing:  # таблицю ще не створено (create_all подбає сам)
                continue
            for column, sql_type in columns:
                if column in existing:
                    continue
                try:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
                    logger.info("Migration: added %s.%s", table, column)
                except Exception as exc:  # noqa: BLE001 - міграція не має валити застосунок
                    logger.warning("Migration skipped for %s.%s: %s", table, column, exc)


def init_db() -> None:
    """Create tables (no demo data) and safely migrate older databases."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _apply_column_migrations()
