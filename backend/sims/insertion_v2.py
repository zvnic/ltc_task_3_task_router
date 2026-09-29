"""Исправленная эвристика вставки — для сравнения с insertion_ls_v1.

Три отличия от оригинала:
1. Стоимость вставки — ПРИРОСТ пробега маршрута, а не его полная длина.
   В оригинале `distance_delta = route.distance_meters`, из-за чего заявка уходит
   в самый короткий маршрут, а не туда, где она дешевле всего вставляется.
2. После локального поиска неназначенные заявки пробуются заново: перестановки
   освобождают окна, но оригинал к отказанным заявкам больше не возвращается.
3. Скан соседства продолжается с места остановки, а не начинается заново после
   каждого улучшения — за тот же лимит времени просматривается кратно больше ходов.
"""

from __future__ import annotations

from time import perf_counter

from app.planning.metrics import calculate_metrics, objective_tuple
from app.planning.reasons import make_unassigned
from app.planning.schedule import calculate_route
from app.planning.validation import validate_plan
from app.schemas import (
    EngineerInput,
    EngineerRoute,
    PlanningInput,
    PlanningResult,
    ServiceRequestInput,
)


def _request_order(
    request: ServiceRequestInput,
    engineers: list[EngineerInput],
) -> tuple[int, int, object, int]:
    eligible_count = sum(
        request.required_skill in engineer.skills
        and (request.required_transport is None or request.required_transport == engineer.transport)
        for engineer in engineers
    )
    return (
        0 if request.priority.value == "urgent" else 1,
        eligible_count,
        request.window_end,
        request.input_order,
    )


def _routes(
    engineers: list[EngineerInput],
    assigned: dict[str, list[ServiceRequestInput]],
) -> list[EngineerRoute] | None:
    result: list[EngineerRoute] = []
    for engineer in engineers:
        route = calculate_route(engineer, assigned[engineer.id])
        if route is None:
            return None
        result.append(route)
    return result


def _route_distance(
    engineer: EngineerInput,
    requests: list[ServiceRequestInput],
) -> int | None:
    route = calculate_route(engineer, requests)
    return None if route is None else route.distance_meters


def _best_insertion(
    request: ServiceRequestInput,
    engineers: list[EngineerInput],
    assigned: dict[str, list[ServiceRequestInput]],
) -> tuple[str, int] | None:
    best: tuple[tuple[int, int], int, int] | None = None
    for engineer in engineers:
        current = assigned[engineer.id]
        base_distance = _route_distance(engineer, current) or 0
        for position in range(len(current) + 1):
            candidate = [*current[:position], request, *current[position:]]
            candidate_distance = _route_distance(engineer, candidate)
            if candidate_distance is None:
                continue
            used_delta = int(not current)
            distance_delta = candidate_distance - base_distance
            ranked = ((used_delta, distance_delta), engineer.input_order, position)
            if best is None or ranked < best:
                best = ranked
                best_engineer = engineer.id
    if best is None:
        return None
    return best_engineer, best[2]


def build_insertion_v2(
    planning_input: PlanningInput,
    time_limit_seconds: int = 10,
) -> PlanningResult:
    started_at = perf_counter()
    deadline = started_at + max(0.05, min(time_limit_seconds, 20))
    engineers = sorted(planning_input.engineers, key=lambda item: item.input_order)
    requests = sorted(
        planning_input.requests,
        key=lambda item: _request_order(item, engineers),
    )
    assigned: dict[str, list[ServiceRequestInput]] = {engineer.id: [] for engineer in engineers}
    unassigned_requests: list[ServiceRequestInput] = []

    for request in requests:
        insertion = _best_insertion(request, engineers, assigned)
        if insertion is None:
            unassigned_requests.append(request)
            continue
        engineer_id, position = insertion
        assigned[engineer_id].insert(position, request)

    def current_objective() -> tuple[int, int, int, int] | None:
        routes = _routes(engineers, assigned)
        if routes is None:
            return None
        unassigned = [
            make_unassigned(item, engineers, fallback="not_selected_within_limit")
            for item in unassigned_requests
        ]
        return objective_tuple(calculate_metrics(routes, unassigned))

    # Перенос одного визита; скан продолжается с места остановки (без рестарта).
    moves = [
        (source.id, target.id)
        for source in engineers
        for target in engineers
    ]
    move_index = 0
    stagnation = 0
    best_tuple = current_objective()
    while perf_counter() < deadline and best_tuple is not None and stagnation <= len(moves):
        source_id, target_id = moves[move_index % len(moves)]
        move_index += 1
        stagnation += 1
        improved_here = False
        for source_position in range(len(assigned[source_id])):
            if perf_counter() >= deadline:
                break
            for target_position in range(len(assigned[target_id]) + 1):
                if source_id == target_id and target_position in {
                    source_position,
                    source_position + 1,
                }:
                    continue
                candidate = {key: list(value) for key, value in assigned.items()}
                moved = candidate[source_id].pop(source_position)
                adjusted = target_position
                if source_id == target_id and target_position > source_position:
                    adjusted -= 1
                candidate[target_id].insert(adjusted, moved)
                candidate_routes = _routes(engineers, candidate)
                if candidate_routes is None:
                    continue
                unassigned = [
                    make_unassigned(item, engineers, fallback="not_selected_within_limit")
                    for item in unassigned_requests
                ]
                candidate_tuple = objective_tuple(
                    calculate_metrics(candidate_routes, unassigned)
                )
                if best_tuple is None or candidate_tuple < best_tuple:
                    assigned = candidate
                    best_tuple = candidate_tuple
                    improved_here = True
                    stagnation = 0
                    break
            if improved_here:
                break

        # после улучшения пробуем пристроить отказанные заявки заново
        if improved_here and unassigned_requests:
            still_unassigned: list[ServiceRequestInput] = []
            for request in unassigned_requests:
                insertion = _best_insertion(request, engineers, assigned)
                if insertion is None:
                    still_unassigned.append(request)
                    continue
                engineer_id, position = insertion
                assigned[engineer_id].insert(position, request)
            unassigned_requests = still_unassigned
            best_tuple = current_objective()

    # финальная попытка для отказанных
    still_unassigned = []
    for request in unassigned_requests:
        insertion = _best_insertion(request, engineers, assigned)
        if insertion is None:
            still_unassigned.append(request)
            continue
        engineer_id, position = insertion
        assigned[engineer_id].insert(position, request)
    unassigned_requests = still_unassigned

    routes = _routes(engineers, assigned) or []
    unassigned = [
        make_unassigned(request, engineers, fallback="not_selected_within_limit")
        for request in unassigned_requests
    ]
    result = PlanningResult(
        algorithm="insertion_ls_v2",
        solution_status="feasible",
        termination_reason=(
            "time_limit" if perf_counter() >= deadline else "local_neighborhood_exhausted"
        ),
        elapsed_ms=max(0, round((perf_counter() - started_at) * 1000)),
        route_estimation_method=planning_input.route_estimation_method,
        coordinate_quality=(
            "synthetic"
            if any(item.coordinate_source.value == "synthetic" for item in requests)
            else "verified"
        ),
        routes=routes,
        unassigned=unassigned,
        metrics=calculate_metrics(routes, unassigned),
        constraint_violations_count=0,
    )
    violations = validate_plan(planning_input, result)
    if violations:
        raise ValueError(f"insertion_v2 validator rejected result: {violations}")
    return result
