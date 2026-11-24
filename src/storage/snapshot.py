"""Read a consistent catalog view backed by immutable binary objects."""

import sqlite3
from datetime import datetime
from pathlib import Path

from ..codec import ObservationBlock, read_station, sha256_file
from .types import (
    BlockInfo,
    IntegrityError,
    StationInfo,
    SyncAttempt,
    SyncStatus,
    _datetime,
    _iso,
)


class Snapshot:
    def __init__(self, root: Path, db: sqlite3.Connection):
        self.root = root
        self.db = db

    def _station(self, row: sqlite3.Row, at: datetime | None) -> StationInfo:
        if at is None:
            location = self.db.execute(
                """
                SELECT l.*, r.source FROM locations l JOIN runs r ON r.id = l.run_id
                WHERE l.station_id = ?
                ORDER BY l.collected_at DESC, l.effective_from DESC, l.id DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
        else:
            location = self.db.execute(
                """
                SELECT l.*, r.source FROM locations l JOIN runs r ON r.id = l.run_id
                WHERE l.station_id = ? AND l.effective_from <= ?
                ORDER BY l.effective_from DESC, l.collected_at DESC, l.id DESC LIMIT 1
                """,
                (row["id"], _iso(at)),
            ).fetchone()
        if location is None:
            return StationInfo(row["id"], row["code"], None, None, None, None, None)
        return self._location(location, row["code"])

    def _location(self, row: sqlite3.Row, code: str) -> StationInfo:
        path = self.root / row["path"]
        _check_checksum(path, row["checksum"])
        try:
            station_id, stored_code, latitude, longitude = read_station(path)
        except (OSError, ValueError) as exc:
            raise IntegrityError(f"cadastro binário inválido: {path}") from exc
        if (station_id, stored_code) != (row["station_id"], code):
            raise IntegrityError(f"identidade da estação divergente: {path}")
        return StationInfo(
            station_id, code, latitude, longitude, _datetime(row["effective_from"]),
            _datetime(row["collected_at"]), row["source"],
        )

    def stations(self, at: datetime | None = None) -> tuple[StationInfo, ...]:
        rows = self.db.execute("SELECT id, code FROM stations ORDER BY code")
        return tuple(self._station(row, at) for row in rows)

    def station(self, code: str, at: datetime | None = None) -> StationInfo | None:
        row = self.db.execute("SELECT id, code FROM stations WHERE code = ?", (code,)).fetchone()
        return self._station(row, at) if row is not None else None

    def station_id(self, code: str) -> int | None:
        row = self.db.execute("SELECT id FROM stations WHERE code = ?", (code,)).fetchone()
        return row["id"] if row is not None else None

    def locations(self, code: str) -> tuple[StationInfo, ...]:
        rows = self.db.execute(
            """
            SELECT l.*, r.source FROM locations l JOIN runs r ON r.id = l.run_id
            JOIN stations s ON s.id = l.station_id WHERE s.code = ?
            ORDER BY l.collected_at, l.id
            """,
            (code,),
        )
        return tuple(self._location(row, code) for row in rows)

    def _block(self, row: sqlite3.Row) -> BlockInfo:
        return BlockInfo(
            row["station_id"], row["year"], self.root / row["path"], row["checksum"],
            row["run_id"], datetime.fromisoformat(row["last_observation"]), row["id"],
        )

    def block(self, station_id: int, year: int) -> BlockInfo | None:
        row = self.db.execute(
            """
            SELECT b.* FROM blocks b JOIN block_heads h ON h.block_id = b.id
            WHERE h.station_id = ? AND h.year = ?
            """,
            (station_id, year),
        ).fetchone()
        return self._block(row) if row is not None else None

    def block_history(self, station_id: int, year: int) -> tuple[BlockInfo, ...]:
        rows = self.db.execute(
            "SELECT * FROM blocks WHERE station_id = ? AND year = ? ORDER BY id",
            (station_id, year),
        )
        return tuple(self._block(row) for row in rows)

    def provenance(self, block: BlockInfo, index: int) -> SyncAttempt | None:
        row = self.db.execute(
            """
            SELECT r.* FROM block_origins o JOIN runs r ON r.id = o.run_id
            WHERE o.block_id = ? AND o.start_hour <= ? AND o.stop_hour > ?
            """,
            (block.block_id, index, index),
        ).fetchone()
        return _attempt(row)

    def status(self, code: str | None = None) -> SyncStatus:
        if code is None:
            run_filter = "WHERE 1 = 1"
            run_args = ()
            block_filter = ""
            block_args = ()
        else:
            run_filter = "JOIN run_targets t ON t.run_id = r.id WHERE t.station_code = ?"
            run_args = (code,)
            block_filter = "JOIN stations s ON s.id = b.station_id WHERE s.code = ?"
            block_args = (code,)
        last = self.db.execute(
            f"SELECT r.* FROM runs r {run_filter} ORDER BY r.id DESC LIMIT 1", run_args,
        ).fetchone()
        success = self.db.execute(
            f"""
            SELECT r.* FROM runs r {run_filter} AND r.status = 'success'
            ORDER BY r.finished_at DESC, r.id DESC LIMIT 1
            """,
            run_args,
        ).fetchone()
        observation = self.db.execute(
            f"""
            SELECT max(b.last_observation) FROM blocks b
            JOIN block_heads h ON h.block_id = b.id {block_filter}
            """,
            block_args,
        ).fetchone()[0]
        return SyncStatus(_attempt(last), _attempt(success), _datetime(observation))

    def verify(self, code: str | None = None) -> int:
        args = () if code is None else (code,)
        code_filter = "" if code is None else "WHERE s.code = ?"
        checks = 0
        for row in self.db.execute(
            f"""
            SELECT l.*, s.code, r.source FROM locations l JOIN stations s ON s.id = l.station_id
            JOIN runs r ON r.id = l.run_id {code_filter}
            """,
            args,
        ):
            self._location(row, row["code"])
            checks += 1
        for row in self.db.execute(
            f"SELECT b.* FROM blocks b JOIN stations s ON s.id = b.station_id {code_filter}",
            args,
        ):
            block = self._block(row)
            _check_checksum(block.path, block.checksum)
            try:
                with ObservationBlock(block.path) as reader:
                    if (reader.station_id, reader.year) != (block.station_id, block.year):
                        raise IntegrityError(f"identidade do bloco divergente: {block.path}")
                    for _ in reader.range(0, reader.count):
                        pass
            except (OSError, ValueError) as exc:
                raise IntegrityError(f"bloco binário inválido: {block.path}") from exc
            checks += 1
        return checks


def _attempt(row: sqlite3.Row | None) -> SyncAttempt | None:
    if row is None:
        return None
    return SyncAttempt(
        row["id"], row["source"], row["mode"], row["status"],
        datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"]),
        datetime.fromisoformat(row["started_at"]), _datetime(row["collected_at"]),
        _datetime(row["finished_at"]), row["error"],
    )


def _check_checksum(path: Path, checksum: str) -> None:
    try:
        actual = sha256_file(path)
    except OSError as exc:
        raise IntegrityError(f"arquivo binário indisponível: {path}") from exc
    if actual != checksum:
        raise IntegrityError(f"checksum divergente: {path}")
