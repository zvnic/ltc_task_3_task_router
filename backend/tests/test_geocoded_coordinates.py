"""Координаты заявок по адресу из OpenStreetMap вместо демо-точек районов."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.imports.address_query import house_base_key, house_key, parse_address
from app.imports.csv_reader import normalize_building_address
from app.planning.routing import haversine_meters
from app.schemas import Coordinates, CoordinateSource
from app.schemas.domain import coordinate_quality
from app.services import datasets
from app.services.datasets import (
    _coordinate_for,
    coordinates_note,
    geocoded_addresses,
    get_service_zone,
)


@pytest.mark.parametrize(
    ("address", "district", "expected"),
    [
        (
            "Город Москва, пр-кт.Волгоградский, д. 97 к 1",
            "Кузьминки",
            "Волгоградский проспект, 97 к1, Москва",
        ),
        (
            "МО, г. Кашира Кржижановского ул. д. 5/1",
            "Кашира",
            "улица Кржижановского, 5/1, Кашира",
        ),
        (
            "Город Москва, б-р.Самаркандский Квартал 137а, д. к5",
            "Выхино-Жулебино",
            "Самаркандский бульвар, 137А к5, Москва",
        ),
        (
            "Город Москва, ул.Международная, д. 28 стр. 1",
            "Таганский",
            "Международная улица, 28 с1, Москва",
        ),
        (
            "Город Москва, ул.3-я Институтская, д. 5 к 2",
            "Рязанский",
            "3-я Институтская улица, 5 к2, Москва",
        ),
        (
            "Город Москва, ул.Юных Ленинцев, д. 84",
            "Кузьминки",
            "улица Юных Ленинцев, 84, Москва",
        ),
    ],
)
def test_address_is_rewritten_in_openstreetmap_form(
    address: str, district: str, expected: str
) -> None:
    precision, query = parse_address(address, district).variants()[0]
    assert precision == "house"
    assert query == expected


def test_variants_go_from_house_to_street() -> None:
    query = parse_address("Город Москва, ул.Дубининская, д. 59 к 2", "Даниловский")
    assert [precision for precision, _ in query.variants()] == ["house", "house_base", "street"]
    assert query.variants()[-1][1] == "Дубининская улица, Москва"


def test_settlement_suffix_is_dropped() -> None:
    query = parse_address(
        "обл.Московская область, г.Домодедово, пгт.Востряково-1, ул.Жуковского, д. 14/18",
        "Домодедово",
    )
    assert query.city == "Домодедово"
    assert query.settlement == "Востряково"


def test_house_keys_ignore_spelling_of_building_parts() -> None:
    assert house_key("97 корп. 1") == house_key("97к1") == house_key("97 к1")
    assert house_key("28 стр. 1") == house_key("28с1")
    assert house_base_key("83с4") == "83"
    assert house_base_key("9Б стр. 1") == "9б"
    assert house_base_key("24/30 с1") == "24/30"
    assert house_base_key("137А к5") == "137а"


def test_coordinate_quality_is_the_weakest_source() -> None:
    assert coordinate_quality([]) == CoordinateSource.VERIFIED.value
    assert coordinate_quality([CoordinateSource.GEOCODED, CoordinateSource.VERIFIED]) == (
        CoordinateSource.GEOCODED.value
    )
    assert coordinate_quality(
        [CoordinateSource.GEOCODED, CoordinateSource.SYNTHETIC, CoordinateSource.VERIFIED]
    ) == CoordinateSource.SYNTHETIC.value


def test_found_address_gets_its_osm_point(monkeypatch: pytest.MonkeyPatch) -> None:
    zone = get_service_zone("vostok")
    address = "Город Москва, ул.Юных Ленинцев, д. 84"
    point = {"latitude": 55.7001, "longitude": 37.7702}
    monkeypatch.setattr(
        datasets,
        "geocoded_addresses",
        lambda: {"addresses": {normalize_building_address(address): point}, "offices": {}},
    )
    coordinates, source = _coordinate_for(address, "Кузьминки", zone, 0.02)
    assert coordinates == point
    assert source == CoordinateSource.GEOCODED.value

    # не найденный адрес остаётся на демо-точке района, стабильно
    missing = "Город Москва, ул.Несуществующая, д. 1"
    first, first_source = _coordinate_for(missing, "Кузьминки", zone, 0.02)
    again, _ = _coordinate_for(missing, "Кузьминки", zone, 0.02)
    assert first_source == CoordinateSource.SYNTHETIC.value
    assert first == again
    center = zone["district_centers"]["Кузьминки"]
    assert abs(first["latitude"] - center["latitude"]) <= 0.01


def test_coordinates_note_counts_sources() -> None:
    def request(source: CoordinateSource) -> Any:
        return SimpleNamespace(coordinate_source=source.value)

    mixed = [request(CoordinateSource.GEOCODED)] * 3 + [request(CoordinateSource.SYNTHETIC)]
    note = coordinates_note(mixed)
    assert "3 из 4" in note
    assert "1 — демонстрационные точки" in note
    only_demo = coordinates_note([request(CoordinateSource.SYNTHETIC)])
    assert "Демонстрационные точки" in only_demo


def test_geocoded_fixture_stays_inside_zone_districts() -> None:
    fixture = geocoded_addresses()
    if not fixture["addresses"]:
        pytest.skip("справочник координат не приложен")
    assert "OpenStreetMap" in fixture["attribution"]
    for code, office in fixture["offices"].items():
        zone = get_service_zone(code)
        demo = Coordinates(**zone["office"])
        found = Coordinates(latitude=office["latitude"], longitude=office["longitude"])
        # офис найден по адресу рядом с прежней демо-точкой зоны
        assert haversine_meters(demo, found) < 3_000
    for item in fixture["addresses"].values():
        assert item["precision"] in {"house", "house_base", "street"}
        center = get_service_zone(item["zone"])["district_centers"][item["district"]]
        distance = haversine_meters(
            Coordinates(**center),
            Coordinates(latitude=item["latitude"], longitude=item["longitude"]),
        )
        limit = 15_000 if item["district"] in {"Домодедово", "Кашира", "Ступино"} else 8_000
        assert distance <= limit, item["address_raw"]
