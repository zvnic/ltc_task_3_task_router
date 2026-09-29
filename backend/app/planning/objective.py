"""Целевая функция планирования: лексикографический и стоимостной режимы.

Режим и веса берутся из настроек (`objective_mode`, `cost_per_*`, `penalty_*`).
По умолчанию работает режим "lexicographic": пропуски аварий → пропуски подключений →
пропуски обычных работ → минуты дороги сверх норматива → число занятых бригад → время
в пути → пробег.
Превышение норматива дороже лишней бригады, но дешевле невыполненной заявки: она
остаётся худшим нарушением SLA. Сравнивается ВЕЛИЧИНА превышения (сумма минут), а не
число визитов с превышением: иначе одно плечо в 9 часов стоило бы столько же, сколько
плечо на минуту дольше норматива. Ступень норматива есть только в ключе сравнения
планов (`plan_objective_key`); паспортный `objective_tuple` из metrics.py остаётся
пятиэлементным. При TRAVEL_NORM_MODE=hard превышений не бывает, и ключ упорядочивает
планы ровно как прежний кортеж. При TRAVEL_NORM_MODE=advisory (по умолчанию) норматив
справочный: ступень всегда 0, превышения показываются диспетчеру, но план не хуже от них
— так организаторы описали норматив 20 минут (слот в графике, а не предел поездки).

Время в пути стоит раньше пробега: бригада тратит на дорогу минуты смены, а не
километры. По пробегу решатель одинаково ценил километр по МКАД и по центру, пешком
и на машине; по времени он отдаёт дальнее плечо машине, а не общественному
транспорту, и берёт загородную трассу, если по ней быстрее. Пробег остался последней
ступенью: из планов с равным временем выбирается более короткий.

Режим "cost" складывает те же величины в одну цену, поэтому между ними возможен
размен: лишний километр можно сравнить с лишней бригадой.

ВАЖНО: реальных тарифов заказчика у нас нет. Все денежные веса стоимостного
режима — демонстрационные, подобраны только чтобы показать механику размена, и
подлежат подтверждению у заказчика перед любым использованием в расчётах.
Нормативами они не являются. Если бригады уже в смене и оплачены независимо от
загрузки, `cost_per_crew_day` следует выставить в 0: их стоимость невозвратна.
"""

from typing import Any

from app.core.config import get_settings
from app.planning.metrics import objective_tuple
from app.planning.policies import (
    travel_norm_is_penalized,
    travel_norm_max_factor,
    travel_norm_mode,
)
from app.schemas import PlanMetrics

LEXICOGRAPHIC = "lexicographic"
COST = "cost"

OBJECTIVE_LABELS = (
    "emergency_unassigned",
    "connection_unassigned",
    "routine_unassigned",
    "used_engineers",
    "travel_minutes",
    "distance_meters",
)

# Порядок `plan_objective_key` в лексикографическом режиме: от OBJECTIVE_LABELS
# отличается одной ступенью — минутами дороги сверх норматива после пропусков заявок.
PLAN_KEY_LABELS = (
    "emergency_unassigned",
    "connection_unassigned",
    "routine_unassigned",
    "norm_excess_minutes",
    "used_engineers",
    "travel_minutes",
    "distance_meters",
)

# Цена дуги для решателя OR-Tools в лексикографическом режиме: минута дороги весит
# TRAVEL_MINUTE_ARC_WEIGHT, километр пробега — единицу. На одной дуге сто километров
# разницы никогда не укладываются в минуту (загородная скорость не выше 70 км/ч),
# поэтому дуги упорядочены по времени, а километр решает только при равных минутах.
# Строгий порядок «время, потом пробег» по всему плану держит `plan_objective_key`:
# им сравниваются планы, решатель по этой цене только ищет.
TRAVEL_MINUTE_ARC_WEIGHT = 100

# Стоимость считается в тысячных долях условной единицы: при таком масштабе цена
# километра переводится в цену метра нацело, без потери точности на округлении.
COST_SCALE = 1000


def objective_mode() -> str:
    return get_settings().objective_mode


def cost_weights() -> dict[str, int]:
    """Демонстрационные цены стоимостного режима в условных единицах."""
    settings = get_settings()
    return {
        "cost_per_crew_day": settings.cost_per_crew_day,
        "cost_per_km": settings.cost_per_km,
        "cost_per_travel_minute": settings.cost_per_travel_minute,
        "penalty_unassigned_routine": settings.penalty_unassigned_routine,
        "penalty_unassigned_connection": settings.penalty_unassigned_connection,
        "penalty_unassigned_emergency": settings.penalty_unassigned_emergency,
        "penalty_norm_excess_minute": settings.penalty_norm_excess_minute,
    }


def penalized_norm_excess_minutes(metrics: PlanMetrics) -> int:
    """Минуты сверх норматива, которые учитывает цель: только в режиме soft."""
    return metrics.norm_excess_minutes if travel_norm_is_penalized() else 0


def plan_cost(metrics: PlanMetrics) -> int:
    """Стоимость плана в тысячных долях условной единицы."""
    weights = cost_weights()
    return (
        weights["cost_per_crew_day"] * COST_SCALE * metrics.used_engineers_count
        + weights["cost_per_km"] * metrics.total_distance_meters
        + weights["cost_per_travel_minute"] * COST_SCALE * metrics.total_travel_minutes
        + weights["penalty_unassigned_routine"] * COST_SCALE * metrics.routine_unassigned_count
        + weights["penalty_unassigned_connection"]
        * COST_SCALE
        * metrics.connection_unassigned_count
        + weights["penalty_unassigned_emergency"]
        * COST_SCALE
        * metrics.emergency_unassigned_count
        + weights["penalty_norm_excess_minute"]
        * COST_SCALE
        * penalized_norm_excess_minutes(metrics)
    )


def lexicographic_plan_key(metrics: PlanMetrics) -> tuple[int, int, int, int, int, int, int]:
    """Ключ по `PLAN_KEY_LABELS` независимо от режима цели.

    Нужен и стоимостному режиму: разрыв по времени в пути и пробегу (`travel_gap`,
    `distance_gap`) честен только между планами с одинаковым охватом, превышениями и
    числом бригад.
    """
    emergency, connection, routine, used_engineers, travel, distance = objective_tuple(metrics)
    return (
        emergency,
        connection,
        routine,
        penalized_norm_excess_minutes(metrics),
        used_engineers,
        travel,
        distance,
    )


def plan_objective_key(metrics: PlanMetrics) -> tuple[int, ...]:
    """Ключ сравнения планов в текущем режиме: чем меньше, тем лучше.

    Лексикографический режим сравнивает по `PLAN_KEY_LABELS`, стоимостной — по одной
    цене `plan_cost`, куда минуты превышения норматива входят слагаемым
    penalty_norm_excess_minute.
    """
    if objective_mode() == COST:
        return (plan_cost(metrics),)
    return lexicographic_plan_key(metrics)


def objective_passport() -> dict[str, Any]:
    """Режим и веса, по которым сравнивались планы, — для паспорта плана."""
    mode = objective_mode()
    norm_policy = {
        "travel_norm_mode": travel_norm_mode(),
        "travel_norm_max_factor": travel_norm_max_factor(),
    }
    if mode != COST:
        return {
            "objective_mode": mode,
            "objective_criteria": list(OBJECTIVE_LABELS),
            "plan_key_criteria": list(PLAN_KEY_LABELS),
            **norm_policy,
        }
    return {
        "objective_mode": mode,
        "objective_weights": cost_weights(),
        "cost_scale": COST_SCALE,
        **norm_policy,
        "weights_note": (
            "демонстрационные условные единицы, не нормативы; требуют подтверждения заказчиком"
        ),
    }


def calculate_cost_weights(
    request_count: int,
    vehicle_count: int,
    maximum_distance: int,
    maximum_travel_minutes: int,
    *,
    maximum_norm_excess_minutes: int = 0,
) -> dict[str, int]:
    """Переводит демонстрационные цены в целые веса OR-Tools (тысячные у.е.).

    `maximum_norm_excess_minutes` — наибольшее допустимое превышение норматива на одной
    дуге (режим soft: потолок минус норматив). Штраф за минуту превышения платится на
    дуге, ведущей в заявку, поэтому входит в верхнюю оценку цены дуги. При 0 (режим
    hard или множитель 1,0) веса остаются ровно прежними.
    """
    weights = cost_weights()
    meter_weight = weights["cost_per_km"]
    travel_minute_weight = weights["cost_per_travel_minute"] * COST_SCALE
    engineer_weight = weights["cost_per_crew_day"] * COST_SCALE
    routine_drop_weight = weights["penalty_unassigned_routine"] * COST_SCALE
    connection_drop_weight = weights["penalty_unassigned_connection"] * COST_SCALE
    emergency_drop_weight = weights["penalty_unassigned_emergency"] * COST_SCALE
    norm_excess_weight = (
        weights["penalty_norm_excess_minute"] * COST_SCALE if maximum_norm_excess_minutes else 0
    )
    maximum_arc_cost = (
        maximum_distance * meter_weight
        + maximum_travel_minutes * travel_minute_weight
        + maximum_norm_excess_minutes * norm_excess_weight
    )
    arc_bound = (request_count + vehicle_count) * maximum_arc_cost
    objective_upper_bound = (
        arc_bound
        + vehicle_count * engineer_weight
        + request_count
        * max(routine_drop_weight, connection_drop_weight, emergency_drop_weight)
    )
    if objective_upper_bound >= 2**63:
        raise ValueError("planning objective exceeds int64")
    result = {
        "cost_scale": COST_SCALE,
        "maximum_edge_distance": maximum_distance,
        "maximum_edge_travel_minutes": maximum_travel_minutes,
        "meter": meter_weight,
        "travel_minute": travel_minute_weight,
        "used_engineer": engineer_weight,
        "routine_unassigned": routine_drop_weight,
        "connection_unassigned": connection_drop_weight,
        "emergency_unassigned": emergency_drop_weight,
        "objective_upper_bound": objective_upper_bound,
    }
    if maximum_norm_excess_minutes:
        result["maximum_edge_norm_excess_minutes"] = maximum_norm_excess_minutes
        result["norm_excess_minute"] = norm_excess_weight
    return result
