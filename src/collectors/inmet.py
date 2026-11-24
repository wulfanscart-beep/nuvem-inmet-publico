"""Coleta pública e seletiva de estações automáticas diretamente do INMET."""

import json
import math
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from http.client import HTTPException
from urllib.request import Request, urlopen

from nuvem_inmet.models import Observation, Station
from nuvem_inmet.validation import normalize_observation, normalize_station, utc_interval


_API = "https://apitempo.inmet.gov.br"
_MAX_RESPONSE_BYTES = 4 * 2**20
_CODE = re.compile(r"[A-Z][0-9]{3}")
_HOUR = re.compile(r"[0-9]{4}")


def _codes(values: Iterable[str]) -> list[str]:
    if isinstance(values, str):
        raise ValueError("Informe os códigos como uma coleção de strings.")
    codes = list(values)
    if not codes or any(not isinstance(code, str) or not _CODE.fullmatch(code) for code in codes):
        raise ValueError("Informe códigos de estações automáticas, como A807.")
    return sorted(set(codes))


def _measurement(value):
    if isinstance(value, str):
        value = value.strip()
    return None if value in (None, "", "-9999", -9999) else value


def _timestamp(row: dict, path: str) -> datetime:
    day = date.fromisoformat(row["DT_MEDICAO"])
    hour = row["HR_MEDICAO"]
    if (
        not isinstance(hour, str) or not _HOUR.fullmatch(hour)
        or hour[2:] != "00" or hour[:2] > "23"
    ):
        raise ValueError(f"Horário inválido do INMET em {path}: {hour!r}.")
    return datetime.combine(day, time(int(hour[:2])), tzinfo=UTC)


def _observation(row: dict, stamp: datetime) -> Observation:
    return normalize_observation(
        row["CD_ESTACAO"], stamp, _measurement(row["TEM_INS"]), _measurement(row["UMD_INS"])
    )


class INMETSource:
    """API pública de dados horários, com seleção e limites por requisição.

    Sem códigos explícitos, seleciona os primeiros códigos da UF configurada
    em ordem alfanumérica. O cadastro descreve as coordenadas conhecidas no
    momento da coleta, sem uma data histórica de vigência fornecida pela API.
    """

    source = "inmet.apitempo"

    def __init__(
        self,
        *,
        state: str | None = "PR",
        station_limit: int | None = 3,
        timeout: float = 30,
        max_days_per_request: int = 7,
        opener=None,
    ):
        if state is not None and (not isinstance(state, str) or not re.fullmatch(r"[A-Z]{2}", state)):
            raise ValueError("Informe a sigla da UF em maiúsculas.")
        if station_limit is not None and (type(station_limit) is not int or station_limit < 1):
            raise ValueError("O limite de estações deve ser um inteiro positivo.")
        if type(max_days_per_request) is not int or not 1 <= max_days_per_request <= 366:
            raise ValueError("Use entre 1 e 366 dias por requisição.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("O tempo limite da requisição deve ser finito e positivo.")
        self.state = state
        self.station_limit = station_limit
        self.timeout = timeout
        self.max_days_per_request = max_days_per_request
        self._open = opener if opener is not None else urlopen

    def _rows(self, path: str, *, max_response_bytes: int = _MAX_RESPONSE_BYTES) -> list[dict]:
        request = Request(_API + path, headers={"Accept": "application/json", "User-Agent": "Nuvem-INMET"})
        try:
            with self._open(request, timeout=self.timeout) as response:
                payload = response.read(max_response_bytes + 1)
        except (OSError, HTTPException) as exc:
            raise ConnectionError(f"Falha ao consultar o INMET em {path}: {exc}") from exc
        if len(payload) > max_response_bytes:
            raise ValueError(f"Resposta do INMET excedeu {max_response_bytes} bytes em {path}.")
        try:
            rows = json.loads(payload, parse_float=Decimal)
        except ValueError as exc:
            raise ValueError(f"Resposta JSON inválida do INMET em {path}.") from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Resposta do INMET em {path} não contém uma lista de registros.")
        return rows

    def stations(self, codes: Iterable[str] | None = None) -> Iterator[Station]:
        selected = set(_codes(codes)) if codes is not None else None
        rows = self._rows("/estacoes/T")
        if selected is None:
            if self.state is not None:
                rows = [row for row in rows if row["SG_ESTADO"] == self.state]
            station_codes = _codes(row["CD_ESTACAO"] for row in rows) if rows else []
            selected = set(station_codes[: self.station_limit])
        for row in sorted(rows, key=lambda row: row["CD_ESTACAO"]):
            if row["CD_ESTACAO"] in selected:
                yield normalize_station(
                    row["CD_ESTACAO"], _measurement(row["VL_LATITUDE"]), _measurement(row["VL_LONGITUDE"])
                )

    def observations(
        self, codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterator[Observation]:
        start, end = utc_interval(start, end)
        last_day = (end - timedelta(hours=1)).date()
        for code in _codes(codes):
            first_day = start.date()
            while first_day <= last_day:
                days = min((last_day - first_day).days, self.max_days_per_request - 1)
                final_day = first_day + timedelta(days=days)
                path = f"/estacao/{first_day.isoformat()}/{final_day.isoformat()}/{code}"
                for row in self._rows(path):
                    if row["CD_ESTACAO"] != code:
                        raise ValueError(f"Resposta do INMET contém outra estação em {path}.")
                    stamp = _timestamp(row, path)
                    if not first_day <= stamp.date() <= final_day:
                        raise ValueError(f"Resposta do INMET contém uma data fora de {path}.")
                    if start <= stamp < end:
                        yield _observation(row, stamp)
                if final_day == last_day:
                    break
                first_day = final_day + timedelta(days=1)
