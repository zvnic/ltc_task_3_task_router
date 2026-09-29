import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import DomainError
from app.imports.csv_reader import (
    normalize_building_address,
    normalize_reference_datetime,
    normalize_text,
    parse_requests_csv,
    read_rows,
    reference_key,
)
from app.models import Dataset, Engineer, Plan, PlanningEvent, ReferenceAssignment, ServiceRequest
from app.schemas import (
    Coordinates,
    CoordinateSource,
    EngineerInput,
    PlanningInput,
    RequestStatus,
    ServiceRequestInput,
)
from app.services.norms import default_norms_config

DATA_ROOT = Path("/data")
FIXTURE_ROOT = DATA_ROOT / "fixtures"
SOURCE_ROOT = DATA_ROOT / "source"
DEMO_FINGERPRINT = "beeline-vostok-demo-v1"
GEOCODED_FIXTURE = "geocoded_addresses.json"
MOSCOW = ZoneInfo("Europe/Moscow")

# Статусы, с которыми заявка не входит в новый расчёт плана: работа закрыта
# (completed), заявка отменена клиентом или бригадой (cancelled) либо перенесена
# на другой день (rescheduled). Остальные статусы — диспетчерский контекст
# активной заявки, она планируется. Единственный источник правды: все проверки
# «активна ли заявка» идут через is_request_plannable.
INACTIVE_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        RequestStatus.COMPLETED.value,
        RequestStatus.CANCELLED.value,
        RequestStatus.RESCHEDULED.value,
    }
)


def is_request_plannable(status: str) -> bool:
    """Входит ли заявка с этим статусом в новый снимок планирования."""
    return status not in INACTIVE_REQUEST_STATUSES


def stable_uuid(kind: str, external_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"beeline-routes:{kind}:{external_id}")


def _load_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _enrichment_values(
    enrichment: dict[str, Any], source_work_type: str, source_work_subtype: str
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    rule = enrichment["work_types"].get(source_work_type)
    if rule is None:
        raise DomainError(
            "unknown_work_type",
            "Для типа заявки нет правила обогащения.",
            details={"value": source_work_type},
        )
    priority = (
        "urgent" if source_work_subtype in enrichment.get("urgent_subtypes", []) else "normal"
    )
    mapping_status = (
        "assumed"
        if source_work_subtype in enrichment.get("assumed_norm_subtypes", [])
        else "mapped"
    )
    # Business class shared by UI/explain and the Q15 solver objective.
    # Accident/urgent → emergency; type name otherwise; skill only as last resort.
    if priority == "urgent" or source_work_subtype in enrichment.get("urgent_subtypes", []):
        work_class = "emergency"
    elif source_work_type == "Локальная заявка":
        work_class = "local"
    elif source_work_type == "Подключение":
        work_class = "connection"
    elif source_work_type == "Дозаказ":
        work_class = "add_order"
    elif source_work_type == "Глобальная проблема":
        # Non-accident global issues are not emergency for UI class.
        work_class = "add_order"
    else:
        skill = str(rule.get("required_skill") or "")
        if skill == "local":
            work_class = "local"
        elif skill == "connection":
            work_class = "connection"
        elif skill == "emergency":
            work_class = "emergency"
        else:
            work_class = "add_order"
    provenance = {
        "norm_id": rule["norm_id"],
        "norm_version": enrichment["version"],
        "norm_source": enrichment["source"],
        "technical_minutes": rule["technical_minutes"],
        "paperwork_minutes": rule["paperwork_minutes"],
        "travel_norm_minutes": rule["travel_norm_minutes"],
        "mapping_status": mapping_status,
        "work_class": work_class,
    }
    return cast(dict[str, Any], rule), priority, provenance


async def _apply_current_enrichment(
    session: AsyncSession, dataset: Dataset, enrichment: dict[str, Any]
) -> bool:
    stored_norms = (
        dataset.assumptions.get("norms_config")
        if isinstance(dataset.assumptions, dict)
        else None
    )
    if isinstance(stored_norms, dict) and stored_norms.get("source_kind") == "dispatcher_settings":
        return False
    changed = False
    for request in dataset.requests:
        if request.enrichment_rule_version == enrichment["version"]:
            continue
        source_type = str(request.source_fields.get("Тип заявки BK", ""))
        source_subtype = str(request.source_fields.get("Тип заявки HD", ""))
        if not source_type:
            # Dispatcher-created and historical simulation requests have an
            # explicit duration and no source work type to bind to a norm.
            continue
        rule, priority, provenance = _enrichment_values(
            enrichment, source_type, source_subtype
        )
        # Preserve demo/manual transport provenance; enrichment does not own transport.
        existing_enrichment = (request.source_fields or {}).get("_enrichment") or {}
        if isinstance(existing_enrichment, dict):
            for key, value in existing_enrichment.items():
                if key.startswith("required_transport") or str(key).endswith("_demo"):
                    provenance = {**provenance, key: value}
        request.duration_minutes = int(rule["duration_minutes"])
        request.expected_duration_minutes = int(rule["duration_minutes"])
        request.required_skill = str(rule["required_skill"])
        request.priority = priority
        request.enrichment_rule_version = str(enrichment["version"])
        request.source_fields = {**request.source_fields, "_enrichment": provenance}
        # Do not wipe required_transport if already set (demo filter / manual).
        changed = True
    if changed:
        dataset.revision += 1
        assumptions = dict(dataset.assumptions)
        assumptions["durations"] = (
            "Нормативы заказчика: обслуживание без дороги 30/70/20/80 минут; "
            "дорога рассчитывается отдельно"
        )
        assumptions["norm_version"] = str(enrichment["version"])
        assumptions["priority_policy"] = "Подтип «Авария» — срочный"
        dataset.assumptions = assumptions
        await session.commit()
    return changed


def geocoded_addresses() -> dict[str, Any]:
    """Координаты адресов выгрузок по OpenStreetMap (``sims.geocode_sources``).

    Нет файла — пустой справочник: наборы строятся на демо-точках районов, как раньше.
    """
    path = FIXTURE_ROOT / GEOCODED_FIXTURE
    if not path.exists():
        return {"addresses": {}, "offices": {}}
    return _load_json(path)


def _office_start_location(zone: dict[str, Any]) -> dict[str, float]:
    """Resolve the only allowed initial route start: the zone office.

    Офис берётся по адресу из OpenStreetMap, если он найден; иначе — демо-координата
    зоны.
    """
    office = geocoded_addresses().get("offices", {}).get(zone["code"]) or zone["office"]
    office = cast(dict[str, Any], office)
    return {"latitude": float(office["latitude"]), "longitude": float(office["longitude"])}


def _office_is_geocoded(zone: dict[str, Any]) -> bool:
    return zone["code"] in geocoded_addresses().get("offices", {})


def _ordered_engineers(dataset: Dataset) -> list[Engineer]:
    return sorted(dataset.engineers, key=lambda item: (item.input_order, item.external_id))


def _build_start_assumptions(zone: dict[str, Any], office_address: str) -> dict[str, Any]:
    """Expose the office-only start policy used by every route in the zone."""
    engineer_count = len(cast(list[str], zone.get("engineer_names") or []))
    return {
        "start_policy": (
            f"Все маршруты зоны начинаются из офиса «{office_address}»; "
            "возврат в офис после последнего визита не требуется."
        ),
        "start_policy_code": "office_address",
        "office_start_note": (
            "Адрес офиса взят из office_address зоны; координаты офиса найдены по адресу "
            "в OpenStreetMap."
            if _office_is_geocoded(zone)
            else "Адрес офиса взят из office_address зоны; координаты офиса являются "
            "демонстрационным обогащением для расчёта."
        ),
        "is_demo_enrichment": True,
        "control_crew_count_hint": engineer_count,
        "office_address": office_address,
        "work_class_mapping": (
            "Классы работ (UI/explain): local ← «Локальная заявка»; connection ← «Подключение»; "
            "add_order ← дозаказ/прочие нормальные типы; emergency ← подтип «Авария» "
            "(priority=urgent). "
            "Цель: аварии → подключения → ремонты/дозаказы → инженеры → время в пути → "
            "пробег."
        ),
    }


async def _apply_office_starts(
    session: AsyncSession,
    dataset: Dataset,
    zone: dict[str, Any],
) -> bool:
    """Make the zone office the persisted start for every engineer."""
    changed = False
    desired = _office_start_location(zone)
    if dataset.office != desired:
        dataset.office = dict(desired)
        changed = True
    engineers = _ordered_engineers(dataset)
    for engineer in engineers:
        current = engineer.start_location or {}
        if (
            float(current.get("latitude", 0.0)) != float(desired["latitude"])
            or float(current.get("longitude", 0.0)) != float(desired["longitude"])
        ):
            engineer.start_location = dict(desired)
            changed = True

    assumptions = dict(dataset.assumptions)
    if assumptions.pop("crew_bases_note", None) is not None:
        changed = True
    office_address = str(
        dataset.import_report.get("office_address") or zone["office_address"]
    )
    start_meta = _build_start_assumptions(zone, office_address)
    for key, value in start_meta.items():
        if assumptions.get(key) != value:
            assumptions[key] = value
            changed = True
    # Drop obsolete single-office forcing narrative if present as only policy remnant
    if assumptions != dataset.assumptions:
        dataset.assumptions = assumptions
    if changed:
        dataset.revision += 1
        await session.commit()
    return changed


def service_zone_configs() -> list[dict[str, Any]]:
    fixture = _load_json(FIXTURE_ROOT / "service_zones.json")
    return cast(list[dict[str, Any]], fixture["zones"])


def get_service_zone(code: str) -> dict[str, Any]:
    zone = next((item for item in service_zone_configs() if item["code"] == code), None)
    if zone is None:
        raise DomainError(
            "unknown_service_zone",
            "Неизвестная зона обслуживания.",
            details={"zone_code": code},
        )
    return zone


def detect_service_zone(rows: list[dict[str, Any]]) -> dict[str, Any]:
    districts = {str(row["district"]) for row in rows}
    candidates = [
        zone
        for zone in service_zone_configs()
        if districts and districts.issubset(set(zone["district_centers"]))
    ]
    if len(candidates) != 1:
        raise DomainError(
            "service_zone_not_detected",
            "Не удалось однозначно определить зону обслуживания по районам CSV.",
            details={
                "districts": sorted(districts),
                "candidate_zones": [zone["code"] for zone in candidates],
            },
        )
    return candidates[0]


def _coordinate_for(
    address: str,
    district: str,
    zone: dict[str, Any],
    stable_offset_degrees: float,
) -> tuple[dict[str, float], str]:
    """Координата адреса заявки и её источник.

    Адрес найден в OpenStreetMap (``geocoded_addresses.json``) — его точка,
    источник ``geocoded``. Иначе демо-точка: центр района со стабильным сдвигом по
    хешу адреса, источник ``synthetic``.
    """
    found = geocoded_addresses().get("addresses", {}).get(normalize_building_address(address))
    if found is not None:
        return (
            {"latitude": float(found["latitude"]), "longitude": float(found["longitude"])},
            CoordinateSource.GEOCODED.value,
        )
    center = zone["district_centers"].get(district)
    if center is None:
        raise DomainError(
            "missing_coordinates",
            "Для района отсутствует демонстрационная координата.",
            details={"district": district, "address": address},
        )
    digest = hashlib.sha256(normalize_building_address(address).encode("utf-8")).digest()
    scale = stable_offset_degrees
    latitude_offset = ((digest[0] / 255) - 0.5) * scale
    longitude_offset = ((digest[1] / 255) - 0.5) * scale * 1.7
    return (
        {
            "latitude": round(float(center["latitude"]) + latitude_offset, 6),
            "longitude": round(float(center["longitude"]) + longitude_offset, 6),
        },
        CoordinateSource.SYNTHETIC.value,
    )


def coordinates_note(requests: list[ServiceRequest]) -> str:
    """Откуда координаты заявок набора — для допущений набора."""
    geocoded = sum(
        1 for item in requests if item.coordinate_source == CoordinateSource.GEOCODED.value
    )
    synthetic = sum(
        1 for item in requests if item.coordinate_source == CoordinateSource.SYNTHETIC.value
    )
    if not geocoded:
        return (
            "Демонстрационные точки внутри районов; не являются результатом "
            "точного геокодирования."
        )
    verified = len(requests) - geocoded - synthetic
    text = f"Координаты {geocoded} из {len(requests)} заявок найдены по адресу в OpenStreetMap"
    if verified:
        text += f"; {verified} — заданы диспетчером"
    if synthetic:
        text += f"; {synthetic} — демонстрационные точки районов (адрес не найден)"
    return text + "."


async def _apply_geocoded_coordinates(session: AsyncSession, dataset: Dataset) -> bool:
    """Перенести заявки набора с демо-точек на найденные координаты адресов.

    Трогаются только заявки на демо-точке (``synthetic``): координаты, заданные
    диспетчером или найденные раньше, не меняются. Версия набора растёт, прежние планы
    остаются со своим снимком и помечаются устаревшими.
    """
    found = geocoded_addresses().get("addresses", {})
    changed = False
    for request in dataset.requests:
        if request.coordinate_source != CoordinateSource.SYNTHETIC.value:
            continue
        item = found.get(normalize_building_address(request.address_raw))
        if item is None:
            continue
        request.coordinates = {
            "latitude": float(item["latitude"]),
            "longitude": float(item["longitude"]),
        }
        request.coordinate_source = CoordinateSource.GEOCODED.value
        changed = True
    note = coordinates_note(list(dataset.requests))
    if dataset.assumptions.get("coordinates") != note:
        dataset.assumptions = {**dataset.assumptions, "coordinates": note}
        changed = True
    if changed:
        dataset.revision += 1
        await session.commit()
    return changed


def request_to_input(row: ServiceRequest) -> ServiceRequestInput:
    return ServiceRequestInput(
        id=str(row.id),
        external_id=row.external_id,
        input_order=row.input_order,
        address=row.address_raw,
        district=row.district,
        coordinates=Coordinates.model_validate(row.coordinates),
        coordinate_source=row.coordinate_source,
        duration_minutes=row.duration_minutes,
        expected_duration_minutes=(row.expected_duration_minutes or row.duration_minutes),
        window_start=row.window_start.astimezone(MOSCOW),
        window_end=row.window_end.astimezone(MOSCOW),
        required_skill=row.required_skill,
        required_transport=row.required_transport,
        required_equipment=row.required_equipment or {},
        priority=row.priority,
        status=row.status,
        completion_deadline=(
            row.completion_deadline.astimezone(MOSCOW) if row.completion_deadline else None
        ),
        source_fields=row.source_fields,
    )


def engineer_to_input(row: Engineer) -> EngineerInput:
    return EngineerInput(
        id=str(row.id),
        external_id=row.external_id,
        input_order=row.input_order,
        name=row.name,
        start_location=Coordinates.model_validate(row.start_location),
        shift_start=row.shift_start.astimezone(MOSCOW),
        shift_end=row.shift_end.astimezone(MOSCOW),
        skills=row.skills,
        transport=row.transport,
        equipment_inventory=row.equipment_inventory or {},
        is_synthetic=row.is_synthetic,
    )


async def get_dataset_with_inputs(session: AsyncSession, dataset_id: UUID) -> Dataset:
    statement = (
        select(Dataset)
        .where(Dataset.id == dataset_id)
        .options(selectinload(Dataset.requests), selectinload(Dataset.engineers))
    )
    dataset = (await session.execute(statement)).scalar_one_or_none()
    if dataset is None:
        raise DomainError("dataset_not_found", "Набор данных не найден.", status_code=404)
    return dataset


def planning_input_from_dataset(dataset: Dataset) -> PlanningInput:
    office_start = Coordinates.model_validate(dataset.office)
    return PlanningInput(
        dataset_id=str(dataset.id),
        revision=dataset.revision,
        planning_date=dataset.planning_date,
        timezone=dataset.timezone,
        requests=[
            request_to_input(row)
            for row in sorted(dataset.requests, key=lambda x: x.input_order)
            if is_request_plannable(row.status)
        ],
        engineers=[
            engineer_to_input(row).model_copy(update={"start_location": office_start})
            for row in sorted(dataset.engineers, key=lambda x: x.input_order)
        ],
        buffer_minutes=get_settings().planning_buffer_minutes,
    )


async def create_dataset_from_csv(
    session: AsyncSession,
    content: bytes,
    *,
    title: str,
    fingerprint: str | None = None,
    zone_code: str | None = None,
    skip_duplicate_rows: bool = False,
) -> Dataset:
    report = prepare_import_report(
        parse_requests_csv(content),
        skip_duplicate_rows=skip_duplicate_rows,
    )
    if report["errors"]:
        raise DomainError(
            "invalid_csv_rows",
            "Импорт содержит ошибки; набор не записан.",
            details={"errors": report["errors"]},
        )
    if report["valid_count"] > 100:
        raise DomainError("too_many_requests", "Допускается не более 100 заявок.")
    enrichment = _load_json(FIXTURE_ROOT / "enrichment_rules.json")
    zone_fixture = _load_json(FIXTURE_ROOT / "service_zones.json")
    zone = get_service_zone(zone_code) if zone_code else detect_service_zone(report["rows"])
    engineers_fixture = _load_json(FIXTURE_ROOT / "engineers.json")
    source_fingerprint = (
        fingerprint
        or hashlib.sha256(
            content
            + enrichment["version"].encode()
            + str(zone_fixture["schema_version"]).encode()
            + zone["code"].encode()
        ).hexdigest()
    )
    existing = (
        await session.execute(
            select(Dataset).where(Dataset.source_fingerprint == source_fingerprint)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing = await get_dataset_with_inputs(session, existing.id)
        await _apply_current_enrichment(session, existing, enrichment)
        await _apply_office_starts(session, existing, zone)
        await _apply_geocoded_coordinates(session, existing)
        assumptions = dict(existing.assumptions)
        assumptions.setdefault("service_zone", zone["code"])
        assumptions.setdefault("service_zone_name", zone["name"])
        assumptions.setdefault("geography", zone["geography_note"])
        assumptions.setdefault("norms_config", default_norms_config())
        office_address = str(
            existing.import_report.get("office_address") or zone["office_address"]
        )
        for key, value in _build_start_assumptions(zone, office_address).items():
            if assumptions.get(key) != value:
                assumptions[key] = value
        if assumptions != existing.assumptions:
            existing.assumptions = assumptions
            await session.commit()
        return await get_dataset_with_inputs(session, existing.id)

    planning_date = (
        report["rows"][0]["window_start"].date() if report["rows"] else datetime.now().date()
    )
    dataset = Dataset(
        id=stable_uuid("dataset", source_fingerprint),
        title=title,
        planning_date=planning_date,
        timezone="Europe/Moscow",
        revision=1,
        office=_office_start_location(zone),
        import_report={key: value for key, value in report.items() if key != "rows"},
        assumptions={
            "service_zone": zone["code"],
            "service_zone_name": zone["name"],
            "geography": zone["geography_note"],
            "office_address": report["office_address"] or zone["office_address"],
            "coordinates": (
                "Демонстрационные точки внутри районов; не являются результатом "
                "точного геокодирования."
            ),
            "engineers": (
                f"{len(zone['engineer_names'])} условных исполнителей (ориентир — unique executors "
                f"контроля); характеристики синтетические/демо"
            ),
            **_build_start_assumptions(
                zone, str(report["office_address"] or zone["office_address"])
            ),
            "durations": (
                "Нормативы заказчика: обслуживание без дороги 30/70/20/80 минут; "
                "дорога рассчитывается отдельно"
            ),
            "norm_version": enrichment["version"],
            "norms_config": default_norms_config(),
            "priority_policy": "Подтип «Авария» — срочный",
            "route_estimation": "Haversine с коэффициентом пути и скоростью транспорта",
            "zone_isolation": (
                "Инженеры назначаются только внутри выбранной зоны; "
                "межзонный резерв не моделируется."
            ),
        },
        source_fingerprint=source_fingerprint,
    )
    session.add(dataset)
    await session.flush()
    created_requests: list[ServiceRequest] = []
    for row in report["rows"]:
        rule, priority, provenance = _enrichment_values(
            enrichment, row["source_work_type"], row["source_work_subtype"]
        )
        coordinates, coordinate_source = _coordinate_for(
            row["address_raw"],
            row["district"],
            zone,
            float(zone_fixture["stable_offset_degrees"]),
        )
        request = ServiceRequest(
            id=stable_uuid(f"request:{source_fingerprint}", row["external_id"]),
            dataset_id=dataset.id,
            external_id=row["external_id"],
            input_order=row["input_order"],
            address_raw=row["address_raw"],
            address_normalized=row["address_normalized"],
            district=row["district"],
            coordinates=coordinates,
            coordinate_source=coordinate_source,
            duration_minutes=rule["duration_minutes"],
            expected_duration_minutes=rule["duration_minutes"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            required_skill=rule["required_skill"],
            required_transport=None,
            required_equipment={},
            priority=priority,
            status="new",
            completion_deadline=None,
            source_fields={
                **row["source_fields"],
                "_enrichment": provenance,
            },
            enrichment_rule_version=enrichment["version"],
        )
        session.add(request)
        created_requests.append(request)
    dataset.assumptions = {
        **dataset.assumptions,
        "coordinates": coordinates_note(created_requests),
    }
    for index, name in enumerate(zone["engineer_names"]):
        item = engineers_fixture["engineers"][index % len(engineers_fixture["engineers"])]
        external_id = (
            item["external_id"]
            if zone["code"] == "vostok"
            else f"{zone['code']}-team-{index + 1:02d}"
        )
        session.add(
            Engineer(
                id=stable_uuid(f"engineer:{source_fingerprint}", external_id),
                dataset_id=dataset.id,
                external_id=external_id,
                input_order=index,
                name=name,
                start_location=_office_start_location(zone),
                shift_start=datetime.fromisoformat(item["shift_start"]),
                shift_end=datetime.fromisoformat(item["shift_end"]),
                skills=item["skills"],
                transport=item["transport"],
                equipment_inventory=item.get("equipment_inventory", {}),
                is_synthetic=True,
            )
        )
    await session.commit()
    return await get_dataset_with_inputs(session, dataset.id)


def prepare_import_report(
    report: dict[str, Any],
    *,
    skip_duplicate_rows: bool,
) -> dict[str, Any]:
    """Apply the explicit duplicate-row policy without hiding other CSV errors."""
    errors = list(report["errors"])
    duplicate_errors = [error for error in errors if error.get("code") == "duplicate_id"]
    if not errors:
        return {
            **report,
            "warnings": [],
            "skipped_duplicate_rows": 0,
        }
    if not skip_duplicate_rows or len(duplicate_errors) != len(errors):
        return report
    return {
        **report,
        "errors": [],
        "warnings": duplicate_errors,
        "skipped_duplicate_rows": len(duplicate_errors),
    }


async def seed_demo(session: AsyncSession) -> Dataset:
    datasets = await seed_service_zones(session)
    return next(
        dataset for dataset in datasets if dataset.assumptions.get("service_zone") == "vostok"
    )


async def seed_service_zones(session: AsyncSession) -> list[Dataset]:
    datasets: list[Dataset] = []
    for zone in service_zone_configs():
        dataset = await create_dataset_from_csv(
            session,
            (SOURCE_ROOT / zone["synthetic_file"]).read_bytes(),
            title=zone["title"],
            fingerprint=zone["fingerprint"],
            zone_code=zone["code"],
        )
        reference = (
            (
                await session.execute(
                    select(ReferenceAssignment).where(ReferenceAssignment.dataset_id == dataset.id)
                )
            )
            .scalars()
            .first()
        )
        if reference is None:
            await import_reference(
                session,
                dataset,
                (SOURCE_ROOT / zone["control_file"]).read_bytes(),
            )
        datasets.append(await get_dataset_with_inputs(session, dataset.id))
    return datasets




async def delete_dataset(session: AsyncSession, dataset_id: UUID) -> dict[str, Any]:
    """Remove a dataset snapshot and all related operational data."""
    dataset = await session.get(Dataset, dataset_id)
    if dataset is None:
        raise DomainError("dataset_not_found", "Набор данных не найден.", status_code=404)

    title = dataset.title
    plan_ids = list(
        (await session.scalars(select(Plan.id).where(Plan.dataset_id == dataset_id))).all()
    )
    event_ids = list(
        (
            await session.scalars(
                select(PlanningEvent.id).where(PlanningEvent.dataset_id == dataset_id)
            )
        ).all()
    )
    request_ids = list(
        (
            await session.scalars(
                select(ServiceRequest.id).where(ServiceRequest.dataset_id == dataset_id)
            )
        ).all()
    )
    engineer_ids = list(
        (
            await session.scalars(select(Engineer.id).where(Engineer.dataset_id == dataset_id))
        ).all()
    )
    ref_ids = list(
        (
            await session.scalars(
                select(ReferenceAssignment.id).where(ReferenceAssignment.dataset_id == dataset_id)
            )
        ).all()
    )

    n_plans = len(plan_ids)
    n_events = len(event_ids)
    n_requests = len(request_ids)
    n_engineers = len(engineer_ids)
    n_refs = len(ref_ids)

    # Integrity order:
    # 1) clear plan self-FKs
    # 2) delete planning_events (result_plan_id has no ON DELETE CASCADE)
    # 3) delete plans
    # 4) delete reference_assignments / service_requests / engineers
    # 5) delete dataset
    if plan_ids:
        await session.execute(
            update(Plan)
            .where(Plan.dataset_id == dataset_id)
            .values(parent_plan_id=None, baseline_plan_id=None)
        )

    if plan_ids:
        await session.execute(
            delete(PlanningEvent).where(
                (PlanningEvent.dataset_id == dataset_id)
                | (PlanningEvent.base_plan_id.in_(plan_ids))
                | (PlanningEvent.result_plan_id.in_(plan_ids))
            )
        )
    else:
        await session.execute(delete(PlanningEvent).where(PlanningEvent.dataset_id == dataset_id))

    if plan_ids:
        await session.execute(delete(Plan).where(Plan.dataset_id == dataset_id))

    await session.execute(
        delete(ReferenceAssignment).where(ReferenceAssignment.dataset_id == dataset_id)
    )
    await session.execute(delete(ServiceRequest).where(ServiceRequest.dataset_id == dataset_id))
    await session.execute(delete(Engineer).where(Engineer.dataset_id == dataset_id))

    await session.execute(delete(Dataset).where(Dataset.id == dataset_id))
    await session.flush()

    return {
        "dataset_id": str(dataset_id),
        "title": title,
        "deleted": {
            "plans": n_plans,
            "events": n_events,
            "requests": n_requests,
            "engineers": n_engineers,
            "reference_assignments": n_refs,
        },
    }


async def delete_datasets_by_service_zone(session: AsyncSession, zone_code: str) -> dict[str, Any]:
    """Delete every dataset snapshot belonging to a service zone code."""
    zone = get_service_zone(zone_code)
    zone_name = str(zone.get("name") or zone_code)
    datasets = list((await session.scalars(select(Dataset))).all())
    matched: list[Dataset] = []
    for item in datasets:
        assumptions = item.assumptions if isinstance(item.assumptions, dict) else {}
        service_zone = assumptions.get("service_zone")
        service_zone_name = assumptions.get("service_zone_name")
        title_l = item.title.casefold()
        if (
            service_zone == zone_code
            or service_zone_name == zone_name
            or item.title == zone_name
            or zone_name.casefold() in title_l
        ):
            matched.append(item)

    deleted_datasets: list[dict[str, Any]] = []
    for item in matched:
        deleted_datasets.append(await delete_dataset(session, item.id))

    return {
        "zone_code": zone_code,
        "zone_title": zone_name,
        "deleted_datasets": deleted_datasets,
        "count": len(deleted_datasets),
    }

async def import_reference(
    session: AsyncSession,
    dataset: Dataset,
    content: bytes,
) -> dict[str, int]:
    rows, _, _ = read_rows(content)
    request_by_key = _requests_by_reference_key(dataset)
    await session.execute(
        delete(ReferenceAssignment).where(ReferenceAssignment.dataset_id == dataset.id)
    )
    counters = _reference_match_counters(rows, request_by_key)
    for row in rows:
        candidates = request_by_key.get(reference_key(row), [])
        status = "matched" if len(candidates) == 1 else "ambiguous" if candidates else "unmatched"
        session.add(
            ReferenceAssignment(
                dataset_id=dataset.id,
                request_id=candidates[0].id if len(candidates) == 1 else None,
                reference_external_id=row["Заявка"].strip(),
                team_name=row.get("Бригада", "").strip() or None,
                reference_status=row.get("Статус BK", "").strip(),
                match_status=status,
                source_fields=row,
            )
        )
    await session.commit()
    return counters


def preview_reference(dataset: Dataset, content: bytes) -> dict[str, int]:
    rows, _, _ = read_rows(content)
    return _reference_match_counters(rows, _requests_by_reference_key(dataset))


def _requests_by_reference_key(
    dataset: Dataset,
) -> dict[tuple[str, str, str, str, str], list[ServiceRequest]]:
    request_by_key: dict[tuple[str, str, str, str, str], list[ServiceRequest]] = {}
    for request in dataset.requests:
        fields = request.source_fields
        key = (
            request.address_normalized,
            normalize_reference_datetime(fields["Начало"]),
            normalize_reference_datetime(fields["Окончание"]),
            normalize_text(fields["Тип заявки BK"]),
            normalize_text(fields["Тип заявки HD"]),
        )
        request_by_key.setdefault(key, []).append(request)
    return request_by_key


def _reference_match_counters(
    rows: list[dict[str, str]],
    request_by_key: dict[tuple[str, str, str, str, str], list[ServiceRequest]],
) -> dict[str, int]:
    counters = {"matched": 0, "ambiguous": 0, "unmatched": 0}
    for row in rows:
        candidates = request_by_key.get(reference_key(row), [])
        status = "matched" if len(candidates) == 1 else "ambiguous" if candidates else "unmatched"
        counters[status] += 1
    return counters
