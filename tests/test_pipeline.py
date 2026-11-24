import json
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from nuvem_inmet.query import Query
from nuvem_inmet.storage import Storage
from nuvem_inmet.sync import synchronize
from nuvem_inmet.validation import normalize_observation, normalize_station

FIXTURE = Path(__file__).parent / "fixtures" / "parana.json"
HOUR = timedelta(hours=1)
CODE = "TEST-CTBA"


def timestamp(value):
    return datetime.fromisoformat(value)


START = timestamp("2024-02-28T23:00Z")
END = timestamp("2024-02-29T04:00Z")


class FixtureSource:
    source = "fixture.parana"

    def __init__(self):
        sample = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.station_rows = [
            normalize_station(code, lat, lon, timestamp(effective))
            for code, lat, lon, effective in sample["stations"]
        ]
        self.rows = [
            normalize_observation(code, timestamp(instant), temp, humidity)
            for code, instant, temp, humidity in sample["observations"]
        ]
        self.calls = []
        self.break_connection = False

    def stations(self, codes=None):
        return (row for row in self.station_rows if codes is None or row.code in codes)

    def observations(self, codes, start, end):
        self.calls.append((set(codes), start, end))
        yield from (
            row for row in self.rows if row.station_code in codes and start <= row.timestamp < end
        )
        if self.break_connection:
            raise ConnectionError("simulated upstream interruption")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "data"
        self.storage = Storage(self.root)
        self.source = FixtureSource()

    def sync(self, mode="historical", start=START, end=END, *, codes=None):
        return synchronize(self.storage, self.source, mode, start, end, codes=codes)

    def test_leap_day_roundtrip_and_offline_exports(self):
        self.sync()
        query = Query(self.root)
        rows = list(query.series(CODE, START, END))
        self.assertEqual([row.timestamp for row in rows], [START + i * HOUR for i in range(5)])
        self.assertEqual(rows[0].observation.temperature, Decimal("-2.7"))
        self.assertEqual(rows[0].observation.humidity, 0)
        self.assertEqual(query.observation(CODE, START + HOUR), self.source.rows[1])
        self.assertIsNone(rows[2].observation)
        self.assertIsNotNone(rows[3].observation)
        self.assertIsNone(rows[3].observation.temperature)
        self.assertIsNone(rows[3].observation.humidity)
        self.assertEqual(rows[4].observation.temperature, 0)
        gaps = list(query.gaps(CODE, START, END))
        self.assertEqual([(gap.start, gap.end) for gap in gaps], [(START + 2 * HOUR, START + 3 * HOUR)])
        self.assertIsNotNone(query.provenance(CODE, START))
        self.assertIsNone(query.provenance(CODE, START + 2 * HOUR))
        self.assertGreaterEqual(query.verify(), 4)
        self.assertEqual(query.nearest(-25.43, -49.27, at=START)[0].station.code, CODE)
        self.assertIsNone(query.stations(at=timestamp("2024-01-01T00:00Z"))[0].latitude)

        stream = StringIO()
        self.assertEqual(query.export_json(CODE, START, END, stream), 5)
        export = json.loads(stream.getvalue())
        self.assertEqual(export[1]["temperature_c"], "23.71234567890123")
        self.assertFalse(export[2]["recorded"])
        self.assertTrue(export[3]["recorded"])
        stream = StringIO()
        self.assertEqual(query.export_csv(CODE, START, END, stream), 5)
        self.assertIn("TEST-CTBA,2024-02-28T23:00:00Z,-2.7,0,true", stream.getvalue())

    def test_incremental_skips_previously_collected_windows_including_gaps(self):
        self.sync()
        self.source.calls.clear()
        self.sync("incremental", start=START + HOUR, end=END + 2 * HOUR)
        self.assertEqual(self.source.calls, [({"TEST-CTBA", "TEST-LDNA"}, END, END + 2 * HOUR)])
        self.source.calls.clear()
        self.sync("incremental")
        self.assertEqual(self.source.calls, [])

    def test_reconcile_records_revision_and_preserves_source_omissions(self):
        self.sync()
        query = Query(self.root)
        initial_attempt = query.provenance(CODE, START)
        revised = normalize_observation(CODE, START, "-2.72", 0)
        recovered = normalize_observation(CODE, START + 2 * HOUR, 5, 10)
        self.source.rows = [revised, recovered]
        self.sync("reconcile")
        self.assertEqual(query.observation(CODE, START), revised)
        self.assertEqual(query.observation(CODE, START + 2 * HOUR), recovered)
        self.assertIsNotNone(query.observation(CODE, START + HOUR))
        self.assertNotEqual(query.provenance(CODE, START), initial_attempt)

    def test_interruption_preserves_last_valid_state(self):
        self.sync()
        query = Query(self.root)
        previous = query.observation(CODE, START)
        status = query.status()
        self.source.rows[0] = normalize_observation(CODE, START, -5, 0)
        self.source.break_connection = True
        with self.assertRaises(ConnectionError):
            self.sync("reconcile")
        self.assertEqual(query.observation(CODE, START), previous)
        self.assertNotEqual(query.status(), status)
        self.assertGreaterEqual(query.verify(), 4)

    def test_duplicate_and_cross_source_conflict(self):
        self.source.rows.append(self.source.rows[0])
        result = self.sync()
        self.assertEqual(result.duplicate_rows, 1)
        query = Query(self.root)
        original = query.observation(CODE, START)
        self.source.rows.append(normalize_observation(CODE, START, 999, 0))
        with self.assertRaises(ValueError):
            self.sync("reconcile")
        self.assertEqual(query.observation(CODE, START), original)
        self.source.source = "fixture.outro"
        self.source.rows = [normalize_observation(CODE, START, -10, 0)]
        with self.assertRaises(ValueError):
            self.sync("reconcile")
        self.assertEqual(query.observation(CODE, START), original)

    def test_relocation_and_series_crossing_new_year(self):
        self.sync()
        later = timestamp("2024-12-31T23:00Z")
        move = later + HOUR
        self.source.station_rows[0] = normalize_station(CODE, "-25.5", "-49.3", move)
        self.source.rows = [
            normalize_observation(CODE, later, 11, 25),
            normalize_observation(CODE, move, 12, 26),
        ]
        self.sync("incremental", later, move + 2 * HOUR, codes=[CODE])
        query = Query(self.root)
        self.assertEqual(query.nearest(-25.43, -49.27, at=later)[0].station.latitude, -25.43)
        self.assertEqual(query.nearest(-25.5, -49.3, at=move)[0].station.latitude, -25.5)
        self.assertEqual(len(query.locations(CODE)), 2)
        series = list(query.series(CODE, later, move + 2 * HOUR))
        self.assertEqual([row.observation for row in series], [*self.source.rows, None])

    def test_inconsistent_times_and_invalid_humidity(self):
        for instant in (
            datetime(2024, 2, 29, 1),
            timestamp("2024-02-29T01:05Z"),
        ):
            with self.assertRaises(ValueError):
                normalize_observation(CODE, instant, 20, 40)
        with self.assertRaises(ValueError):
            normalize_observation(CODE, START, 20, 101)
        self.source.rows = [normalize_observation(CODE, START, float("-12.37"), 0)]
        self.sync()
        self.assertEqual(Query(self.root).observation(CODE, START).temperature, Decimal("-12.37"))


if __name__ == "__main__":
    unittest.main()
