"""Comandos para sincronizar e consultar o acervo."""

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from nuvem_inmet.validation import utc_hour, utc_interval


def _hour(text: str) -> datetime:
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=UTC)
        return utc_hour(datetime.fromisoformat(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Use AAAA-MM-DD ou um horário ISO 8601 com fuso, como 2024-02-29T01:00Z."
        ) from error


def _clock(text: str) -> time:
    try:
        value = time.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use HH:MM em UTC.") from error
    if len(text) != 5 or value.tzinfo is not None:
        raise argparse.ArgumentTypeError("Use HH:MM em UTC.")
    return value


def _decimal(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("Informe um número decimal.") from error
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("Informe um número finito e positivo.")
    return value


def _window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", required=True, type=_hour, help="Início inclusivo em UTC")
    parser.add_argument("--end", required=True, type=_hour, help="Fim exclusivo em UTC")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=os.environ.get("NUVEM_INMET_DATA", "data"),
        help="Diretório do acervo local (padrão: data)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="Sincronizar dados oficiais ou estimar o custo do BigQuery")
    sync.add_argument("--source", choices=("inmet", "archive", "conventional", "bigquery"), default="inmet")
    sync.add_argument("--archive", type=Path, action="append", help="ZIP anual oficial do INMET. Repita por ano.")
    sync.add_argument("--bdmep-csv", type=Path, action="append", help="CSV horário do BDMEP convencional. Repita para mais de um arquivo.")
    sync.add_argument("--timeout", type=float, default=30.0, help="Limite em segundos por requisição ao INMET")
    sync.add_argument("mode", choices=("historical", "incremental", "reconcile"))
    _window(sync)
    sync.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    sync.add_argument("--location", help="Localização dos jobs no BigQuery")
    sync.add_argument("--station", action="append", help="Código da estação. Repita para incluir outras.")
    sync.add_argument("--region-prefix", default="41", help="Prefixo IBGE da amostra (Paraná: 41)")
    sync.add_argument("--station-limit", type=int, default=3, help="Tamanho da amostra padrão")
    sync.add_argument("--max-bytes", type=int, default=100_000_000, help="Limite cumulativo em bytes")
    sync.add_argument("--max-cost", type=_decimal, help="Teto monetário na mesma moeda de --price-per-tib")
    sync.add_argument("--price-per-tib", type=_decimal, help="Preço on-demand por TiB na sua região")
    sync.add_argument(
        "--allow-billed-queries", action="store_true",
        help="Autoriza executar consultas potencialmente cobradas. O padrão é apenas estimar.",
    )

    service = commands.add_parser("run", help="Iniciar a carga histórica e a atualização diária")
    service.add_argument("--station", help="Restringe a coleta a uma estação convencional")
    service.add_argument("--start", type=_hour, default="2020-01-01",
                         help="Início da carga (padrão: 2020-01-01 UTC)")
    service.add_argument("--archive", type=Path, action="append", default=[],
                         help="ZIP anual do INMET para a carga nacional. Repita por ano.")
    service.add_argument("--offline", action="store_true", help="Usa apenas os ZIPs e CSVs locais")
    service.add_argument("--mirror-history", action="store_true",
                         help="Importa o espelho nacional em Parquet de 2020 a 2025")
    service.add_argument("--history-days", type=int, default=31,
                         help="Dias de histórico nacional por ciclo da API, de 0 a 31")
    service.add_argument("--at", type=_clock, default="08:00", help="Horário diário em UTC")
    service.add_argument("--bdmep-csv", type=Path, action="append", default=[],
                         help="CSV local para a carga histórica. Repita para mais de um arquivo.")
    service.add_argument("--bdmep-url", default=os.environ.get("INMET_BDMEP_CSV_URL"),
                         help="Endereço HTTPS de um CSV horário do BDMEP, mantido atualizado pela fonte")
    service.add_argument("--report", type=Path, help="Destino do relatório CSV")
    service.add_argument("--once", action="store_true", help="Executar um ciclo e sair")

    stations = commands.add_parser("stations", help="Listar as estações locais")
    stations.add_argument("--at", type=_hour, help="Horário de vigência das coordenadas")
    point = commands.add_parser("point", help="Consultar uma hora")
    point.add_argument("station")
    point.add_argument("at", type=_hour)

    gaps = commands.add_parser("gaps", help="Listar intervalos sem registro")
    gaps.add_argument("station")
    _window(gaps)
    export = commands.add_parser("export", help="Exportar série local para a saída padrão")
    export.add_argument("station")
    export.add_argument("--format", choices=("csv", "json"), default="csv")
    _window(export)
    monthly = commands.add_parser("report-monthly", help="Gerar resumo mensal de temperatura e umidade em CSV")
    monthly.add_argument("station", nargs="?", help="Se omitida, gera o CSV nacional")
    _window(monthly)

    for command, description in (
        ("status", "Consultar disponibilidade e sincronizações"),
        ("verify", "Verificar os checksums do acervo"),
    ):
        inspector = commands.add_parser(command, help=description)
        inspector.add_argument("--station")
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str, indent=2))


def _sync(args: argparse.Namespace) -> None:
    from nuvem_inmet.storage import Storage
    from nuvem_inmet.sync import synchronize

    start, end = utc_interval(args.start, args.end)
    if args.archive and args.source != "archive":
        raise ValueError("Use --source archive com --archive.")
    if args.bdmep_csv and args.source != "conventional":
        raise ValueError("Use --source conventional com --bdmep-csv.")
    if args.allow_billed_queries and args.source != "bigquery":
        raise ValueError("--allow-billed-queries é exclusivo do BigQuery.")

    if args.source == "bigquery":
        from nuvem_inmet.collectors import BigQuerySource

        source = BigQuerySource(
            args.project,
            max_bytes=args.max_bytes,
            execute=args.allow_billed_queries,
            region_prefix=args.region_prefix,
            station_limit=args.station_limit,
            location=args.location,
            max_cost=args.max_cost,
            price_per_tib=args.price_per_tib,
        )
        if not args.allow_billed_queries:
            breakdown = source.estimate(args.station, start, end)
            _json({
                "queries": [asdict(item) for item in breakdown],
                "estimated_billed_bytes": sum(item.estimated_billed_bytes for item in breakdown),
                "max_bytes": source.max_bytes,
            })
            return
    elif args.source == "archive":
        from nuvem_inmet.collectors.archive import InmetArchive

        if not args.archive:
            raise ValueError("Informe pelo menos um ZIP oficial em --archive.")
        source = InmetArchive(args.archive, station_limit=args.station_limit)
    elif args.source == "conventional":
        from nuvem_inmet.collectors import InmetConventionalCSV

        if not args.bdmep_csv or not args.station:
            raise ValueError("Informe o CSV horário em --bdmep-csv e o código WMO em --station.")
        source = InmetConventionalCSV(args.bdmep_csv)
    else:
        from nuvem_inmet.collectors import INMETSource

        source = INMETSource(station_limit=args.station_limit, timeout=args.timeout)

    result = synchronize(Storage(args.data_dir), source, args.mode, start, end, codes=args.station)
    _json(asdict(result))


def _local(args: argparse.Namespace) -> None:
    from nuvem_inmet.query import Query

    query = Query(args.data_dir)
    match args.command:
        case "stations":
            _json([asdict(station) for station in query.stations(at=args.at)])
        case "point":
            observation = query.observation(args.station, args.at)
            _json(None if observation is None else asdict(observation))
        case "gaps":
            _json([asdict(gap) for gap in query.gaps(args.station, args.start, args.end)])
        case "status":
            _json(asdict(query.status(args.station)))
        case "verify":
            count = query.verify(args.station)
            print(f"Checksums verificados: {count} objetos.")
        case "report-monthly":
            from nuvem_inmet.query.monthly import export_monthly, export_national_monthly

            if args.station:
                export_monthly(query, args.station, args.start, args.end, sys.stdout)
            else:
                export_national_monthly(query, args.start, args.end, sys.stdout)
        case "export":
            exporter = query.export_csv if args.format == "csv" else query.export_json
            exporter(args.station, args.start, args.end, sys.stdout)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "sync":
            _sync(args)
        elif args.command == "run":
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            if args.station:
                from nuvem_inmet.service import run

                if args.archive or args.offline or args.mirror_history:
                    raise ValueError("--archive, --offline e --mirror-history são opções da carga nacional.")
                report = args.report or args.data_dir / f"relatorio-{args.station}.csv"
                run(args.data_dir, args.station, args.start, args.at, files=args.bdmep_csv,
                    url=args.bdmep_url, report=report, once=args.once)
            else:
                from nuvem_inmet.national import run_national

                report = args.report or args.data_dir / "relatorio-brasil.csv"
                run_national(
                    args.data_dir, args.start, args.at, archives=args.archive,
                    conventional=args.bdmep_csv, url=args.bdmep_url, report=report,
                    offline=args.offline, mirror_history=args.mirror_history,
                    history_days=args.history_days, once=args.once,
                )
        else:
            _local(args)
    except KeyboardInterrupt:
        print("Operação interrompida.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
