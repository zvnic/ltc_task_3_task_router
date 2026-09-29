from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.imports.csv_reader import audit_sources, normalize_building_address, parse_requests_csv
from app.models import ServiceRequest
from app.services.datasets import (
    planning_input_from_dataset,
    prepare_import_report,
    request_to_input,
)


def test_original_sources_have_expected_profile() -> None:
    source_root = Path("/data/source")
    report = audit_sources(
        (source_root / "vostok_synthetic.csv").read_bytes(),
        (source_root / "vostok_control.csv").read_bytes(),
    )
    assert report["requests"]["valid_count"] == 66
    assert report["requests"]["skipped_empty"] == 2
    assert report["requests"]["office_address"]
    assert report["control"]["encoding"] == "cp1251"
    assert report["control"]["rows"] == 66
    assert report["control"]["unique_matches"] == 66
    assert report["control"]["ambiguous"] == 0
    assert report["control"]["unmatched"] == 0
    assert report["control"]["id_intersection"] == 0


def test_three_service_zones_have_independent_complete_matches() -> None:
    source_root = Path("/data/source")
    expected = {
        "vostok": (66, 12, 66, 0),
        "yugo_vostok": (83, 12, 79, 4),
        "yugocenter": (56, 11, 56, 0),
    }
    for zone, (request_count, team_count, unique_matches, ambiguous) in expected.items():
        report = audit_sources(
            (source_root / f"{zone}_synthetic_utf8.csv").read_bytes(),
            (source_root / f"{zone}_control_utf8.csv").read_bytes(),
        )
        assert report["requests"]["encoding"] == "utf-8-sig"
        assert report["requests"]["valid_count"] == request_count
        assert report["requests"]["skipped_empty"] == 2
        assert report["control"]["unique_matches"] == unique_matches
        assert report["control"]["ambiguous"] == ambiguous
        assert report["control"]["unmatched"] == 0
        teams = report["control"]["profile"]["teams"]
        assert len([name for name in teams if name != "<не назначено>"]) == team_count


def test_connection_column_is_optional_for_zone_sources() -> None:
    content = (
        "Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес;"
        "Гигабитное подключение\n"
        "1;Подключение;Тест;17.08.2026 10:00;17.08.2026 12:00;Кузьминки;Москва;Нет\n"
    ).encode()
    report = parse_requests_csv(content)
    assert report["valid_count"] == 1
    assert report["rows"][0]["connection_type"] is None


def test_database_utc_window_is_serialized_in_dataset_timezone() -> None:
    row = ServiceRequest(
        id=uuid4(),
        dataset_id=uuid4(),
        external_id="midnight-window",
        input_order=0,
        address_raw="Кашира",
        address_normalized="кашира",
        district="Кашира",
        coordinates={"latitude": 54.8534, "longitude": 38.1908},
        coordinate_source="synthetic",
        duration_minutes=90,
        window_start=datetime.fromisoformat("2026-08-16T21:01:00+00:00"),
        window_end=datetime.fromisoformat("2026-08-17T20:59:00+00:00"),
        required_skill="emergency",
        required_transport=None,
        priority="normal",
        status="new",
        source_fields={},
        enrichment_rule_version="1",
    )
    item = request_to_input(row)
    assert item.window_start.isoformat() == "2026-08-17T00:01:00+03:00"
    assert item.window_end.isoformat() == "2026-08-17T23:59:00+03:00"


def test_terminal_requests_are_excluded_from_new_planning_snapshot() -> None:
    active = ServiceRequest(
        id=uuid4(),
        dataset_id=uuid4(),
        external_id="active",
        input_order=0,
        address_raw="Москва",
        address_normalized="москва",
        district="Москва",
        coordinates={"latitude": 55.75, "longitude": 37.61},
        coordinate_source="synthetic",
        duration_minutes=30,
        window_start=datetime.fromisoformat("2026-08-17T10:00:00+03:00"),
        window_end=datetime.fromisoformat("2026-08-17T12:00:00+03:00"),
        required_skill="local",
        required_transport=None,
        required_equipment={},
        priority="normal",
        status="new",
        completion_deadline=None,
        source_fields={},
        enrichment_rule_version="1",
    )
    completed = ServiceRequest(
        **{
            key: value
            for key, value in vars(active).items()
            if not key.startswith("_sa_") and key not in {"id", "external_id", "status"}
        },
        id=uuid4(),
        external_id="completed",
        status="completed",
    )
    dataset = SimpleNamespace(
        id=uuid4(),
        revision=1,
        planning_date=date(2026, 8, 17),
        timezone="Europe/Moscow",
        office={"latitude": 55.75, "longitude": 37.61},
        requests=[active, completed],
        engineers=[],
    )

    snapshot = planning_input_from_dataset(dataset)

    assert [request.external_id for request in snapshot.requests] == ["active"]


def test_building_normalization_removes_only_terminal_apartment() -> None:
    assert normalize_building_address("Москва, д. 2, кв. 59") == "москва, д. 2"
    assert normalize_building_address("Москва, кв. 2, д. 59") == "москва, кв. 2, д. 59"


def test_invalid_gigabit_value_is_reported() -> None:
    content = (
        "Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес;"
        "Подключение;Гигабитное подключение\n"
        "1;Подключение;Тест;17.08.2026 10:00;17.08.2026 12:00;Кузьминки;Москва;FMC;Может быть\n"
    ).encode()
    report = parse_requests_csv(content)
    assert report["valid_count"] == 0
    assert report["errors"] == [{"line": 2, "code": "invalid_gigabit"}]


CSV_HEADER = (
    "Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес;"
    "Подключение;Гигабитное подключение\n"
)


def test_empty_connection_is_valid_and_duplicate_id_is_reported() -> None:
    rows = (
        "1;Подключение;Тест;17.08.2026 10:00;17.08.2026 12:00;Кузьминки;Москва;;Нет\n"
        "1;Подключение;Тест;17.08.2026 12:00;17.08.2026 14:00;Кузьминки;Москва 2;;Да\n"
    )
    report = parse_requests_csv((CSV_HEADER + rows).encode())
    assert report["valid_count"] == 1
    assert report["rows"][0]["connection_type"] is None
    assert report["errors"] == [
        {"line": 3, "code": "duplicate_id", "external_id": "1", "first_line": 2}
    ]


def test_duplicate_row_can_be_explicitly_skipped_without_hiding_other_errors() -> None:
    duplicate_rows = (
        "1;Подключение;Тест;17.08.2026 10:00;17.08.2026 12:00;Кузьминки;Москва;;Нет\n"
        "1;Подключение;Тест;17.08.2026 12:00;17.08.2026 14:00;Кузьминки;Москва 2;;Да\n"
    )
    duplicate_report = parse_requests_csv((CSV_HEADER + duplicate_rows).encode())
    accepted = prepare_import_report(duplicate_report, skip_duplicate_rows=True)

    assert accepted["errors"] == []
    assert accepted["skipped_duplicate_rows"] == 1
    assert accepted["warnings"] == duplicate_report["errors"]
    assert accepted["valid_count"] == 1

    mixed_rows = duplicate_rows + (
        "2;Подключение;Тест;не дата;17.08.2026 14:00;Кузьминки;Москва 3;;Нет\n"
    )
    mixed_report = parse_requests_csv((CSV_HEADER + mixed_rows).encode())
    blocked = prepare_import_report(mixed_report, skip_duplicate_rows=True)

    assert {error["code"] for error in blocked["errors"]} == {
        "duplicate_id",
        "invalid_datetime",
    }


def test_invalid_date_has_source_line() -> None:
    row = "1;Подключение;Тест;не дата;17.08.2026 12:00;Кузьминки;Москва;;Нет\n"
    report = parse_requests_csv((CSV_HEADER + row).encode())
    assert report["errors"] == [{"line": 2, "code": "invalid_datetime", "value": "не дата"}]


def test_reference_audit_reports_ambiguous_composite_key() -> None:
    shared = "Подключение;Тест;17.08.2026 10:00;17.08.2026 12:00;Кузьминки;Москва, кв. 1;;Нет"
    source = (CSV_HEADER + f"1;{shared}\n2;{shared}\n").encode()
    control = (CSV_HEADER + f"CONTROL;{shared}\n").encode()
    report = audit_sources(source, control)
    assert report["control"]["unique_matches"] == 0
    assert report["control"]["ambiguous"] == 1
    assert report["control"]["id_intersection"] == 0
