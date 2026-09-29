"""Расстояния по сети дорог OpenStreetMap между точками зон (OSRM).

Прямая × коэффициент извилистости не видит реки, железные дороги и развязки: два
дома на разных берегах Москвы-реки по прямой рядом, а по дороге — через мост в
нескольких километрах. Справочник `road_distances.json` хранит для каждой зоны
матрицу расстояний по дорогам между офисом и адресами заявок — для машины, пешехода
и велосипеда. Его строит `sims.road_matrix` один раз (OSRM на сервере FOSSGIS), при
расчёте плана сеть не нужна.

Плечо между двумя точками одной зоны берёт расстояние отсюда, но не меньше прямой:
OSRM меряет между точками, притянутыми к дороге, и у соседних домов на одной улице
получает ноль. Точек нет в справочнике — новая заявка с произвольным адресом, чужая
зона, общественный транспорт — плечо считается прежней формулой.

Справочник только дополняется: значения уже посчитанных пар не меняются, иначе
обещанные плечи опубликованных планов перестали бы сходиться у валидатора.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from app.schemas import Coordinates, Transport

ROAD_MATRIX_PATH = Path("/data/fixtures/road_distances.json")
# Профиль OSRM для транспорта бригады; общественного транспорта в OSRM нет.
PROFILE_BY_TRANSPORT: dict[Transport, str] = {
    Transport.CAR: "car",
    Transport.WALKING: "walking",
    Transport.BICYCLE: "bicycle",
}
Point = tuple[float, float]


def point_key(point: Coordinates) -> Point:
    return round(point.latitude, 6), round(point.longitude, 6)


@dataclass(frozen=True)
class RoadMatrix:
    """Матрицы расстояний по зонам: точка → номер строки, профиль → метры."""

    positions: dict[Point, dict[str, int]] = field(default_factory=dict)
    distances: dict[str, dict[str, list[list[int | None]]]] = field(default_factory=dict)
    sha256: str | None = None

    def meters(
        self, origin: Coordinates, destination: Coordinates, transport: Transport
    ) -> int | None:
        profile = PROFILE_BY_TRANSPORT.get(transport)
        if profile is None:
            return None
        origin_zones = self.positions.get(point_key(origin))
        destination_zones = self.positions.get(point_key(destination))
        if not origin_zones or not destination_zones:
            return None
        for zone, row in origin_zones.items():
            column = destination_zones.get(zone)
            if column is None:
                continue
            matrix = self.distances.get(zone, {}).get(profile)
            if matrix is None:
                return None
            return matrix[row][column]
        return None


def parse_road_matrix(payload: dict[str, Any], sha256: str | None = None) -> RoadMatrix:
    positions: dict[Point, dict[str, int]] = {}
    distances: dict[str, dict[str, list[list[int | None]]]] = {}
    zones = payload.get("zones")
    if not isinstance(zones, dict):
        return RoadMatrix(sha256=sha256)
    for zone, item in zones.items():
        for index, (latitude, longitude) in enumerate(item["points"]):
            positions.setdefault((round(latitude, 6), round(longitude, 6)), {})[zone] = index
        distances[zone] = {
            profile: item[profile] for profile in PROFILE_BY_TRANSPORT.values() if profile in item
        }
    return RoadMatrix(positions=positions, distances=distances, sha256=sha256)


@cache
def road_matrix() -> RoadMatrix:
    """Справочник из data/fixtures; нет файла — пустой, все плечи по формуле."""
    if not ROAD_MATRIX_PATH.exists():
        return RoadMatrix()
    raw = ROAD_MATRIX_PATH.read_bytes()
    return parse_road_matrix(json.loads(raw), hashlib.sha256(raw).hexdigest())


def road_network_summary() -> dict[str, object] | None:
    """Что покрывает справочник — для интерфейса: источник, профили, точки по зонам."""
    matrix = road_matrix()
    if not matrix.distances:
        return None
    return {
        "source": "OSRM, дороги OpenStreetMap",
        "attribution": "© участники OpenStreetMap, лицензия ODbL",
        "profiles": sorted({profile for zone in matrix.distances.values() for profile in zone}),
        "points_by_zone": {
            zone: len(next(iter(profiles.values()), []))
            for zone, profiles in matrix.distances.items()
        },
        "sha256": matrix.sha256,
    }


def road_meters(
    origin: Coordinates, destination: Coordinates, transport: Transport
) -> int | None:
    """Расстояние по сети дорог между точками одной зоны или None — считать формулой."""
    return road_matrix().meters(origin, destination, transport)
