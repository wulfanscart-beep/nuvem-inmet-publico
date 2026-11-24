import unittest
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import Mock
from urllib.error import URLError

from nuvem_inmet.collectors import INMETSource


class INMETSourceTest(unittest.TestCase):
    def test_default_sample_uses_small_catalog_subset_and_current_coordinates(self):
        catalog = BytesIO(b"""[
            {"CD_ESTACAO":"A999","SG_ESTADO":"SP","VL_LATITUDE":-23,"VL_LONGITUDE":-47},
            {"CD_ESTACAO":"A822","SG_ESTADO":"PR","VL_LATITUDE":-25,"VL_LONGITUDE":-48},
            {"CD_ESTACAO":"A807","SG_ESTADO":"PR","VL_LATITUDE":"-25.448","VL_LONGITUDE":-49.23}
        ]""")
        opener = Mock(return_value=catalog)
        stations = list(INMETSource(opener=opener, station_limit=1).stations())
        self.assertEqual([(row.code, row.latitude, row.longitude, row.effective_from) for row in stations],
                         [("A807", -25.448, -49.23, None)])
        self.assertEqual(opener.call_args.args[0].full_url, "https://apitempo.inmet.gov.br/estacoes/T")

    def test_batches_are_nonoverlapping_and_preserve_utc_precision_and_missing_values(self):
        fetch = Mock(side_effect=[
            BytesIO(b"""[
                {"CD_ESTACAO":"A807","DT_MEDICAO":"2020-02-28","HR_MEDICAO":"2200","TEM_INS":20,"UMD_INS":90},
                {"CD_ESTACAO":"A807","DT_MEDICAO":"2020-02-28","HR_MEDICAO":"2300","TEM_INS":-2.731234567890123456,"UMD_INS":0},
                {"CD_ESTACAO":"A807","DT_MEDICAO":"2020-02-29","HR_MEDICAO":"0000","TEM_INS":null,"UMD_INS":"15.25"}
            ]"""),
            BytesIO(b"""[
                {"CD_ESTACAO":"A807","DT_MEDICAO":"2020-03-01","HR_MEDICAO":"0000","TEM_INS":"","UMD_INS":"-9999"},
                {"CD_ESTACAO":"A807","DT_MEDICAO":"2020-03-01","HR_MEDICAO":"0100","TEM_INS":20,"UMD_INS":90}
            ]"""),
        ])
        start, end = datetime(2020, 2, 28, 23, tzinfo=UTC), datetime(2020, 3, 1, 1, tzinfo=UTC)
        observations = list(INMETSource(opener=fetch, max_days_per_request=2).observations(["A807"], start, end))
        self.assertEqual([call.args[0].selector for call in fetch.call_args_list], [
            "/estacao/2020-02-28/2020-02-29/A807", "/estacao/2020-03-01/2020-03-01/A807",
        ])
        self.assertEqual(
            [(row.timestamp, row.temperature, row.humidity) for row in observations],
            [
                (start, Decimal("-2.731234567890123456"), Decimal(0)),
                (datetime(2020, 2, 29, tzinfo=UTC), None, Decimal("15.25")),
                (datetime(2020, 3, 1, tzinfo=UTC), None, None),
            ],
        )

    def test_network_error_is_not_an_empty_success(self):
        with self.assertRaisesRegex(ConnectionError, "Falha ao consultar o INMET"):
            list(INMETSource(opener=Mock(side_effect=URLError("offline"))).stations())

    def test_error_object_is_not_an_empty_success(self):
        fetch = Mock(return_value=BytesIO(b'{"message":"unavailable"}'))
        with self.assertRaisesRegex(ValueError, "lista de registros"):
            list(INMETSource(opener=fetch).stations())

    def test_malformed_code_is_rejected_before_request(self):
        fetch = Mock()
        with self.assertRaises(ValueError):
            list(INMETSource(opener=fetch).stations(["../T"]))
        fetch.assert_not_called()
