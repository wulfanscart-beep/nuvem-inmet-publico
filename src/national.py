"""Carrega arquivos nacionais e acompanha novas observações do INMET."""

import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep

from nuvem_inmet.collectors import InmetConventionalCSV
from nuvem_inmet.collectors.archive import NationalInmetArchive
from nuvem_inmet.collectors.inmet_national import NationalINMETSource
from nuvem_inmet.query import Query
from nuvem_inmet.query.monthly import export_national_monthly
from nuvem_inmet.service import _download, _next_run, _write_report
from nuvem_inmet.storage import Storage
from nuvem_inmet.storage.catalog import _merged
from nuvem_inmet.sync import _uncovered
from nuvem_inmet.validation import utc_hour

_LOG = logging.getLogger(__name__)
_DAY = timedelta(days=1)
_COUNTRY = "*"
_MIRROR_SOURCE = "inmet.kaggle.parquet.v1"
_MIRROR_END = datetime(2026, 1, 1, tzinfo=UTC)
Interval = tuple[datetime, datetime]


def _attempt(storage: Storage, source: str, start: datetime, end: datetime, load: Callable):
    run_id = storage.begin_sync(source, "reconcile", start, end, station_codes=(_COUNTRY,))
    try:
        return load(run_id)
    except (Exception, KeyboardInterrupt) as error:
        try:
            storage.fail_sync(run_id, str(error))
        except Exception as catalog_error:
            error.add_note(f"Não foi possível registrar a falha: {catalog_error}")
        raise


def _collect_files(
    storage: Storage,
    source: NationalInmetArchive | InmetConventionalCSV,
    start: datetime,
    end: datetime,
    *,
    observations: Callable,
    review_recent: bool,
):
    available = source.coverage()
    review_from = end - 7 * _DAY if review_recent else end
    windows = []
    for code, intervals in sorted(available.items()):
        for first, last in intervals:
            left, right = max(start, first), min(end, last)
            if left >= right:
                continue
            stable_end = min(right, review_from)
            if left < stable_end:
                existing = storage.coverage(source.source, code, left, stable_end)
                windows.extend((code, a, b) for a, b in _uncovered(left, stable_end, existing))
            if right > review_from:
                windows.append((code, max(left, review_from), right))
    if not windows:
        return None

    selected = {code for code, _, _ in windows}

    def load(run_id):
        stations = (row for row in source.stations() if row.code in selected)
        return storage.apply_sync(
            run_id, stations, observations(windows), start, end, windows=windows,
        )

    return _attempt(storage, source.source, start, end, load)


def _sync_live(storage: Storage, source: NationalINMETSource, start: datetime, end: datetime):
    def load(run_id):
        stations = list(source.stations())
        if not stations:
            raise ValueError("O cadastro nacional do INMET veio vazio.")

        def rows():
            day = start
            while day < end:
                found = False
                for row in source.observations(day.date()):
                    found = True
                    yield row
                if not found:
                    raise ValueError(f"O INMET não retornou leituras nacionais para {day.date()}.")
                day += _DAY

        return storage.apply_sync(
            run_id, stations, rows(), start, end,
            windows=((station.code, start, end) for station in stations),
            record_windows=False,
        )

    result = _attempt(storage, source.source, start, end, load)
    _LOG.info("INMET nacional, %s a %s (fim exclusivo): %s registros de origem.",
              start.date(), end.date(), result.rows)
    return result


def _history_batches(
    storage: Storage, source: str, start: datetime, end: datetime, limit: int,
) -> Iterable[Interval]:
    if start >= end or not limit:
        return
    covered = _merged([
        *storage.completed_ranges(source, _COUNTRY, start, end),
        *storage.completed_ranges(_MIRROR_SOURCE, _COUNTRY, start, end),
    ])
    for left, right in _uncovered(start, end, covered):
        year_end = datetime(left.year + 1, 1, 1, tzinfo=UTC)
        until = min(right, left + limit * _DAY, year_end)
        yield left, until
        limit -= (until - left).days
        if not limit or until < right:
            return


def _collect_mirror(storage: Storage, start: datetime, end: datetime):
    from nuvem_inmet.collectors.parquet_history import ParquetHistory

    source = ParquetHistory(
        start, end,
        cache_dir=storage.root / ".cache-parquet-v1",
        on_progress=lambda used, limit: _LOG.info(
            "Espelho nacional: %.0f MB transferidos (teto %.0f MB).",
            used / 1_000_000, limit / 1_000_000,
        ),
    )

    def load(run_id):
        stations = list(source.stations())
        if not stations:
            raise ValueError("O espelho nacional veio sem estações.")
        return storage.apply_sync(
            run_id, stations, source.observations(), start, end,
            windows=((station.code, start, end) for station in stations),
            record_windows=False,
        )

    result = _attempt(storage, _MIRROR_SOURCE, start, end, load)
    try:
        source.remove_completed_cache()
    except OSError as error:
        _LOG.warning("Dados publicados. A remoção do cache temporário falhou: %s", error)
    return result


def _archive_year(path: Path) -> int:
    years = re.findall(r"(?<!\d)20\d{2}(?!\d)", path.stem)
    if len(years) != 1:
        raise ValueError(f"Identifique o ano no nome do ZIP histórico: {path.name}.")
    return int(years[0])


def run_national(
    root: Path,
    start: datetime,
    daily_at: time,
    *,
    archives: list[Path],
    conventional: list[Path],
    url: str | None,
    report: Path,
    offline: bool = False,
    mirror_history: bool = False,
    history_days: int = 31,
    once: bool = False,
) -> None:
    """Carrega a cobertura histórica disponível e mantém a coleta diária em execução."""
    start = utc_hour(start)
    if start.hour or start >= datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0):
        raise ValueError("Use uma data UTC anterior ao dia atual para a carga nacional.")
    if not 0 <= history_days <= 31:
        raise ValueError("Use de 0 a 31 dias por ciclo de carga histórica via API.")
    if offline and (url or mirror_history):
        raise ValueError("Para modo offline, informe apenas arquivos locais.")
    if offline and not (archives or conventional):
        raise ValueError("Informe --archive ou --bdmep-csv para a carga nacional offline.")

    storage = Storage(root)
    pending_archives = sorted(archives, key=_archive_year)
    mirror_start = max(start, datetime(2020, 1, 1, tzinfo=UTC))
    pending_mirror = mirror_history and mirror_start < _MIRROR_END
    while True:
        error = None
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            while pending_archives:
                path = pending_archives[0]
                year = _archive_year(path)
                left = max(start, datetime(year, 1, 1, tzinfo=UTC))
                right = min(end, datetime(year + 1, 1, 1, tzinfo=UTC))
                if left < right:
                    archive = NationalInmetArchive(path, year)
                    result = _collect_files(
                        storage, archive, left, right,
                        observations=archive.observations, review_recent=False,
                    )
                    if result is not None:
                        _LOG.info("Arquivo %s: %s registros.", path.name, result.rows)
                pending_archives.pop(0)
            if pending_mirror:
                if not storage.completed_sync(_MIRROR_SOURCE, _COUNTRY, mirror_start, _MIRROR_END):
                    _LOG.info("Iniciando a projeção do espelho nacional desde %s.", mirror_start.date())
                    result = _collect_mirror(storage, mirror_start, _MIRROR_END)
                    _LOG.info("Espelho nacional 2020–2025: %s registros.", result.rows)
                pending_mirror = False
            if conventional or url:
                with TemporaryDirectory(prefix="nuvem-inmet-") as directory:
                    sources = list(conventional)
                    if url:
                        path = Path(directory) / "bdmep.csv"
                        _download(url, path)
                        sources.append(path)
                    source = InmetConventionalCSV(sources)
                    result = _collect_files(
                        storage, source, start, end,
                        observations=source.observations_across_stations, review_recent=True,
                    )
                    if result is not None:
                        _LOG.info("BDMEP convencional: %s registros.", result.rows)
        except Exception as failure:
            error = failure

        if not offline:
            try:
                api = NationalINMETSource()
                recent = max(start, end - _DAY)
                if not storage.completed_sync(api.source, _COUNTRY, recent, end):
                    _sync_live(storage, api, recent, end)
                for left, right in _history_batches(storage, api.source, start, recent, history_days):
                    _sync_live(storage, api, left, right)
            except Exception as failure:
                if error is not None:
                    _LOG.warning("Falha na carga histórica ou no BDMEP: %s", error)
                error = failure

        try:
            query = Query(root)
            if query.status().last_observation is not None:
                count = _write_report(
                    report, lambda stream: export_national_monthly(query, start, end, stream),
                )
                _LOG.info("Relatório %s atualizado (%s pares estação-mês).", report, count)
        except Exception as failure:
            if error is not None:
                _LOG.warning("Falha na coleta: %s", error)
            error = failure
        if error is not None:
            if once:
                raise error
            _LOG.warning("Atualização incompleta: %s. Nova tentativa em 15 minutos.", error)
            sleep(15 * 60)
        elif once:
            return
        else:
            now = datetime.now(UTC)
            sleep((_next_run(now, daily_at) - now).total_seconds())
