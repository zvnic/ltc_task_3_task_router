import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import DomainError
from app.db import get_session
from app.imports.csv_reader import normalize_building_address, parse_requests_csv
from app.models import Dataset, Engineer, Plan, PlanningEvent, ReferenceAssignment, ServiceRequest
from app.planning.metrics import objective_tuple
from app.planning.objective import OBJECTIVE_LABELS
from app.planning.policies import travel_norm_max_factor, travel_norm_mode
from app.planning.reasons import REASON_TEXT
from app.planning.registry import SOLVERS, algorithm_catalog
from app.planning.replan import normal_insertion_policy
from app.planning.road_matrix import road_network_summary
from app.planning.routing import (
    MAX_BICYCLE_LEG_METERS,
    MAX_CAR_LOCAL_WALK_METERS,
    MAX_WALKING_LEG_METERS,
    MKAD_SEMI_AXIS_EAST_KM,
    MKAD_SEMI_AXIS_NORTH_KM,
    ROUTE_ESTIMATION_METHOD,
    SUBURBAN_BOARDING_MIN_KM,
    SUBURBAN_BOARDING_MINUTES,
    SUBURBAN_SPEED_KMH,
    TRANSPORT_ACCESS_MINUTES,
    TRANSPORT_MIN_TRAVEL_MINUTES,
    TRANSPORT_PARAMETERS,
)
from app.planning.validation import validate_plan
from app.routing import build_detailed_route, geocode_address
from app.schemas import (
    DetailedEngineerRoute,
    EngineerInput,
    GeocodeResult,
    PlanDiff,
    PlanningInput,
    PlanningRequestEvent,
    PlanningResult,
    Priority,
    RequestStatus,
    RequiredSkill,
    ServiceRequestInput,
    StopChange,
    Transport,
    apply_emergency_sla,
)
from app.schemas.domain import ManualAssignRequest, RequestPlacements
from app.services.datasets import (
    create_dataset_from_csv,
    delete_dataset,
    delete_datasets_by_service_zone,
    detect_service_zone,
    get_dataset_with_inputs,
    import_reference,
    planning_input_from_dataset,
    request_to_input,
    seed_demo,
    service_zone_configs,
    stable_uuid,
)
from app.services.datasets import (
    preview_reference as preview_reference_matches,
)
from app.services.norms import (
    bind_request_to_current_norm,
    current_norms_config,
    update_norms_config,
)
from app.services.plans import (
    assign_unassigned_request,
    calculate_plan_batch,
    calculate_replan_pair,
    manual_insert_model_info,
    request_placements,
)

router = APIRouter(prefix="/api/v1")

# Виды планов, которые считаются текущим планом набора: выбранный расчёт,
# перепланирование и ручное назначение отказанной заявки.
CURRENT_PLAN_KINDS = (
    "optimized",
    "baseline_fallback",
    "replan_optimized",
    "replan_baseline_fallback",
    "manual_insert",
)


class DatasetCreateResponse(BaseModel):
    dataset_id: UUID
    title: str
    revision: int
    import_report: dict[str, Any]


class RequestPatch(BaseModel):
    expected_revision: int = Field(ge=1)
    duration_minutes: int | None = Field(default=None, gt=0, le=720)
    expected_duration_minutes: int | None = Field(default=None, gt=0, le=720)
    priority: Priority | None = None
    status: RequestStatus | None = None
    required_transport: Transport | None = None
    required_equipment: dict[str, int] | None = None

    @field_validator("required_equipment")
    @classmethod
    def validate_required_equipment(
        cls, value: dict[str, int] | None
    ) -> dict[str, int] | None:
        if value is not None and any(
            not code.strip() or amount <= 0 for code, amount in value.items()
        ):
            raise ValueError("required equipment amounts must be positive")
        return value


class EngineerPatch(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    skills: list[RequiredSkill] | None = Field(default=None, min_length=1, max_length=3)
    transport: Transport | None = None
    equipment_inventory: dict[str, int] | None = None

    @field_validator("skills")
    @classmethod
    def validate_unique_skills(
        cls, value: list[RequiredSkill] | None
    ) -> list[RequiredSkill] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("skills must be unique")
        return value

    @field_validator("equipment_inventory")
    @classmethod
    def validate_equipment_inventory(
        cls, value: dict[str, int] | None
    ) -> dict[str, int] | None:
        if value is not None and any(
            not code.strip() or amount < 0 for code, amount in value.items()
        ):
            raise ValueError("equipment inventory amounts must be non-negative")
        return value


class RequestCreate(BaseModel):
    expected_revision: int = Field(ge=1)
    request: ServiceRequestInput


class PlanRunRequest(BaseModel):
    algorithm_ids: list[str] = Field(
        default_factory=lambda: ["ortools_gls_v1", "insertion_ls_v1"],
        min_length=1,
        max_length=3,
    )
    time_limit_seconds: int | None = Field(default=None, ge=1, le=20)

    @field_validator("algorithm_ids")
    @classmethod
    def validate_algorithms(cls, value: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value))
        unknown = sorted(set(unique) - set(SOLVERS))
        if unknown:
            raise ValueError(f"unknown algorithms: {', '.join(unknown)}")
        return unique


@router.get("/algorithms")
async def list_algorithms() -> list[dict[str, Any]]:
    return algorithm_catalog()


class EngineerImport(BaseModel):
    expected_revision: int = Field(ge=1)
    engineers: list[EngineerInput] = Field(min_length=1, max_length=15)


class NormRowPayload(BaseModel):
    norm_id: str = Field(min_length=1, max_length=80)
    travel_minutes: int = Field(ge=1, le=240)
    technical_minutes: int = Field(ge=0, le=720)
    paperwork_minutes: int = Field(ge=0, le=720)
    expected_service_minutes: int = Field(ge=1, le=720)


class NormUpdatePayload(BaseModel):
    expected_revision: int = Field(ge=1)
    rows: list[NormRowPayload] = Field(min_length=4, max_length=4)


def _plan_payload(plan: Plan) -> dict[str, Any]:
    return {
        "id": str(plan.id),
        "dataset_id": str(plan.dataset_id),
        "input_revision": plan.input_revision,
        "parent_plan_id": str(plan.parent_plan_id) if plan.parent_plan_id else None,
        "baseline_plan_id": str(plan.baseline_plan_id) if plan.baseline_plan_id else None,
        "kind": plan.kind,
        "event_time": plan.event_time.isoformat() if plan.event_time else None,
        "created_at": plan.created_at.isoformat(),
        "result": plan.result,
        "metrics": plan.metrics,
        "model_info": plan.model_info,
    }


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
async def public_config() -> dict[str, str]:
    return {
        "map_style_url": get_settings().map_style_url,
        "route_estimation_method": ROUTE_ESTIMATION_METHOD,
    }


@router.get("/directories")
async def planning_directories() -> dict[str, Any]:
    transport_labels = {
        "car": "Автомобиль",
        "walking": "Пешком",
        "bicycle": "Велосипед",
        "public_transport": "Общественный транспорт",
    }
    return {
        "skills": [
            {"code": "local", "label": "Локальные работы"},
            {"code": "connection", "label": "Подключение и дозаказы"},
            {"code": "emergency", "label": "Аварийные работы"},
        ],
        "transports": [
            {
                "code": transport.value,
                "label": transport_labels[transport.value],
                "speed_kmh": parameters[0],
                "path_factor": parameters[1],
                "suburban_speed_kmh": SUBURBAN_SPEED_KMH.get(transport),
                "suburban_boarding_minutes": SUBURBAN_BOARDING_MINUTES.get(transport),
                "access_minutes": TRANSPORT_ACCESS_MINUTES[transport],
                "min_travel_minutes": TRANSPORT_MIN_TRAVEL_MINUTES[transport],
            }
            for transport, parameters in TRANSPORT_PARAMETERS.items()
        ],
        # Модель дороги целиком: интерфейс показывает её диспетчеру теми же числами,
        # которыми считает решатель, а не своей копией.
        "travel_model": {
            "route_estimation_method": ROUTE_ESTIMATION_METHOD,
            "road_network": road_network_summary(),
            "mkad_semi_axes_km": [MKAD_SEMI_AXIS_NORTH_KM, MKAD_SEMI_AXIS_EAST_KM],
            "car_local_walk_meters": MAX_CAR_LOCAL_WALK_METERS,
            "suburban_boarding_min_km": SUBURBAN_BOARDING_MIN_KM,
            "walking_leg_limit_meters": MAX_WALKING_LEG_METERS,
            "bicycle_leg_limit_meters": MAX_BICYCLE_LEG_METERS,
            "travel_norm_mode": travel_norm_mode(),
            "travel_norm_max_factor": travel_norm_max_factor(),
        },
        "unassigned_reasons": [
            {"code": code, "label": label} for code, label in REASON_TEXT.items()
        ],
        "priorities": [
            {"code": "normal", "label": "Обычная"},
            {"code": "urgent", "label": "Срочная аварийная"},
        ],
        "work_types": [
            {"source": "Локальная заявка", "skill": "local", "duration_minutes": 30},
            {"source": "Подключение", "skill": "connection", "duration_minutes": 70},
            {"source": "Дозаказ", "skill": "connection", "duration_minutes": 20},
            {"source": "Глобальная проблема", "skill": "emergency", "duration_minutes": 80},
        ],
        "objective": {
            "order": list(OBJECTIVE_LABELS),
            "weighting": (
                "Веса рассчитываются от верхних границ конкретного снимка так, "
                "чтобы каждый старший критерий доминировал над суммой младших."
            ),
        },
        "service_zones": [
            {
                "code": zone["code"],
                "name": zone["name"],
                "geography_note": zone["geography_note"],
                "engineer_count": len(zone["engineer_names"]),
                "district_count": len(zone["district_centers"]),
            }
            for zone in service_zone_configs()
        ],
    }


@router.get("/geocode", response_model=GeocodeResult)
async def geocode(
    q: str = Query(min_length=3, max_length=200),
) -> GeocodeResult:
    """Координаты адреса для новой заявки в течение дня (форма «Новая заявка»).

    Ищет только по явному запросу диспетчера: Nominatim запрещает автодополнение.
    """
    return await geocode_address(q, settings=get_settings())


@router.get("/datasets")
async def list_datasets(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    request_counts = (
        select(
            ServiceRequest.dataset_id,
            func.count(ServiceRequest.id).label("request_count"),
        )
        .group_by(ServiceRequest.dataset_id)
        .subquery()
    )
    plans_subquery = (
        select(Plan.dataset_id, func.max(Plan.created_at).label("latest_created_at"))
        .where(Plan.kind.in_(CURRENT_PLAN_KINDS))
        .group_by(Plan.dataset_id)
        .subquery()
    )
    statement = (
        select(Dataset, Plan, request_counts.c.request_count)
        .outerjoin(request_counts, request_counts.c.dataset_id == Dataset.id)
        .outerjoin(plans_subquery, plans_subquery.c.dataset_id == Dataset.id)
        .outerjoin(
            Plan,
            (Plan.dataset_id == Dataset.id)
            & (Plan.created_at == plans_subquery.c.latest_created_at),
        )
        .order_by(Dataset.title)
    )
    rows = (await session.execute(statement)).all()
    return [
        {
            "id": str(dataset.id),
            "title": dataset.title,
            "planning_date": dataset.planning_date.isoformat(),
            "timezone": dataset.timezone,
            "revision": dataset.revision,
            "service_zone": dataset.assumptions.get("service_zone"),
            "service_zone_name": dataset.assumptions.get("service_zone_name"),
            "request_count": request_count or 0,
            "current_plan_id": str(plan.id) if plan else None,
        }
        for dataset, plan, request_count in rows
    ]


@router.post("/datasets/demo", response_model=DatasetCreateResponse)
async def ensure_demo(session: AsyncSession = Depends(get_session)) -> DatasetCreateResponse:
    dataset = await seed_demo(session)
    return DatasetCreateResponse(
        dataset_id=dataset.id,
        title=dataset.title,
        revision=dataset.revision,
        import_report=dataset.import_report,
    )



@router.delete("/datasets/{dataset_id}")
async def remove_dataset(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Удалить выбранный набор данных (заявки, инженеры, планы, события)."""
    result = await delete_dataset(session, dataset_id)
    await session.commit()
    return result


@router.delete("/zones/{zone_code}")
async def remove_zone_datasets(
    zone_code: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Удалить все наборы данных указанной зоны обслуживания."""
    result = await delete_datasets_by_service_zone(session, zone_code)
    await session.commit()
    return result


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    dataset = await get_dataset_with_inputs(session, dataset_id)
    return {
        "id": str(dataset.id),
        "title": dataset.title,
        "planning_date": dataset.planning_date.isoformat(),
        "timezone": dataset.timezone,
        "revision": dataset.revision,
        "service_zone": dataset.assumptions.get("service_zone"),
        "service_zone_name": dataset.assumptions.get("service_zone_name"),
        "office": dataset.office,
        "import_report": dataset.import_report,
        "assumptions": dataset.assumptions,
        "request_count": len(dataset.requests),
        "engineer_count": len(dataset.engineers),
    }


@router.get("/datasets/{dataset_id}/norms")
async def get_dataset_norms(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    dataset = await get_dataset_with_inputs(session, dataset_id)
    return current_norms_config(dataset) | {"revision": dataset.revision}


@router.patch("/datasets/{dataset_id}/norms")
async def patch_dataset_norms(
    dataset_id: UUID,
    payload: NormUpdatePayload,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    statement = (
        select(Dataset)
        .where(Dataset.id == dataset_id)
        .options(selectinload(Dataset.requests))
        .with_for_update()
    )
    dataset = (await session.execute(statement)).scalar_one_or_none()
    if dataset is None:
        raise DomainError("dataset_not_found", "Набор данных не найден.", status_code=404)
    return await update_norms_config(
        session,
        dataset,
        expected_revision=payload.expected_revision,
        rows=[row.model_dump() for row in payload.rows],
    )


@router.post("/datasets/import/preview")
async def preview_import(file: UploadFile = File(...)) -> dict[str, Any]:
    content = await file.read(5 * 1024 * 1024 + 1)
    report = parse_requests_csv(content)
    zone = detect_service_zone(report["rows"]) if not report["errors"] else None
    return {key: value for key, value in report.items() if key != "rows"} | {
        "preview": report["rows"][:10],
        "service_zone": (
            {"code": zone["code"], "name": zone["name"]} if zone is not None else None
        ),
    }


@router.post("/datasets/import", response_model=DatasetCreateResponse)
async def commit_import(
    session: AsyncSession = Depends(get_session),
    file: UploadFile = File(...),
    title: str = Form(default="Импортированный набор", min_length=1, max_length=200),
    skip_duplicate_rows: bool = Form(default=False),
) -> DatasetCreateResponse:
    content = await file.read(5 * 1024 * 1024 + 1)
    dataset = await create_dataset_from_csv(
        session,
        content,
        title=title,
        skip_duplicate_rows=skip_duplicate_rows,
    )
    return DatasetCreateResponse(
        dataset_id=dataset.id,
        title=dataset.title,
        revision=dataset.revision,
        import_report=dataset.import_report,
    )


@router.post("/datasets/{dataset_id}/reference/preview")
async def preview_reference(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
    file: UploadFile = File(...),
) -> dict[str, int]:
    dataset = await get_dataset_with_inputs(session, dataset_id)
    content = await file.read(5 * 1024 * 1024 + 1)
    return preview_reference_matches(dataset, content)


@router.post("/datasets/{dataset_id}/reference/import")
async def commit_reference(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
    file: UploadFile = File(...),
) -> dict[str, int]:
    dataset = await get_dataset_with_inputs(session, dataset_id)
    return await import_reference(session, dataset, await file.read(5 * 1024 * 1024 + 1))


@router.get("/datasets/{dataset_id}/reference")
async def list_reference(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await get_dataset_with_inputs(session, dataset_id)
    rows = (
        await session.execute(
            select(ReferenceAssignment)
            .where(ReferenceAssignment.dataset_id == dataset_id)
            .order_by(ReferenceAssignment.reference_external_id)
        )
    ).scalars()
    return [
        {
            "reference_external_id": row.reference_external_id,
            "request_id": str(row.request_id) if row.request_id else None,
            "team_name": row.team_name,
            "reference_status": row.reference_status,
            "match_status": row.match_status,
        }
        for row in rows
    ]


@router.get("/datasets/{dataset_id}/requests")
async def list_requests(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
    search: str | None = None,
    district: str | None = None,
    priority: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, Any]:
    dataset = await get_dataset_with_inputs(session, dataset_id)
    statement = select(ServiceRequest).where(ServiceRequest.dataset_id == dataset_id)
    if search:
        term = f"%{search}%"
        statement = statement.where(
            ServiceRequest.external_id.ilike(term) | ServiceRequest.address_raw.ilike(term)
        )
    if district:
        statement = statement.where(ServiceRequest.district == district)
    if priority:
        statement = statement.where(ServiceRequest.priority == priority)
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = (
        await session.execute(
            statement.order_by(ServiceRequest.input_order).offset(offset).limit(limit)
        )
    ).scalars()
    latest_plan = (
        await session.execute(
            select(Plan)
            .where(
                Plan.dataset_id == dataset_id,
                Plan.kind.in_(CURRENT_PLAN_KINDS),
            )
            .order_by(Plan.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    assignment_by_request: dict[str, dict[str, Any]] = {}
    if latest_plan is not None:
        for route in latest_plan.result["routes"]:
            for stop in route["stops"]:
                assignment_by_request[stop["request_id"]] = {
                    "assigned_engineer_id": route["engineer_id"],
                    "assigned_engineer_name": route["engineer_name"],
                    "eta": stop["service_start_at"],
                    "unassigned_reason": None,
                    "plan_is_stale": latest_plan.input_revision != dataset.revision,
                }
        for item in latest_plan.result["unassigned"]:
            assignment_by_request[item["request_id"]] = {
                "assigned_engineer_id": None,
                "assigned_engineer_name": None,
                "eta": None,
                "unassigned_reason": item["explanation"],
                "plan_is_stale": latest_plan.input_revision != dataset.revision,
            }
    return {
        "items": [
            request_to_input(row).model_dump(mode="json")
            | assignment_by_request.get(
                str(row.id),
                {
                    "assigned_engineer_id": None,
                    "assigned_engineer_name": None,
                    "eta": None,
                    "unassigned_reason": None,
                    "plan_is_stale": False,
                },
            )
            for row in rows
        ],
        "total": total or 0,
        "offset": offset,
        "limit": limit,
    }


@router.patch("/requests/{request_id}")
async def patch_request(
    request_id: UUID,
    payload: RequestPatch,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    request_row = await session.get(ServiceRequest, request_id)
    if request_row is None:
        raise DomainError("request_not_found", "Заявка не найдена.", status_code=404)
    dataset = await session.get(Dataset, request_row.dataset_id, with_for_update=True)
    if dataset is None:
        raise DomainError("dataset_not_found", "Набор данных не найден.", status_code=404)
    if dataset.revision != payload.expected_revision:
        raise DomainError("stale_input", "Набор уже изменён.", status_code=409)
    updates = payload.model_dump(exclude={"expected_revision"}, exclude_none=True)
    enrichment = (request_row.source_fields or {}).get("_enrichment")
    if (
        ({"duration_minutes", "expected_duration_minutes"} & updates.keys())
        and isinstance(enrichment, dict)
        and enrichment.get("norm_id")
    ):
        raise DomainError(
            "norm_controlled_duration",
            "Длительность этой заявки задаётся в разделе «Нормативы».",
            status_code=409,
        )
    next_duration = int(updates.get("duration_minutes", request_row.duration_minutes))
    next_expected = int(
        updates.get(
            "expected_duration_minutes",
            request_row.expected_duration_minutes or request_row.duration_minutes,
        )
    )
    if next_expected > next_duration:
        raise DomainError(
            "invalid_expected_duration",
            "Ожидаемое время не может превышать безопасный норматив.",
            status_code=422,
        )
    for field, value in updates.items():
        setattr(request_row, field, value)
    dataset.revision += 1
    await session.commit()
    return {
        "request": request_to_input(request_row).model_dump(mode="json"),
        "revision": dataset.revision,
    }


@router.post("/datasets/{dataset_id}/requests")
async def create_request(
    dataset_id: UUID,
    payload: RequestCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    dataset = await get_dataset_with_inputs(session, dataset_id)
    await session.execute(select(Dataset.id).where(Dataset.id == dataset_id).with_for_update())
    if dataset.revision != payload.expected_revision:
        raise DomainError("stale_input", "Набор уже изменён.", status_code=409)
    if len(dataset.requests) >= 100:
        raise DomainError("request_limit_reached", "Допускается не более 100 заявок.")
    item = bind_request_to_current_norm(
        payload.request,
        current_norms_config(dataset),
    )
    if item.window_start.date() != dataset.planning_date:
        raise DomainError("invalid_request_date", "Окно заявки должно совпадать с датой набора.")
    if any(row.external_id == item.external_id for row in dataset.requests):
        raise DomainError("duplicate_request", "Внешний ID заявки уже существует.", status_code=409)
    request_id = stable_uuid(f"request:{dataset.source_fingerprint}", item.external_id)
    row = ServiceRequest(
        id=request_id,
        dataset_id=dataset.id,
        external_id=item.external_id,
        input_order=max((request.input_order for request in dataset.requests), default=-1) + 1,
        address_raw=item.address,
        address_normalized=normalize_building_address(item.address),
        district=item.district,
        coordinates=item.coordinates.model_dump(),
        coordinate_source=item.coordinate_source.value,
        duration_minutes=item.duration_minutes,
        expected_duration_minutes=item.expected_service_minutes,
        window_start=item.window_start,
        window_end=item.window_end,
        required_skill=item.required_skill.value,
        required_transport=item.required_transport.value if item.required_transport else None,
        required_equipment=item.required_equipment,
        priority=item.priority.value,
        status=item.status.value,
        completion_deadline=item.completion_deadline,
        source_fields=item.source_fields,
        enrichment_rule_version=str(
            item.source_fields.get("_enrichment", {}).get("norm_version", "manual-v1")
        ),
    )
    session.add(row)
    dataset.revision += 1
    await session.commit()
    return {"request": request_to_input(row).model_dump(mode="json"), "revision": dataset.revision}


@router.get("/datasets/{dataset_id}/engineers")
async def list_engineers(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    dataset = await get_dataset_with_inputs(session, dataset_id)
    return [
        {
            "id": str(row.id),
            "external_id": row.external_id,
            "input_order": row.input_order,
            "name": row.name,
            "start_location": row.start_location,
            "shift_start": row.shift_start.isoformat(),
            "shift_end": row.shift_end.isoformat(),
            "skills": row.skills,
            "transport": row.transport,
            "equipment_inventory": row.equipment_inventory or {},
            "is_synthetic": row.is_synthetic,
        }
        for row in sorted(dataset.engineers, key=lambda item: item.input_order)
    ]


@router.patch("/engineers/{engineer_id}")
async def patch_engineer(
    engineer_id: UUID,
    payload: EngineerPatch,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    engineer = await session.get(Engineer, engineer_id)
    if engineer is None:
        raise DomainError("engineer_not_found", "Инженер не найден.", status_code=404)
    dataset = await session.get(Dataset, engineer.dataset_id, with_for_update=True)
    if dataset is None:
        raise DomainError("dataset_not_found", "Набор данных не найден.", status_code=404)
    if dataset.revision != payload.expected_revision:
        raise DomainError("stale_input", "Набор уже изменён.", status_code=409)
    updates = payload.model_dump(exclude={"expected_revision"}, exclude_none=True)
    for field, value in updates.items():
        setattr(engineer, field, value)
    dataset.revision += 1
    await session.commit()
    return {"id": str(engineer.id), "revision": dataset.revision}


@router.post("/datasets/{dataset_id}/engineers/import")
async def import_engineers(
    dataset_id: UUID,
    payload: EngineerImport,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    dataset = await get_dataset_with_inputs(session, dataset_id)
    await session.execute(select(Dataset.id).where(Dataset.id == dataset_id).with_for_update())
    if dataset.revision != payload.expected_revision:
        raise DomainError("stale_input", "Набор уже изменён.", status_code=409)
    external_ids = [engineer.external_id for engineer in payload.engineers]
    if len(external_ids) != len(set(external_ids)):
        raise DomainError("duplicate_engineer", "В JSON повторяется внешний ID инженера.")
    if any(engineer.shift_start.date() != dataset.planning_date for engineer in payload.engineers):
        raise DomainError("invalid_engineer_date", "Смена должна совпадать с датой набора.")
    await session.execute(delete(Engineer).where(Engineer.dataset_id == dataset.id))
    for input_order, item in enumerate(sorted(payload.engineers, key=lambda row: row.input_order)):
        session.add(
            Engineer(
                id=stable_uuid(f"engineer:{dataset.source_fingerprint}", item.external_id),
                dataset_id=dataset.id,
                external_id=item.external_id,
                input_order=input_order,
                name=item.name,
                start_location=dict(dataset.office),
                shift_start=item.shift_start,
                shift_end=item.shift_end,
                skills=[skill.value for skill in item.skills],
                transport=item.transport.value,
                equipment_inventory=item.equipment_inventory,
                is_synthetic=item.is_synthetic,
            )
        )
    dataset.revision += 1
    await session.commit()
    return {"imported": len(payload.engineers), "revision": dataset.revision}


@router.post("/datasets/{dataset_id}/plans")
async def create_plan(
    dataset_id: UUID,
    request: Request,
    payload: PlanRunRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    lock: asyncio.Lock = request.app.state.planning_lock
    if lock.locked():
        raise DomainError("planning_busy", "Расчёт уже выполняется.", status_code=409)
    await lock.acquire()
    release_in_finally = True
    try:
        dataset = await get_dataset_with_inputs(session, dataset_id)
        planning_input = planning_input_from_dataset(dataset)
        original_revision = dataset.revision
        run_request = payload or PlanRunRequest()
        time_limit_seconds = (
            run_request.time_limit_seconds or get_settings().solver_time_limit_seconds
        )
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            request.app.state.planning_pool,
            calculate_plan_batch,
            planning_input.model_dump(mode="json"),
            run_request.algorithm_ids,
            time_limit_seconds,
        )
        try:
            pair = await asyncio.wait_for(
                asyncio.shield(future), timeout=get_settings().planning_timeout_seconds
            )
        except TimeoutError as exc:
            release_in_finally = False
            future.add_done_callback(lambda _future: loop.call_soon_threadsafe(lock.release))
            raise DomainError(
                "planning_timeout",
                "Расчёт превысил допустимое время; предыдущий план сохранён.",
                status_code=504,
            ) from exc
        refreshed_revision = await session.scalar(
            select(Dataset.revision).where(Dataset.id == dataset_id)
        )
        if refreshed_revision != original_revision:
            raise DomainError(
                "stale_input",
                "Данные изменились во время расчёта; результат не опубликован.",
                status_code=409,
            )
        baseline = PlanningResult.model_validate(pair["baseline"])
        optimized = PlanningResult.model_validate(pair["optimized"])
        snapshot = planning_input.model_dump(mode="json")
        experiment = {
            "experiment_id": pair["experiment_id"],
            **pair["experiment"],
        }
        baseline_row = Plan(
            dataset_id=dataset_id,
            input_revision=original_revision,
            kind="baseline",
            input_snapshot=snapshot,
            result=baseline.model_dump(mode="json"),
            metrics=baseline.metrics.model_dump(mode="json"),
            model_info={
                "algorithm_id": "baseline_v1",
                "algorithm": baseline.algorithm,
                "same_snapshot": True,
                "selected": pair["selected_algorithm"] == "baseline_v1",
                "objective_tuple": objective_tuple(baseline.metrics),
                "kpis": pair["evaluations"]["baseline_v1"],
                "experiment": experiment,
            },
        )
        session.add(baseline_row)
        await session.flush()
        run_plan_ids: dict[str, str] = {"baseline_v1": str(baseline_row.id)}
        for algorithm_id, result_payload in pair["runs"].items():
            if algorithm_id == "baseline_v1":
                continue
            run_result = PlanningResult.model_validate(result_payload)
            run_row = Plan(
                dataset_id=dataset_id,
                input_revision=original_revision,
                baseline_plan_id=baseline_row.id,
                kind="algorithm_run",
                input_snapshot=snapshot,
                result=run_result.model_dump(mode="json"),
                metrics=run_result.metrics.model_dump(mode="json"),
                model_info={
                    "algorithm_id": algorithm_id,
                    "algorithm": run_result.algorithm,
                    "same_snapshot": True,
                    "selected": algorithm_id == pair["selected_algorithm"],
                    "objective_tuple": objective_tuple(run_result.metrics),
                    "time_limit_seconds": time_limit_seconds,
                    "kpis": pair["evaluations"][algorithm_id],
                    "experiment": experiment,
                },
            )
            session.add(run_row)
            await session.flush()
            run_plan_ids[algorithm_id] = str(run_row.id)
        selected_kind = (
            "optimized" if pair["selected_algorithm"] != "baseline_v1" else "baseline_fallback"
        )
        selected_row = Plan(
            dataset_id=dataset_id,
            input_revision=original_revision,
            baseline_plan_id=baseline_row.id,
            kind=selected_kind,
            input_snapshot=snapshot,
            result=optimized.model_dump(mode="json"),
            metrics=optimized.metrics.model_dump(mode="json"),
            model_info={
                "algorithm_id": pair["selected_algorithm"],
                "algorithm": optimized.algorithm,
                "same_snapshot": True,
                "compared_algorithms": list(pair["runs"]),
                "objective_tuple": objective_tuple(optimized.metrics),
                "kpis": pair["evaluations"][pair["selected_algorithm"]],
                "experiment": experiment,
                "selection_reason": pair["selection_reason"],
            },
        )
        session.add(selected_row)
        await session.commit()
        return {
            "baseline_plan_id": str(baseline_row.id),
            "plan_id": str(selected_row.id),
            "selected_algorithm": pair["selected_algorithm"],
            "experiment_id": pair["experiment_id"],
            "run_plan_ids": run_plan_ids,
            "baseline": _plan_payload(baseline_row),
            "selected": _plan_payload(selected_row),
        }
    finally:
        if release_in_finally and lock.locked():
            lock.release()


@router.get("/datasets/{dataset_id}/plans")
async def list_plans(
    dataset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await get_dataset_with_inputs(session, dataset_id)
    rows = (
        await session.execute(
            select(Plan).where(Plan.dataset_id == dataset_id).order_by(Plan.created_at.desc())
        )
    ).scalars()
    return [_plan_payload(row) for row in rows]


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise DomainError("plan_not_found", "План не найден.", status_code=404)
    return _plan_payload(plan)


@router.get(
    "/plans/{plan_id}/engineers/{engineer_id}/route",
    response_model=DetailedEngineerRoute,
)
async def get_engineer_route_geometry(
    plan_id: UUID,
    engineer_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> DetailedEngineerRoute:
    cache_key = f"{plan_id}:{engineer_id}"
    cached = request.app.state.route_geometry_cache.get(cache_key)
    if cached is not None:
        return DetailedEngineerRoute.model_validate(cached)

    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise DomainError("plan_not_found", "План не найден.", status_code=404)
    result = PlanningResult.model_validate(plan.result)
    route = next((item for item in result.routes if item.engineer_id == engineer_id), None)
    if route is None:
        raise DomainError(
            "engineer_route_not_found",
            "Маршрут инженера не найден.",
            status_code=404,
        )
    if not route.stops:
        raise DomainError(
            "engineer_route_empty",
            "У инженера нет визитов в этом плане.",
            status_code=409,
        )

    snapshot = PlanningInput.model_validate(plan.input_snapshot)
    request_by_id = {item.id: item for item in snapshot.requests}
    points = [route.start_location]
    for stop in route.stops:
        location = stop.location
        if location is None:
            source_request = request_by_id.get(stop.request_id)
            if source_request is None:
                raise DomainError(
                    "route_location_missing",
                    "Для визита отсутствуют координаты в снимке плана.",
                    status_code=409,
                )
            location = source_request.coordinates
        points.append(location)
    # stop.departure_at — выезд на плечо, ведущее в этот визит; к первому визиту бригада
    # выезжает в последний момент, а не в начало смены (route.departure_at)
    departure_times = [stop.departure_at for stop in route.stops]
    segment_transports = [
        Transport(str(stop.facts.get("travel_mode_from_previous", route.transport.value)))
        for stop in route.stops
    ]
    route_task = asyncio.create_task(
        build_detailed_route(
            engineer_id=route.engineer_id,
            transport=route.transport,
            points=points,
            departure_times=departure_times,
            segment_transports=segment_transports,
            settings=get_settings(),
        )
    )
    try:
        while True:
            try:
                detailed = await asyncio.wait_for(
                    asyncio.shield(route_task),
                    timeout=0.2,
                )
                break
            except TimeoutError:
                if await request.is_disconnected():
                    route_task.cancel()
                    await asyncio.gather(route_task, return_exceptions=True)
                    raise asyncio.CancelledError from None
    finally:
        if not route_task.done():
            route_task.cancel()
            await asyncio.gather(route_task, return_exceptions=True)
    request.app.state.route_geometry_cache[cache_key] = detailed.model_dump(mode="json")
    return detailed


@router.get("/plans/{plan_id}/comparison")
async def get_comparison(
    plan_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    selected = await session.get(Plan, plan_id)
    if selected is None:
        raise DomainError("plan_not_found", "План не найден.", status_code=404)
    if selected.baseline_plan_id is None:
        raise DomainError("comparison_unavailable", "Для плана нет baseline.", status_code=409)
    baseline = await session.get(Plan, selected.baseline_plan_id)
    if baseline is None:
        raise DomainError("comparison_unavailable", "Baseline не найден.", status_code=409)
    distance_delta = (
        baseline.metrics["total_distance_meters"] - selected.metrics["total_distance_meters"]
    )
    return {
        "same_snapshot": baseline.input_snapshot == selected.input_snapshot,
        "baseline": baseline.metrics,
        "selected": selected.metrics,
        "distance_saving_meters": distance_delta,
        "distance_saving_percent": (
            distance_delta / baseline.metrics["total_distance_meters"] * 100
            if baseline.metrics["total_distance_meters"]
            else None
        ),
        "note": "Целевая функция учитывает штрафы; пробег показан отдельной метрикой.",
    }


def _stop_index(result: dict[str, Any]) -> dict[str, tuple[str, int, str]]:
    index: dict[str, tuple[str, int, str]] = {}
    for route in result["routes"]:
        for stop in route["stops"]:
            index[stop["request_id"]] = (
                route["engineer_id"],
                stop["sequence"],
                stop["service_start_at"],
            )
    return index


@router.get("/plans/{plan_id}/diff", response_model=PlanDiff)
async def get_diff(
    plan_id: UUID,
    base_plan_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> PlanDiff:
    plan = await session.get(Plan, plan_id)
    base = await session.get(Plan, base_plan_id)
    if plan is None or base is None or plan.dataset_id != base.dataset_id:
        raise DomainError(
            "incompatible_plans", "Планы не найдены или несовместимы.", status_code=404
        )
    previous = _stop_index(base.result)
    current = _stop_index(plan.result)
    changes = []
    changed_engineers: set[str] = set()
    for request_id in sorted(set(previous) | set(current)):
        old = previous.get(request_id)
        new = current.get(request_id)
        if old == new:
            continue
        if old:
            changed_engineers.add(old[0])
        if new:
            changed_engineers.add(new[0])
        changes.append(
            StopChange(
                request_id=request_id,
                previous_engineer_id=old[0] if old else None,
                new_engineer_id=new[0] if new else None,
                previous_sequence=old[1] if old else None,
                new_sequence=new[1] if new else None,
                previous_eta=datetime.fromisoformat(old[2]) if old else None,
                new_eta=datetime.fromisoformat(new[2]) if new else None,
            )
        )
    return PlanDiff(
        changes=changes,
        changed_engineer_ids=sorted(changed_engineers),
        distance_delta_meters=plan.metrics["total_distance_meters"]
        - base.metrics["total_distance_meters"],
        assigned_delta=plan.metrics["assigned_count"] - base.metrics["assigned_count"],
    )


@router.get("/plans/{plan_id}/export")
async def export_plan(
    plan_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await get_plan(plan_id, session)


@router.get("/plans/{plan_id}/placements", response_model=RequestPlacements)
async def list_request_placements(
    plan_id: UUID,
    request_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> RequestPlacements:
    """Все места, куда отказанную заявку можно вставить без сдвига прежних визитов."""
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise DomainError("plan_not_found", "План не найден.", status_code=404)
    return request_placements(
        PlanningInput.model_validate(plan.input_snapshot),
        PlanningResult.model_validate(plan.result),
        str(request_id),
    )


def _ensure_plan_valid_under_current_settings(plan: Plan) -> None:
    """Опубликованный план должен проходить validator при ТЕКУЩИХ настройках.

    Режим норматива дороги и потолок превышения — настройки процесса, а не свойство
    плана. План, посчитанный в soft с превышением, в hard (или при меньшем потолке) уже
    недопустим, и перепланирование от него падало с 500 внутри решателя. Отвечаем 409 с
    понятной причиной: день нужно пересчитать при текущих настройках.
    """
    snapshot = PlanningInput.model_validate(plan.input_snapshot)
    result = PlanningResult.model_validate(plan.result)
    violations = validate_plan(snapshot, result)
    if not violations:
        return
    norm_related = [
        code
        for code in violations
        if code.startswith(("travel_norm_exceeded", "travel_norm_ceiling_exceeded"))
    ]
    if norm_related:
        raise DomainError(
            "plan_norm_policy_mismatch",
            "План посчитан при другом режиме норматива дороги: в нём есть плечи, "
            "недопустимые при текущих настройках. Пересчитайте день.",
            status_code=409,
            details={"violations": norm_related[:10], "violations_total": len(violations)},
        )
    raise DomainError(
        "plan_invalid_under_current_settings",
        "Опубликованный план не проходит проверку при текущих настройках. Пересчитайте день.",
        status_code=409,
        details={"violations": violations[:10], "violations_total": len(violations)},
    )


async def _newer_current_plan_id(session: AsyncSession, plan: Plan) -> UUID | None:
    """Текущий план набора, созданный позже этого, если такой есть."""
    newer: UUID | None = await session.scalar(
        select(Plan.id)
        .where(
            Plan.dataset_id == plan.dataset_id,
            Plan.kind.in_(CURRENT_PLAN_KINDS),
            Plan.created_at > plan.created_at,
        )
        .order_by(Plan.created_at.desc())
        .limit(1)
    )
    return newer


@router.post("/plans/{plan_id}/assign")
async def assign_request(
    plan_id: UUID,
    payload: ManualAssignRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Ручное назначение отказанной заявки: новый план `manual_insert` от исходного.

    Исходный план и его снимок не меняются: новый план берёт тот же снимок, а
    прежние визиты — исполнителя, порядок и время работ — не двигает.

    Назначение не должно потеряться под пересчётом, поэтому:
    - оно берёт тот же `planning_lock`, что расчёт и перепланирование: пока они идут,
      назначение получает 409 planning_busy, а они не начнутся, пока идёт назначение;
    - оно поднимает revision набора, и план, посчитанный на старой ревизии (replan по
      исходному плану из устаревшей вкладки), получает 409 stale_input и не
      публикуется поверх ручного назначения.
    """
    lock: asyncio.Lock = request.app.state.planning_lock
    if lock.locked():
        raise DomainError(
            "planning_busy",
            "Идёт расчёт или перепланирование; назначьте заявку после него.",
            status_code=409,
        )
    async with lock:
        parent = await session.get(Plan, plan_id)
        if parent is None:
            raise DomainError("plan_not_found", "План не найден.", status_code=404)
        dataset = await session.get(Dataset, parent.dataset_id, with_for_update=True)
        if dataset is None:
            raise DomainError("dataset_not_found", "Набор данных не найден.", status_code=404)
        if (
            dataset.revision != payload.expected_revision
            or parent.input_revision != payload.expected_revision
        ):
            raise DomainError(
                "stale_input", "Набор или исходный план уже изменены.", status_code=409
            )
        # Под блокировкой набора: назначение в план, который уже сменился более новым
        # на той же ревизии (расчёт не поднимает revision), молча потеряло бы то изменение.
        newer_plan_id = await _newer_current_plan_id(session, parent)
        if newer_plan_id is not None:
            raise DomainError(
                "stale_plan",
                "У набора уже есть более новый план; назначайте в нём.",
                status_code=409,
                details={"current_plan_id": str(newer_plan_id)},
            )
        _ensure_plan_valid_under_current_settings(parent)
        snapshot = PlanningInput.model_validate(parent.input_snapshot)
        parent_result = PlanningResult.model_validate(parent.result)
        result, placement = assign_unassigned_request(
            snapshot,
            parent_result,
            str(payload.request_id),
            str(payload.engineer_id),
            payload.position,
        )
        # Новая ревизия: все планы, посчитанные до назначения, становятся устаревшими.
        # Снимок входа остаётся тем же — заявки и бригады назначение не меняет.
        dataset.revision += 1
        row = Plan(
            dataset_id=parent.dataset_id,
            input_revision=dataset.revision,
            parent_plan_id=parent.id,
            baseline_plan_id=parent.baseline_plan_id,
            kind="manual_insert",
            input_snapshot=parent.input_snapshot,
            result=result.model_dump(mode="json"),
            metrics=result.metrics.model_dump(mode="json"),
            model_info=manual_insert_model_info(
                parent.model_info,
                parent_result,
                snapshot,
                result,
                placement,
                str(payload.request_id),
            ),
        )
        session.add(row)
        await session.commit()
        return _plan_payload(row)


@router.post("/plans/{plan_id}/replan")
async def replan(
    plan_id: UUID,
    event: PlanningRequestEvent,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    base = await session.get(Plan, plan_id)
    if base is None:
        raise DomainError("plan_not_found", "Исходный план не найден.", status_code=404)
    dataset = await get_dataset_with_inputs(session, base.dataset_id)
    norm_bound_event = event.model_copy(
        update={
            "request": bind_request_to_current_norm(
                event.request,
                current_norms_config(dataset),
            )
        }
    )
    event_request_id = stable_uuid(f"event-request:{base.dataset_id}", event.idempotency_key)
    normalized_policy_event = apply_emergency_sla(norm_bound_event)
    normalized_event = normalized_policy_event.model_copy(
        update={
            "request": normalized_policy_event.request.model_copy(
                update={"id": str(event_request_id)}
            )
        }
    )
    event_payload = normalized_event.model_dump(mode="json")
    existing = (
        await session.execute(
            select(PlanningEvent).where(
                PlanningEvent.dataset_id == base.dataset_id,
                PlanningEvent.idempotency_key == event.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload != event_payload:
            raise DomainError(
                "idempotency_conflict", "Ключ события уже использован.", status_code=409
            )
        if existing.result_plan_id is None:
            raise DomainError("planning_busy", "Событие ещё рассчитывается.", status_code=409)
        result = await session.get(Plan, existing.result_plan_id)
        if result is None:
            raise DomainError("plan_not_found", "Результат события не найден.", status_code=404)
        return {"idempotent": True, "event_id": str(existing.id), "plan": _plan_payload(result)}
    if (
        dataset.revision != event.expected_revision
        or base.input_revision != event.expected_revision
    ):
        raise DomainError("stale_input", "Исходный план или revision устарели.", status_code=409)
    if normalized_event.event_time.date() != dataset.planning_date:
        raise DomainError("invalid_event_time", "Событие должно быть в дате набора.")
    # Номер заявки уникален в наборе: без этой проверки повтор номера падал в базе
    # и диспетчер видел «Внутренняя ошибка сервиса» вместо понятной причины.
    if any(row.external_id == event.request.external_id for row in dataset.requests):
        raise DomainError(
            "request_external_id_exists",
            f"Заявка с номером {event.request.external_id} уже есть в наборе — "
            "укажите другой номер.",
            status_code=409,
        )
    if len(dataset.requests) >= 100:
        raise DomainError(
            "request_limit_reached",
            "Нельзя добавить заявку: достигнут лимит 100 заявок.",
            status_code=409,
        )
    lock: asyncio.Lock = request.app.state.planning_lock
    if lock.locked():
        raise DomainError("planning_busy", "Расчёт уже выполняется.", status_code=409)
    await lock.acquire()
    release_in_finally = True
    try:
        # Под той же блокировкой, что расчёт и ручное назначение: перепланирование от
        # плана, который уже сменился более новым на той же ревизии (повторный расчёт
        # revision не поднимает), опубликовалось бы поверх и молча потеряло тот план.
        # Ручное назначение ревизию поднимает, и его ловит проверка stale_input выше.
        newer_plan_id = await _newer_current_plan_id(session, base)
        if newer_plan_id is not None:
            raise DomainError(
                "stale_plan",
                "У набора уже есть более новый план; перепланируйте от него.",
                status_code=409,
                details={"current_plan_id": str(newer_plan_id)},
            )
        _ensure_plan_valid_under_current_settings(base)
        planning_input = planning_input_from_dataset(dataset)
        requested_algorithm_id = str(
            base.model_info.get("algorithm_id", "ortools_gls_v1")
        )
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            request.app.state.planning_pool,
            calculate_replan_pair,
            planning_input.model_dump(mode="json"),
            base.result,
            event_payload,
            get_settings().solver_time_limit_seconds,
            requested_algorithm_id,
        )
        try:
            pair = await asyncio.wait_for(
                asyncio.shield(future), timeout=get_settings().planning_timeout_seconds
            )
        except TimeoutError as exc:
            release_in_finally = False
            future.add_done_callback(lambda _future: loop.call_soon_threadsafe(lock.release))
            raise DomainError(
                "planning_timeout",
                "Перепланирование превысило допустимое время; исходный план сохранён.",
                status_code=504,
            ) from exc
        current_revision = await session.scalar(
            select(Dataset.revision).where(Dataset.id == dataset.id)
        )
        if current_revision != normalized_event.expected_revision:
            raise DomainError(
                "stale_input",
                "Данные изменились во время расчёта; результат не опубликован.",
                status_code=409,
            )
        event_request = normalized_event.request
        session.add(
            ServiceRequest(
                id=event_request_id,
                dataset_id=dataset.id,
                external_id=event_request.external_id,
                input_order=event_request.input_order,
                address_raw=event_request.address,
                address_normalized=normalize_building_address(event_request.address),
                district=event_request.district,
                coordinates=event_request.coordinates.model_dump(),
                coordinate_source=event_request.coordinate_source.value,
                duration_minutes=event_request.duration_minutes,
                expected_duration_minutes=event_request.expected_service_minutes,
                window_start=event_request.window_start,
                window_end=event_request.window_end,
                required_skill=event_request.required_skill.value,
                required_transport=(
                    event_request.required_transport.value
                    if event_request.required_transport
                    else None
                ),
                required_equipment=event_request.required_equipment,
                priority=event_request.priority.value,
                status=event_request.status.value,
                completion_deadline=event_request.completion_deadline,
                source_fields=event_request.source_fields,
                enrichment_rule_version="event-v1",
            )
        )
        dataset.revision += 1
        new_snapshot = planning_input.model_copy(
            update={
                "revision": dataset.revision,
                "requests": [*planning_input.requests, event_request],
            }
        ).model_dump(mode="json")
        baseline_result = PlanningResult.model_validate(pair["baseline"])
        optimized_result = PlanningResult.model_validate(pair["optimized"])
        experiment = {
            "experiment_id": pair["experiment_id"],
            **pair["experiment"],
        }
        baseline_row = Plan(
            dataset_id=dataset.id,
            input_revision=dataset.revision,
            parent_plan_id=base.id,
            kind="replan_baseline",
            event_time=normalized_event.event_time,
            input_snapshot=new_snapshot,
            result=baseline_result.model_dump(mode="json"),
            metrics=baseline_result.metrics.model_dump(mode="json"),
            model_info={
                "algorithm": baseline_result.algorithm,
                "event_kind": event_request.priority.value,
                "insertion_policy": (
                    normal_insertion_policy()
                    if event_request.priority is Priority.NORMAL
                    else "urgent_priority_replan_unprotected_suffix"
                ),
                "protected_request_ids": pair["protected_request_ids"],
                "simulation": True,
                "algorithm_id": "baseline_v1",
                "objective_tuple": objective_tuple(baseline_result.metrics),
                "selected": pair["selected_algorithm"] == "baseline_v1",
                "kpis": pair["evaluations"]["baseline_v1"],
                "experiment": experiment,
            },
        )
        session.add(baseline_row)
        await session.flush()
        selected_kind = (
            "replan_optimized"
            if pair["selected_algorithm"] != "baseline_v1"
            else "replan_baseline_fallback"
        )
        selected_row = Plan(
            dataset_id=dataset.id,
            input_revision=dataset.revision,
            parent_plan_id=base.id,
            baseline_plan_id=baseline_row.id,
            kind=selected_kind,
            event_time=normalized_event.event_time,
            input_snapshot=new_snapshot,
            result=optimized_result.model_dump(mode="json"),
            metrics=optimized_result.metrics.model_dump(mode="json"),
            model_info={
                "algorithm_id": pair["selected_algorithm"],
                "event_kind": event_request.priority.value,
                "insertion_policy": (
                    normal_insertion_policy()
                    if event_request.priority is Priority.NORMAL
                    else "urgent_priority_replan_unprotected_suffix"
                ),
                "requested_algorithm_id": requested_algorithm_id,
                "algorithm": optimized_result.algorithm,
                "protected_request_ids": pair["protected_request_ids"],
                "simulation": True,
                "objective_tuple": objective_tuple(optimized_result.metrics),
                "kpis": pair["evaluations"][pair["selected_algorithm"]],
                "experiment": experiment,
                "selection_reason": pair["selection_reason"],
            },
        )
        session.add(selected_row)
        await session.flush()
        event_row = PlanningEvent(
            id=stable_uuid(f"event:{dataset.id}", normalized_event.idempotency_key),
            dataset_id=dataset.id,
            base_plan_id=base.id,
            event_time=normalized_event.event_time,
            kind=(
                "urgent_request"
                if event_request.priority is Priority.URGENT
                else "normal_request"
            ),
            payload=event_payload,
            idempotency_key=normalized_event.idempotency_key,
            result_plan_id=selected_row.id,
        )
        session.add(event_row)
        await session.commit()
        return {
            "idempotent": False,
            "event_id": str(event_row.id),
            "baseline_plan_id": str(baseline_row.id),
            "plan": _plan_payload(selected_row),
            "protected_request_ids": pair["protected_request_ids"],
        }
    finally:
        if release_in_finally and lock.locked():
            lock.release()
