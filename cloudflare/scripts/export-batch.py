"""Exporta um dia do acervo local para o formato gzip consumido pelo Worker."""

import argparse
import gzip
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from nuvem_inmet.query import Query


def export_day(query: Query, day: date, output: Path, codes: list[str] | None = None) -> int:
    start = datetime.combine(day, time.min, UTC)
    end = start + timedelta(days=1)
    stations = codes if codes is not None else [station.code for station in query.stations()]
    batch = []
    for code in sorted(set(stations)):
        for slot in query.series(code, start, end):
            row = slot.observation
            if row is not None:
                batch.append([
                    code,
                    row.timestamp.hour,
                    None if row.temperature is None else str(row.temperature),
                    None if row.humidity is None else str(row.humidity),
                ])
    if not batch:
        raise ValueError(f"O acervo não tem observações em {day}.")
    with gzip.open(output, "wt", encoding="utf-8") as stream:
        json.dump(batch, stream, ensure_ascii=False, separators=(",", ":"))
    return len(batch)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--station", action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = export_day(Query(args.data_dir), args.date, args.output, args.station)
    print(f"{count} leituras de {args.date} exportadas para {args.output}")


if __name__ == "__main__":
    main()
