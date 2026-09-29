"""Расстояние по дорогам OpenStreetMap между точками зоны (модель v6)."""

from __future__ import annotations

from collections.abc import Callable
from math import ceil

import pytest

from app.planning import road_matrix as road_matrix_module
from app.planning.road_matrix import RoadMatrix, parse_road_matrix
from app.planning.routing import (
    FORMULA_ROUTE_ESTIMATION_METHOD,
    TRANSPORT_ACCESS_MINUTES,
    TRANSPORT_PARAMETERS,
    edge_metrics,
    effective_edge_metrics,
    haversine_meters,
)
from app.schemas import Coordinates, Transport

# Два дома в Печатниках по разные стороны железной дороги: ~1,6 км по прямой
WEST = Coordinates(latitude=55.692, longitude=37.720)
EAST = Coordinates(latitude=55.692, longitude=37.745)
# Сосед на той же улице: OSRM притягивает обе точки к одной дороге
NEIGHBOUR = Coordinates(latitude=55.6922, longitude=37.7202)
ELSEWHERE = Coordinates(latitude=55.75, longitude=37.62)


def matrix(
    car: list[list[int | None]], walking: list[list[int | None]] | None = None
) -> RoadMatrix:
    zone: dict[str, object] = {
        "points": [
            [WEST.latitude, WEST.longitude],
            [EAST.latitude, EAST.longitude],
            [NEIGHBOUR.latitude, NEIGHBOUR.longitude],
        ],
        "car": car,
    }
    if walking is not None:
        zone["walking"] = walking
    return parse_road_matrix({"zones": {"pechatniki": zone}}, sha256="test")


UseMatrix = Callable[[RoadMatrix], None]


@pytest.fixture
def use_matrix(monkeypatch: pytest.MonkeyPatch) -> UseMatrix:
    def apply(value: RoadMatrix) -> None:
        monkeypatch.setattr(road_matrix_module, "road_matrix", lambda: value)

    return apply


def test_car_leg_uses_road_distance_and_the_same_speed_model(use_matrix: UseMatrix) -> None:
    use_matrix(matrix([[0, 4_800, 30], [4_700, 0, 4_790], [30, 4_810, 0]]))
    distance, minutes = edge_metrics(WEST, EAST, Transport.CAR)
    assert distance == 4_800
    speed_kmh, _ = TRANSPORT_PARAMETERS[Transport.CAR]
    assert minutes == ceil(4.8 / speed_kmh * 60) + TRANSPORT_ACCESS_MINUTES[Transport.CAR]
    # обратная дорога своя: односторонние улицы
    assert edge_metrics(EAST, WEST, Transport.CAR)[0] == 4_700
    # v5 — прежняя формула: прямая × коэффициент пути
    straight = haversine_meters(WEST, EAST)
    formula, _ = edge_metrics(WEST, EAST, Transport.CAR, method=FORMULA_ROUTE_ESTIMATION_METHOD)
    assert formula == round(straight * TRANSPORT_PARAMETERS[Transport.CAR][1])
    assert distance > formula


def test_road_distance_is_never_shorter_than_straight_line(use_matrix: UseMatrix) -> None:
    use_matrix(matrix([[0, 4_800, 0], [4_700, 0, 4_790], [0, 4_810, 0]]))
    distance, _ = edge_metrics(WEST, NEIGHBOUR, Transport.CAR)
    assert distance == round(haversine_meters(WEST, NEIGHBOUR))
    assert distance > 0


def test_points_outside_the_matrix_and_transit_keep_the_formula(use_matrix: UseMatrix) -> None:
    use_matrix(matrix([[0, 4_800, 30], [4_700, 0, 4_790], [30, 4_810, 0]], walking=None))
    formula_method = FORMULA_ROUTE_ESTIMATION_METHOD
    # новый адрес вне справочника
    assert edge_metrics(WEST, ELSEWHERE, Transport.CAR) == edge_metrics(
        WEST, ELSEWHERE, Transport.CAR, method=formula_method
    )
    # общественного транспорта в OSRM нет — модель v5
    assert edge_metrics(WEST, EAST, Transport.PUBLIC_TRANSPORT) == edge_metrics(
        WEST, EAST, Transport.PUBLIC_TRANSPORT, method=formula_method
    )
    # профиля пешехода в справочнике нет — формула
    assert edge_metrics(WEST, EAST, Transport.WALKING) == edge_metrics(
        WEST, EAST, Transport.WALKING, method=formula_method
    )


def test_unreachable_pair_falls_back_to_formula(use_matrix: UseMatrix) -> None:
    use_matrix(matrix([[0, None, 30], [4_700, 0, 4_790], [30, 4_810, 0]]))
    assert edge_metrics(WEST, EAST, Transport.CAR) == edge_metrics(
        WEST, EAST, Transport.CAR, method=FORMULA_ROUTE_ESTIMATION_METHOD
    )


def test_car_walks_to_the_neighbour_by_the_footpath_network(use_matrix: UseMatrix) -> None:
    use_matrix(
        matrix(
            [[0, 4_800, 420], [4_700, 0, 4_790], [420, 4_810, 0]],
            walking=[[0, 2_100, 60], [2_100, 0, 2_050], [60, 2_050, 0]],
        )
    )
    distance, minutes, mode = effective_edge_metrics(WEST, NEIGHBOUR, Transport.CAR)
    assert mode is Transport.WALKING
    assert (distance, minutes) == edge_metrics(WEST, NEIGHBOUR, Transport.WALKING)
    assert distance == 60


def test_empty_matrix_means_formula_everywhere(use_matrix: UseMatrix) -> None:
    use_matrix(RoadMatrix())
    for transport in Transport:
        assert edge_metrics(WEST, EAST, transport) == edge_metrics(
            WEST, EAST, transport, method=FORMULA_ROUTE_ESTIMATION_METHOD
        )


def test_attached_matrix_covers_zone_offices() -> None:
    from app.services.datasets import _office_start_location, get_service_zone

    value = road_matrix_module.road_matrix()
    if not value.distances:
        pytest.skip("справочник расстояний не приложен")
    for code in value.distances:
        office = _office_start_location(get_service_zone(code))
        point = Coordinates(latitude=office["latitude"], longitude=office["longitude"])
        assert code in value.positions[road_matrix_module.point_key(point)]
        size = len(value.distances[code]["car"])
        assert all(len(row) == size for row in value.distances[code]["car"])
