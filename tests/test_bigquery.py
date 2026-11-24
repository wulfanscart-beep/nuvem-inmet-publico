import unittest
from datetime import UTC, date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nuvem_inmet.collectors import BigQuerySource, QueryBudgetError


_START = datetime(2020, 12, 31, 23, tzinfo=UTC)
_END = datetime(2021, 1, 1, tzinfo=UTC)
_MIB = 2**20


def _parameter(name, type_, value):
    return SimpleNamespace(name=name, type_=type_, value=value)


def _estimate(bytes_=1):
    return SimpleNamespace(total_bytes_processed=bytes_)


def _job(rows, bytes_):
    return SimpleNamespace(cache_hit=False, total_bytes_billed=bytes_, result=Mock(return_value=rows))


class BigQuerySourceTest(unittest.TestCase):
    def setUp(self):
        sdk = SimpleNamespace(
            ScalarQueryParameter=_parameter,
            ArrayQueryParameter=_parameter,
            QueryJobConfig=SimpleNamespace,
        )
        patcher = patch("nuvem_inmet.collectors.bigquery._bigquery_sdk", return_value=sdk)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Mock()

    def test_default_estimate_only_uses_dry_run_parameterized_parana_subset(self):
        self.client.query.return_value = _estimate()
        source = BigQuerySource(client=self.client)
        with self.assertRaises(PermissionError):
            list(source.stations())
        self.client.query.assert_not_called()

        stations, observations = source.estimate(None, _START, _END)
        self.assertEqual(stations.estimated_bytes, 1)
        self.assertEqual(stations.estimated_billed_bytes, 10 * _MIB)
        self.assertEqual(observations.estimated_billed_bytes, 20 * _MIB)
        for call in self.client.query.call_args_list:
            config = call.kwargs["job_config"]
            self.assertTrue(config.dry_run)
            self.assertFalse(config.use_query_cache)
            values = {param.name: param.value for param in config.query_parameters}
            self.assertEqual(values["region_prefix"], "41")
            self.assertEqual(values["station_limit"], 3)
        self.assertEqual(values["last_year"], 2020)
        self.assertEqual(values["last_date"], date(2020, 12, 31))

    def test_explicit_codes_are_bound_as_an_array(self):
        self.client.query.return_value = _estimate()
        source = BigQuerySource(client=self.client)
        code = "A807') OR TRUE --"
        source.estimate_observations([code], _START, _END)
        call = self.client.query.call_args
        self.assertIn("UNNEST(@station_codes)", call.args[0])
        self.assertNotIn(code, call.args[0])
        self.assertEqual(call.kwargs["job_config"].query_parameters[-1].value, [code])

    def test_live_cap_is_cumulative_and_includes_warehouse_minimum(self):
        station = {"id_estacao": "A807", "latitude": -25.4, "longitude": -49.23}
        observation = {
            "id_estacao": "A807", "data": _START.date(), "hora": time(23),
            "temperatura_bulbo_hora": -3.125, "umidade_rel_hora": 0,
        }
        self.client.query.side_effect = [
            _estimate(), _job([station], 10 * _MIB),
            _estimate(), _job([observation], 10 * _MIB),
            _estimate(),
        ]
        source = BigQuerySource(client=self.client, max_bytes=25 * _MIB, execute=True)
        self.assertEqual([row.code for row in source.stations()], ["A807"])
        row, = source.observations(["A807"], _START, _END)
        self.assertEqual((row.timestamp, row.temperature, row.humidity), (_START, Decimal("-3.125"), Decimal(0)))
        self.assertEqual(source.remaining_bytes, 5 * _MIB)
        with self.assertRaisesRegex(QueryBudgetError, "saldo"):
            list(source.stations())

        live_calls = [call for call in self.client.query.call_args_list if not call.kwargs["job_config"].dry_run]
        self.assertEqual([call.kwargs["job_config"].maximum_bytes_billed for call in live_calls], [25 * _MIB, 15 * _MIB])
        self.assertTrue(all(call.kwargs["job_retry"] is None for call in live_calls))

    def test_monetary_bound_uses_exact_fraction_and_floor(self):
        source = BigQuerySource(max_bytes=2_000_000_000, max_cost="0.01", price_per_tib="6.25")
        self.assertEqual(source.max_bytes, 1759218604)
