"""Versioned catalog schema. Observation values live in binary objects."""

SCHEMA_VERSION = 1
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('historical', 'incremental', 'reconcile')),
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    start TEXT NOT NULL,
    end TEXT NOT NULL CHECK (start < end),
    started_at TEXT NOT NULL,
    collected_at TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS run_targets (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    station_code TEXT NOT NULL,
    PRIMARY KEY (run_id, station_code)
);
CREATE INDEX IF NOT EXISTS run_targets_by_station ON run_targets(station_code, run_id);

CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 4294967295),
    code TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(id),
    path TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    effective_from TEXT,
    collected_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS locations_by_station
    ON locations(station_id, effective_from, collected_at, id);

CREATE TABLE IF NOT EXISTS windows (
    station_id INTEGER NOT NULL REFERENCES stations(id),
    run_id INTEGER NOT NULL REFERENCES runs(id),
    start TEXT NOT NULL,
    end TEXT NOT NULL CHECK (start < end),
    PRIMARY KEY (station_id, run_id, start, end)
);

CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(id),
    year INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    last_observation TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS blocks_by_station_year ON blocks(station_id, year, id);
CREATE TABLE IF NOT EXISTS block_heads (
    station_id INTEGER NOT NULL REFERENCES stations(id),
    year INTEGER NOT NULL,
    block_id INTEGER NOT NULL UNIQUE REFERENCES blocks(id),
    PRIMARY KEY (station_id, year)
);
CREATE TABLE IF NOT EXISTS block_origins (
    block_id INTEGER NOT NULL REFERENCES blocks(id),
    start_hour INTEGER NOT NULL,
    stop_hour INTEGER NOT NULL CHECK (start_hour < stop_hour),
    run_id INTEGER NOT NULL REFERENCES runs(id),
    PRIMARY KEY (block_id, start_hour)
);
PRAGMA user_version = 1;
COMMIT;
"""
