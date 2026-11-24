"""Normalização sem inferência meteorológica nem arredondamento."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

from nuvem_inmet.models import Observation, Station

Number = Decimal | float | int | str


def utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("O horário precisa ter um fuso explícito.")
    return value.astimezone(UTC)


def utc_hour(value: datetime) -> datetime:
    value = utc_datetime(value)
    if value.minute or value.second or value.microsecond:
        raise ValueError("O horário precisa estar alinhado a uma hora UTC.")
    return value


def utc_interval(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = utc_hour(start), utc_hour(end)
    if start >= end:
        raise ValueError("O início deve anteceder o fim exclusivo.")
    return start, end


def _code(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError("Código de estação inválido.")
    return value


def _decimal(value: Number | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, float, int, str)):
        raise ValueError("Tipo de dado meteorológico inválido.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Valor meteorológico inválido.") from error
    if not number.is_finite():
        raise ValueError("O valor meteorológico deve ser finito.")
    return number


def _coordinate(value: Number | None, limit: float) -> float | None:
    number = _decimal(value)
    if number is None:
        return None
    if not -limit <= number <= limit:
        raise ValueError("Coordenada fora do intervalo permitido.")
    coordinate = float(number)
    if not isfinite(coordinate):
        raise ValueError("A coordenada deve ser finita.")
    return coordinate


def normalize_station(
    code: str,
    latitude: Number | None,
    longitude: Number | None,
    effective_from: datetime | None = None,
) -> Station:
    return Station(
        _code(code),
        _coordinate(latitude, 90),
        _coordinate(longitude, 180),
        None if effective_from is None else utc_datetime(effective_from),
    )


def normalize_observation(
    station_code: str,
    timestamp: datetime,
    temperature: Number | None,
    humidity: Number | None,
) -> Observation:
    temperature = _decimal(temperature)
    humidity = _decimal(humidity)
    if humidity is not None and not 0 <= humidity <= 100:
        raise ValueError("A umidade relativa deve estar entre 0 e 100%.")
    return Observation(_code(station_code), utc_hour(timestamp), temperature, humidity)
