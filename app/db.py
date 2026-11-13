"""Database bootstrap: engine, session factory, declarative base, migrations."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "runtracker.db"

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


def _apply_light_migrations() -> None:
    """Tiny in-place migrations for databases created by older versions."""
    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(activities)")}
        if columns and "file_hash" not in columns:
            connection.exec_driver_sql("ALTER TABLE activities ADD COLUMN file_hash VARCHAR(64)")
            connection.commit()


def init_db() -> None:
    """Create tables (no demo data is seeded — the app starts empty)."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _apply_light_migrations()
