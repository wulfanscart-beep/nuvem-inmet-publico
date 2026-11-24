import csv
import tempfile
import unittest
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from nuvem_inmet.models import Station
from nuvem_inmet.national import run_national
from nuvem_inmet.query import Query
from nuvem_inmet.validation import normalize_observation


class NationalProgramTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.report = self.root / "relatorio-brasil.csv"
        self.today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        self.start = self.today - timedelta(days=3)

    def _cycle(self, **kwargs):
        return run_national(
            self.root, self.start, time(8), archives=[], conventional=[],
            url=None, report=self.report, once=True, **kwargs,
        )

    def test_one_call_per_day_backfill_resumes_and_report_has_national_station_codes(self):
        calls = []

        class Source:
            source = "mock.country"
            def stations(self):
                return [Station("A101", -3.1, -60), Station("A401", -13, -39), Station("A801", -26, -49)]

            def observations(self, day):
                calls.append(day)
                stamp = datetime.combine(day, time(12), UTC)
                for station in self.stations():
                    yield normalize_observation(station.code, stamp, "28.1", 60)

        with patch("nuvem_inmet.national.NationalINMETSource", Source):
            self._cycle(history_days=1)
            self._cycle(history_days=1)
            self._cycle(history_days=1)

        self.assertEqual(calls, [
            (self.today - timedelta(days=1)).date(),
            self.start.date(),
            (self.start + timedelta(days=1)).date(),
        ])
        query = Query(self.root)
        self.assertEqual(len(query.stations()), 3)
        self.assertEqual(query.observation("A401", self.start.replace(hour=12)).humidity, 60)
        self.assertEqual(query.observation("A801", self.start.replace(hour=12)).temperature, query.observation(
            "A101", self.start.replace(hour=12)).temperature)
        with self.report.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({row["codigo_estacao"] for row in rows}, {"A101", "A401", "A801"})

    def test_api_outage_preserves_national_report_and_marks_attempt(self):
        first_day = self.start.date()

        class Source:
            source = "mock.country"

            def stations(self):
                return [Station("A401", -13, -39)]

            def observations(self, day):
                if day == first_day:
                    raise ConnectionError("history request failed")
                stamp = datetime.combine(day, time(12), UTC)
                yield normalize_observation("A401", stamp, 28, 60)

        with patch("nuvem_inmet.national.NationalINMETSource", Source):
            self._cycle(history_days=0)
            previous = self.report.read_bytes()
            with self.assertRaises(ConnectionError):
                self._cycle(history_days=2)
        query = Query(self.root)
        self.assertEqual(query.status().last_attempt.status, "failed")
        self.assertEqual(self.report.read_bytes(), previous)
        self.assertIsNone(query.observation("A401", self.start.replace(hour=12)))

    def test_mirror_checkpoint_survives_restart_without_merging_station_types(self):
        self.start = datetime(2025, 12, 30, tzinfo=UTC)
        scanned = []

        class Mirror:
            def __init__(self, start, end, *, on_progress, cache_dir):
                self.start = start
                scanned.append((start, end))

            def stations(self):
                return [Station("A406", None, None), Station("83222", None, None)]

            def observations(self):
                yield normalize_observation("A406", self.start.replace(hour=12), 28, 65)
                yield normalize_observation("83222", self.start.replace(hour=12), 29, 67)

            def remove_completed_cache(self):
                pass

        class Live:
            source = "mock.country"

            def stations(self):
                return [Station("A406", None, None)]

            def observations(self, day):
                stamp = datetime.combine(day, time(12), UTC)
                yield normalize_observation("A406", stamp, 31, 55)

        with (
            patch("nuvem_inmet.collectors.parquet_history.ParquetHistory", Mirror),
            patch("nuvem_inmet.national.NationalINMETSource", Live),
        ):
            self._cycle(mirror_history=True, history_days=0)
            self._cycle(mirror_history=True, history_days=0)

        self.assertEqual(scanned, [(self.start, datetime(2026, 1, 1, tzinfo=UTC))])
        query = Query(self.root)
        stamp = self.start.replace(hour=12)
        self.assertEqual(query.observation("A406", stamp).temperature, 28)
        self.assertEqual(query.observation("83222", stamp).temperature, 29)
