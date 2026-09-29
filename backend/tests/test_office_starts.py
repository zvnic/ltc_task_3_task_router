"""Every route in a service zone starts at that zone's office."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.services.datasets import (
    _build_start_assumptions,
    _office_start_location,
    geocoded_addresses,
    get_service_zone,
    planning_input_from_dataset,
)


def _fixture_zones_path() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "data" / "fixtures" / "service_zones.json",
        Path("/data/fixtures/service_zones.json"),
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("service_zones.json not found for tests")


def test_every_zone_uses_office_as_the_only_route_start() -> None:
    payload = json.loads(_fixture_zones_path().read_text(encoding="utf-8"))
    codes = {zone["code"] for zone in payload["zones"]}
    assert codes == {"vostok", "yugo_vostok", "yugocenter"}

    for zone in payload["zones"]:
        assert zone["office_address"]
        assert zone["start_policy"] == "office_address"
        assert "crew_bases" not in zone
        assert "engineer_bases" not in zone
        # офис берётся по адресу из OpenStreetMap, если найден, иначе демо-координата зоны
        office = geocoded_addresses()["offices"].get(zone["code"]) or zone["office"]
        assert _office_start_location(zone) == {
            "latitude": float(office["latitude"]),
            "longitude": float(office["longitude"]),
        }


def test_start_assumptions_expose_office_address_policy() -> None:
    zone = get_service_zone("yugo_vostok")
    meta = _build_start_assumptions(zone, office_address=zone["office_address"])
    assert meta["is_demo_enrichment"] is True
    assert meta["control_crew_count_hint"] == 12
    assert zone["office_address"] in meta["start_policy"]
    assert meta["start_policy_code"] == "office_address"
    assert "office_start_note" in meta
    assert "crew_bases_note" not in meta
    assert "work_class_mapping" in meta
    assert "emergency" in meta["work_class_mapping"]
    assert "skill:business" not in meta["work_class_mapping"]


def test_planning_snapshot_overrides_legacy_engineer_start_with_dataset_office() -> None:
    office = {"latitude": 55.5915, "longitude": 37.6764}
    engineer = SimpleNamespace(
        id=uuid4(),
        external_id="team-01",
        input_order=0,
        name="Бригада 1",
        start_location={"latitude": 54.8534, "longitude": 38.1908},
        shift_start=datetime.fromisoformat("2026-08-17T08:00:00+03:00"),
        shift_end=datetime.fromisoformat("2026-08-17T18:00:00+03:00"),
        skills=["local"],
        transport="car",
        equipment_inventory={},
        is_synthetic=True,
    )
    dataset = SimpleNamespace(
        id=uuid4(),
        revision=1,
        planning_date=date(2026, 8, 17),
        timezone="Europe/Moscow",
        office=office,
        requests=[],
        engineers=[engineer],
    )

    planning_input = planning_input_from_dataset(dataset)

    assert planning_input.engineers[0].start_location.model_dump() == office


def test_work_class_provenance_from_enrichment() -> None:
    from app.services.datasets import FIXTURE_ROOT, _enrichment_values, _load_json

    enrichment = _load_json(FIXTURE_ROOT / "enrichment_rules.json")
    _, priority, prov = _enrichment_values(enrichment, "Локальная заявка", "")
    assert priority == "normal"
    assert prov["work_class"] == "local"
    _, priority_u, prov_u = _enrichment_values(enrichment, "Глобальная проблема", "Авария")
    assert priority_u == "urgent"
    assert prov_u["work_class"] == "emergency"
    _, _, prov_c = _enrichment_values(enrichment, "Подключение", "")
    assert prov_c["work_class"] == "connection"
    _, _, prov_d = _enrichment_values(enrichment, "Дозаказ", "")
    assert prov_d["work_class"] == "add_order"
    _, priority_g, prov_g = _enrichment_values(enrichment, "Глобальная проблема", "")
    assert priority_g == "normal"
    assert prov_g["work_class"] == "add_order"
