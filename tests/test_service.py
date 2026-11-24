import csv
import tempfile
import unittest
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from nuvem_inmet.query import Query
from nuvem_inmet.service import _next_run, run


class DailyServiceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "data"
        self.report = self.root / "monthly.csv"
        self.yesterday = (datetime.now(UTC) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

    def _write_snapshot(self, destination, temperature):
        day = self.yesterday.date().isoformat()
        destination.write_text(
            "Estacao;Data;Hora;TempBulboSeco;UmidadeRelativa\n"
            f"83222;{day};1200;{temperature};68\n",
            encoding="utf-8",
        )

    def _run(self, *, once=True):
        run(self.root, "83222", self.yesterday, time(8), files=[],
            url="https://source.example/bdmep.csv", report=self.report, once=once)

    def test_remote_cycle_updates_recent_values_and_preserves_report_after_failure(self):
        with patch("nuvem_inmet.service._download", side_effect=lambda _, path: self._write_snapshot(path, 27)):
            self._run()
        with patch("nuvem_inmet.service._download", side_effect=lambda _, path: self._write_snapshot(path, 29)):
            self._run()
        query = Query(self.root)
        observation = query.observation("83222", self.yesterday.replace(hour=12))
        self.assertEqual((observation.temperature, observation.humidity), (Decimal(29), Decimal(68)))
        rows = list(csv.reader(self.report.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(rows[-1][1:], ["29.00", "68.0", "29.0", "68", "", ""])
        before = self.report.read_bytes()
        with patch("nuvem_inmet.service._download", side_effect=ConnectionError("simulated outage")):
            with self.assertRaises(ConnectionError):
                self._run()
        self.assertEqual(self.report.read_bytes(), before)
        self.assertEqual(query.status("83222").last_attempt.status, "failed")
        self.assertEqual(query.status("83222").last_success.status, "success")
        self.assertEqual(query.observation("83222", self.yesterday.replace(hour=12)), observation)

    def test_persistent_command_runs_immediately_then_waits_for_next_daily_cycle(self):
        with (
            patch("nuvem_inmet.service._download", side_effect=lambda _, path: self._write_snapshot(path, 27)),
            patch("nuvem_inmet.service.sleep", side_effect=KeyboardInterrupt) as wait,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._run(once=False)
        seconds, = wait.call_args.args
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 24 * 3600)
        self.assertTrue(self.report.is_file())
        today = datetime(2026, 4, 1, 9, tzinfo=UTC)
        self.assertEqual(_next_run(today, time(8)), datetime(2026, 4, 2, 8, tzinfo=UTC))
        self.assertEqual(_next_run(today, time(10)), datetime(2026, 4, 1, 10, tzinfo=UTC))
