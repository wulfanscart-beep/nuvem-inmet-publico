"""Executa a coleta inicial e mantém o acervo e o relatório atualizados."""

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from functools import partial
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from nuvem_inmet.collectors import InmetConventionalCSV
from nuvem_inmet.query import Query
from nuvem_inmet.query.monthly import export_monthly
from nuvem_inmet.storage import Storage
from nuvem_inmet.sync import _uncovered
from nuvem_inmet.validation import utc_hour

_LOG = logging.getLogger(__name__)
_DAY = timedelta(days=1)
_RETRY = timedelta(minutes=15)
_DOWNLOAD_LIMIT = 32 * 2**20
_PUBLIC_HISTORY_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/saraivaufc/"
    "conventional-weather-stations-brazil/"
    "conventional_weather_stations_inmet_brazil_1961_2019.csv?raw=true&datasetVersionNumber=1"
)
_PUBLIC_HISTORY_BOUNDS = (datetime(1961, 1, 1, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC))
_PUBLIC_HISTORY_RANGES = ((0, 264), (116977623, 119382588))
_PUBLIC_HISTORY_SHA256 = "a34831f0e8ff5ef5c4a7931977ffbb572b2b774fe4a45ff95d18bb8a63697a14"


class _PublicHistoryCSV(InmetConventionalCSV):
    source = "inmet.bdmep.conventional.saraivaufc.v1"


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use um endereço HTTPS de CSV sem credenciais embutidas.")


class _HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, destination: Path) -> None:
    _validate_url(url)
    opener = build_opener(_HTTPSRedirect)
    try:
        with opener.open(url, timeout=30) as response, destination.open("wb") as output:
            remaining = _DOWNLOAD_LIMIT
            while chunk := response.read(min(remaining + 1, 64 * 1024)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError("O CSV remoto excedeu o limite de 32 MiB.")
                output.write(chunk)
    except HTTPError as error:
        raise ConnectionError(f"A fonte do CSV retornou HTTP {error.code}.") from error
    except (URLError, TimeoutError) as error:
        raise ConnectionError("Falha de conexão com a fonte do CSV.") from error


def _download_public_history(destination: Path) -> None:
    opener = build_opener(_HTTPSRedirect)
    checksum = sha256()
    try:
        with destination.open("wb") as output:
            for first, last in _PUBLIC_HISTORY_RANGES:
                request = Request(_PUBLIC_HISTORY_URL, headers={"Range": f"bytes={first}-{last}"})
                with opener.open(request, timeout=30) as response:
                    expected = f"bytes {first}-{last}/819326774"
                    if response.status != 206 or response.headers.get("Content-Range") != expected:
                        raise ValueError("A fonte histórica deixou de fornecer um recorte verificável da estação.")
                    length = last - first + 1
                    chunk = response.read(length + 1)
                    if len(chunk) != length:
                        raise ValueError("Recorte da fonte histórica incompleto.")
                    output.write(chunk)
                    checksum.update(chunk)
    except (URLError, TimeoutError) as error:
        raise ConnectionError("Falha ao consultar o espelho histórico do INMET.") from error
    if checksum.hexdigest() != _PUBLIC_HISTORY_SHA256:
        raise ValueError("O checksum do recorte histórico diverge da versão validada.")


def _windows(storage, source, station, available, end):
    recent = end - 7 * _DAY
    for lower, upper in available:
        old_until = min(recent, upper)
        if lower < old_until:
            covered = storage.coverage(source, station, lower, old_until)
            yield from _uncovered(lower, old_until, covered)
        if upper > recent:
            yield max(lower, recent), upper


def _update(
    storage: Storage, source: InmetConventionalCSV, station: str, start: datetime, end: datetime,
    *, prepare: Callable[[], None] | None = None,
) -> int:
    run_id = storage.begin_sync(source.source, "reconcile", start, end, station_codes=(station,))
    try:
        if prepare is not None:
            prepare()
        available = [
            (max(first, start), min(last, end))
            for first, last in source.available_ranges(station) if first < end and last > start
        ]
        if not available:
            raise ValueError(f"O CSV não oferece cobertura utilizável para a estação {station}.")
        windows = list(_windows(storage, source.source, station, available, end))
        stations = list(source.stations((station,))) if windows else []
        rows = source.observations_in_windows(station, windows)
        result = storage.apply_sync(
            run_id, stations, rows, start, end,
            windows=((station, lower, upper) for lower, upper in windows),
        )
    except (Exception, KeyboardInterrupt) as error:
        try:
            storage.fail_sync(run_id, str(error))
        except Exception as catalog_error:
            error.add_note(f"Não foi possível registrar a falha: {catalog_error}")
        raise
    return result.rows


def _write_report(path: Path, emit: Callable) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            count = emit(stream)
        os.replace(temporary, path)
        return count
    finally:
        temporary.unlink(missing_ok=True)


def _report(query: Query, station: str, start: datetime, end: datetime, path: Path) -> None:
    query.verify(station)
    _write_report(path, lambda stream: export_monthly(query, station, start, end, stream))


def _next_run(now: datetime, daily_at: time) -> datetime:
    today = datetime.combine(now.date(), daily_at, tzinfo=UTC)
    return today if today > now else today + _DAY


def run(
    root: Path,
    station: str,
    start: datetime,
    daily_at: time,
    *,
    files: list[Path],
    url: str | None,
    report: Path,
    once: bool = False,
) -> None:
    """Importa os CSVs e depois executa uma coleta por dia enquanto o processo roda."""
    if len(station) != 5 or not station.isascii() or not station.isdigit():
        raise ValueError("Use um código WMO convencional de cinco dígitos.")
    start = utc_hour(start)
    if start.date() >= datetime.now(UTC).date():
        raise ValueError("O início deve anteceder o dia UTC atual.")
    if url:
        _validate_url(url)
    history_start = max(start, _PUBLIC_HISTORY_BOUNDS[0])
    history_end = _PUBLIC_HISTORY_BOUNDS[1]
    use_public_history = station == "83222" and not files and history_start < history_end
    if not (files or url or use_public_history):
        raise ValueError("Informe o CSV em --bdmep-csv ou a fonte atualizada em --bdmep-url.")

    storage = Storage(root)
    pending_history = files if url else []
    while True:
        try:
            now = datetime.now(UTC)
            end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if use_public_history:
                if not storage.completed_sync(_PublicHistoryCSV.source, station, history_start, history_end):
                    with TemporaryDirectory(prefix="nuvem-inmet-") as directory:
                        downloaded = Path(directory) / "historico.csv"
                        rows = _update(
                            storage, _PublicHistoryCSV([downloaded]), station, history_start, history_end,
                            prepare=partial(_download_public_history, downloaded),
                        )
                        _LOG.info("Histórico público: %s linhas armazenadas para %s.", rows, station)
                _report(Query(root), station, start, end, report)
                _LOG.info("Relatório histórico disponível em %s.", report)
                use_public_history = False
            if pending_history:
                _update(storage, InmetConventionalCSV(pending_history), station, start, end)
                _report(Query(root), station, start, end, report)
                pending_history = []
            if url:
                with TemporaryDirectory(prefix="nuvem-inmet-") as directory:
                    downloaded = Path(directory) / "bdmep.csv"
                    rows = _update(
                        storage, InmetConventionalCSV([downloaded]), station, start, end,
                        prepare=partial(_download, url, downloaded),
                    )
            elif files:
                rows = _update(storage, InmetConventionalCSV(files), station, start, end)
            else:
                break
            query = Query(root)
            latest = query.status(station).last_observation
            if latest is None:
                raise ValueError(f"A fonte não trouxe nenhuma observação para {station}.")
            _report(query, station, start, end, report)
            _LOG.info("%s: %s linhas verificadas, última observação %s, relatório %s",
                      station, rows, latest.isoformat(), report)
        except Exception as error:
            if once:
                raise
            _LOG.warning("Atualização interrompida: %s. Nova tentativa em 15 minutos.", error)
            sleep(_RETRY.total_seconds())
        else:
            if once:
                return
            now = datetime.now(UTC)
            sleep((_next_run(now, daily_at) - now).total_seconds())
    if not once:
        raise ValueError(
            "Histórico disponível no acervo. Para atualizações diárias, informe --bdmep-url "
            "com um CSV atualizado da estação ou --bdmep-csv com um arquivo local que seja renovado."
        )
