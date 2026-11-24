"""Importa recortes semicolados de estações convencionais do BDMEP."""

from __future__ import annotations

import codecs
import csv
import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from io import TextIOWrapper
from pathlib import Path

from nuvem_inmet.models import Observation, Station
from nuvem_inmet.validation import normalize_observation, normalize_station, utc_interval

from .archive import _column, _label, _numeric, _timestamp


_WMO = re.compile(r"[0-9]{5}")
_WMO_IN_PREAMBLE = re.compile(r"\b(?:OMM|WMO)\s*:\s*([0-9]{5})\b", re.IGNORECASE)
_DAY = {"DATA", "DATA MEDICAO", "DATA DA MEDICAO", "DATA YYYY MM DD"}
_HOUR = {"HORA", "HORA UTC", "HORA MEDICAO", "HORA MEDICAO UTC", "HORA DA MEDICAO"}
_TEMP = {
    "TEMPBULBOSECO", "TEMP BULBO SECO", "TEMPERATURA DO AR BULBO SECO C",
    "TEMPERATURA DO AR BULBO SECO HORARIA C", "TEMPERATURA DO AR BULBO SECO HORARIO C",
}
_HUMIDITY = {
    "UMIDADERELATIVA", "UMIDADE RELATIVA", "UMIDADE RELATIVA DO AR",
    "UMIDADE RELATIVA DO AR HORARIA", "UMIDADE RELATIVA DO AR HORARIO",
}
_CODE = {"ESTACAO", "CODIGO ESTACAO", "CODIGO DA ESTACAO"}
_CODE_WMO = {"CODIGOOMM", "CODIGO OMM", "CODIGO WMO"}
_LAT = {"LATITUDE", "LATITUDE GRAUS"}
_LON = {"LONGITUDE", "LONGITUDE GRAUS"}
_Interval = tuple[datetime, datetime]


def _optional_column(header: list[str], names: set[str]) -> int | None:
    return _column(header, names) if names.intersection(header) else None


def _code(value: str) -> str:
    if not isinstance(value, str) or not _WMO.fullmatch(value):
        raise ValueError(f"Código OMM inválido para estação convencional: {value!r}.")
    return value


def _codes(values: Iterable[str] | None) -> set[str] | None:
    if isinstance(values, str):
        raise ValueError("Informe uma coleção de códigos OMM.")
    return None if values is None else {_code(value) for value in values}


def _cell(row: list[str], index: int | None) -> str:
    return "" if index is None else row[index].strip()


@dataclass(frozen=True)
class _Report:
    code: tuple[int | None, str]
    wmo: int | None
    day: int
    hour: int
    temperature: int
    humidity: int
    latitude: tuple[int | None, str]
    longitude: tuple[int | None, str]
    period: tuple[date, date] | None

    def station_code(self, row: list[str]) -> str:
        code_column, metadata = self.code
        found = {
            _code(value)
            for value in (_cell(row, code_column), _cell(row, self.wmo), metadata)
            if value
        }
        if len(found) != 1:
            raise ValueError("Código OMM ausente ou divergente no cadastro e na linha.")
        return found.pop()

    def station(self, code: str, row: list[str]) -> Station:
        coordinates = []
        for index, metadata in (self.latitude, self.longitude):
            value = _numeric(_cell(row, index))
            from_metadata = _numeric(metadata)
            if value is not None and from_metadata is not None and value != from_metadata:
                raise ValueError(f"Coordenadas divergentes no cadastro e na linha de {code}.")
            coordinates.append(from_metadata if value is None else value)
        return normalize_station(code, *coordinates)


def _preamble(rows: Iterator[list[str]]) -> _Report:
    fields: dict[str, str] = {}
    station_code = ""
    period = None
    for row in rows:
        if not row:
            continue
        header = [_label(cell) for cell in row]
        if _DAY.intersection(header):
            code = _optional_column(header, _CODE)
            wmo = _optional_column(header, _CODE_WMO)
            if not station_code and code is None and wmo is None:
                raise ValueError("Informe um CSV com código OMM nas linhas ou no cabeçalho.")
            return _Report(
                (code, station_code), wmo, _column(header, _DAY), _column(header, _HOUR),
                _column(header, _TEMP), _column(header, _HUMIDITY),
                (_optional_column(header, _LAT), fields.get("latitude", "")),
                (_optional_column(header, _LON), fields.get("longitude", "")), period,
            )
        name, _, value = row[0].partition(":")
        field = _label(name)
        value = value.strip() or _cell(row, 1 if len(row) > 1 else None)
        if field in _LAT:
            fields["latitude"] = value
        elif field in _LON:
            fields["longitude"] = value
        elif field in (_CODE | _CODE_WMO):
            match = _WMO_IN_PREAMBLE.search(value)
            declared_code = match.group(1) if match else value if _WMO.fullmatch(value) else ""
            if station_code and declared_code and station_code != declared_code:
                raise ValueError("Códigos OMM divergentes nos metadados do CSV.")
            station_code = declared_code or station_code
        elif field == "PERIODO SOLICITADO DOS DADOS":
            left, sep, right = value.partition(" a ")
            if not sep:
                raise ValueError("Período solicitado inválido nos metadados do CSV.")
            period = (_timestamp(left, "0000").date(), _timestamp(right, "0000").date())
            if period[0] > period[1]:
                raise ValueError("Período solicitado inválido nos metadados do CSV.")
    raise ValueError("Cabeçalho tabular do BDMEP ausente no CSV.")


@contextmanager
def _read(path: Path) -> Iterator[tuple[_Report, Iterator[list[str]]]]:
    with path.open("rb") as raw:
        try:
            codecs.getincrementaldecoder("utf-8-sig")().decode(raw.peek(4096))
        except UnicodeDecodeError:
            encoding = "cp1252"
        else:
            encoding = "utf-8-sig"
        with TextIOWrapper(raw, encoding=encoding, newline="") as stream:
            reader = csv.reader(stream, delimiter=";", strict=True)
            try:
                yield _preamble(reader), (row for row in reader if any(cell.strip() for cell in row))
            except (csv.Error, IndexError, ValueError) as exc:
                raise ValueError(f"{path.name}, linha {reader.line_num}: {exc}") from exc


def _merged_dates(intervals: list[tuple[int, int]]) -> Iterator[tuple[int, int]]:
    if intervals:
        ordered = iter(sorted(intervals))
        start, end = next(ordered)
        for left, right in ordered:
            if left > end + 1:
                yield start, end
                start, end = left, right
            else:
                end = max(end, right)
        yield start, end


def _merged_windows(windows: list[_Interval]) -> list[_Interval]:
    merged: list[_Interval] = []
    for left, right in sorted(windows):
        if merged and left <= merged[-1][1]:
            first, last = merged[-1]
            merged[-1] = first, max(last, right)
        else:
            merged.append((left, right))
    return merged


def _check_coverage(
    code: str,
    intervals: list[tuple[int, int]],
    windows: Iterable[tuple[datetime, datetime]],
) -> None:
    merged = list(_merged_dates(intervals))
    left_edges = [left for left, _ in merged]
    for start, end in windows:
        first = start.date().toordinal()
        last = (end - timedelta(hours=1)).date().toordinal()
        index = bisect_right(left_edges, first) - 1
        last_covered = merged[index][1] if index >= 0 else 0
        if last_covered < last:
            day = date.fromordinal(max(first, last_covered + 1))
            raise ValueError(f"Os CSVs de {code} não comprovam cobertura em {day}.")


def _observation(code: str, stamp: datetime, report: _Report, row: list[str]) -> Observation:
    return normalize_observation(
        code, stamp, _numeric(row[report.temperature]), _numeric(row[report.humidity])
    )


class InmetConventionalCSV:
    """Lê exportações convencionais com bulbo seco e umidade da mesma linha UTC."""

    source = "inmet.bdmep.conventional"

    def __init__(self, paths: str | Path | Iterable[str | Path]):
        paths = (paths,) if isinstance(paths, (str, Path)) else paths
        self._paths = tuple(map(Path, paths))
        if not self._paths:
            raise ValueError("Informe ao menos um CSV convencional exportado do BDMEP.")

    def stations(self, codes: Iterable[str] | None = None) -> Iterator[Station]:
        selected = _codes(codes)
        stations: dict[str, Station] = {}
        for path in self._paths:
            with _read(path) as (report, rows):
                for row in rows:
                    code = report.station_code(row)
                    if selected is not None and code not in selected:
                        continue
                    station = report.station(code, row)
                    if code in stations and stations[code] != station:
                        raise ValueError(f"Coordenadas divergentes sem data de vigência para {code}.")
                    stations[code] = station
        yield from (stations[code] for code in sorted(stations))

    def _scan(
        self,
        selected: set[str] | None,
        coverage: dict[str, list[tuple[int, int]]],
    ) -> Iterator[tuple[str, datetime, _Report, list[str]]]:
        for path in self._paths:
            with _read(path) as (report, rows):
                days_seen: dict[str, set[int]] = defaultdict(set)
                for row in rows:
                    code = report.station_code(row)
                    if selected is not None and code not in selected:
                        continue
                    stamp = _timestamp(row[report.day], row[report.hour])
                    if report.period is not None and not report.period[0] <= stamp.date() <= report.period[1]:
                        raise ValueError(f"Data de {code} fora do período declarado em {path.name}.")
                    days_seen[code].add(stamp.date().toordinal())
                    yield code, stamp, report, row
                for code, days in days_seen.items():
                    if report.period is None:
                        coverage[code].extend(_merged_dates([(day, day) for day in days]))
                    else:
                        first, last = report.period
                        coverage[code].append((first.toordinal(), last.toordinal()))

    def coverage(self, codes: Iterable[str] | None = None) -> dict[str, list[tuple[datetime, datetime]]]:
        """Retorna períodos comprovados. O início é inclusivo e o fim exclusivo."""
        coverage: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for _ in self._scan(_codes(codes), coverage):
            pass
        return {
            code: [
                (
                    datetime.combine(date.fromordinal(first), time(), tzinfo=UTC),
                    datetime.combine(date.fromordinal(last + 1), time(), tzinfo=UTC),
                )
                for first, last in _merged_dates(intervals)
            ]
            for code, intervals in coverage.items()
        }

    def available_ranges(self, code: str) -> list[tuple[datetime, datetime]]:
        """Retorna as janelas UTC comprovadas do código OMM solicitado."""
        return self.coverage([code]).get(code, [])

    def observations_in_windows(
        self, code: str, windows: Iterable[_Interval]
    ) -> Iterator[Observation]:
        """Lê uma vez os CSVs para todos os intervalos solicitados da estação."""
        code = _code(code)
        yield from self.observations_across_stations((code, start, end) for start, end in windows)

    def observations_across_stations(
        self, windows: Iterable[tuple[str, datetime, datetime]]
    ) -> Iterator[Observation]:
        """Percorre cada CSV uma vez e valida a cobertura de cada estação solicitada."""
        by_station: dict[str, list[_Interval]] = defaultdict(list)
        for code, start, end in windows:
            by_station[_code(code)].append(utc_interval(start, end))
        if not by_station:
            return

        intervals = {code: _merged_windows(requested) for code, requested in by_station.items()}
        starts = {code: [left for left, _ in merged] for code, merged in intervals.items()}
        coverage: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for code, stamp, report, row in self._scan(set(intervals), coverage):
            index = bisect_right(starts[code], stamp) - 1
            if index >= 0 and stamp < intervals[code][index][1]:
                yield _observation(code, stamp, report, row)
        for code in sorted(intervals):
            _check_coverage(code, coverage[code], intervals[code])

    def observations(
        self, codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterator[Observation]:
        start, end = utc_interval(start, end)
        selected = _codes(codes)
        if not selected:
            raise ValueError("Informe ao menos um código OMM para consultar as observações.")
        yield from self.observations_across_stations((code, start, end) for code in selected)
