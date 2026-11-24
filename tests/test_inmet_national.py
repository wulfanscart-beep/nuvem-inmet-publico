import json
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import Mock
from urllib.error import URLError

from nuvem_inmet.collectors import NationalINMETSource


_DAY = date(2020, 2, 29)
_CATALOG = [
    {"CD_ESTACAO": "A807", "SG_ESTADO": "PR", "VL_LATITUDE": -25.4, "VL_LONGITUDE": -49.2},
    {"CD_ESTACAO": "A101", "SG_ESTADO": "PA", "VL_LATITUDE": -1.4, "VL_LONGITUDE": -48.4},
    {"CD_ESTACAO": "A001", "SG_ESTADO": "DF", "VL_LATITUDE": -15.8, "VL_LONGITUDE": -47.9},
    {"CD_ESTACAO": "A402", "SG_ESTADO": "BA", "VL_LATITUDE": -12.6, "VL_LONGITUDE": -39.1},
]


def _json(value) -> BytesIO:
    return BytesIO(json.dumps(value).encode())


def _row(code: str | int, day: date = _DAY) -> dict:
    return {
        "CD_ESTACAO": code, "DT_MEDICAO": day.isoformat(), "HR_MEDICAO": "1800",
        "TEM_INS": "28.125", "UMD_INS": "65.5",
    }


class NationalINMETSourceTest(unittest.TestCase):
    def test_uses_one_request_for_all_stations_on_the_requested_day(self):
        data = BytesIO(b"""[
            {"CD_ESTACAO":"A807","DT_MEDICAO":"2020-02-29","HR_MEDICAO":"0000",
             "TEM_INS":-3.12500000000000001,"UMD_INS":0},
            {"CD_ESTACAO":"A101","DT_MEDICAO":"2020-02-29","HR_MEDICAO":"1800",
             "TEM_INS":null,"UMD_INS":"-9999"}
        ]""")
        opener = Mock(side_effect=[_json(_CATALOG), data])
        source = NationalINMETSource(opener=opener, timeout=12)
        self.assertEqual([s.code for s in source.stations()], ["A001", "A101", "A402", "A807"])
        self.assertEqual([s.code for s in source.stations(["A402"])], ["A402"])
        self.assertEqual(
            [(row.station_code, row.timestamp, row.temperature, row.humidity)
             for row in source.observations(_DAY)],
            [
                ("A807", datetime(2020, 2, 29, tzinfo=UTC), Decimal("-3.12500000000000001"), Decimal(0)),
                ("A101", datetime(2020, 2, 29, 18, tzinfo=UTC), None, None),
            ],
        )
        self.assertEqual([call.args[0].selector for call in opener.call_args_list],
                         ["/estacoes/T", "/estacao/dados/2020-02-29"])
        self.assertTrue(all(call.kwargs["timeout"] == 12 for call in opener.call_args_list))

    def test_does_not_assign_catalog_data_to_unrecognized_station(self):
        opener = Mock(side_effect=[_json(_CATALOG), _json([_row("A999")])])
        with self.assertRaisesRegex(ValueError, "A999.*ausente do cadastro"):
            list(NationalINMETSource(opener=opener).observations(_DAY))

    def test_identifies_conventional_station_and_requires_separate_csv(self):
        for code in ("83222", 83222):
            with self.subTest(code=code):
                opener = Mock(side_effect=[_json(_CATALOG), _json([_row(code)])])
                with self.assertRaisesRegex(ValueError, "convencional 83222.*BDMEP"):
                    list(NationalINMETSource(opener=opener).observations(_DAY))

    def test_rejects_unrequested_dates_in_the_daily_response(self):
        opener = Mock(side_effect=[_json(_CATALOG), _json([_row("A807", date(2020, 3, 1))])])
        with self.assertRaisesRegex(ValueError, "data fora"):
            list(NationalINMETSource(opener=opener).observations(_DAY))

    def test_daily_response_size_is_bounded(self):
        opener = Mock(side_effect=[_json(_CATALOG), _json([_row("A807")])])
        with self.assertRaisesRegex(ValueError, "excedeu 32 bytes"):
            list(NationalINMETSource(opener=opener, max_response_bytes=32).observations(_DAY))

    def test_network_failure_is_not_empty_daily_data(self):
        opener = Mock(side_effect=[_json(_CATALOG), URLError("upstream indisponível")])
        with self.assertRaisesRegex(ConnectionError, "/estacao/dados/2020-02-29"):
            list(NationalINMETSource(opener=opener).observations(_DAY))
