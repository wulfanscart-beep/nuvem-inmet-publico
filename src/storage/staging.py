"""Disk-backed staging checks incoming rows before a writer lock is acquired."""

import sqlite3
from bisect import bisect_right
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from ..models import Observation
from .types import ConflictError, _utc


def stage_observations(
    db: sqlite3.Connection,
    observations: Iterable[Observation],
    ranges: dict[str, list[tuple[datetime, datetime]]],
) -> tuple[int, int]:
    db.execute(
        """
        CREATE TABLE observations (
            station_code TEXT NOT NULL,
            year INTEGER NOT NULL,
            hour INTEGER NOT NULL,
            temperature TEXT,
            humidity TEXT,
            PRIMARY KEY (station_code, year, hour)
        ) WITHOUT ROWID
        """
    )
    lefts = {code: [left for left, _ in intervals] for code, intervals in ranges.items()}
    rows = duplicates = 0
    with db:
        for record in observations:
            instant = _utc(record.timestamp)
            code = record.station_code
            index = bisect_right(lefts.get(code, ()), instant) - 1
            if index < 0 or instant >= ranges[code][index][1]:
                raise ValueError(f"observação fora da janela coletada: {code} em {instant.isoformat()}")
            if instant.minute or instant.second or instant.microsecond:
                raise ValueError(f"observação fora da hora inteira: {code} em {instant.isoformat()}")

            year = instant.year
            offset = instant - datetime(year, 1, 1, tzinfo=UTC)
            hour = offset.days * 24 + offset.seconds // 3600
            temperature = _decimal_text(record.temperature)
            humidity = _decimal_text(record.humidity)
            inserted = db.execute(
                "INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?, ?)",
                (code, year, hour, temperature, humidity),
            )
            if inserted.rowcount:
                rows += 1
                continue
            previous = db.execute(
                "SELECT temperature, humidity FROM observations WHERE station_code = ? AND year = ? AND hour = ?",
                (code, year, hour),
            ).fetchone()
            if tuple(map(_number, previous)) != (record.temperature, record.humidity):
                raise ConflictError(f"duplicata conflitante para {code} em {instant.isoformat()}")
            duplicates += 1
    return rows, duplicates


def stage_rows(db: sqlite3.Connection):
    rows = db.execute(
        """
        SELECT station_code, year, hour, temperature, humidity FROM observations
        ORDER BY station_code, year, hour
        """
    )
    for code, year, hour, temperature, humidity in rows:
        yield code, year, hour, _number(temperature), _number(humidity)


def _number(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("use um Decimal finito ou None para os valores meteorológicos")
    return str(value)
