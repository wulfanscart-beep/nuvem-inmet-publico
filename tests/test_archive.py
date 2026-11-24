import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from nuvem_inmet.collectors import archive as collector
from nuvem_inmet.collectors.archive import InmetArchive, NationalInmetArchive
from nuvem_inmet.models import Observation, Station


def _station_csv(code, rows, *, state="PR", latitude="-25,44", old_header=False):
    heading = "DATA (YYYY-MM-DD);HORA (UTC)" if old_header else "Data;Hora UTC"
    return (
        f"REGIAO:;S\nUF:;{state}\nESTACAO:;AMOSTRA\nCODIGO (WMO):;{code}\n"
        f"LATITUDE:;{latitude}\nLONGITUDE:;-49,23\nALTITUDE:;922,91\n"
        "DATA DE FUNDAÇÃO:;2020-03-04\n"
        f"{heading};PRECIPITAÇÃO TOTAL, HORÁRIO (mm);"
        "TEMPERATURA DO AR - BULBO SECO, HORARIA (°C);UMIDADE RELATIVA DO AR, HORARIA (%);\n"
        f"{rows}"
    ).encode("cp1252" if old_header else "utf-8-sig")


class InmetArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.start = datetime(2024, 2, 29, tzinfo=UTC)
        self.end = datetime(2024, 2, 29, 3, tzinfo=UTC)

    def archive(self, name, files, *, first="01-01-2024", last="31-12-2024"):
        path = Path(self.tmp.name) / name
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for state, code, data in files:
                archive.writestr(f"nested/INMET_S_{state}_{code}_AMOSTRA_{first}_A_{last}.CSV", data)
            archive.writestr("README.txt", b"metadata outside official station CSVs")
        return path

    def test_parana_selection_and_utc_half_open_interval(self):
        path = self.archive("2024.zip", [
            ("PR", "A807", _station_csv("A807",
                "2024/02/28;2300 UTC;2,0;15;84;\n"
                "2024/02/29;0000 UTC;9,9;-2,71234;0;\n"
                "2024/02/29;0200 UTC;;;;\n"
                "2024/02/29;0300 UTC;;12;49;\n")),
            ("PR", "A818", b"unreadable station member outside the selection"),
            ("SC", "A805", b"unreadable station member outside Parana"),
        ])
        adapter = InmetArchive(path, station_limit=1)
        self.assertEqual(list(adapter.stations()), [Station("A807", -25.44, -49.23)])
        observations = list(adapter.observations(("A807",), self.start, self.end))
        self.assertEqual(observations, [
            Observation("A807", self.start, Decimal("-2.71234"), Decimal("0")),
            Observation("A807", datetime(2024, 2, 29, 2, tzinfo=UTC), None, None),
        ])

    def test_multiple_archives_older_header_and_latest_coordinates(self):
        previous = self.archive("2023.zip", [
            ("PR", "A807", _station_csv("A807", "2023-12-31;23:00;;-9999;-9999,0;\n", old_header=True)),
        ], first="01-01-2023", last="31-12-2023")
        latest = self.archive("2024.zip", [
            ("PR", "A807", _station_csv("A807", "2024/01/01;0000 UTC;;10,297;63,5;\n", latitude="-25,60")),
        ])
        adapter = InmetArchive([latest, previous])
        self.assertEqual(list(adapter.stations()), [Station("A807", -25.6, -49.23, None)])
        start = datetime(2023, 12, 31, 23, tzinfo=UTC)
        after_midnight = datetime(2024, 1, 1, 1, tzinfo=UTC)
        self.assertEqual(list(adapter.observations(["A807"], start, after_midnight)), [
            Observation("A807", start, None, None),
            Observation("A807", datetime(2024, 1, 1, tzinfo=UTC), Decimal("10.297"), Decimal("63.5")),
        ])

    def test_rejects_missing_coverage_and_overlaps(self):
        station = ("PR", "A807", _station_csv("A807", ""))
        archive = self.archive("2024.zip", [station])
        adapter = InmetArchive([archive])
        start = datetime(2024, 12, 31, 23, tzinfo=UTC)
        end = datetime(2025, 1, 1, 1, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "A807.*2025-01-01"):
            list(adapter.observations(["A807"], start, end))
        second = self.archive("duplicate.zip", [station])
        with self.assertRaisesRegex(ValueError, "sobrepostos"):
            list(InmetArchive([archive, second]).stations())

    def test_rejects_header_identity_mismatch_and_invalid_hour(self):
        mismatch = self.archive("mismatch.zip", [("PR", "A807", _station_csv("A818", ""))])
        with self.assertRaisesRegex(ValueError, "diverge"):
            list(InmetArchive(mismatch).stations())
        non_hour = self.archive("nonhourly.zip", [
            ("PR", "A807", _station_csv("A807", "2024/02/29;0030 UTC;;17;50;\n")),
        ])
        with self.assertRaisesRegex(ValueError, "incompatível com observações horárias"):
            list(InmetArchive(non_hour).observations(["A807"], self.start, self.end))


class NationalInmetArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "2024.zip"
        self.leap_day = datetime(2024, 2, 29, tzinfo=UTC)

    def build(self, *members):
        with ZipFile(self.path, "w", ZIP_DEFLATED) as zipped:
            for state, code, first, last, rows in members:
                name = f"INMET_N_{state}_{code}_AMOSTRA_{first}_A_{last}.CSV"
                zipped.writestr(f"2024/{name}", _station_csv(code, rows, state=state))
            zipped.writestr("README.txt", "informação extra")
        return NationalInmetArchive(self.path, 2024)

    def test_national_station_selection_and_staggered_nominal_coverage(self):
        archive = self.build(
            ("AM", "A101", "01-01-2024", "31-12-2024", "2024/02/29;0000 UTC;;10;100;\n"),
            ("BA", "A403", "29-02-2024", "01-03-2024", "2024/02/29;0000 UTC;;15;58;\n"),
            ("PR", "A807", "28-02-2024", "28-02-2024", "2024/02/28;2300 UTC;;8;75;\n"),
        )
        self.assertEqual(archive.source, InmetArchive.source)
        stations = list(archive.stations())
        self.assertEqual([station.code for station in stations], ["A101", "A403", "A807"])
        self.assertTrue(all(station.effective_from is None for station in stations))
        self.assertEqual(archive.coverage(), {
            "A101": ((datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)),),
            "A403": ((self.leap_day, datetime(2024, 3, 2, tzinfo=UTC)),),
            "A807": ((datetime(2024, 2, 28, tzinfo=UTC), self.leap_day),),
        })
        self.assertEqual(
            list(archive.observations([
                ("A403", datetime(2024, 2, 28, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC)),
                ("A807", self.leap_day, datetime(2024, 3, 1, tzinfo=UTC)),
            ])),
            [Observation("A403", self.leap_day, Decimal(15), Decimal(58))],
        )

    def test_reads_member_once_for_disjoint_and_overlapping_windows(self):
        archive = self.build((
            "BA", "A403", "29-02-2024", "29-02-2024",
            "2024/02/29;0000 UTC;;1;50;\n"
            "2024/02/29;0100 UTC;;2;51;\n"
            "2024/02/29;0200 UTC;;3;52;\n"
            "2024/02/29;0300 UTC;;4;53;\n",
        ))
        zero, one, two, three, four = [self.leap_day.replace(hour=hour) for hour in range(5)]
        with patch.object(collector, "_read_member", wraps=collector._read_member) as read:
            rows = list(archive.observations([("A403", three, four), ("A403", zero, two), ("A403", one, two)]))
        self.assertEqual(read.call_count, 1)
        self.assertEqual([row.timestamp for row in rows], [zero, one, three])

    def test_detects_invalid_chronology_after_selected_window(self):
        archive = self.build((
            "BA", "A403", "29-02-2024", "29-02-2024",
            "2024/02/29;0000 UTC;;1;50;\n"
            "2024/02/29;0300 UTC;;2;51;\n"
            "2024/02/29;0200 UTC;;3;52;\n",
        ))
        with self.assertRaisesRegex(ValueError, "ordem cronológica"):
            list(archive.observations([("A403", self.leap_day, self.leap_day.replace(hour=1))]))

    def test_rejects_invalid_state_year_and_overlap(self):
        with self.assertRaisesRegex(ValueError, "UF inválida"):
            self.build(("XX", "A403", "29-02-2024", "29-02-2024", ""))
        with self.assertRaisesRegex(ValueError, "fora de 2024"):
            self.build(("BA", "A403", "01-01-2023", "01-01-2024", ""))
        with self.assertRaisesRegex(ValueError, "sobrepostos"):
            self.build(
                ("BA", "A403", "01-01-2024", "29-02-2024", ""),
                ("BA", "A403", "29-02-2024", "31-12-2024", ""),
            )


if __name__ == "__main__":
    unittest.main()
