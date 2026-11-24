"""Coleta seletiva do INMET publicado pela Base dos Dados no BigQuery."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from nuvem_inmet.models import Observation, Station
from nuvem_inmet.validation import normalize_observation, normalize_station, utc_interval

if TYPE_CHECKING:
    from google.cloud import bigquery


_STATIONS = "`basedosdados.br_inmet_bdmep.estacao`"
_OBSERVATIONS = "`basedosdados.br_inmet_bdmep.microdados`"
_TIB = 2**40
_MB = 2**20
_MIN_BYTES_PER_TABLE = 10 * _MB


class QueryBudgetError(RuntimeError):
    """A previsão de cobrança excede o limite da coleta."""


@dataclass(frozen=True)
class QueryEstimate:
    name: str
    estimated_bytes: int
    estimated_billed_bytes: int


@dataclass(frozen=True)
class _Query:
    name: str
    sql: str
    parameters: list[Any]
    table_count: int = 1


def _bigquery_sdk():
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError("Instale o extra [bigquery] para consultar o BigQuery.") from exc
    return bigquery


def _station_codes(codes: Iterable[str]) -> list[str]:
    if isinstance(codes, str):
        raise ValueError("Informe os códigos das estações como uma coleção de strings.")
    selected = list(codes)
    if not selected or any(
        not isinstance(code, str) or not code or code != code.strip() or "\x00" in code
        for code in selected
    ):
        raise ValueError("Informe ao menos um código válido de estação.")
    return sorted(set(selected))


class BigQuerySource:
    """Adaptador com orçamento acumulado por instância e execução opcional.

    As estimativas consultam a API por dry run. Só execute=True permite buscar
    linhas. O orçamento em bytes se aplica ao conjunto das consultas desta
    instância, incluindo consultas de cadastro e todas as janelas observadas.
    """

    source = "basedosdados.br_inmet_bdmep"

    def __init__(
        self,
        project: str | None = None,
        *,
        max_bytes: int = 100_000_000,
        execute: bool = False,
        region_prefix: str | None = "41",
        station_limit: int | None = 3,
        location: str | None = None,
        max_cost: Decimal | str | None = None,
        price_per_tib: Decimal | str | None = None,
        client: bigquery.Client | None = None,
    ):
        if type(max_bytes) is not int or not 0 < max_bytes < 2**63:
            raise ValueError("O limite de bytes deve ser um inteiro positivo de 64 bits.")
        if region_prefix is not None and (
            not isinstance(region_prefix, str)
            or len(region_prefix) != 2
            or not region_prefix.isascii()
            or not region_prefix.isdecimal()
        ):
            raise ValueError("Use os dois dígitos do código da UF do IBGE.")
        if station_limit is not None and (
            type(station_limit) is not int or not 0 < station_limit < 2**63
        ):
            raise ValueError("O limite de estações deve ser um inteiro positivo de 64 bits.")
        if (max_cost is None) != (price_per_tib is None):
            raise ValueError("Informe juntos o limite de custo e o preço vigente por TiB.")
        if max_cost is not None and price_per_tib is not None:
            try:
                cost, price = Decimal(max_cost), Decimal(price_per_tib)
            except (InvalidOperation, TypeError) as exc:
                raise ValueError("O custo e o preço por TiB devem ser valores decimais.") from exc
            if not cost.is_finite() or not price.is_finite() or cost <= 0 or price <= 0:
                raise ValueError("O custo e o preço por TiB devem ser finitos e positivos.")
            # Divisão inteira das frações exatas impede arredondamento para cima.
            cost_num, cost_den = cost.as_integer_ratio()
            price_num, price_den = price.as_integer_ratio()
            max_bytes = min(max_bytes, (cost_num * price_den * _TIB) // (cost_den * price_num))
            if max_bytes == 0:
                raise ValueError("O limite de custo informado cobre menos de um byte.")

        self._max_bytes = max_bytes
        self.region_prefix = region_prefix
        self.station_limit = station_limit
        self._project = project
        self._execute = execute is True
        self._location = location
        self._client = client
        self._consumed_budget = 0
        self._lock = threading.Lock()

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def remaining_bytes(self) -> int:
        return self.max_bytes - self._consumed_budget

    def _get_client(self):
        if self._client is None:
            sdk = _bigquery_sdk()
            from google.auth.exceptions import DefaultCredentialsError

            try:
                self._client = sdk.Client(project=self._project, location=self._location)
            except DefaultCredentialsError as exc:
                raise RuntimeError(
                    "Configure credenciais padrão do Google (ADC) e um projeto autorizado "
                    "a criar jobs no BigQuery."
                ) from exc
        return self._client

    def _station_selection(self, codes: Iterable[str] | None, columns: str) -> _Query:
        sdk = _bigquery_sdk()
        sql = f"SELECT {columns} FROM {_STATIONS}"
        parameters = []
        if codes is not None:
            sql += "\nWHERE id_estacao IN UNNEST(@station_codes)"
            parameters.append(sdk.ArrayQueryParameter("station_codes", "STRING", _station_codes(codes)))
        else:
            sql += "\nWHERE id_estacao IS NOT NULL"
            if self.region_prefix is not None:
                sql += "\n  AND STARTS_WITH(id_municipio, @region_prefix)"
                parameters.append(sdk.ScalarQueryParameter("region_prefix", "STRING", self.region_prefix))
            if self.station_limit is not None:
                sql += "\nQUALIFY DENSE_RANK() OVER (ORDER BY id_estacao) <= @station_limit"
                parameters.append(sdk.ScalarQueryParameter("station_limit", "INT64", self.station_limit))
        return _Query("estações", sql, parameters)

    def _station_query(self, codes: Iterable[str] | None) -> _Query:
        selected = self._station_selection(codes, "id_estacao, latitude, longitude")
        return _Query(selected.name, selected.sql + "\nORDER BY id_estacao", selected.parameters)

    def _observation_query(
        self, codes: Iterable[str] | None, start: datetime, end: datetime
    ) -> _Query:
        start, end = utc_interval(start, end)
        last = end - timedelta(microseconds=1)
        sdk = _bigquery_sdk()
        parameters = [
            sdk.ScalarQueryParameter("first_year", "INT64", start.year),
            sdk.ScalarQueryParameter("last_year", "INT64", last.year),
            sdk.ScalarQueryParameter("first_date", "DATE", start.date()),
            sdk.ScalarQueryParameter("last_date", "DATE", last.date()),
            sdk.ScalarQueryParameter("start_at", "DATETIME", start.replace(tzinfo=None)),
            sdk.ScalarQueryParameter("end_at", "DATETIME", end.replace(tzinfo=None)),
        ]
        if codes is None:
            selected = self._station_selection(None, "id_estacao")
            station_filter = f"id_estacao IN ({selected.sql})"
            parameters.extend(selected.parameters)
        else:
            station_filter = "id_estacao IN UNNEST(@station_codes)"
            parameters.append(sdk.ArrayQueryParameter("station_codes", "STRING", _station_codes(codes)))
        sql = f"""
SELECT id_estacao, data, hora, temperatura_bulbo_hora, umidade_rel_hora
FROM {_OBSERVATIONS}
WHERE ano BETWEEN @first_year AND @last_year
  AND data BETWEEN @first_date AND @last_date
  AND (hora IS NULL OR (DATETIME(data, hora) >= @start_at AND DATETIME(data, hora) < @end_at))
  AND {station_filter}
ORDER BY id_estacao, data, hora
"""
        return _Query("observações", sql, parameters, table_count=2 if codes is None else 1)

    def _estimate(self, query: _Query) -> QueryEstimate:
        config = _bigquery_sdk().QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=query.parameters
        )
        job = self._get_client().query(query.sql, job_config=config, location=self._location)
        processed = job.total_bytes_processed
        if type(processed) is not int or processed < 0:
            raise RuntimeError(f"BigQuery não informou os bytes previstos de {query.name}.")
        rounded = ((processed + _MB - 1) // _MB) * _MB
        # O dry run informa apenas o total. Considere o mínimo adicional de
        # cada tabela para estimativas que usam o cadastro como subconsulta.
        billed = max(rounded, _MIN_BYTES_PER_TABLE) + (query.table_count - 1) * _MIN_BYTES_PER_TABLE
        return QueryEstimate(query.name, processed, billed)

    def estimate_stations(self, codes: Iterable[str] | None = None) -> QueryEstimate:
        return self._estimate(self._station_query(codes))

    def estimate_observations(
        self, codes: Iterable[str] | None, start: datetime, end: datetime
    ) -> QueryEstimate:
        return self._estimate(self._observation_query(codes, start, end))

    def estimate(
        self, codes: Iterable[str] | None, start: datetime, end: datetime
    ) -> tuple[QueryEstimate, QueryEstimate]:
        selected = None if codes is None else _station_codes(codes)
        stations = self._station_query(selected)
        observations = self._observation_query(selected, start, end)
        return self._estimate(stations), self._estimate(observations)

    def _run(self, query: _Query):
        if not self._execute:
            raise PermissionError("A coleta exige autorização explícita (execute=True).")
        with self._lock:
            remaining = self.remaining_bytes
            estimate = self._estimate(query)
            if estimate.estimated_billed_bytes > remaining:
                raise QueryBudgetError(
                    f"{query.name}: {estimate.estimated_billed_bytes:,} bytes faturáveis previstos "
                    f"para um saldo de {remaining:,} bytes."
                )
            config = _bigquery_sdk().QueryJobConfig(
                dry_run=False, maximum_bytes_billed=remaining, query_parameters=query.parameters
            )
            # Não reutilize saldo de uma consulta submetida cujo custo permaneça desconhecido.
            self._consumed_budget = self.max_bytes
            job = self._get_client().query(
                query.sql, job_config=config, location=self._location, job_retry=None
            )
            rows = job.result(page_size=2048, job_retry=None)
            billed = 0 if job.cache_hit else job.total_bytes_billed
            if type(billed) is not int or not 0 <= billed <= remaining:
                raise RuntimeError(f"BigQuery não confirmou uma cobrança dentro do saldo de {query.name}.")
            self._consumed_budget = self.max_bytes - remaining + billed
            return rows

    def stations(self, codes: Iterable[str] | None = None) -> Iterator[Station]:
        rows = self._run(self._station_query(codes))
        return (
            normalize_station(row["id_estacao"], row["latitude"], row["longitude"])
            for row in rows
        )

    def observations(
        self, codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterator[Observation]:
        if codes is None:
            raise ValueError("Consulte o cadastro para selecionar códigos antes de coletar observações.")
        rows = self._run(self._observation_query(codes, start, end))
        for row in rows:
            day, hour = row["data"], row["hora"]
            if type(day) is not date or not isinstance(hour, time) or hour.tzinfo is not None:
                raise ValueError(f"Data ou hora UTC inválida em {row['id_estacao']}: {day!r} {hour!r}.")
            yield normalize_observation(
                row["id_estacao"],
                datetime.combine(day, hour, tzinfo=UTC),
                row["temperatura_bulbo_hora"],
                row["umidade_rel_hora"],
            )
