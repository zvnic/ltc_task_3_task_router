from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import floor

from app.core.config import get_settings
from app.schemas import (
    EngineerInput,
    Priority,
    RequiredSkill,
    ServiceRequestInput,
    WorkPriorityClass,
)


def request_priority_class(request: ServiceRequestInput) -> WorkPriorityClass:
    """Map customer work classes to the solver's lexicographic priority tiers."""
    if request.priority is Priority.URGENT:
        return WorkPriorityClass.EMERGENCY

    enrichment = request.source_fields.get("_enrichment")
    work_class = enrichment.get("work_class") if isinstance(enrichment, dict) else None
    if work_class == "emergency":
        return WorkPriorityClass.EMERGENCY
    if work_class == "connection":
        return WorkPriorityClass.CONNECTION
    if work_class in {"local", "add_order", "routine"}:
        return WorkPriorityClass.ROUTINE

    source_type = str(request.source_fields.get("Тип заявки BK", "")).strip()
    source_subtype = str(request.source_fields.get("Тип заявки HD", "")).strip()
    if source_subtype.casefold() == "авария":
        return WorkPriorityClass.EMERGENCY
    if source_type.casefold() == "подключение":
        return WorkPriorityClass.CONNECTION
    if request.required_skill is RequiredSkill.EMERGENCY:
        return WorkPriorityClass.EMERGENCY
    if request.required_skill is RequiredSkill.CONNECTION and source_type.casefold() != "дозаказ":
        return WorkPriorityClass.CONNECTION
    return WorkPriorityClass.ROUTINE


def equipment_totals(requests: list[ServiceRequestInput]) -> Counter[str]:
    totals: Counter[str] = Counter()
    for request in requests:
        totals.update(request.required_equipment)
    return totals


def engineer_has_equipment_for_requests(
    engineer: EngineerInput,
    requests: list[ServiceRequestInput],
) -> bool:
    return all(
        amount <= engineer.equipment_inventory.get(code, 0)
        for code, amount in equipment_totals(requests).items()
    )


def effective_service_start_deadline(request: ServiceRequestInput) -> datetime:
    """Latest valid service start, including an optional completion SLA."""
    if request.completion_deadline is None:
        return request.window_end
    return min(
        request.window_end,
        request.completion_deadline - timedelta(minutes=request.duration_minutes),
    )


def request_travel_norm_minutes(request: ServiceRequestInput) -> int | None:
    """Return the active per-leg travel norm bound to the request."""
    enrichment = request.source_fields.get("_enrichment")
    if not isinstance(enrichment, dict):
        return None
    value = enrichment.get("travel_norm_minutes")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


TRAVEL_NORM_HARD = "hard"
TRAVEL_NORM_SOFT = "soft"
TRAVEL_NORM_ADVISORY = "advisory"


def travel_norm_mode() -> str:
    """Режим норматива дороги: "advisory" — справочный, "soft" — штраф, "hard" — запрет."""
    return get_settings().travel_norm_mode


def travel_norm_is_hard() -> bool:
    return travel_norm_mode() == TRAVEL_NORM_HARD


def travel_norm_is_penalized() -> bool:
    """Штрафует ли цель минуты дороги сверх норматива: только в режиме soft."""
    return travel_norm_mode() == TRAVEL_NORM_SOFT


def travel_norm_max_factor() -> float:
    """Множитель потолка плеча в режиме soft (TRAVEL_NORM_MAX_FACTOR)."""
    return get_settings().travel_norm_max_factor


@dataclass(frozen=True)
class TravelNormPolicy:
    """Как норматив дороги ограничивает плечо; читается из настроек один раз на расчёт.

    Единственное место, где режим превращает норматив в запрет плеча (`limit`):
    в режиме hard предел — сам норматив, в режиме soft — потолок «норматив ×
    множитель», в режиме advisory предела нет. Превышение в пределах потолка не
    запрещается, а считается (`travel_norm_excess_minutes`); штрафует его цель только
    в режиме soft.
    """

    hard: bool
    max_factor: float
    advisory: bool = False

    def limit(self, norm_minutes: int | None) -> int | None:
        """Наибольшее допустимое время плеча до заявки с этим нормативом."""
        if norm_minutes is None or self.advisory:
            return None
        if self.hard:
            return norm_minutes
        # round убирает хвост двоичной дроби (10 × 2,3 = 22,999…), floor — до минуты
        return max(norm_minutes, floor(round(norm_minutes * self.max_factor, 6)))

    def max_excess(self, norm_minutes: int | None) -> int:
        """Наибольшее штрафуемое превышение норматива на одном плече (0 в hard и advisory)."""
        limit = self.limit(norm_minutes)
        if norm_minutes is None or limit is None:
            return 0
        return limit - norm_minutes


def travel_norm_policy() -> TravelNormPolicy:
    settings = get_settings()
    return TravelNormPolicy(
        hard=settings.travel_norm_mode == TRAVEL_NORM_HARD,
        max_factor=settings.travel_norm_max_factor,
        advisory=settings.travel_norm_mode == TRAVEL_NORM_ADVISORY,
    )
