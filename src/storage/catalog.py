"""Apply complete synchronization runs and publish durable catalog snapshots."""

import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

from ..codec import ObservationBlock, read_station, sha256_file, write_observations, write_station
from ..models import Observation, Station
from .schema import SCHEMA, SCHEMA_VERSION
from .snapshot import Snapshot, _check_checksum
from .staging import stage_observations, stage_rows
from .types import ApplyResult, ConflictError, IntegrityError, _iso, _utc

Window = tuple[str, datetime, datetime]
Interval = tuple[datetime, datetime]


class Storage:
    """A SQLite catalog and immutable binary files at a local path."""

    def __init__(self, root: str | Path, *, readonly: bool = False):
        self.root = Path(root).resolve()
        self.catalog = self.root / "catalog.sqlite3"
        self.readonly = readonly
        if not readonly:
            self.root.mkdir(parents=True, exist_ok=True)
        with closing(self._connect(readonly=readonly)) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not readonly:
                _enable_wal(db)
                db.executescript(SCHEMA)
            elif version != SCHEMA_VERSION:
                raise IntegrityError(f"versão de catálogo incompatível: {version}")

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly or self.readonly:
            db = sqlite3.connect(self.catalog.as_uri() + "?mode=ro", uri=True, timeout=30)
        else:
            db = sqlite3.connect(self.catalog, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def snapshot(self):
        with closing(self._connect(readonly=True)) as db:
            db.execute("BEGIN")
            yield Snapshot(self.root, db)

    @contextmanager
    def _write(self):
        if self.readonly:
            raise ValueError("o armazenamento foi aberto em modo de leitura")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db

    def begin_sync(
        self, source: str, mode: str, start: datetime, end: datetime,
        *, station_codes: Iterable[str] = (),
    ) -> int:
        start, end = _interval(start, end)
        if not source or mode not in {"historical", "incremental", "reconcile"}:
            raise ValueError("fonte e modo de sincronização inválidos")
        with self._write() as db:
            run_id = db.execute(
                """
                INSERT INTO runs (source, mode, status, start, end, started_at)
                VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (source, mode, _iso(start), _iso(end), _iso(datetime.now(UTC))),
            ).lastrowid
            db.executemany(
                "INSERT OR IGNORE INTO run_targets VALUES (?, ?)",
                ((run_id, code) for code in station_codes),
            )
        return run_id

    def fail_sync(self, run_id: int, error: str) -> None:
        with self._write() as db:
            db.execute(
                """
                UPDATE runs SET status = 'failed', finished_at = ?, error = ?
                WHERE id = ? AND status = 'running'
                """,
                (_iso(datetime.now(UTC)), error, run_id),
            )

    def coverage(
        self, source: str, station_code: str, start: datetime, end: datetime,
    ) -> list[Interval]:
        start, end = _interval(start, end)
        with self.snapshot() as snapshot:
            rows = snapshot.db.execute(
                """
                SELECT w.start, w.end FROM windows w JOIN runs r ON r.id = w.run_id
                JOIN stations s ON s.id = w.station_id
                WHERE s.code = ? AND r.source = ? AND w.start < ? AND w.end > ?
                """,
                (station_code, source, _iso(end), _iso(start)),
            )
            return _merged(
                (
                    max(start, datetime.fromisoformat(row["start"])),
                    min(end, datetime.fromisoformat(row["end"])),
                )
                for row in rows
            )

    def completed_ranges(
        self, source: str, target: str, start: datetime, end: datetime,
    ) -> list[Interval]:
        start, end = _interval(start, end)
        with self.snapshot() as snapshot:
            rows = snapshot.db.execute(
                """
                SELECT r.start,r.end FROM runs r JOIN run_targets t ON t.run_id = r.id
                WHERE r.status = 'success' AND r.source = ? AND t.station_code = ?
                  AND r.start < ? AND r.end > ?
                """,
                (source, target, _iso(end), _iso(start)),
            )
            return _merged(
                (max(start, datetime.fromisoformat(row[0])), min(end, datetime.fromisoformat(row[1])))
                for row in rows
            )

    def completed_sync(
        self, source: str, station_code: str, start: datetime, end: datetime,
    ) -> bool:
        """Check whether a source snapshot was processed for this station and interval."""
        start, end = _interval(start, end)
        with self.snapshot() as snapshot:
            row = snapshot.db.execute(
                """
                SELECT 1 FROM runs r JOIN run_targets t ON t.run_id = r.id
                WHERE r.status = 'success' AND r.source = ? AND t.station_code = ?
                  AND r.start <= ? AND r.end >= ?
                LIMIT 1
                """,
                (source, station_code, _iso(start), _iso(end)),
            ).fetchone()
        return row is not None

    def apply_sync(
        self,
        run_id: int,
        stations: Iterable[Station],
        observations: Iterable[Observation],
        start: datetime,
        end: datetime,
        *,
        windows: Iterable[Window] | None = None,
        record_windows: bool = True,
    ) -> ApplyResult:
        if self.readonly:
            raise ValueError("o armazenamento foi aberto em modo de leitura")
        start, end = _interval(start, end)
        captures = _station_captures(stations)
        codes = {station.code for station in captures.values()}
        requested = windows if windows is not None else ((code, start, end) for code in codes)
        ranges = _windows(requested, start, end)

        with TemporaryDirectory(prefix=".sync-", dir=self.root) as temporary:
            staging_path = Path(temporary)
            with closing(sqlite3.connect(staging_path / "observations.sqlite3")) as staging:
                rows, duplicates = stage_observations(staging, observations, ranges)
                collected_at = _iso(datetime.now(UTC))
                staged = _StagedFiles(self.root, staging_path)
                with closing(self._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        changed = self._apply(
                            db, staging, staged, run_id, captures, codes, ranges,
                            start, end, collected_at, record_windows,
                        )
                        staged.publish()
                    except BaseException:
                        db.rollback()
                        staged.discard_published()
                        raise
                    # A failed commit can have an uncertain outcome. Keep any published
                    # files so committed rows can never reference deleted objects.
                    db.commit()
        return ApplyResult(len(codes), rows, changed, duplicates)

    def _apply(
        self, db, staging, staged, run_id, captures, codes, ranges,
        start, end, collected_at, record_windows,
    ) -> int:
        run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None or run["status"] != "running":
            raise ValueError("sincronização inexistente ou já encerrada")
        if (_iso(start), _iso(end)) != (run["start"], run["end"]):
            raise ValueError("intervalo incompatível com a tentativa de sincronização")

        db.executemany(
            "INSERT OR IGNORE INTO stations(code) VALUES (?)", ((code,) for code in sorted(codes)),
        )
        station_ids = {}
        for code in sorted(codes | ranges.keys()):
            row = db.execute("SELECT id FROM stations WHERE code = ?", (code,)).fetchone()
            if row is None:
                raise ValueError(f"cadastro ausente para a estação {code}")
            station_ids[code] = row["id"]
        db.executemany(
            "INSERT OR IGNORE INTO run_targets VALUES (?, ?)",
            ((run_id, code) for code in station_ids),
        )
        for (code, effective), station in captures.items():
            _save_location(db, staged, run, station_ids[code], station, effective, collected_at)
        if record_windows:
            db.executemany(
                "INSERT INTO windows VALUES (?, ?, ?, ?)",
                (
                    (station_ids[code], run_id, _iso(left), _iso(right))
                    for code, intervals in ranges.items()
                    for left, right in intervals
                ),
            )

        groups = groupby(stage_rows(staging), key=lambda row: (row[0], row[1]))
        changed = sum(
            _save_block(db, staged, run, station_ids[code], year, records)
            for (code, year), records in groups
        )
        db.execute(
            "UPDATE runs SET status = 'success', collected_at = ?, finished_at = ? WHERE id = ?",
            (collected_at, _iso(datetime.now(UTC)), run_id),
        )
        return changed


def _enable_wal(db: sqlite3.Connection) -> None:
    # SQLite can return BUSY immediately when two processes enable WAL on a new catalog.
    deadline = monotonic() + 30
    while True:
        try:
            mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        except sqlite3.OperationalError as exc:
            code = exc.sqlite_errorcode & 0xFF
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or monotonic() >= deadline:
                raise
            sleep(0.05)
        else:
            if mode != "wal":
                raise IntegrityError(f"não foi possível ativar WAL no catálogo: {mode}")
            return


def _interval(start: datetime, end: datetime) -> Interval:
    start, end = _utc(start), _utc(end)
    if start >= end or any(t.minute or t.second or t.microsecond for t in (start, end)):
        raise ValueError("use um intervalo crescente com horas UTC inteiras")
    return start, end


def _merged(intervals: Iterable[Interval]) -> list[Interval]:
    merged = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            previous_left, previous_right = merged[-1]
            merged[-1] = (previous_left, max(previous_right, right))
        else:
            merged.append((left, right))
    return merged


def _windows(windows: Iterable[Window], start: datetime, end: datetime):
    ranges = defaultdict(list)
    for code, left, right in windows:
        left, right = _interval(left, right)
        if not code or left < start or right > end:
            raise ValueError("janela de coleta fora do intervalo solicitado")
        ranges[code].append((left, right))
    return {code: _merged(intervals) for code, intervals in ranges.items()}


def _station_captures(stations: Iterable[Station]) -> dict[tuple[str, str | None], Station]:
    captures = {}
    for station in stations:
        if not station.code:
            raise ValueError("código de estação vazio")
        effective = _iso(station.effective_from) if station.effective_from is not None else None
        key = station.code, effective
        previous = captures.get(key)
        if previous is not None and (
            (previous.latitude, previous.longitude) != (station.latitude, station.longitude)
        ):
            raise ConflictError(f"coordenadas conflitantes no cadastro de {station.code}")
        captures[key] = station
    return captures


class _StagedFiles:
    """Publish only complete objects, then make their references visible in SQLite."""

    def __init__(self, root: Path, staging: Path):
        self.root = root
        self.staging = staging
        self.files: list[tuple[Path, Path]] = []
        self.installed: list[Path] = []

    def new(self, folder: Path, prefix: str) -> tuple[Path, str]:
        name = f"{prefix}-{uuid4().hex}.bin"
        temporary = self.staging / name
        relative = folder / name
        self.files.append((temporary, self.root / relative))
        return temporary, relative.as_posix()

    def publish(self) -> None:
        if not self.files:
            return
        synced_dirs = set()
        for temporary, target in self.files:
            with temporary.open("rb") as source:
                os.fsync(source.fileno())
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, target)
            self.installed.append(target)
            directory = target.parent
            while directory != self.root:
                synced_dirs.add(directory)
                directory = directory.parent
        synced_dirs.add(self.root)
        for directory in sorted(synced_dirs, key=lambda path: len(path.parts), reverse=True):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def discard_published(self) -> None:
        for path in self.installed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # An unreferenced immutable file is safe to leave on disk.
                pass


def _save_location(db, staged, run, station_id, station, effective, collected_at) -> None:
    current = db.execute(
        """
        SELECT l.*, r.source FROM locations l JOIN runs r ON r.id = l.run_id
        WHERE l.station_id = ? AND l.effective_from IS ?
        ORDER BY l.collected_at DESC, l.id DESC LIMIT 1
        """,
        (station_id, effective),
    ).fetchone()
    if current is not None:
        path = staged.root / current["path"]
        _check_checksum(path, current["checksum"])
        old_id, old_code, old_latitude, old_longitude = read_station(path)
        if (old_id, old_code) != (station_id, station.code):
            raise IntegrityError(f"identidade da estação divergente: {path}")
        old_coordinates = old_latitude, old_longitude
        new_coordinates = station.latitude, station.longitude
        if old_coordinates == new_coordinates or new_coordinates == (None, None):
            return
        if current["run_id"] > run["id"] or (
            current["source"] != run["source"] and old_coordinates != (None, None)
        ):
            raise ConflictError(f"coordenadas conflitantes no cadastro de {station.code}")

    temporary, relative = staged.new(Path("stations"), f"{station_id:010d}")
    write_station(temporary, station_id, station.code, station.latitude, station.longitude)
    db.execute(
        """
        INSERT INTO locations (station_id, path, checksum, effective_from, collected_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (station_id, relative, sha256_file(temporary), effective, collected_at, run["id"]),
    )


def _read_current(db, staged, station_id, year):
    block = Snapshot(staged.root, db).block(station_id, year)
    if block is None:
        return {}
    _check_checksum(block.path, block.checksum)
    origins = iter(db.execute(
        """
        SELECT o.*, r.source FROM block_origins o JOIN runs r ON r.id = o.run_id
        WHERE o.block_id = ? ORDER BY o.start_hour
        """,
        (block.block_id,),
    ))
    origin = next(origins, None)
    values = {}
    with ObservationBlock(block.path) as reader:
        if (reader.station_id, reader.year) != (station_id, year):
            raise IntegrityError(f"identidade do bloco divergente: {block.path}")
        for hour, value in enumerate(reader.range(0, reader.count)):
            if value is None:
                continue
            while origin is not None and origin["stop_hour"] <= hour:
                origin = next(origins, None)
            if origin is None or origin["start_hour"] > hour:
                raise IntegrityError(f"procedência ausente na hora {hour}: {block.path}")
            values[hour] = (value, origin["run_id"], origin["source"])
    return values


def _save_block(db, staged, run, station_id, year, records) -> bool:
    values = _read_current(db, staged, station_id, year)
    changed = False
    year_start = datetime(year, 1, 1, tzinfo=UTC)
    newer_coverage = None
    for code, _, hour, temperature, humidity in records:
        value = temperature, humidity
        previous = values.get(hour)
        if previous is not None:
            old, old_run, old_source = previous
            if old == value:
                continue
            instant = year_start + timedelta(hours=hour)
            if old_source != run["source"] or run["mode"] != "reconcile" or old_run > run["id"]:
                raise ConflictError(f"observações conflitantes para {code} em {instant.isoformat()}")
            if newer_coverage is None:
                newer_coverage = _later_windows(db, run, station_id)
            if any(left <= instant < right for left, right in newer_coverage):
                raise ConflictError(f"coleta superada para {code} em {instant.isoformat()}")
        values[hour] = (value, run["id"], run["source"])
        changed = True
    if not changed:
        return False

    temporary, relative = staged.new(Path("observations") / f"{station_id:010d}", str(year))
    ordered = sorted(values.items())
    write_observations(temporary, station_id, year, ((hour, *data[0]) for hour, data in ordered))
    latest = year_start + timedelta(hours=ordered[-1][0])
    block_id = db.execute(
        """
        INSERT INTO blocks (station_id, year, path, checksum, run_id, last_observation)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (station_id, year, relative, sha256_file(temporary), run["id"], _iso(latest)),
    ).lastrowid
    db.execute(
        """
        INSERT INTO block_heads VALUES (?, ?, ?)
        ON CONFLICT (station_id, year) DO UPDATE SET block_id = excluded.block_id
        """,
        (station_id, year, block_id),
    )

    for origin, group in groupby(ordered, key=lambda item: item[1][1]):
        first = last = next(group)[0]
        for last, _ in group:
            pass
        db.execute("INSERT INTO block_origins VALUES (?, ?, ?, ?)", (block_id, first, last + 1, origin))
    return True


def _later_windows(db, run, station_id) -> list[Interval]:
    windows = db.execute(
        """
        SELECT w.start, w.end FROM windows w
        JOIN runs r ON r.id = w.run_id
        WHERE w.station_id = ? AND w.run_id > ? AND r.source = ?
          AND w.start < ? AND w.end > ?
        """,
        (station_id, run["id"], run["source"], run["end"], run["start"]),
    )
    return _merged(
        (datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"]))
        for row in windows
    )
