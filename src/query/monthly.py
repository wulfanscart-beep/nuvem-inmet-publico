"""Resumo mensal de temperatura e umidade baseado nas leituras simultâneas."""

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TextIO

from nuvem_inmet.query import Query, Slot
from nuvem_inmet.validation import utc_interval

_MONTHLY_FIELDS = (
    "mes",
    "temperatura_media_c",
    "umidade_media_percentual",
    "temperatura_maxima_c",
    "umidade_na_referencia_percentual",
    "temperatura_insuflada_c",
    "umidade_no_insuflamento_percentual",
)


@dataclass(slots=True)
class _Month:
    month: str
    temperature_sum: Decimal = Decimal(0)
    humidity_sum: Decimal = Decimal(0)
    pairs: int = 0
    maximum: Decimal | None = None
    reference_humidity: Decimal | None = None

    def add(self, slot: Slot) -> None:
        observation = slot.observation
        if observation is None or not 10 <= slot.timestamp.hour <= 21:
            return
        temperature, humidity = observation.temperature, observation.humidity
        if temperature is None:
            return
        if self.maximum is None or temperature > self.maximum:
            self.maximum = temperature
            self.reference_humidity = humidity
        if humidity is not None and 24 <= temperature <= 50:
            self.temperature_sum += temperature
            self.humidity_sum += humidity
            self.pairs += 1

    def columns(self) -> tuple:
        return (
            self.month,
            _mean(self.temperature_sum, self.pairs, "0.01"),
            _mean(self.humidity_sum, self.pairs, "0.1"),
            self.maximum,
            self.reference_humidity,
            None,
            None,
        )


def _mean(total: Decimal, count: int, quantum: str) -> Decimal | None:
    if not count:
        return None
    return (total / count).quantize(Decimal(quantum), rounding=ROUND_HALF_UP)


def _months(slots: Iterable[Slot]) -> Iterator[_Month]:
    current = None
    current_key = None
    for slot in slots:
        year, month = slot.timestamp.year, slot.timestamp.month
        if current_key != (year, month):
            if current is not None:
                yield current
            current = _Month(f"{year:04d}-{month:02d}")
            current_key = year, month
        current.add(slot)
    if current is not None:
        yield current


def export_monthly(
    query: Query, station: str, start: datetime, end: datetime, output: TextIO,
) -> int:
    """Exporta um mês por linha. As colunas de insuflamento exigem a matriz externa."""
    start, end = utc_interval(start, end)
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(_MONTHLY_FIELDS)
    count = 0
    for month in _months(query.series(station, start, end)):
        writer.writerow(month.columns())
        count += 1
    return count


def export_national_monthly(
    query: Query,
    start: datetime,
    end: datetime,
    output: TextIO,
    *,
    stations: Iterable[str] | None = None,
    include_empty: bool = False,
) -> int:
    """Exporta um resumo por estação e mês, omitindo linhas sem dados por padrão."""
    start, end = utc_interval(start, end)
    codes = sorted({station.code for station in query.stations()} if stations is None else set(stations))
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("codigo_estacao", *_MONTHLY_FIELDS))
    count = 0
    for code in codes:
        for month in _months(query.series(code, start, end)):
            columns = month.columns()
            if include_empty or any(value is not None for value in columns[1:]):
                writer.writerow((code, *columns))
                count += 1
    return count
