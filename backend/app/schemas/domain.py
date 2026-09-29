from collections.abc import Iterable
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequiredSkill(StrEnum):
    LOCAL = "local"
    CONNECTION = "connection"
    EMERGENCY = "emergency"


class Transport(StrEnum):
    CAR = "car"
    WALKING = "walking"
    BICYCLE = "bicycle"
    PUBLIC_TRANSPORT = "public_transport"


class RoutingQuality(StrEnum):
    EXACT = "exact"
    CACHED = "cached"
    ESTIMATED = "estimated"


class Priority(StrEnum):
    NORMAL = "normal"
    URGENT = "urgent"


class WorkPriorityClass(StrEnum):
    EMERGENCY = "emergency"
    CONNECTION = "connection"
    ROUTINE = "routine"


class RequestStatus(StrEnum):
    NEW = "new"
    ASSIGNED = "assigned"
    EN_ROUTE = "en_route"
    ARRIVED = "arrived"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ISSUE = "issue"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"


class CoordinateSource(StrEnum):
    """Откуда координата заявки.

    verified — подтверждена (введена диспетчером вручную или получена от заказчика);
    geocoded — найдена по адресу в OpenStreetMap; synthetic — демо-точка района.
    """

    VERIFIED = "verified"
    GEOCODED = "geocoded"
    SYNTHETIC = "synthetic"


def coordinate_quality(sources: Iterable[CoordinateSource]) -> str:
    """Худший источник координат среди заявок плана: synthetic < geocoded < verified."""
    values = {source.value for source in sources}
    if CoordinateSource.SYNTHETIC.value in values:
        return CoordinateSource.SYNTHETIC.value
    if CoordinateSource.GEOCODED.value in values:
        return CoordinateSource.GEOCODED.value
    return CoordinateSource.VERIFIED.value


class Coordinates(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ServiceRequestInput(StrictModel):
    id: str
    external_id: str
    input_order: int = Field(ge=0)
    address: str = Field(min_length=1)
    district: str = Field(min_length=1)
    coordinates: Coordinates
    coordinate_source: CoordinateSource = CoordinateSource.SYNTHETIC
    duration_minutes: int = Field(gt=0, le=720)
    expected_duration_minutes: int | None = Field(default=None, gt=0, le=720)
    window_start: datetime
    window_end: datetime
    required_skill: RequiredSkill
    required_transport: Transport | None = None
    required_equipment: dict[str, int] = Field(default_factory=dict)
    priority: Priority = Priority.NORMAL
    status: RequestStatus = RequestStatus.NEW
    completion_deadline: datetime | None = None
    source_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("required_equipment")
    @classmethod
    def validate_required_equipment(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key.strip() or amount <= 0 for key, amount in value.items()):
            raise ValueError("required equipment must have non-empty codes and positive amounts")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "ServiceRequestInput":
        if self.window_start > self.window_end:
            raise ValueError("window_start must not be after window_end")
        if self.window_start.date() != self.window_end.date():
            raise ValueError("request window must be inside one day")
        if self.window_start.utcoffset() is None or self.window_end.utcoffset() is None:
            raise ValueError("request datetimes must include UTC offset")
        if self.completion_deadline is not None:
            if self.completion_deadline.utcoffset() is None:
                raise ValueError("completion_deadline must include UTC offset")
            if self.completion_deadline < self.window_start:
                raise ValueError("completion_deadline must not be before window_start")
        if (
            self.expected_duration_minutes is not None
            and self.expected_duration_minutes > self.duration_minutes
        ):
            raise ValueError("expected_duration_minutes must not exceed duration_minutes")
        return self

    @property
    def expected_service_minutes(self) -> int:
        """Forecast duration; full duration stays reserved to protect later visits."""
        return self.expected_duration_minutes or self.duration_minutes


class EngineerInput(StrictModel):
    id: str
    external_id: str
    input_order: int = Field(ge=0)
    name: str = Field(min_length=1)
    start_location: Coordinates
    shift_start: datetime
    shift_end: datetime
    skills: list[RequiredSkill] = Field(min_length=1, max_length=3)
    transport: Transport
    equipment_inventory: dict[str, int] = Field(default_factory=dict)
    is_synthetic: bool = True

    @field_validator("skills")
    @classmethod
    def unique_skills(cls, value: list[RequiredSkill]) -> list[RequiredSkill]:
        if len(value) != len(set(value)):
            raise ValueError("skills must be unique")
        return value

    @field_validator("equipment_inventory")
    @classmethod
    def validate_equipment_inventory(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key.strip() or amount < 0 for key, amount in value.items()):
            raise ValueError(
                "equipment inventory must have non-empty codes and non-negative amounts"
            )
        return value

    @model_validator(mode="after")
    def validate_shift(self) -> "EngineerInput":
        if self.shift_start >= self.shift_end:
            raise ValueError("shift_start must be before shift_end")
        if self.shift_start.date() != self.shift_end.date():
            raise ValueError("shift must be inside one day")
        if self.shift_start.utcoffset() is None or self.shift_end.utcoffset() is None:
            raise ValueError("shift datetimes must include UTC offset")
        return self


class DatasetInput(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    title: str
    planning_date: date
    timezone: str = "Europe/Moscow"
    office: Coordinates
    requests: list[ServiceRequestInput] = Field(max_length=100)
    engineers: list[EngineerInput] = Field(max_length=15)


class PlanningInput(StrictModel):
    dataset_id: str
    revision: int = Field(ge=1)
    planning_date: date
    timezone: str = "Europe/Moscow"
    requests: list[ServiceRequestInput] = Field(max_length=100)
    engineers: list[EngineerInput] = Field(max_length=15)
    route_estimation_method: str = "road_matrix_by_transport_v6"
    buffer_minutes: int = Field(default=0, ge=0, le=30)


class RouteStop(StrictModel):
    request_id: str
    sequence: int = Field(ge=1)
    location: Coordinates | None = None
    arrival_at: datetime
    service_start_at: datetime
    expected_service_end_at: datetime | None = None
    service_end_at: datetime
    departure_at: datetime
    wait_minutes: int = Field(ge=0)
    travel_minutes_from_previous: int = Field(ge=0)
    distance_meters_from_previous: int = Field(ge=0)
    reserve_minutes: int = Field(default=0, ge=0)
    explanation_codes: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)


class EngineerRoute(StrictModel):
    engineer_id: str
    engineer_name: str
    transport: Transport
    routing_method: str = "road_matrix_by_transport_v6"
    routing_quality: RoutingQuality = RoutingQuality.ESTIMATED
    start_location: Coordinates
    departure_at: datetime
    stops: list[RouteStop] = Field(default_factory=list)
    distance_meters: int = Field(ge=0)
    travel_minutes: int = Field(ge=0)
    service_minutes: int = Field(ge=0)
    wait_minutes: int = Field(ge=0)
    finish_at: datetime
    explanation: str


class UnassignedRequest(StrictModel):
    request_id: str
    external_id: str
    priority: Priority
    priority_class: WorkPriorityClass = WorkPriorityClass.ROUTINE
    reason_code: str
    explanation: str
    details: dict[str, Any] = Field(default_factory=dict)


class EngineerMetrics(StrictModel):
    engineer_id: str
    visits: int = Field(ge=0)
    distance_meters: int = Field(ge=0)
    travel_minutes: int = Field(ge=0)
    service_minutes: int = Field(ge=0)
    wait_minutes: int = Field(ge=0)


class PlanMetrics(StrictModel):
    assigned_count: int = Field(ge=0)
    unassigned_count: int = Field(ge=0)
    urgent_unassigned_count: int = Field(ge=0)
    normal_unassigned_count: int = Field(ge=0)
    emergency_unassigned_count: int = Field(default=0, ge=0)
    connection_unassigned_count: int = Field(default=0, ge=0)
    routine_unassigned_count: int = Field(default=0, ge=0)
    # Визиты, доехавшие дольше норматива времени дороги (TRAVEL_NORM_MODE=soft).
    # В режиме hard всегда 0: там такое плечо недопустимо. Старые планы без поля — 0.
    # Число для отчёта; в цели сравнения стоит величина — norm_excess_minutes.
    norm_violation_count: int = Field(default=0, ge=0)
    # Сумма по визитам max(0, дорога − норматив) в минутах: ступень цели режима soft.
    # В режиме hard всегда 0. Старые планы без поля — 0.
    norm_excess_minutes: int = Field(default=0, ge=0)
    used_engineers_count: int = Field(ge=0)
    total_distance_meters: int = Field(ge=0)
    total_travel_minutes: int = Field(ge=0)
    per_engineer: list[EngineerMetrics] = Field(default_factory=list)


class PlanningResult(StrictModel):
    algorithm: str
    solution_status: str
    termination_reason: str
    elapsed_ms: int = Field(ge=0)
    route_estimation_method: str
    coordinate_quality: str
    objective_weights: dict[str, int] = Field(default_factory=dict)
    # Только у перепланирования: сколько заявок сменило исполнителя и сдвинулось по времени
    # относительно базового плана (compared_count, reassigned_count, time_shifted_count,
    # shift_threshold_minutes).
    replan_churn: dict[str, int] = Field(default_factory=dict)
    routes: list[EngineerRoute]
    unassigned: list[UnassignedRequest]
    metrics: PlanMetrics
    constraint_violations_count: int = Field(ge=0)


class PlanningRequestEvent(StrictModel):
    event_id: str
    idempotency_key: str = Field(min_length=1, max_length=120)
    event_time: datetime
    expected_revision: int = Field(ge=1)
    request: ServiceRequestInput

    @model_validator(mode="after")
    def validate_event(self) -> "PlanningRequestEvent":
        if self.event_time.date() != self.request.window_start.date():
            raise ValueError("event and request must be on the same day")
        if self.request.window_end < self.event_time:
            raise ValueError("request window must not end before event_time")
        return self


class UrgentRequestEvent(PlanningRequestEvent):
    @model_validator(mode="after")
    def validate_urgent(self) -> "UrgentRequestEvent":
        if self.request.priority is not Priority.URGENT:
            raise ValueError("event request priority must be urgent")
        return self


def apply_emergency_sla(event: PlanningRequestEvent) -> PlanningRequestEvent:
    """Attach the customer-confirmed 100-minute completion SLA to urgent events."""
    if event.request.priority is not Priority.URGENT:
        return event.model_copy(
            update={
                "request": event.request.model_copy(
                    update={"window_start": max(event.request.window_start, event.event_time)}
                )
            }
        )
    deadline = event.event_time + timedelta(minutes=100)
    source_fields = dict(event.request.source_fields)
    source_fields["_sla"] = {
        "kind": "emergency_completion",
        "starts_at": event.event_time.isoformat(),
        "minutes": 100,
        "complete_by": deadline.isoformat(),
        "source": "customer_requirement_100m_completion",
    }
    request = event.request.model_copy(
        update={
            "window_start": max(event.request.window_start, event.event_time),
            "completion_deadline": deadline,
            "source_fields": source_fields,
        }
    )
    return event.model_copy(update={"request": request})


class StopChange(StrictModel):
    request_id: str
    previous_engineer_id: str | None = None
    new_engineer_id: str | None = None
    previous_sequence: int | None = None
    new_sequence: int | None = None
    previous_eta: datetime | None = None
    new_eta: datetime | None = None


class PlanDiff(StrictModel):
    changes: list[StopChange]
    changed_engineer_ids: list[str]
    distance_delta_meters: int
    assigned_delta: int


class RequestPlacement(StrictModel):
    """Допустимое место ручной вставки отказанной заявки в опубликованный план.

    `position` — индекс в маршруте бригады, куда встаёт заявка: 0 — перед первым
    визитом, len(stops) — после последнего. Прежние визиты не двигаются. Приросты
    бывают отрицательными: короткие плечи автобригада проходит пешком, с другим
    коэффициентом извилистости, чем прежнее плечо на машине.
    """

    engineer_id: str
    engineer_name: str
    position: int = Field(ge=0)
    arrival_at: datetime
    service_start_at: datetime
    service_end_at: datetime
    added_distance_meters: int
    added_travel_minutes: int
    # Хотя бы одно из двух новых плеч (до заявки и от неё до следующего визита) идёт
    # дольше норматива дороги. В режиме TRAVEL_NORM_MODE=hard таких вариантов нет.
    norm_exceeded: bool
    # Прирост минут дороги сверх норматива по маршруту бригады. Бывает отрицательным:
    # вставка делит длинный переезд на два коротких. В режиме hard всегда 0.
    added_norm_excess_minutes: int = 0


class RequestPlacements(StrictModel):
    request_id: str
    placements: list[RequestPlacement]


class ManualAssignRequest(StrictModel):
    """Ручное назначение отказанной заявки в выбранное место маршрута бригады."""

    request_id: UUID
    engineer_id: UUID
    position: int = Field(ge=0)
    expected_revision: int = Field(ge=1)
