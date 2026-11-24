"""Atomic local storage for meteorological observations."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class IntegrityError(ValueError):
    """Catalog or binary content does not match its expected state."""


class ConflictError(ValueError):
    """Two records claim incompatible values for the same key."""


@dataclass(frozen=True, slots=True)
class StationInfo:
    station_id: int
    code: str
    latitude: float | None
    longitude: float | None
    effective_from: datetime | None
    collected_at: datetime | None
    source: str | None


@dataclass(frozen=True, slots=True)
class SyncAttempt:
    run_id: int
    source: str
    mode: str
    status: str
    start: datetime
    end: datetime
    started_at: datetime
    collected_at: datetime | None
    finished_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class BlockInfo:
    station_id: int
    year: int
    path: Path
    checksum: str
    run_id: int
    last_observation: datetime
    block_id: int


@dataclass(frozen=True, slots=True)
class SyncStatus:
    last_attempt: SyncAttempt | None
    last_success: SyncAttempt | None
    last_observation: datetime | None


@dataclass(frozen=True, slots=True)
class ApplyResult:
    stations: int
    rows: int
    changed_blocks: int
    duplicate_rows: int


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("a data e hora devem incluir fuso horário")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
