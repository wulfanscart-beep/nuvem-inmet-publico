"""Modelos compartilhados pelo coletor, acervo e consumidores."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Station:
    code: str
    latitude: float | None
    longitude: float | None
    effective_from: datetime | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    station_code: str
    timestamp: datetime
    temperature: Decimal | None
    humidity: Decimal | None
