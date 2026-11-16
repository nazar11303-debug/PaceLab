"""ORM models: activities and shoes."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Shoe(Base):
    __tablename__ = "shoes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    brand: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    max_distance_km: Mapped[float] = mapped_column(Float, default=700.0, nullable=False)
    current_distance_km: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    activities: Mapped[list["Activity"]] = relationship(back_populates="shoe")

    @property
    def wear_percent(self) -> float:
        if not self.max_distance_km:
            return 0.0
        return min(100.0, round(self.current_distance_km / self.max_distance_km * 100, 1))

    @property
    def remaining_km(self) -> float:
        return max(0.0, round(self.max_distance_km - self.current_distance_km, 1))

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "brand": self.brand,
            "max_distance_km": self.max_distance_km,
            "current_distance_km": round(self.current_distance_km, 2),
            "is_active": self.is_active,
            "wear_percent": self.wear_percent,
            "remaining_km": self.remaining_km,
        }


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_pace_sec: Mapped[float] = mapped_column(Float, nullable=False)
    avg_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_gain: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    shoe_id: Mapped[int | None] = mapped_column(ForeignKey("shoes.id"), nullable=True)
    raw_points_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: JSON-рядок з погодою на годину старту (Open-Meteo), None якщо не вдалось отримати
    weather_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Оригінальна (неочищена) дистанція треку — для порівняння після міграції
    distance_km_raw: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Очищена дистанція (паузи та GPS-«телепорти» відкинуто)
    distance_km_clean: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Версія парсера, яким оброблено трек (None — завантажено до появи версій)
    parser_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Суб'єктивна важкість тренування за шкалою Борга (1–10), вводить користувач
    srpe: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Позначка «змагання» — такі зусилля мають вищу вагу для оцінки VDOT
    is_race: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    shoe: Mapped[Shoe | None] = relationship(back_populates="activities")

    # ---------------------------------------------------------------- helpers
    @property
    def points(self) -> list[dict]:
        try:
            return json.loads(self.raw_points_json or "{}").get("points", [])
        except (ValueError, AttributeError):
            return []

    @property
    def splits(self) -> list[dict]:
        try:
            return json.loads(self.raw_points_json or "{}").get("splits", [])
        except (ValueError, AttributeError):
            return []

    @property
    def has_gps(self) -> bool:
        return any(p.get("lat") is not None and p.get("lon") is not None for p in self.points)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "date": self.date.isoformat(sep=" ") if self.date else None,
            "distance_km": round(self.distance_km, 2),
            "duration_sec": self.duration_sec,
            "avg_pace_sec": round(self.avg_pace_sec, 1),
            "avg_hr": self.avg_hr,
            "max_hr": self.max_hr,
            "elevation_gain": round(self.elevation_gain, 1),
            "shoe_id": self.shoe_id,
            "shoe_name": self.shoe.name if self.shoe else None,
            "file_hash": self.file_hash,
            "weather_json": self.weather_json,
            "distance_km_raw": self.distance_km_raw,
            "distance_km_clean": self.distance_km_clean,
            "parser_version": self.parser_version,
            "srpe": self.srpe,
            "is_race": self.is_race,
        }


class AppSettings(Base):
    """Прості key/value налаштування застосунку (цілі челенджів тощо)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)

