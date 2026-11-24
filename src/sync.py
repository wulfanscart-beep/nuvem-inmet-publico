"""Orquestra aquisição e persistência sem depender de um provedor específico."""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

from nuvem_inmet.models import Observation, Station
from nuvem_inmet.storage import ApplyResult, Storage
from nuvem_inmet.validation import normalize_observation, normalize_station, utc_interval

MODES = ("historical", "incremental", "reconcile")


class Source(Protocol):
    source: str

    def stations(self, codes: Sequence[str] | None = None) -> Iterable[Station]: ...

    def observations(
        self, codes: Sequence[str], start: datetime, end: datetime
    ) -> Iterable[Observation]: ...


def _uncovered(
    start: datetime, end: datetime, covered: Iterable[tuple[datetime, datetime]]
) -> Iterable[tuple[datetime, datetime]]:
    for left, right in covered:
        if start < left:
            yield start, left
        start = max(start, right)
    if start < end:
        yield start, end


def synchronize(
    storage: Storage,
    source: Source,
    mode: str,
    start: datetime,
    end: datetime,
    *,
    codes: Sequence[str] | None = None,
) -> ApplyResult:
    """Sincroniza um intervalo UTC semiaberto e registra o resultado da tentativa."""
    if mode not in MODES:
        raise ValueError(f"Modalidade desconhecida: {mode!r}")
    start, end = utc_interval(start, end)
    codes = tuple(dict.fromkeys(codes)) if codes is not None else None
    if codes == ():
        raise ValueError("Informe ao menos uma estação ou use a amostra do coletor.")
    run_id = storage.begin_sync(source.source, mode, start, end, station_codes=codes or ())
    try:
        stations = [
            normalize_station(station.code, station.latitude, station.longitude, station.effective_from)
            for station in source.stations(codes)
        ]
        found = {station.code for station in stations}
        if not found:
            raise ValueError("A fonte não retornou nenhuma estação para a seleção.")
        versions = {(station.code, station.effective_from) for station in stations}
        if len(versions) != len(stations):
            raise ValueError("A fonte retornou versões duplicadas do cadastro.")
        if codes is not None and found != set(codes):
            raise ValueError(
                f"Seleção divergente no cadastro: ausentes={sorted(set(codes) - found)}, "
                f"inesperadas={sorted(found - set(codes))}."
            )

        grouped = defaultdict(list)
        windows = []
        for code in sorted(found):
            covered = [] if mode == "reconcile" else storage.coverage(
                source.source, code, start, end
            )
            for left, right in _uncovered(start, end, covered):
                windows.append((code, left, right))
                grouped[left, right].append(code)

        def observations() -> Iterable[Observation]:
            for (left, right), requested in sorted(grouped.items()):
                allowed = set(requested)
                for row in source.observations(tuple(requested), left, right):
                    observation = normalize_observation(
                        row.station_code, row.timestamp, row.temperature, row.humidity
                    )
                    if observation.station_code not in allowed or not left <= observation.timestamp < right:
                        raise ValueError("A fonte retornou uma observação fora da janela solicitada.")
                    yield observation

        return storage.apply_sync(run_id, stations, observations(), start, end, windows=windows)
    except (Exception, KeyboardInterrupt) as error:
        try:
            storage.fail_sync(run_id, str(error))
        except Exception as catalog_error:
            error.add_note(f"Não foi possível registrar a falha no catálogo: {catalog_error}")
        raise
