"""Prepare hourly INMET CSV archives for the cloud API with bounded memory."""

import argparse
import codecs
import csv
import gzip
import hashlib
import json
import re
import struct
from collections import Counter
from contextlib import ExitStack
from datetime import date
from io import TextIOWrapper
from itertools import groupby
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from nuvem_inmet.collectors.archive import (
    _DATE_FIELDS, _HOUR_FIELDS, _MEMBER_NAME, _column,
    _label, _numeric, _read_preamble, _timestamp,
)

FIELD_FILE = Path(__file__).resolve().parents[1] / "src" / "fields.json"
FIELDS = json.loads(FIELD_FILE.read_text(encoding="utf-8"))
EPOCH = date(1970, 1, 1)
HEADER = struct.Struct(">4siH")
ENTRY = struct.Struct(">5sI")
LIMIT = 2 * 1024 * 1024


def station_rows(rows, header, start, end):
    hour_column = _column(header, _HOUR_FIELDS)
    positions = [_column(header, set(field["csv"])) for field in FIELDS]
    latest = None
    for row in rows:
        if not any(row):
            continue
        stamp = _timestamp(row[0], row[hour_column])
        if latest is not None and stamp <= latest:
            raise ValueError(f"Hora fora de ordem ou repetida: {stamp}")
        latest = stamp
        if start <= stamp.date() < end:
            values = [_numeric(row[position]) for position in positions]
            for i, field in enumerate(FIELDS):
                value = values[i]
                if value is not None and field["source"].startswith("UMD_") and not 0 <= value <= 100:
                    raise ValueError(f"Umidade inválida em {stamp}: {value}")
            yield stamp.date(), [
                stamp.hour,
                *(None if value is None else str(value) for value in values),
            ]


def station_members(source, stack):
    if source.is_dir():
        for path in sorted(source.iterdir()):
            if _MEMBER_NAME.fullmatch(path.name):
                yield path.name, path.open("rb")
    else:
        archive = stack.enter_context(ZipFile(source))
        for name in sorted(archive.namelist()):
            basename = PurePosixPath(name).name
            if _MEMBER_NAME.fullmatch(basename):
                yield basename, archive.open(name)


def write_fragment(stream, code, rows):
    body = json.dumps(rows, separators=(",", ":")).encode()
    compressed = gzip.compress(body, compresslevel=6, mtime=0)
    stream.write(ENTRY.pack(code.encode().ljust(5, b" "), len(compressed)))
    stream.write(compressed)


def pack_day(day, fragment):
    stations = {}
    with fragment.open("rb") as source:
        while prefix := source.read(ENTRY.size):
            code, length = ENTRY.unpack(prefix)
            payload = source.read(length)
            if code in stations or len(payload) != length:
                raise ValueError(f"Estação repetida ou fragmento incompleto em {day}: {code!r}")
            stations[code] = payload
    return len(stations), assemble_day(day, stations)


def assemble_day(day, stations):
    index = bytearray(HEADER.pack(b"NIM2", (day - EPOCH).days, len(stations)))
    offset = len(index) + len(stations) * ENTRY.size
    parts = []
    for code, payload in sorted(stations.items()):
        index.extend(ENTRY.pack(code, offset))
        parts.append(payload)
        offset += len(payload)
    if offset > LIMIT:
        raise ValueError(f"Lote de {day} acima de {LIMIT} bytes.")
    return index + b"".join(parts)


def build(source, start, end, output):
    output.mkdir(parents=True, exist_ok=True)
    totals = Counter()
    with TemporaryDirectory() as staging, ExitStack() as stack:
        files = {}
        for name, raw in station_members(source, stack):
            member = _MEMBER_NAME.fullmatch(name)
            with raw:
                try:
                    codecs.getincrementaldecoder("utf-8-sig")().decode(raw.peek(4096))
                except UnicodeDecodeError:
                    encoding = "cp1252"
                else:
                    encoding = "utf-8-sig"
                with TextIOWrapper(raw, encoding=encoding, newline="") as stream:
                    csv_rows = csv.reader(stream, delimiter=";", strict=True)
                    metadata, header = _read_preamble(csv_rows)
                    if metadata.get("CODIGO WMO") != member["code"]:
                        raise ValueError(f"Código de estação divergente no CSV {name}.")
                    for day, observations in groupby(
                        station_rows(csv_rows, header, start, end), key=lambda item: item[0]
                    ):
                        if day not in files:
                            path = Path(staging) / (day.isoformat() + ".tmp")
                            files[day] = stack.enter_context(path.open("wb"))
                        rows = [row for _, row in observations]
                        totals[day] += len(rows)
                        write_fragment(files[day], member["code"], rows)
        for handle in files.values():
            handle.flush()
        if not files:
            raise ValueError("Nenhuma observação no intervalo.")
        manifest = []
        for day, handle in sorted(files.items()):
            count, packed = pack_day(day, Path(handle.name))
            manifest.append(write_day(output, day, count, totals[day], packed))
    write_manifest(output, manifest)


def write_day(output, day, stations, observations, packed):
    path = output / (day.isoformat() + ".bin")
    path.write_bytes(packed)
    return {
        "day": day.isoformat(), "stations": stations, "observations": observations,
        "bytes": len(packed), "sha256": hashlib.sha256(packed).hexdigest(),
    }


def write_manifest(output, manifest):
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"{len(manifest)} dias, {sum(day['observations'] for day in manifest)} linhas, "
        f"{sum(day['bytes'] for day in manifest)} bytes."
    )



def pack_consolidated_day(path, day):
    by_station = {}
    with path.open(encoding="utf-8", newline="") as stream:
        for code, clock, *readings in csv.reader(stream, strict=True):
            stamp = _timestamp(day.isoformat(), clock)
            if not re.fullmatch(r"[A-Z][0-9]{3}", code):
                raise ValueError(f"Estação automática inválida: {code!r}")
            decimals = [_numeric(value) for value in readings]
            values = [None if value is None else str(value) for value in decimals]
            hours = by_station.setdefault(code, {})
            previous = hours.get(stamp.hour)
            if previous is not None and previous != values:
                raise ValueError(f"Observações divergentes para {code} em {stamp}.")
            hours[stamp.hour] = values
    packed_stations = {
        code.encode().ljust(5, b" "): gzip.compress(
            json.dumps([[hour, *values] for hour, values in sorted(hours.items())],
                       separators=(",", ":")).encode(),
            compresslevel=6, mtime=0,
        )
        for code, hours in by_station.items()
    }
    return len(by_station), sum(map(len, by_station.values())), assemble_day(day, packed_stations)


def build_consolidated(source, start, end, output):
    output.mkdir(parents=True, exist_ok=True)
    start_text, end_text = start.isoformat(), end.isoformat()
    with TemporaryDirectory() as staging:
        paths = {}
        with ExitStack() as stack:
            stream = stack.enter_context(source.open(encoding="utf-8-sig", newline=""))
            rows = csv.reader(stream, strict=True)
            header = [_label(cell) for cell in next(rows)]
            date_index = _column(header, _DATE_FIELDS)
            hour_index = _column(header, _HOUR_FIELDS)
            code_index = _column(header, {"ESTACAO", "CODIGO WMO"})
            positions = [_column(header, set(field["csv"])) for field in FIELDS]
            writers = {}
            for row in rows:
                iso_day = row[date_index]
                if not start_text <= iso_day < end_text:
                    continue
                if iso_day not in writers:
                    day = date.fromisoformat(iso_day)
                    path = Path(staging) / (day.isoformat() + ".csv")
                    paths[day] = path
                    part = stack.enter_context(path.open("w", encoding="utf-8", newline=""))
                    writers[iso_day] = csv.writer(part)
                writers[iso_day].writerow((row[code_index], row[hour_index],
                                           *(row[position] for position in positions)))
        if not paths:
            raise ValueError("Nenhuma observação no intervalo.")
        manifest = [write_day(output, day, *pack_consolidated_day(path, day))
                    for day, path in sorted(paths.items())]
    write_manifest(output, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="ZIP, diretório de estações ou CSV consolidado do INMET")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="Data UTC exclusiva")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.start >= args.end:
        parser.error("O fim exclusivo deve ser posterior ao início.")
    if args.source.suffix.lower() == ".csv":
        build_consolidated(args.source, args.start, args.end, args.output)
    else:
        build(args.source, args.start, args.end, args.output)


if __name__ == "__main__":
    main()
