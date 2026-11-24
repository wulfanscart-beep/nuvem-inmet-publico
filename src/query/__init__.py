"""Consulta e exportação de observações do acervo local sem acesso à rede."""

import csv
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from heapq import nsmallest
from math import asin, cos, isfinite, radians, sin, sqrt
from pathlib import Path
from typing import TextIO

from ..codec import FormatError, ObservationBlock
from ..models import Observation
from ..storage import BlockInfo, IntegrityError, StationInfo, Storage, SyncAttempt, SyncStatus
from ..validation import utc_datetime, utc_hour, utc_interval

_HOUR = timedelta(hours=1)
_EXPORT_FIELDS = (
    "station_code",
    "timestamp_utc",
    "temperature_c",
    "relative_humidity_percent",
    "recorded",
)


@dataclass(frozen=True, slots=True)
class Slot:
    timestamp: datetime
    observation: Observation | None


@dataclass(frozen=True, slots=True)
class Gap:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class NearbyStation:
    station: StationInfo
    distance_km: float


def _year_start(value: datetime) -> datetime:
    return datetime(value.year, 1, 1, tzinfo=UTC)


@contextmanager
def _reader(block: BlockInfo) -> Iterator[ObservationBlock]:
    try:
        with ObservationBlock(block.path) as reader:
            if (reader.station_id, reader.year) != (block.station_id, block.year):
                raise IntegrityError(f"O cabeçalho não corresponde ao catálogo para {block.path}.")
            yield reader
    except (OSError, FormatError) as error:
        raise IntegrityError(f"Não foi possível ler o bloco binário {block.path}.") from error


def _distance_km(latitude: float, longitude: float, other_latitude: float, other_longitude: float) -> float:
    latitude_delta = radians(other_latitude - latitude)
    longitude_delta = radians(other_longitude - longitude)
    chord = (
        sin(latitude_delta / 2) ** 2
        + cos(radians(latitude))
        * cos(radians(other_latitude))
        * sin(longitude_delta / 2) ** 2
    )
    return 2 * 6371.0088 * asin(sqrt(min(1.0, chord)))


class Query:
    """Abre um acervo existente em modo de leitura."""

    def __init__(self, root: str | Path):
        self._storage = Storage(Path(root), readonly=True)

    def stations(self, at: datetime | None = None) -> tuple[StationInfo, ...]:
        """Lista as estações com a localização conhecida na data consultada."""
        with self._storage.snapshot() as catalog:
            return catalog.stations(utc_datetime(at) if at is not None else None)

    def locations(self, code: str) -> tuple[StationInfo, ...]:
        """Lista o histórico de localizações registradas para a estação."""
        with self._storage.snapshot() as catalog:
            return catalog.locations(code)

    def nearest(
        self,
        latitude: float,
        longitude: float,
        *,
        at: datetime | None = None,
        limit: int = 1,
    ) -> list[NearbyStation]:
        """Ordena estações com coordenadas conhecidas pela distância geodésica."""
        if not isfinite(latitude) or not -90 <= latitude <= 90:
            raise ValueError("A latitude deve estar entre -90 e 90 graus.")
        if not isfinite(longitude) or not -180 <= longitude <= 180:
            raise ValueError("A longitude deve estar entre -180 e 180 graus.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("O limite deve ser um inteiro positivo.")
        candidates = (
            NearbyStation(station, _distance_km(latitude, longitude, station.latitude, station.longitude))
            for station in self.stations(at)
            if station.latitude is not None and station.longitude is not None
        )
        return nsmallest(limit, candidates, key=lambda item: (item.distance_km, item.station.code))

    def observation(self, code: str, at: datetime) -> Observation | None:
        """Lê uma hora UTC diretamente na posição correspondente do bloco."""
        at = utc_hour(at)
        with self._storage.snapshot() as catalog:
            station_id = catalog.station_id(code)
            if station_id is None:
                raise KeyError(code)
            block = catalog.block(station_id, at.year)
            if block is None:
                return None
            with _reader(block) as reader:
                values = reader.at((at - _year_start(at)) // _HOUR)
        return None if values is None else Observation(code, at, *values)

    def series(self, code: str, start: datetime, end: datetime) -> Iterator[Slot]:
        """Percorre todas as horas do intervalo [start, end) em uma versão do catálogo."""
        start, end = utc_interval(start, end)
        with self._storage.snapshot() as catalog:
            station_id = catalog.station_id(code)
            if station_id is None:
                raise KeyError(code)
            cursor = start
            while cursor < end:
                year_start = _year_start(cursor)
                stop = year_start.replace(year=cursor.year + 1) if cursor.year < end.year else end
                first = (cursor - year_start) // _HOUR
                last = (stop - year_start) // _HOUR
                block = catalog.block(station_id, cursor.year)
                if block is None:
                    for index in range(first, last):
                        yield Slot(year_start + index * _HOUR, None)
                else:
                    with _reader(block) as reader:
                        for index, values in enumerate(reader.range(first, last), first):
                            timestamp = year_start + index * _HOUR
                            observation = None if values is None else Observation(code, timestamp, *values)
                            yield Slot(timestamp, observation)
                cursor = stop

    def gaps(self, code: str, start: datetime, end: datetime) -> Iterator[Gap]:
        """Percorre intervalos consecutivos de horas sem registro, com fim exclusivo."""
        gap_start = None
        for slot in self.series(code, start, end):
            if slot.observation is None:
                if gap_start is None:
                    gap_start = slot.timestamp
            elif gap_start is not None:
                yield Gap(gap_start, slot.timestamp)
                gap_start = None
        if gap_start is not None:
            yield Gap(gap_start, utc_hour(end))

    def provenance(self, code: str, at: datetime) -> SyncAttempt | None:
        """Localiza a coleta responsável pelo registro armazenado."""
        at = utc_hour(at)
        with self._storage.snapshot() as catalog:
            station_id = catalog.station_id(code)
            if station_id is None:
                raise KeyError(code)
            block = catalog.block(station_id, at.year)
            if block is None:
                return None
            index = (at - _year_start(at)) // _HOUR
            with _reader(block) as reader:
                return None if reader.at(index) is None else catalog.provenance(block, index)

    def status(self, code: str | None = None) -> SyncStatus:
        """Informa a última tentativa, sincronização bem-sucedida e observação disponível."""
        with self._storage.snapshot() as catalog:
            return catalog.status(code)

    def verify(self, code: str | None = None) -> int:
        """Verifica os checksums do catálogo e devolve a quantidade de objetos validados."""
        with self._storage.snapshot() as catalog:
            return catalog.verify(code)

    def _export_rows(
        self, code: str, start: datetime, end: datetime
    ) -> Iterator[tuple[str, str, str | None, str | None, bool]]:
        for slot in self.series(code, start, end):
            observation = slot.observation
            temperature = observation.temperature if observation is not None else None
            humidity = observation.humidity if observation is not None else None
            yield (
                code,
                slot.timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
                str(temperature) if temperature is not None else None,
                str(humidity) if humidity is not None else None,
                observation is not None,
            )

    def export_csv(self, code: str, start: datetime, end: datetime, stream: TextIO) -> int:
        """Escreve um recorte CSV e devolve a quantidade de linhas de dados."""
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(_EXPORT_FIELDS)
        count = 0
        for code, timestamp, temperature, humidity, recorded in self._export_rows(code, start, end):
            writer.writerow((code, timestamp, temperature, humidity, "true" if recorded else "false"))
            count += 1
        return count

    def export_json(self, code: str, start: datetime, end: datetime, stream: TextIO) -> int:
        """Escreve um recorte JSON com decimais como texto e devolve a quantidade de registros."""
        stream.write("[")
        count = 0
        for values in self._export_rows(code, start, end):
            if count:
                stream.write(",")
            stream.write("\n")
            json.dump(dict(zip(_EXPORT_FIELDS, values, strict=True)), stream, ensure_ascii=False)
            count += 1
        stream.write("\n]\n" if count else "]\n")
        return count
