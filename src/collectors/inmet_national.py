"""Leituras horárias do INMET em uma requisição nacional por dia UTC."""

import re
from collections.abc import Iterable, Iterator
from datetime import date

from nuvem_inmet.models import Observation, Station

from .inmet import INMETSource, _codes, _observation, _timestamp


class NationalINMETSource:
    """Consulta o cadastro automático e o recorte horário nacional diário.

    Cada instância mantém o cadastro da coleta. Esta rota ainda não tem
    cobertura convencional verificada. Se devolver um código OMM numérico,
    a coleta sinaliza a divergência e orienta usar o CSV convencional.
    """

    source = "inmet.apitempo.nacional"

    def __init__(
        self,
        *,
        timeout: float = 30,
        max_response_bytes: int = 32 * 2**20,
        opener=None,
    ):
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("O limite da resposta em bytes deve ser um inteiro positivo.")
        self.max_response_bytes = max_response_bytes
        self._api = INMETSource(state=None, station_limit=None, timeout=timeout, opener=opener)
        self._stations: dict[str, Station] | None = None

    def _catalog(self) -> dict[str, Station]:
        if self._stations is None:
            stations: dict[str, Station] = {}
            for station in self._api.stations():
                if station.code in stations:
                    raise ValueError(f"Cadastro nacional contém estação duplicada: {station.code}.")
                stations[station.code] = station
            if not stations:
                raise ValueError("O cadastro nacional do INMET não retornou estações automáticas.")
            self._stations = stations
        return self._stations

    def stations(self, codes: Iterable[str] | None = None) -> Iterator[Station]:
        selected = _codes(codes) if codes is not None else None
        stations = self._catalog()
        if selected is None:
            yield from stations.values()
            return
        for code in selected:
            if code not in stations:
                raise ValueError(f"Estação automática {code} ausente do cadastro nacional.")
            yield stations[code]

    def observations(self, day: date) -> Iterator[Observation]:
        if type(day) is not date:
            raise ValueError("Informe o dia UTC como uma data sem horário.")
        stations = self._catalog()
        path = f"/estacao/dados/{day.isoformat()}"
        for row in self._api._rows(path, max_response_bytes=self.max_response_bytes):
            code = row["CD_ESTACAO"]
            if isinstance(code, (str, int)) and re.fullmatch(r"[0-9]{5}", str(code)):
                raise ValueError(f"Estação convencional {code} na resposta nacional. Use o CSV do BDMEP.")
            if not isinstance(code, str) or code not in stations:
                raise ValueError(f"Estação {code!r} ausente do cadastro nacional em {path}.")
            stamp = _timestamp(row, path)
            if stamp.date() != day:
                raise ValueError(f"Resposta do INMET contém uma data fora de {path}.")
            yield _observation(row, stamp)
