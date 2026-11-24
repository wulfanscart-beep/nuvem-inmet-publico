"""Projeção remota de observações históricas do INMET em um espelho Parquet fixo."""

from __future__ import annotations

import io
import os
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from http.client import HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from nuvem_inmet.models import Observation, Station
from nuvem_inmet.validation import normalize_observation, normalize_station, utc_interval


_DATASET = "https://www.kaggle.com/api/v1/datasets/download/mauricioelandim/brazil-rain-data-inmet/"
_CATALOG_COLUMNS = ("station_code", "latitude", "longitude")
_OBSERVATION_COLUMNS = ("station_code", "datetime", "dry_bulb_temp_C", "relative_humidity_pct")
_PROGRESS_BYTES = 25_000_000
_GROUPS_PER_BATCH = 6
_PARALLEL_REQUESTS = 8
_TRANSIENT_HTTP = {429, 500, 502, 503, 504}
_BACKOFF = (1, 3, 8, 20)
HISTORY_START = datetime(2020, 1, 1, tzinfo=UTC)
HISTORY_END = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Object:
    filename: str
    size: int
    etag: str

    @property
    def url(self) -> str:
        return f"{_DATASET}{self.filename}?raw=true&datasetVersionNumber=1"


_OBJECTS = (
    _Object("automaticas_processed.parquet", 2_016_590_528, '"8f7b2dad39086ab5671f4fa0e3c6f073"'),
    _Object("convencionais_processed.parquet", 104_721_027, '"195372793e65848dcef703d254d36c7c"'),
)


class _Transfer:
    def __init__(self, limit: int, on_progress: Callable[[int, int], None] | None = None):
        self.limit = limit
        self.used = 0
        self._requested = 0
        self._on_progress = on_progress
        self._next_report = _PROGRESS_BYTES

    def check(self, amount: int) -> None:
        if self._requested + amount > self.limit:
            raise ValueError(
                f"A projeção do espelho Parquet exige ao menos {self._requested + amount} bytes. "
                f"Limite de transferência: {self.limit} bytes."
            )

    def reserve(self, amount: int) -> None:
        self.check(amount)
        self._requested += amount

    def record(self, amount: int) -> None:
        self.used += amount
        if self._on_progress is not None and self.used >= self._next_report:
            self._next_report = self.used + _PROGRESS_BYTES
            self._on_progress(self.used, self.limit)


class _RangeFile(io.RawIOBase):
    def __init__(self, item: _Object, transfer: _Transfer, cache_dir: Path | None = None):
        super().__init__()
        self._item = item
        self._transfer = transfer
        self._url = item.url
        self._position = 0
        self._cache: list[tuple[int, int, bytes]] = []
        self._refresh_url = Lock()
        self._cache_dir = cache_dir

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        origins = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._item.size}
        if whence not in origins or origins[whence] + offset < 0:
            raise ValueError("Deslocamento inválido no arquivo Parquet.")
        self._position = origins[whence] + offset
        return self._position

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("Leitura de arquivo Parquet fechado.")
        first = self._position
        available = self._item.size - first
        size = available if size < 0 else min(size, available)
        if size <= 0:
            return b""
        for left, right, data in self._cache:
            if left <= first and first + size <= right:
                self._position += size
                return data[first - left:first - left + size]
        span = first, first + size
        data = self._load_chunk(span)
        if data is None:
            self._transfer.reserve(size)
            data = self._fetch(span, remember_redirect=True)
            self._transfer.record(len(data))
            self._save_chunk(span, data)
        self._position += size
        return data

    def _load_chunk(self, span: tuple[int, int]) -> bytes | None:
        if self._cache_dir is None:
            return None
        first, end = span
        path = self._cache_dir / f"{first}-{end}.range"
        try:
            with path.open("rb") as source:
                checksum = source.read(32)
                data = source.read(end - first + 1)
        except FileNotFoundError:
            return None
        if len(data) == end - first and sha256(data).digest() == checksum:
            return data
        return None

    def _save_chunk(self, span: tuple[int, int], data: bytes) -> None:
        if self._cache_dir is None:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        first, end = span
        path = self._cache_dir / f"{first}-{end}.range"
        stream = NamedTemporaryFile(dir=self._cache_dir, delete=False)
        temporary = Path(stream.name)
        try:
            with stream:
                stream.write(sha256(data).digest())
                stream.write(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _fetch(self, span: tuple[int, int], *, remember_redirect: bool = False) -> bytes:
        url = self._url
        try:
            return self._request_range(url, span, remember_redirect=remember_redirect)
        except HTTPError as error:
            if error.code not in (401, 403) or url == self._item.url:
                raise
            error.close()
            # Só a URL temporária expirou. Renove-a uma vez pelo endpoint fixo.
            with self._refresh_url:
                if self._url == url:
                    return self._request_range(self._item.url, span, remember_redirect=True)
            return self._request_range(self._url, span)

    def _request_range(self, url: str, span: tuple[int, int], *, remember_redirect: bool = False) -> bytes:
        first, end = span
        size = end - first
        last = end - 1
        request = Request(url, headers={
            "Range": f"bytes={first}-{last}",
            "Accept-Encoding": "identity",
            "User-Agent": "Nuvem-INMET",
        })
        with _open_with_retry(request) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise ValueError("O espelho redirecionou a leitura para fora de HTTPS.")
            expected = f"bytes {first}-{last}/{self._item.size}"
            if response.status != 206 or response.headers.get("Content-Range") != expected:
                raise ValueError("O espelho não confirmou o recorte de bytes solicitado.")
            if response.headers.get("ETag") != self._item.etag:
                raise ValueError("O espelho retornou uma versão do arquivo Parquet diferente da esperada.")
            data = response.read(size)
            if remember_redirect:
                location = urlsplit(response.geturl())
                if location.scheme == "https" and location.hostname == "storage.googleapis.com":
                    self._url = response.geturl()
        if len(data) != size:
            raise ValueError("O espelho retornou um recorte Parquet incompleto.")
        return data

    def prefetch(self, spans: Iterable[tuple[int, int]], workers: ThreadPoolExecutor) -> None:
        self._cache.clear()
        missing = []
        for span in _contiguous(spans):
            data = self._load_chunk(span)
            if data is None:
                missing.append(span)
            else:
                self._cache.append((*span, data))
        self._transfer.reserve(sum(end - first for first, end in missing))
        pending = {workers.submit(self._fetch, span): span for span in missing}
        failure = None
        for fetched in as_completed(pending):
            first, end = pending[fetched]
            try:
                data = fetched.result()
            except Exception as error:
                if failure is None:
                    failure = error
            else:
                self._transfer.record(len(data))
                self._save_chunk((first, end), data)
                self._cache.append((first, end, data))
        if failure is not None:
            self._cache.clear()
            raise failure

    def clear_cache(self) -> None:
        self._cache.clear()


def _open_with_retry(request: Request):
    for delay in (*_BACKOFF, None):
        try:
            return urlopen(request, timeout=30)
        except HTTPError as error:
            if error.code not in _TRANSIENT_HTTP or delay is None:
                raise
            retry_after = (error.headers or {}).get("Retry-After", "")
            error.close()
            if retry_after.isdigit():
                delay = min(60, max(delay, int(retry_after)))
        except (URLError, HTTPException, ConnectionError, TimeoutError):
            if delay is None:
                raise
        sleep(delay)


def _pyarrow():
    try:
        import pyarrow
        import pyarrow.parquet
    except ImportError as error:
        raise RuntimeError("Instale o extra [parquet] para importar o espelho Parquet.") from error
    return pyarrow


def _contiguous(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for first, end in sorted(spans):
        if merged and first <= merged[-1][1]:
            previous, previous_end = merged[-1]
            merged[-1] = previous, max(previous_end, end)
        else:
            merged.append((first, end))
    return merged


def _groups(file, columns, start: datetime, end: datetime, codes: set[str] | None):
    start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    for index in range(file.metadata.num_row_groups):
        group = file.metadata.row_group(index)
        fields = {group.column(i).path_in_schema: group.column(i) for i in range(group.num_columns)}
        bounds = fields["datetime"].statistics
        if bounds is not None and bounds.has_min_max and (bounds.max < start or bounds.min >= end):
            continue
        stations = fields["station_code"].statistics
        if codes is not None and stations is not None and stations.has_min_max:
            if not any(stations.min <= code <= stations.max for code in codes):
                continue
        spans = []
        for name in columns:
            field = fields[name]
            offset = field.dictionary_page_offset or field.data_page_offset
            spans.append((offset, offset + field.total_compressed_size))
        yield index, spans


def _station_codes(codes: Iterable[str] | None) -> set[str] | None:
    if codes is None:
        return None
    if isinstance(codes, str):
        raise ValueError("Informe os códigos das estações como uma coleção.")
    selected = set(codes)
    if not selected or any(not isinstance(code, str) or not code for code in selected):
        raise ValueError("Informe ao menos um código de estação válido.")
    return selected


class ParquetHistory:
    """Importa uma projeção nacional das estações automáticas e convencionais.

    Use uma única janela histórica por importação. Os grupos do espelho são
    organizados por estação e mesclam anos.
    """

    source = "inmet.kaggle.parquet.v1"

    def __init__(
        self,
        start: datetime,
        end: datetime,
        *,
        max_transfer_bytes: int = 800_000_000,
        on_progress: Callable[[int, int], None] | None = None,
        cache_dir: Path | None = None,
    ):
        self.start, self.end = utc_interval(start, end)
        if self.start < HISTORY_START or self.end > HISTORY_END:
            raise ValueError("O espelho cobre de 2020-01-01 até 2026-01-01 exclusivo nesta importação.")
        if type(max_transfer_bytes) is not int or max_transfer_bytes < 1:
            raise ValueError("O limite de transferência precisa ser um inteiro positivo.")
        self._transfer = _Transfer(max_transfer_bytes, on_progress)
        self._cache_dir = cache_dir
        self._files = None

    @property
    def bytes_transferred(self) -> int:
        return self._transfer.used

    def _object_cache(self, item: _Object) -> Path | None:
        if self._cache_dir is None:
            return None
        version = sha256(item.etag.encode("ascii")).hexdigest()[:16]
        return self._cache_dir / f"{item.filename}-{version}"

    def remove_completed_cache(self) -> None:
        for item in _OBJECTS:
            directory = self._object_cache(item)
            if directory is not None and directory.is_dir():
                for path in directory.glob("*.range"):
                    path.unlink(missing_ok=True)

    def _scan(self, columns, start: datetime, end: datetime, codes: set[str] | None):
        if self._files is None:
            parquet = _pyarrow().parquet
            files = []
            for item in _OBJECTS:
                reader = _RangeFile(item, self._transfer, self._object_cache(item))
                files.append((parquet.ParquetFile(reader, pre_buffer=False), reader))
            self._files = files
        plans = [list(_groups(file, columns, start, end, codes)) for file, _ in self._files]
        self._transfer.check(sum(end - first for plan in plans for _, spans in plan for first, end in spans))
        with ThreadPoolExecutor(max_workers=_PARALLEL_REQUESTS) as workers:
            for (file, reader), plan in zip(self._files, plans):
                for offset in range(0, len(plan), _GROUPS_PER_BATCH):
                    groups = plan[offset:offset + _GROUPS_PER_BATCH]
                    reader.prefetch((span for _, spans in groups for span in spans), workers)
                    try:
                        yield from file.iter_batches(
                            batch_size=32_768,
                            row_groups=[index for index, _ in groups],
                            columns=list(columns),
                            use_threads=False,
                        )
                    finally:
                        reader.clear_cache()

    def stations(self, codes: Iterable[str] | None = None) -> Iterator[Station]:
        selected = _station_codes(codes)
        arrow = _pyarrow()
        positions = {}
        for batch in self._scan(_CATALOG_COLUMNS, self.start, self.end, selected):
            summary = arrow.Table.from_batches([batch]).group_by("station_code", use_threads=False).aggregate([
                ("latitude", "min"), ("latitude", "max"), ("longitude", "min"), ("longitude", "max"),
            ])
            for row in summary.to_pylist():
                code = row["station_code"]
                if selected is not None and code not in selected:
                    continue
                station = positions.setdefault(code, (set(), set()))
                station[0].update((row["latitude_min"], row["latitude_max"]))
                station[1].update((row["longitude_min"], row["longitude_max"]))
        for code, (latitudes, longitudes) in sorted(positions.items()):
            latitudes.discard(None)
            longitudes.discard(None)
            if len(latitudes) == 1 and len(longitudes) == 1:
                yield normalize_station(code, latitudes.pop(), longitudes.pop())
            else:
                # O espelho não informa a vigência de posições divergentes.
                yield normalize_station(code, None, None)

    def observations(
        self,
        codes: Iterable[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[Observation]:
        start, end = utc_interval(self.start if start is None else start, self.end if end is None else end)
        if start < self.start or end > self.end:
            raise ValueError("A consulta excede a janela configurada para o espelho Parquet.")
        selected = _station_codes(codes)
        for batch in self._scan(_OBSERVATION_COLUMNS, start, end, selected):
            for row in batch.to_pylist():
                code = row["station_code"]
                if selected is not None and code not in selected:
                    continue
                stamp = row["datetime"].replace(tzinfo=UTC)
                if start <= stamp < end:
                    yield normalize_observation(code, stamp, row["dry_bulb_temp_C"], row["relative_humidity_pct"])
