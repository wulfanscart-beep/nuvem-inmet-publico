"""Compara formatos para o mesmo conjunto sintético de séries horárias."""

import argparse
import csv
import hashlib
import itertools
import json
import math
import random
import resource
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from nuvem_inmet.codec import ObservationBlock, hours_in_year, write_observations

FORMATS = ("bin", "csv", "json", "parquet")
FIELDS = ("hora_utc", "temperatura_c", "umidade_relativa_pct")
SENSOR_FIELDS = list(FIELDS[1:])
PARQUET_GROUP_HOURS = 720
BASE_SEED = 48960


def synthetic_series(station, year):
    rng = random.Random(BASE_SEED + station)
    station_bias = (station % 11) - 5
    hours = hours_in_year(year)
    for index in range(hours):
        if rng.random() < 0.03:
            yield None
            continue
        seasonal = 9 * math.cos(math.tau * index / hours)
        daily = 6 * math.cos(math.tau * ((index % 24) - 17) / 24)
        variation = rng.uniform(-3, 3)
        temp = Decimal(round((18 + seasonal + daily + variation + station_bias) * 10)) / 10
        humidity = Decimal(min(100, max(0, round(70 - daily * 3 + rng.uniform(-14, 14)))))
        if rng.random() < 0.02:
            temp = None
        elif rng.random() < 0.02:
            humidity = None
        yield temp, humidity


def write_dataset(root, stations, year):
    import pyarrow as pa
    import pyarrow.parquet as pq

    start = datetime(year, 1, 1, tzinfo=UTC)
    times = [start + timedelta(hours=index) for index in range(hours_in_year(year))]
    utc_labels = [hour.isoformat().replace("+00:00", "Z") for hour in times]
    for station in range(1, stations + 1):
        prefix = root / f"{station:05d}"
        series = list(synthetic_series(station, year))
        write_observations(
            prefix.with_suffix(".bin"),
            station,
            year,
            ((index, *value) for index, value in enumerate(series) if value is not None),
        )
        pairs = [value if value is not None else (None, None) for value in series]
        temperatures, humidities = zip(*pairs)
        humidity_ints = [int(value) if value is not None else None for value in humidities]
        with prefix.with_suffix(".csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(FIELDS)
            writer.writerows(zip(utc_labels, temperatures, humidity_ints))
        with prefix.with_suffix(".json").open("w", encoding="utf-8") as stream:
            # Os dados sintéticos usam décimos, que o serializador representa em decimal.
            objects = (
                dict(zip(FIELDS, (hour, float(temp) if temp is not None else None, humidity)))
                for hour, temp, humidity in zip(utc_labels, temperatures, humidity_ints)
            )
            stream.write("[")
            for index, row in enumerate(objects):
                if index:
                    stream.write(",")
                json.dump(row, stream, separators=(",", ":"))
            stream.write("]")
        table = pa.table(
            {
                FIELDS[0]: pa.array(times, type=pa.timestamp("us", tz="UTC")),
                FIELDS[1]: pa.array(temperatures, type=pa.decimal128(5, 1)),
                FIELDS[2]: pa.array(humidity_ints, type=pa.uint8()),
            }
        )
        pq.write_table(
            table,
            prefix.with_suffix(".parquet"),
            compression="zstd",
            row_group_size=PARQUET_GROUP_HOURS,
        )
    print(f"Python {sys.version.split()[0]} | PyArrow {pa.__version__}")


def numeric_pair(temp, humidity):
    if temp is None and humidity is None:
        return None
    return temp, Decimal(humidity) if humidity is not None else None


def read_csv(path, index):
    with path.open(newline="", encoding="utf-8") as stream:
        rows = csv.reader(stream)
        next(rows)
        if index is not None:
            rows = itertools.islice(rows, index, index + 1)
        for _, temp, humidity in rows:
            yield numeric_pair(Decimal(temp) if temp else None, humidity or None)


def read_json(path, index):
    with path.open(encoding="utf-8") as stream:
        rows = json.load(stream, parse_float=Decimal)
    if index is not None:
        rows = (rows[index],)
    for row in rows:
        yield numeric_pair(row[FIELDS[1]], row[FIELDS[2]])


def read_parquet(path, index):
    import pyarrow.parquet as pq

    with pq.ParquetFile(path) as source:
        if index is not None:
            group, offset = divmod(index, PARQUET_GROUP_HOURS)
            table = source.read_row_group(group, columns=SENSOR_FIELDS, use_threads=False)
            yield numeric_pair(table[0][offset].as_py(), table[1][offset].as_py())
        else:
            for batch in source.iter_batches(columns=SENSOR_FIELDS, batch_size=1024, use_threads=False):
                temperatures, humidities = batch.columns
                for temp, humidity in zip(temperatures.to_pylist(), humidities.to_pylist()):
                    yield numeric_pair(temp, humidity)


def read_binary(path, index):
    with ObservationBlock(path) as block:
        if index is not None:
            yield block.at(index)
        else:
            yield from block.range(0, block.count)


READERS = {"bin": read_binary, "csv": read_csv, "json": read_json, "parquet": read_parquet}


def read_workload(root, file_format, workload, year, lookups):
    if file_format == "parquet":
        # Inicializa a dependência antes do cronômetro.
        import pyarrow.parquet

    paths = sorted(root.glob(f"*.{file_format}"))
    if workload == "ponto":
        rng = random.Random(BASE_SEED)
        hours = hours_in_year(year)
        requests = [(rng.choice(paths), rng.randrange(hours)) for _ in range(lookups)]
    else:
        requests = [(path, None) for path in paths]

    read = READERS[file_format]
    digest = hashlib.sha256()
    slots = 0
    started = time.perf_counter()
    for path, index in requests:
        for row in read(path, index):
            temp, humidity = row if row is not None else (None, None)
            digest.update(struct.pack(
                "<hB",
                -32768 if temp is None else int(temp * 10),
                255 if humidity is None else int(humidity),
            ))
            slots += 1
    elapsed = time.perf_counter() - started
    expected_slots = lookups if workload == "ponto" else len(paths) * hours_in_year(year)
    if slots != expected_slots:
        raise RuntimeError(f"{file_format}: {slots} horas lidas, esperado {expected_slots}")
    print(json.dumps({
        "seconds": elapsed,
        "rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "digest": digest.hexdigest(),
    }))


def run_child(*arguments):
    command = [sys.executable, str(Path(__file__).resolve()), *(str(arg) for arg in arguments)]
    return subprocess.check_output(command, text=True)


def measure(root, year, stations, lookups, repeats):
    print(run_child("_write", root, stations, year), end="")
    print(f"Estações {stations} | ano {year} | consultas {lookups} | repetições {repeats}")
    print("| Formato | Bytes | Ponto (ms/consulta) | Pico RSS ponto (MiB) | Série (s) | Pico RSS série (MiB) |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    digests = {}
    for file_format in FORMATS:
        size = sum(path.stat().st_size for path in root.glob(f"*.{file_format}"))
        metrics = []
        for workload in ("ponto", "série"):
            runs = [
                json.loads(run_child("_read", root, file_format, workload, year, lookups))
                for _ in range(repeats)
            ]
            expected_digest = digests.setdefault(workload, runs[0]["digest"])
            if any(run["digest"] != expected_digest for run in runs):
                raise RuntimeError(f"{file_format}: conteúdo divergente na carga {workload}")
            seconds = statistics.median(run["seconds"] for run in runs)
            rss_mib = max(run["rss_mib"] for run in runs)
            metrics.extend((seconds * 1000 / lookups if workload == "ponto" else seconds, rss_mib))
        point, point_rss, series, series_rss = metrics
        print(f"| {file_format} | {size} | {point:.4f} | {point_rss:.1f} | {series:.4f} | {series_rss:.1f} |")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stations", type=int, default=8)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--lookups", type=int, default=250)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.year <= 9999 or min(args.stations, args.lookups, args.repeats) < 1:
        parser.error("informe ano entre 1 e 9999 e contagens maiores que zero")
    with tempfile.TemporaryDirectory(prefix="nuvem-inmet-bench-") as temporary:
        measure(Path(temporary), args.year, args.stations, args.lookups, args.repeats)


if __name__ == "__main__":
    if sys.argv[1:2] == ["_write"]:
        write_dataset(Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    elif sys.argv[1:2] == ["_read"]:
        read_workload(Path(sys.argv[2]), sys.argv[3], sys.argv[4], int(sys.argv[5]), int(sys.argv[6]))
    else:
        main()
