import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from nuvem_inmet.models import Observation, Station
from nuvem_inmet.query import Query
from nuvem_inmet.storage import ConflictError, IntegrityError, Storage


START = datetime(2024, 12, 31, 23, tzinfo=UTC)
HOUR = timedelta(hours=1)
END = START + 3 * HOUR
STATION = Station("TEST", -25.43, -49.27)


def _observation(hour, temperature):
    return Observation(STATION.code, START + hour * HOUR, Decimal(temperature), Decimal(0))


class AtomicStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Storage(self.root)

    def _attempt(self, source="fixture", mode="historical"):
        return self.store.begin_sync(source, mode, START, END, station_codes=(STATION.code,))

    def _apply(self, run_id, observations, station=STATION):
        return self.store.apply_sync(run_id, (station,), observations, START, END)

    def test_later_conflict_rolls_back_preceding_year_and_station_update(self):
        self._apply(self._attempt(), [_observation(1, -5)])
        query = Query(self.root)
        before = sorted(self.root.rglob("*.bin"))

        attempt = self._attempt(mode="incremental")
        with self.assertRaises(ConflictError):
            self._apply(
                attempt,
                [_observation(0, 10), _observation(1, -9)],
                Station(STATION.code, -24.2, -50.1),
            )
        self.store.fail_sync(attempt, "conflito detectado")

        self.assertEqual(sorted(self.root.rglob("*.bin")), before)
        self.assertIsNone(query.observation(STATION.code, START))
        self.assertEqual(query.stations()[0].latitude, STATION.latitude)
        self.assertEqual(query.status(STATION.code).last_attempt.status, "failed")
        self.assertEqual(query.status(STATION.code).last_observation, START + HOUR)

    def test_merge_preserves_origin_of_unchanged_rows_and_retains_revisions(self):
        first = self._attempt("A")
        self._apply(first, [_observation(1, 1)])
        second = self._attempt("B")
        self._apply(second, [_observation(2, 2)])

        query = Query(self.root)
        self.assertEqual(query.provenance(STATION.code, START + HOUR).run_id, first)
        self.assertEqual(query.provenance(STATION.code, START + 2 * HOUR).run_id, second)
        with self.store.snapshot() as snapshot:
            station_id = snapshot.station_id(STATION.code)
            earlier, later = snapshot.block_history(station_id, 2025)
        self.assertTrue(earlier.path.exists())
        self.assertTrue(later.path.exists())

    def test_stale_reconciliation_cannot_override_more_recent_source_run(self):
        older = self._attempt(mode="reconcile")
        newer = self._attempt(mode="reconcile")
        self._apply(newer, [_observation(1, 2)])
        with self.assertRaises(ConflictError):
            self._apply(older, [_observation(1, 1)])
        self.assertEqual(Query(self.root).observation(STATION.code, START + HOUR), _observation(1, 2))

    def test_later_collection_of_identical_value_blocks_stale_revision(self):
        self._apply(self._attempt(), [_observation(1, 2)])
        older = self._attempt(mode="reconcile")
        self._apply(self._attempt(mode="reconcile"), [_observation(1, 2)])
        with self.assertRaises(ConflictError):
            self._apply(older, [_observation(1, 1)])
        self.assertEqual(Query(self.root).observation(STATION.code, START + HOUR), _observation(1, 2))

    def test_checksum_detects_tampered_binary(self):
        self._apply(self._attempt(), [_observation(1, -1)])
        with self.store.snapshot() as snapshot:
            block = snapshot.block(snapshot.station_id(STATION.code), 2025)
        with block.path.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaises(IntegrityError):
            Query(self.root).verify()

    def test_undated_partial_position_does_not_apply_to_old_observations(self):
        self._apply(
            self._attempt(),
            [_observation(1, 0)],
            Station(STATION.code, STATION.latitude, None),
        )
        query = Query(self.root)
        self.assertEqual(query.stations()[0].latitude, STATION.latitude)
        self.assertIsNone(query.stations()[0].longitude)
        self.assertIsNone(query.stations(at=START)[0].latitude)
        self.assertEqual(query.nearest(STATION.latitude, STATION.longitude), [])
