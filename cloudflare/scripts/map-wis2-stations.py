"""Match WIS2 stations against a published INMET annual automatic-station ZIP."""

import argparse
import json
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile


REGISTRY = "https://wis2bra.inmet.gov.br/oapi/collections/stations/items?f=json&limit=2000"
OUTPUT = Path(__file__).resolve().parents[1] / "src" / "wis2-stations.json"


def normalize_name(name):
    decomposed = unicodedata.normalize("NFKD", name.upper())
    return re.sub(r"[^A-Z0-9]", "", decomposed)


def automatic_stations(path):
    stations = defaultdict(list)
    with ZipFile(path) as archive:
        for filename in archive.namelist():
            match = re.search(r"_([A-Z][0-9]{3})_", filename)
            if not match or not filename.lower().endswith(".csv"):
                continue
            with archive.open(filename) as source:
                header = [
                    source.readline().decode("latin-1").split(";", 1)[-1].strip()
                    for _ in range(8)
                ]
            _, _, name, code, latitude, longitude, *_ = header
            if code != match[1]:
                raise ValueError(f"Código divergente no arquivo {filename}")
            stations[normalize_name(name)].append((
                code, float(latitude.replace(",", ".")), float(longitude.replace(",", ".")),
            ))
    return stations


def distance_m(lat, lon, other_lat, other_lon):
    return 111_320 * math.hypot(lat - other_lat, (lon - other_lon) * math.cos(math.radians(lat)))


def match_registry(stations, registry):
    matches = defaultdict(list)
    for feature in registry:
        properties = feature["properties"]
        wigos = properties["wigos_station_identifier"]
        topics = properties.get("topics", [])
        if not wigos.startswith("0-76-0-") or not any(topic.endswith("/synop") for topic in topics):
            continue
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            continue
        lon, lat, *_ = geometry["coordinates"]
        candidates = [
            code for code, other_lat, other_lon in stations[normalize_name(properties["name"])]
            if distance_m(lat, lon, other_lat, other_lon) <= 100
        ]
        if len(candidates) == 1:
            matches[candidates[0]].append((wigos, properties.get("status")))
    mapping = {}
    for code, identifiers in matches.items():
        active = [wigos for wigos, status in identifiers if status == "operational"]
        if len(identifiers) == 1:
            mapping[identifiers[0][0]] = code
        elif len(active) == 1:
            mapping[active[0]] = code
    return mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="ZIP anual de estações automáticas do INMET")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    request = Request(REGISTRY, headers={"Accept": "application/json"})
    with urlopen(request, timeout=20) as response:
        registry = json.load(response)
    features = registry["features"]
    if not features or registry["numberMatched"] != len(features):
        raise ValueError("Catálogo WIS2 ausente ou incompleto")
    stations = automatic_stations(args.archive)
    mapping = match_registry(stations, features)
    if not mapping:
        raise ValueError("Nenhuma estação automática foi vinculada")
    args.output.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(mapping)} estações vinculadas de {sum(map(len, stations.values()))} no ZIP")


if __name__ == "__main__":
    main()
