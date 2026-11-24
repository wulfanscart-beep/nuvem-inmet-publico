import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nuvem_inmet.collectors import InmetConventionalCSV


def _hour(day: int, hour: int = 0) -> datetime:
    return datetime(2020, 2, day, hour, tzinfo=UTC)


class ConventionalCSVTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def _file(self, name: str, payload: str, encoding: str = "utf-8-sig") -> Path:
        path = self.directory / name
        path.write_text(payload, encoding=encoding)
        return path

    def test_legacy_pairs_are_selected_by_exact_station_and_utc_hour(self):
        path = self._file("legacy.csv", (
            "CodigoOMM;Estacao;Data;Hora;Latitude;Longitude;TempBulboSeco;UmidadeRelativa;\n"
            "83222;83222;28/02/2020;1200;-12.667;-39.083;27.35;0;\n"
            "82989;82989;28/02/2020;1200;-9.28;-37.9;31.2;22;\n"
            "83222;83222;28/02/2020;1800;-12.667;-39.083;;53;\n"
            "83222;83222;29/02/2020;0000;-12.667;-39.083;22.1;-9999;\n"
        ))
        source = InmetConventionalCSV(path)
        station, = source.stations(["83222"])
        self.assertEqual((station.code, station.latitude, station.longitude), ("83222", -12.667, -39.083))
        self.assertIsNone(station.effective_from)
        rows = list(source.observations(["83222"], _hour(28, 12), _hour(29, 1)))
        self.assertEqual(
            [(row.timestamp, row.temperature, row.humidity) for row in rows],
            [(_hour(28, 12), Decimal("27.35"), Decimal(0)),
             (_hour(28, 18), None, Decimal(53)), (_hour(29), Decimal("22.1"), None)],
        )

    def test_declared_utc_period_and_explicit_modern_fields(self):
        path = self._file("bdmep.csv", (
            "Estação : CRUZ DAS ALMAS - BA (OMM: 83222)\n"
            "Latitude (graus) : -12,667\n"
            "Longitude (graus) : -39,083\n"
            "Período solicitado dos dados: 27/02/2020 a 29/02/2020\n"
            "Hora em UTC\n"
            "Data Medicao;Hora Medicao;TEMPERATURA DO AR - BULBO SECO, HORARIA (°C);"
            "UMIDADE RELATIVA DO AR, HORARIA (%);\n"
            "2020-02-29;12:00 UTC;-1,234;84,5;\n"
        ), encoding="cp1252")
        source = InmetConventionalCSV(path)
        end = datetime(2020, 3, 1, tzinfo=UTC)
        self.assertEqual(source.available_ranges("83222"), [(_hour(27), end)])
        row, = source.observations(["83222"], _hour(27), end)
        self.assertEqual((row.timestamp, row.temperature, row.humidity),
                         (_hour(29, 12), Decimal("-1.234"), Decimal("84.5")))

    def test_missing_calendar_day_stays_outside_undeclared_coverage(self):
        source = InmetConventionalCSV(self._file("gaps.csv", (
            "Estacao;Data;Hora;TempBulboSeco;UmidadeRelativa\n"
            "83222;27/02/2020;1200;25;70\n"
            "83222;29/02/2020;1200;24;75\n"
        )))
        end = datetime(2020, 3, 1, tzinfo=UTC)
        self.assertEqual(source.available_ranges("83222"), [(_hour(27), _hour(28)), (_hour(29), end)])
        self.assertEqual(source.available_ranges("83236"), [])
        with self.assertRaisesRegex(ValueError, "2020-02-28"):
            list(source.observations(["83222"], _hour(27), end))
        rows = source.observations_in_windows("83222", [(_hour(29), end), (_hour(27), _hour(28))])
        self.assertEqual([row.timestamp for row in rows], [_hour(27, 12), _hour(29, 12)])

    def test_overlapping_windows_read_each_input_record_once(self):
        source = InmetConventionalCSV(self._file("overlap.csv", (
            "Estacao;Data;Hora;TempBulboSeco;UmidadeRelativa\n"
            "83222;28/02/2020;0000;20;70\n"
            "83222;28/02/2020;1200;25;55\n"
            "83222;28/02/2020;1800;26;60\n"
        )))
        rows = source.observations_in_windows("83222", [(_hour(28), _hour(28, 13)), (_hour(28, 12), _hour(29))])
        self.assertEqual([row.timestamp for row in rows], [_hour(28), _hour(28, 12), _hour(28, 18)])
        self.assertEqual(list(InmetConventionalCSV("absent.csv").observations_in_windows("83222", [])), [])

    def test_interleaved_stations_use_one_pass_and_distinct_coverage(self):
        source = InmetConventionalCSV(self._file("multi.csv", (
            "Estacao;Data;Hora;TempBulboSeco;UmidadeRelativa\n"
            "83222;28/02/2020;0000;20;70\n"
            "82989;28/02/2020;1200;28;65\n"
            "83222;28/02/2020;1200;25;55\n"
            "82989;28/02/2020;1800;29;60\n"
            "83222;29/02/2020;1200;26;58\n"
        )))
        windows = (
            ("83222", _hour(28, 12), _hour(29, 13)),
            ("82989", _hour(28, 10), _hour(28, 18)),
            ("83222", _hour(28, 12), _hour(28, 13)),
        )
        with patch.object(source, "_scan", wraps=source._scan) as scan:
            rows = list(source.observations_across_stations(iter(windows)))
        scan.assert_called_once()
        self.assertEqual([(row.station_code, row.timestamp) for row in rows], [
            ("82989", _hour(28, 12)), ("83222", _hour(28, 12)), ("83222", _hour(29, 12)),
        ])
        with self.assertRaisesRegex(ValueError, "82989.*2020-02-29"):
            list(source.observations_across_stations((*windows, ("82989", _hour(29), _hour(29, 1)))))

    def test_daily_aggregate_cannot_replace_observed_pair(self):
        source = InmetConventionalCSV(self._file("daily.csv", (
            "Estacao;Data;Hora;Temp Comp Media;Umidade Relativa Media\n"
            "83222;28/02/2020;1200;20;70\n"
        )))
        with self.assertRaisesRegex(ValueError, "Coluna ausente ou ambígua"):
            list(source.stations(["83222"]))

    def test_contradictory_row_station_codes_fail(self):
        source = InmetConventionalCSV(self._file("conflict.csv", (
            "Estacao;CodigoOMM;Data;Hora;TempBulboSeco;UmidadeRelativa\n"
            "83222;83236;28/02/2020;1200;20;70\n"
        )))
        with self.assertRaisesRegex(ValueError, "OMM ausente ou divergente"):
            list(source.stations(["83222"]))
