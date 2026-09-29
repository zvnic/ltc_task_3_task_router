from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.core.errors import DomainError
from app.planning import (
    build_replan_pair,
    evaluate_result,
    evaluate_results,
    experiment_metadata,
    run_solvers,
    select_best,
    selection_reason,
    validate_plan,
)
from app.planning.evaluator import manual_selection_reason
from app.planning.metrics import calculate_metrics, objective_tuple, route_norm_excess_minutes
from app.planning.routing import travel_exceeds_norm
from app.planning.schedule import insert_request_preserving_visits
from app.schemas import (
    EngineerInput,
    EngineerRoute,
    PlanningInput,
    PlanningRequestEvent,
    PlanningResult,
    RouteStop,
    ServiceRequestInput,
    apply_emergency_sla,
)
from app.schemas.domain import RequestPlacement, RequestPlacements

# Политика ручного назначения: заявка встаёт в выбранное диспетчером место маршрута,
# исполнитель, порядок и время прежних визитов не меняются.
MANUAL_INSERT_POLICY = "manual_position_without_reordering"
# Алгоритм плана с ручным назначением в паспорте и отпечатке эксперимента: решателя
# нет, лимита времени нет.
MANUAL_INSERT_ALGORITHM = "manual_gap_insertion"


def calculate_plan_pair(
    planning_payload: dict[str, Any],
    solver_time_limit_seconds: int,
) -> dict[str, Any]:
    return calculate_plan_batch(
        planning_payload,
        ["baseline_v1", "ortools_gls_v1"],
        solver_time_limit_seconds,
    )


def calculate_plan_batch(
    planning_payload: dict[str, Any],
    algorithm_ids: list[str],
    solver_time_limit_seconds: int,
) -> dict[str, Any]:
    planning_input = PlanningInput.model_validate(planning_payload)
    ordered_ids = list(dict.fromkeys(["baseline_v1", *algorithm_ids]))
    results = run_solvers(planning_input, ordered_ids, solver_time_limit_seconds)
    selected_algorithm, selected = select_best(results)
    evaluations = evaluate_results(planning_input, results)
    return {
        "experiment_id": str(uuid4()),
        "experiment": experiment_metadata(
            planning_input,
            ordered_ids,
            solver_time_limit_seconds,
        ),
        "baseline": results["baseline_v1"].model_dump(mode="json"),
        "optimized": selected.model_dump(mode="json"),
        "selected_algorithm": selected_algorithm,
        "selection_reason": selection_reason(selected_algorithm, results),
        "evaluations": evaluations,
        "runs": {
            algorithm_id: result.model_dump(mode="json")
            for algorithm_id, result in results.items()
        },
    }


def calculate_replan_pair(
    planning_payload: dict[str, Any],
    base_result_payload: dict[str, Any],
    event_payload: dict[str, Any],
    solver_time_limit_seconds: int,
    algorithm_id: str = "ortools_gls_v1",
) -> dict[str, Any]:
    planning_input = PlanningInput.model_validate(planning_payload)
    base_result = PlanningResult.model_validate(base_result_payload)
    event = apply_emergency_sla(PlanningRequestEvent.model_validate(event_payload))
    baseline, optimized, protected_ids = build_replan_pair(
        planning_input,
        base_result,
        event,
        solver_time_limit_seconds,
        algorithm_id,
    )
    evaluation_input = planning_input.model_copy(
        update={
            "revision": planning_input.revision + 1,
            "requests": [*planning_input.requests, event.request],
        }
    )
    candidate_results = {"baseline_v1": baseline}
    if algorithm_id != "baseline_v1":
        candidate_results[algorithm_id] = optimized
    selected_algorithm, selected = select_best(candidate_results)
    evaluations = evaluate_results(evaluation_input, candidate_results)
    algorithm_ids = list(candidate_results)
    return {
        "experiment_id": str(uuid4()),
        "experiment": experiment_metadata(
            evaluation_input,
            algorithm_ids,
            solver_time_limit_seconds,
        ),
        "baseline": baseline.model_dump(mode="json"),
        "optimized": selected.model_dump(mode="json"),
        "selected_algorithm": selected_algorithm,
        "selection_reason": selection_reason(selected_algorithm, candidate_results),
        "evaluations": evaluations,
        "protected_request_ids": protected_ids,
    }


def _leg_exceeds_norm(stop: RouteStop) -> bool:
    """Доехали ли до визита дольше норматива: та же проверка, что в метриках плана."""
    norm = stop.facts.get("travel_norm_minutes")
    return type(norm) is int and travel_exceeds_norm(stop.travel_minutes_from_previous, norm)


@dataclass(frozen=True)
class PlacementCandidate:
    """Вариант вставки заявки в маршрут бригады без сдвига прежних визитов."""

    engineer: EngineerInput
    position: int
    route_before: EngineerRoute
    route_after: EngineerRoute

    @property
    def inserted_stop(self) -> RouteStop:
        return self.route_after.stops[self.position]

    @property
    def added_distance_meters(self) -> int:
        return self.route_after.distance_meters - self.route_before.distance_meters

    @property
    def added_travel_minutes(self) -> int:
        return self.route_after.travel_minutes - self.route_before.travel_minutes

    @property
    def added_norm_excess_minutes(self) -> int:
        """Прирост минут дороги сверх норматива по маршруту бригады."""
        return route_norm_excess_minutes(self.route_after) - route_norm_excess_minutes(
            self.route_before
        )

    @property
    def norm_exceeded(self) -> bool:
        """Идёт ли дольше норматива дороги хоть одно из двух новых плеч.

        Вставка убирает плечо «предыдущий визит → следующий» и создаёт два новых:
        до заявки и от неё до следующего визита. Проверяются оба: вариант, в котором
        новая заявка укладывается в норматив, а переезд от неё к следующему клиенту —
        нет, тоже помечается.
        """
        new_legs = self.route_after.stops[self.position : self.position + 2]
        return any(_leg_exceeds_norm(stop) for stop in new_legs)

    def to_schema(self) -> RequestPlacement:
        stop = self.inserted_stop
        return RequestPlacement(
            engineer_id=self.engineer.id,
            engineer_name=self.engineer.name,
            position=self.position,
            arrival_at=stop.arrival_at,
            service_start_at=stop.service_start_at,
            service_end_at=stop.service_end_at,
            added_distance_meters=self.added_distance_meters,
            added_travel_minutes=self.added_travel_minutes,
            norm_exceeded=self.norm_exceeded,
            added_norm_excess_minutes=self.added_norm_excess_minutes,
        )


def enumerate_request_placements(
    planning_input: PlanningInput,
    base_result: PlanningResult,
    request: ServiceRequestInput,
) -> list[PlacementCandidate]:
    """Все места, куда заявку можно вставить, не тронув прежние визиты.

    Каждая позиция маршрута каждой бригады проверяется `insert_request_preserving_visits` —
    тем же правилом, что и внутридневная вставка обычной заявки
    (`replan._build_normal_gap_insertion`): навык, транспорт, оборудование, окно,
    SLA, смена и пешее плечо до 1000 м проверяются как у решателей, а исполнитель,
    порядок и время работ прежних визитов не меняются. Норматив дороги работает по
    TRAVEL_NORM_MODE: в hard плечо сверх норматива недопустимо, в soft допустимо до
    потолка «норматив × TRAVEL_NORM_MAX_FACTOR», помечается в `norm_exceeded`, а
    прирост минут сверх норматива пишется в `added_norm_excess_minutes`.

    Порядок — по приросту времени в пути, затем расчётного расстояния, при равенстве по
    бригаде и позиции, чтобы список не зависел от порядка маршрутов в плане.
    """
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    request_by_id = {item.id: item for item in planning_input.requests}
    candidates: list[PlacementCandidate] = []
    for route in base_result.routes:
        engineer = engineer_by_id.get(route.engineer_id)
        if engineer is None:
            continue
        existing = [request_by_id[stop.request_id] for stop in route.stops]
        for position in range(len(route.stops) + 1):
            inserted = insert_request_preserving_visits(
                engineer,
                route,
                existing,
                request,
                position,
            )
            if inserted is not None:
                candidates.append(PlacementCandidate(engineer, position, route, inserted))
    candidates.sort(
        key=lambda item: (
            item.added_travel_minutes,
            item.added_distance_meters,
            item.engineer.input_order,
            item.position,
        )
    )
    return candidates


def _unassigned_request(
    planning_input: PlanningInput,
    base_result: PlanningResult,
    request_id: str,
) -> ServiceRequestInput:
    """Заявка из снимка плана, которая стоит в его отказах; иначе понятная ошибка."""
    request = next((item for item in planning_input.requests if item.id == request_id), None)
    if request is None:
        raise DomainError(
            "request_not_found",
            "Заявки нет в снимке этого плана.",
            status_code=404,
        )
    if any(stop.request_id == request_id for route in base_result.routes for stop in route.stops):
        raise DomainError(
            "request_already_assigned",
            "Заявка уже назначена в этом плане.",
            status_code=409,
        )
    if not any(item.request_id == request_id for item in base_result.unassigned):
        raise DomainError(
            "request_not_unassigned",
            "Заявки нет среди отказов этого плана.",
            status_code=409,
        )
    return request


def request_placements(
    planning_input: PlanningInput,
    base_result: PlanningResult,
    request_id: str,
) -> RequestPlacements:
    """Все допустимые места для отказанной заявки, по приросту расстояния."""
    request = _unassigned_request(planning_input, base_result, request_id)
    candidates = enumerate_request_placements(planning_input, base_result, request)
    return RequestPlacements(
        request_id=request_id,
        placements=[candidate.to_schema() for candidate in candidates],
    )


def _moved_visits(before: PlanningResult, after: PlanningResult) -> list[str]:
    """Прежние визиты, которые изменились: исполнитель, порядок, время или резерв.

    Приезд и ожидание следующего визита вставка меняет законно — к нему теперь едут
    от новой заявки; начало, конец и защитный резерв работ меняться не должны.
    """

    def visits(result: PlanningResult) -> dict[str, tuple[Any, ...]]:
        return {
            stop.request_id: (
                route.engineer_id,
                stop.service_start_at,
                stop.service_end_at,
                stop.expected_service_end_at,
                stop.reserve_minutes,
            )
            for route in result.routes
            for stop in route.stops
        }

    old = visits(before)
    new = visits(after)
    problems = [
        f"visit_moved:{request_id}"
        for request_id, state in old.items()
        if new.get(request_id) != state
    ]
    new_order = {
        route.engineer_id: [stop.request_id for stop in route.stops if stop.request_id in old]
        for route in after.routes
    }
    for route in before.routes:
        if new_order.get(route.engineer_id) != [stop.request_id for stop in route.stops]:
            problems.append(f"visit_order_changed:{route.engineer_id}")
    return problems


def assign_unassigned_request(
    planning_input: PlanningInput,
    base_result: PlanningResult,
    request_id: str,
    engineer_id: str,
    position: int,
) -> tuple[PlanningResult, PlacementCandidate]:
    """Новый план: отказанная заявка вставлена в выбранное место маршрута бригады.

    Исходный план не меняется. Метрики пересчитываются `calculate_metrics`, результат
    до возврата проходит `validate_plan` и сверку прежних визитов; любое замечание —
    409, и план не записывается.
    """
    started = perf_counter()
    request = _unassigned_request(planning_input, base_result, request_id)
    engineer = next(
        (item for item in planning_input.engineers if item.id == engineer_id),
        None,
    )
    route = next((item for item in base_result.routes if item.engineer_id == engineer_id), None)
    if engineer is None or route is None:
        raise DomainError(
            "engineer_not_found",
            "Бригады нет в этом плане.",
            status_code=404,
        )
    if position > len(route.stops):
        raise DomainError(
            "invalid_position",
            f"Позиция вне маршрута бригады: допустимо от 0 до {len(route.stops)}.",
            details={"max_position": len(route.stops)},
        )
    request_by_id = {item.id: item for item in planning_input.requests}
    existing = [request_by_id[stop.request_id] for stop in route.stops]
    inserted = insert_request_preserving_visits(engineer, route, existing, request, position)
    if inserted is None:
        raise DomainError(
            "placement_infeasible",
            "В это место заявку не вставить: сдвинутся прежние визиты или нарушатся "
            "окно, смена, навык, транспорт или оборудование.",
            status_code=409,
            details={"engineer_id": engineer_id, "position": position},
        )
    inserted = inserted.model_copy(
        update={
            "explanation": (
                f"{engineer.name}: заявка назначена диспетчером вручную в свободный "
                "интервал; исполнители, порядок и время обслуживания прежних визитов "
                "сохранены."
            )
        }
    )
    placement = PlacementCandidate(engineer, position, route, inserted)
    routes = [
        inserted if item.engineer_id == engineer_id else item.model_copy(deep=True)
        for item in base_result.routes
    ]
    unassigned = [
        item.model_copy(deep=True)
        for item in base_result.unassigned
        if item.request_id != request_id
    ]
    result = base_result.model_copy(
        update={
            "algorithm": MANUAL_INSERT_ALGORITHM,
            "termination_reason": "manual_request_inserted_without_reordering",
            "elapsed_ms": round((perf_counter() - started) * 1000),
            "routes": routes,
            "unassigned": unassigned,
            "metrics": calculate_metrics(routes, unassigned),
            "constraint_violations_count": 0,
            "replan_churn": {},
        }
    )
    problems = [*_moved_visits(base_result, result), *validate_plan(planning_input, result)]
    if problems:
        raise DomainError(
            "manual_insert_rejected",
            "План с ручным назначением не прошёл проверку и не записан.",
            status_code=409,
            details={"violations": problems},
        )
    return result, placement


def manual_insert_model_info(
    parent_model_info: dict[str, Any],
    parent_result: PlanningResult,
    planning_input: PlanningInput,
    result: PlanningResult,
    placement: PlacementCandidate,
    request_id: str,
) -> dict[str, Any]:
    """Паспорт плана с ручным назначением.

    `algorithm_id` наследуется от исходного плана: по нему перепланирование выбирает
    решатель, а ручная вставка решателем не является. Отпечаток эксперимента
    (`experiment`) считается по тому же снимку, что у исходного плана, с алгоритмом
    `manual_gap_insertion` и нулевым лимитом времени; причина выбора
    (`selection_reason`) — ручное назначение диспетчером.
    """
    parent_algorithm = str(
        parent_model_info.get("algorithm_id", parent_model_info.get("algorithm", "parent_plan"))
    )
    info: dict[str, Any] = {
        "algorithm": result.algorithm,
        "insertion_policy": MANUAL_INSERT_POLICY,
        "same_snapshot": True,
        "objective_tuple": objective_tuple(result.metrics),
        "kpis": evaluate_result(planning_input, result),
        "experiment": {
            "experiment_id": str(uuid4()),
            **experiment_metadata(planning_input, [MANUAL_INSERT_ALGORITHM], 0),
        },
        "selection_reason": manual_selection_reason(
            MANUAL_INSERT_ALGORITHM, result, parent_algorithm, parent_result
        ),
        "manual_assignment": {
            "request_id": request_id,
            "engineer_id": placement.engineer.id,
            "position": placement.position,
            "added_distance_meters": placement.added_distance_meters,
            "added_travel_minutes": placement.added_travel_minutes,
            "norm_exceeded": placement.norm_exceeded,
            "added_norm_excess_minutes": placement.added_norm_excess_minutes,
        },
    }
    if "algorithm_id" in parent_model_info:
        info["algorithm_id"] = parent_model_info["algorithm_id"]
    if parent_model_info.get("simulation"):
        info["simulation"] = True
    return info
