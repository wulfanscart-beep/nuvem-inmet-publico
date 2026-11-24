"""Arquivos anuais das estações automáticas.

Publicação oficial em https://portal.inmet.gov.br/dadoshistoricos.
"""

from __future__ import annotations

import codecs
import csv
import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from io import TextIOWrapper
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo

from nuvem_inmet.models import Observation, Station
from nuvem_inmet.validation import normalize_observation, normalize_station, utc_interval

_MEMBER_NAME = re.compile(
    r"INMET_[A-Z]+_(?P<state>[A-Z]{2})_(?P<code>[A-Z]\d{3})_.+_"
    r"(?P<first>\d{2}-\d{2}-\d{4})_A_(?P<last>\d{2}-\d{2}-\d{4})\.CSV",
    re.IGNORECASE | re.ASCII,
)
_CLOCK = re.compile(r"(?P<hour>[0-9]{2}):?(?P<minute>[0-9]{2})(?::00)?(?:\s*UTC)?")
_DATE_FIELDS = {"DATA", "DATA YYYY MM DD"}
_HOUR_FIELDS = {"HORA UTC"}
_TEMP_FIELDS = {"TEMPERATURA DO AR BULBO SECO HORARIA C", "TEMPERATURA DO AR BULBO SECO HORARIO C"}
_HUMIDITY_FIELDS = {"UMIDADE RELATIVA DO AR HORARIA", "UMIDADE RELATIVA DO AR HORARIO"}
_STATION_FIELDS = {"CODIGO WMO", "UF", "LATITUDE", "LONGITUDE"}
_BRAZILIAN_STATES = frozenset(
    "AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO".split()
)

Interval = tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class _Member:
    path: Path
    info: ZipInfo
    state: str
    code: str
    first_day: date
    last_day: date

    @property
    def interval(self) -> Interval:
        start = datetime.combine(self.first_day, time.min, tzinfo=UTC)
        end = datetime.combine(self.last_day + timedelta(days=1), time.min, tzinfo=UTC)
        return start, end


def _label(value: str) -> str:
    upper = unicodedata.normalize("NFKD", value.upper())
    plain = "".join(char for char in upper if not unicodedata.combining(char))
    return " ".join(re.findall(r"[A-Z0-9]+", plain))


def _numeric(text: str) -> Decimal | None:
    value = text.strip()
    if not value:
        return None
    try:
        number = Decimal(value.replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"Valor decimal inválido no arquivo INMET: {value!r}.") from error
    if not number.is_finite():
        raise ValueError(f"Valor decimal não finito no arquivo INMET: {value!r}.")
    return None if number == -9999 else number


def _timestamp(day: str, clock: str) -> datetime:
    text = day.strip().replace("/", "-")
    if re.fullmatch(r"[0-9]{2}-[0-9]{2}-[0-9]{4}", text):
        year, month, day_of_month = reversed(text.split("-"))
        text = f"{year}-{month}-{day_of_month}"
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise ValueError(f"Data inválida no arquivo INMET: {day!r}.")
    hour = _CLOCK.fullmatch(clock.strip().upper())
    if hour is None or hour["minute"] != "00":
        raise ValueError(f"Horário UTC incompatível com observações horárias: {clock!r}.")
    return datetime.combine(date.fromisoformat(text), time(int(hour["hour"])), tzinfo=UTC)


def _read_preamble(rows: Iterator[list[str]]) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    for row in rows:
        if not row:
            continue
        name = _label(row[0])
        if name in _DATE_FIELDS:
            return fields, [_label(cell) for cell in row]
        if name in _STATION_FIELDS:
            if name in fields or len(row) < 2:
                raise ValueError(f"Metadado inválido ou repetido: {row[0]!r}.")
            fields[name] = row[1].strip()
    raise ValueError("Cabeçalho de dados ausente no CSV oficial do INMET.")


def _column(header: list[str], alternatives: set[str]) -> int:
    matches = [index for index, name in enumerate(header) if name in alternatives]
    if len(matches) != 1:
        raise ValueError(f"Coluna ausente ou ambígua: {', '.join(sorted(alternatives))}.")
    return matches[0]


@contextmanager
def _read_member(
    member: _Member, archive: ZipFile | None = None,
) -> Iterator[Iterator[list[str]]]:
    container = ZipFile(member.path) if archive is None else nullcontext(archive)
    with container as zipped, zipped.open(member.info) as raw:
        prefix = raw.peek(4096)
        try:
            codecs.getincrementaldecoder("utf-8-sig")().decode(prefix)
        except UnicodeDecodeError:
            encoding = "cp1252"
        else:
            encoding = "utf-8-sig"
        with TextIOWrapper(raw, encoding=encoding, newline="") as stream:
            try:
                yield csv.reader(stream, delimiter=";", strict=True)
            except (UnicodeDecodeError, csv.Error, ValueError) as error:
                raise ValueError(f"{member.path.name}/{member.info.filename}: {error}") from error


def _station(member: _Member, fields: dict[str, str]) -> Station:
    if fields.get("CODIGO WMO") != member.code or fields.get("UF") != member.state:
        raise ValueError("O código WMO ou a UF do cabeçalho diverge do nome do CSV.")
    if not {"LATITUDE", "LONGITUDE"}.issubset(fields):
        raise ValueError("O cabeçalho da estação não informa os campos de coordenadas.")
    # A data do arquivo e a fundação da estação não estabelecem a vigência das coordenadas.
    return normalize_station(member.code, _numeric(fields["LATITUDE"]), _numeric(fields["LONGITUDE"]))


def _index_members(
    path: Path, zipped: ZipFile, *, allow_unknown: bool = True,
) -> Iterator[_Member]:
    for info in zipped.infolist():
        if info.is_dir():
            continue
        filename = PurePosixPath(info.filename).name
        match = _MEMBER_NAME.fullmatch(filename)
        if match is None:
            if not allow_unknown and filename.upper().startswith("INMET_") and filename.lower().endswith(".csv"):
                raise ValueError(f"Nome de CSV meteorológico não reconhecido: {info.filename!r}.")
            continue
        first, last = (
            datetime.strptime(match[key], "%d-%m-%Y").date() for key in ("first", "last")
        )
        if first > last:
            raise ValueError(f"Período inválido no nome do arquivo {info.filename!r}.")
        yield _Member(path, info, match["state"].upper(), match["code"].upper(), first, last)


def _ordered_members(code: str, members: Iterable[_Member]) -> list[_Member]:
    ordered = sorted(members, key=lambda item: item.first_day)
    if not ordered:
        raise ValueError(f"Estação {code!r} ausente nos ZIPs informados.")
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.last_day >= later.first_day:
            raise ValueError(f"Períodos de CSV sobrepostos para {code} nos ZIPs informados.")
    return ordered


def _ordered_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = previous_start, max(previous_end, end)
        else:
            merged.append((start, end))
    return merged


def _read_observations(
    member: _Member, windows: Iterable[Interval], archive: ZipFile | None = None,
) -> Iterator[Observation]:
    intervals = tuple(windows)
    starts = tuple(left for left, _ in intervals)
    with _read_member(member, archive) as rows:
        fields, header = _read_preamble(rows)
        _station(member, fields)
        hour_index = _column(header, _HOUR_FIELDS)
        temp_index = _column(header, _TEMP_FIELDS)
        humidity_index = _column(header, _HUMIDITY_FIELDS)
        last_index = max(hour_index, temp_index, humidity_index)
        previous = None
        for row in rows:
            if not any(cell.strip() for cell in row):
                continue
            if len(row) <= last_index:
                raise ValueError("Linha com colunas insuficientes.")
            at = _timestamp(row[0], row[hour_index])
            if not member.first_day <= at.date() <= member.last_day:
                raise ValueError(f"Horário {at} fora do período identificado pelo CSV.")
            if previous is not None and at < previous:
                raise ValueError(f"Horário {at} fora da ordem cronológica do CSV.")
            previous = at
            index = bisect_right(starts, at) - 1
            if index >= 0 and at < intervals[index][1]:
                yield normalize_observation(
                    member.code, at, _numeric(row[temp_index]), _numeric(row[humidity_index])
                )


class InmetArchive:
    """Lê ZIPs baixados do portal oficial, sem extrair arquivos ou usar rede.

    O nome do CSV identifica estação, UF e período coberto pelo arquivo. A
    seleção padrão usa até três códigos do Paraná presentes nos ZIPs fornecidos.
    A cobertura nominal de cada estação deve abranger toda a janela solicitada.
    """

    source = "inmet.historico"

    def __init__(self, paths: str | Path | Iterable[str | Path], *, station_limit: int = 3):
        if type(station_limit) is not int or station_limit < 1:
            raise ValueError("O limite de estações deve ser um inteiro positivo.")
        self.station_limit = station_limit
        sources = (paths,) if isinstance(paths, (str, Path)) else tuple(paths)
        if not sources:
            raise ValueError("Informe ao menos um ZIP histórico do INMET.")
        self._members: dict[str, list[_Member]] = defaultdict(list)
        for path in map(Path, sources):
            self._index(path)
        if not self._members:
            raise ValueError("Nenhum CSV histórico do INMET encontrado nos ZIPs informados.")

    def _index(self, path: Path) -> None:
        with ZipFile(path) as zipped:
            for member in _index_members(path, zipped):
                self._members[member.code].append(member)

    def _station_members(self, codes: Iterable[str] | None) -> Iterator[list[_Member]]:
        if codes is None:
            codes = sorted(
                code for code, members in self._members.items() if any(row.state == "PR" for row in members)
            )[:self.station_limit]
        if isinstance(codes, str):
            raise ValueError("Informe uma coleção de códigos WMO.")
        for code in sorted(set(codes)):
            yield _ordered_members(code, self._members.get(code, ()))

    def stations(self, codes: Iterable[str] | None = None) -> Iterator[Station]:
        for members in self._station_members(codes):
            member = members[-1]
            with _read_member(member) as rows:
                fields, _ = _read_preamble(rows)
                yield _station(member, fields)

    def observations(
        self, codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterator[Observation]:
        start, end = utc_interval(start, end)
        first_day = start.date()
        last_day = (end - timedelta(microseconds=1)).date()
        selected = []
        for members in self._station_members(codes):
            cursor = first_day.toordinal()
            for member in members:
                if member.last_day < first_day or member.first_day > last_day:
                    continue
                if member.first_day.toordinal() > cursor:
                    break
                cursor = member.last_day.toordinal() + 1
                selected.append(member)
            if cursor <= last_day.toordinal():
                missing = date.fromordinal(cursor)
                raise ValueError(f"A cobertura nominal de {members[0].code} não inclui {missing}.")

        for member in selected:
            yield from _read_observations(member, [(start, end)])


class NationalInmetArchive:
    """Indexa um ZIP anual e lê todas as UFs que constam no arquivo."""

    source = InmetArchive.source

    def __init__(self, path: str | Path, year: int):
        if type(year) is not int or not 1 <= year < 9999:
            raise ValueError("Informe um ano válido para o ZIP histórico nacional.")
        self.path = Path(path)
        self.year = year
        indexed = defaultdict(list)
        with ZipFile(self.path) as zipped:
            for member in _index_members(self.path, zipped, allow_unknown=False):
                if member.state not in _BRAZILIAN_STATES:
                    raise ValueError(f"UF inválida no CSV {member.info.filename!r}: {member.state!r}.")
                if member.first_day.year != year or member.last_day.year != year:
                    raise ValueError(f"Período fora de {year} no CSV {member.info.filename!r}.")
                indexed[member.code].append(member)
        if not indexed:
            raise ValueError(f"Nenhum CSV histórico de {year} encontrado em {self.path.name}.")
        self._members = {code: _ordered_members(code, indexed[code]) for code in sorted(indexed)}

    def stations(self) -> Iterator[Station]:
        """Lê o cadastro dos CSVs sem inferir a vigência das coordenadas."""
        with ZipFile(self.path) as zipped:
            for members in self._members.values():
                member = members[-1]
                with _read_member(member, zipped) as rows:
                    fields, _ = _read_preamble(rows)
                    yield _station(member, fields)

    def coverage(self) -> dict[str, tuple[Interval, ...]]:
        """Devolve intervalos nominais por código, com início inclusivo e fim exclusivo."""
        return {
            code: tuple(_ordered_intervals(member.interval for member in members))
            for code, members in self._members.items()
        }

    def observations(self, windows: Iterable[tuple[str, datetime, datetime]]) -> Iterator[Observation]:
        """Lê cada membro selecionado uma vez e retorna as linhas das janelas pedidas."""
        by_code = defaultdict(list)
        for code, start, end in windows:
            if code not in self._members:
                raise ValueError(f"Estação {code!r} ausente no ZIP anual.")
            by_code[code].append(utc_interval(start, end))
        if not by_code:
            return
        with ZipFile(self.path) as zipped:
            for code, requested in sorted(by_code.items()):
                intervals = _ordered_intervals(requested)
                for member in self._members[code]:
                    first, last = member.interval
                    clipped = [
                        (max(start, first), min(end, last))
                        for start, end in intervals
                        if start < last and end > first
                    ]
                    if clipped:
                        yield from _read_observations(member, clipped, zipped)
