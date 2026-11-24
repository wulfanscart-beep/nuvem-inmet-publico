import csv
import unittest
from datetime import datetime
from io import StringIO
from tempfile import TemporaryDirectory

from nuvem_inmet.query import Query, Slot
from nuvem_inmet.query.monthly import export_monthly, export_national_monthly
from nuvem_inmet.storage import Storage
from nuvem_inmet.validation import normalize_observation, normalize_station


def instant(value):
    return datetime.fromisoformat(value + "Z")


class MonthlyTest(unittest.TestCase):
    def test_intended_filters_reference_tie_and_empty_month(self):
        raw = [
            ("2026-01-05T09:00", 49, 90),
            ("2026-01-05T10:00", 24, 80),
            ("2026-01-05T12:00", 31, 62),
            ("2026-01-05T13:00", 23, 30),
            ("2026-01-05T14:00", 49, None),
            ("2026-01-05T18:00", 50, 30),
            ("2026-01-05T19:00", 50, 33),
            ("2026-01-05T21:00", 25, 45),
            ("2026-01-05T22:00", 52, 90),
            ("2026-03-01T10:00", 51, 25),
            ("2026-03-01T12:00", 28, 75),
            ("2026-03-01T21:00", 52, None),
        ]

        class Sample:
            def series(self, station, start, end):
                for timestamp, temperature, humidity in raw[:9]:
                    stamp = instant(timestamp)
                    yield Slot(stamp, normalize_observation(station, stamp, temperature, humidity))
                yield Slot(instant("2026-02-01T00:00"), None)
                for timestamp, temperature, humidity in raw[9:]:
                    stamp = instant(timestamp)
                    yield Slot(stamp, normalize_observation(station, stamp, temperature, humidity))

        output = StringIO()
        count = export_monthly(Sample(), "83222", instant("2026-01-01T00:00"),
                               instant("2026-04-01T00:00"), output)
        self.assertEqual(count, 3)
        output.seek(0)
        self.assertEqual(list(csv.reader(output))[1:], [
            ["2026-01", "36.00", "50.0", "50", "30", "", ""],
            ["2026-02", "", "", "", "", "", ""],
            ["2026-03", "28.00", "75.0", "52", "", "", ""],
        ])

    def test_national_defaults_omit_empty_rows_and_distinguish_stations(self):
        start = instant("2020-01-31T23:00")
        end = instant("2020-03-01T11:00")
        measurements = (
            normalize_observation("A002", instant("2020-02-29T10:00"), 0, 70),
            normalize_observation("A001", instant("2020-03-01T10:00"), 30, 0),
            normalize_observation("A001", instant("2020-02-29T22:00"), 39, 30),
        )
        stations = [normalize_station(code, None, None) for code in ("A002", "A003", "A001")]
        with TemporaryDirectory() as directory:
            storage = Storage(directory)
            run = storage.begin_sync("fixture.nacional", "historical", start, end)
            storage.apply_sync(run, stations, measurements, start, end)
            query = Query(directory)

            output = StringIO()
            count = export_national_monthly(query, start, end, output)
            self.assertEqual(count, 2)
            output.seek(0)
            header, *rows = csv.reader(output)
            self.assertEqual(header, [
                "codigo_estacao", "mes", "temperatura_media_c", "umidade_media_percentual",
                "temperatura_maxima_c", "umidade_na_referencia_percentual",
                "temperatura_insuflada_c", "umidade_no_insuflamento_percentual",
            ])
            self.assertEqual(rows, [
                ["A001", "2020-03", "30.00", "0.0", "30.0", "0", "", ""],
                ["A002", "2020-02", "", "", "0.0", "70", "", ""],
            ])

            output = StringIO()
            count = export_national_monthly(
                query, start, end, output, stations=["A001", "A001"], include_empty=True,
            )
            self.assertEqual(count, 3)
            output.seek(0)
            self.assertEqual(list(csv.reader(output))[1:], [
                ["A001", "2020-01", "", "", "", "", "", ""],
                ["A001", "2020-02", "", "", "", "", "", ""],
                ["A001", "2020-03", "30.00", "0.0", "30.0", "0", "", ""],
            ])
