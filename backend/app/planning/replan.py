from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from time import perf_counter

from app.planning.baseline import build_baseline
from app.planning.metrics import calculate_metrics, route_norm_excess_minutes
from app.planning.objective import plan_objective_key
from app.planning.optimizer import build_optimized
from app.planning.policies import travel_norm_is_penalized
from app.planning.reasons import REASON_TEXT, make_unassigned
from app.planning.schedule import calculate_route, insert_request_preserving_visits
from app.planning.validation import validate_plan
from app.schemas import (
    EngineerInput,
    EngineerRoute,
    PlanningInput,
    PlanningRequestEvent,
    PlanningResult,
    Priority,
    RouteStop,
    ServiceRequestInput,
    UnassignedRequest,
    apply_emergency_sla,
)

# Сдвиг визита больше этого порога бригада и клиент замечают, меньше — нет.
CHURN_SHIFT_THRESHOLD_MINUTES = 15
# Доля лимита расчёта, которую отдаём стабилизации: она не должна съедать время решателя.
STABILIZATION_TIME_SHARE = 0.2
STABILIZATION_MINIMUM_SECONDS = 0.5

# Политика вставки внутридневной обычной заявки (паспорт плана, `insertion_policy`):
# в hard и advisory — минимальный прирост времени в пути, при равенстве пробега; в soft —
# сначала минимальный прирост минут сверх норматива дороги, затем времени в пути и пробега.
# Правило — `_build_normal_gap_insertion`.
NORMAL_INSERTION_POLICY_HARD = "min_incremental_travel_then_distance_without_reordering"
NORMAL_INSERTION_POLICY_SOFT = (
    "min_norm_excess_then_travel_then_distance_without_reordering"
)


def normal_insertion_policy() -> str:
    """Имя правила вставки обычной заявки в текущем режиме норматива дороги."""
    if travel_norm_is_penalized():
        return NORMAL_INSERTION_POLICY_SOFT
    return NORMAL_INSERTION_POLICY_HARD


def _protected_prefixes(
    base_result: PlanningResult,
    event_time: datetime,
) -> dict[str, list[RouteStop]]:
    prefixes: dict[str, list[RouteStop]] = {}
    for route in base_result.routes:
        prefix: list[RouteStop] = []
        for stop in route.stops:
            completed = stop.service_end_at <= event_time
            working = stop.service_start_at <= event_time < stop.service_end_at
            travelling_or_waiting = stop.departure_at <= event_time < stop.service_start_at
            if completed or working or travelling_or_waiting:
                prefix.append(stop)
                continue
            break
        prefixes[route.engineer_id] = prefix
    return prefixes


def _anchor_engineers(
    planning_input: PlanningInput,
    prefixes: dict[str, list[RouteStop]],
    request_by_id: dict[str, ServiceRequestInput],
    event_time: datetime,
) -> list[EngineerInput]:
    anchors: list[EngineerInput] = []
    for engineer in planning_input.engineers:
        prefix = prefixes.get(engineer.id, [])
        if prefix:
            last_stop = prefix[-1]
            start_location = request_by_id[last_stop.request_id].coordinates
            shift_start = max(event_time, last_stop.service_end_at)
        else:
            start_location = engineer.start_location
            shift_start = max(event_time, engineer.shift_start)
        if shift_start >= engineer.shift_end:
            continue
        anchors.append(
            engineer.model_copy(
                update={"start_location": start_location, "shift_start": shift_start}
            )
        )
    return anchors


@dataclass
class _Tail:
    """Незащищённый хвост дня: последовательности заявок по бригадам и их маршруты.

    `previous_engineer` — исполнитель заявки в базовом плане: к нему хвост и стараются
    вернуть. `always_used` — бригады с защищённым префиксом: они уже заняты сегодня,
    поэтому пустой хвост их из числа задействованных не убирает.
    """

    engineer_by_id: dict[str, EngineerInput]
    order: list[str]
    sequences: dict[str, list[ServiceRequestInput]]
    routes: dict[str, EngineerRoute]
    unassigned: list[UnassignedRequest]
    always_used: set[str]
    buffer_minutes: int
    previous_engineer: dict[str, str]

    def materialize(
        self,
        engineer_id: str,
        requests: list[ServiceRequestInput],
    ) -> EngineerRoute | None:
        return calculate_route(self.engineer_by_id[engineer_id], requests, self.buffer_minutes)

    def objective_key(self, replaced: dict[str, EngineerRoute]) -> tuple[int, ...]:
        routes = [replaced.get(engineer_id, self.routes[engineer_id]) for engineer_id in self.order]
        metrics = calculate_metrics(routes, self.unassigned)
        used = {route.engineer_id for route in routes if route.stops} | self.always_used
        return plan_objective_key(metrics.model_copy(update={"used_engineers_count": len(used)}))

    def kept_with_previous(self, engineer_id: str, requests: list[ServiceRequestInput]) -> int:
        return sum(self.previous_engineer.get(request.id) == engineer_id for request in requests)


def _build_tail(
    suffix_input: PlanningInput,
    suffix_result: PlanningResult,
    previous_engineer: dict[str, str],
    always_used: set[str],
) -> _Tail:
    request_by_id = {request.id: request for request in suffix_input.requests}
    return _Tail(
        engineer_by_id={engineer.id: engineer for engineer in suffix_input.engineers},
        order=[route.engineer_id for route in suffix_result.routes],
        sequences={
            route.engineer_id: [request_by_id[stop.request_id] for stop in route.stops]
            for route in suffix_result.routes
        },
        routes={route.engineer_id: route for route in suffix_result.routes},
        unassigned=list(suffix_result.unassigned),
        always_used=always_used,
        buffer_minutes=suffix_input.buffer_minutes,
        previous_engineer=previous_engineer,
    )


def _swap_whole_tails(tail: _Tail, deadline: float) -> None:
    """Меняет хвосты двух бригад целиком.

    Снимает перетряску, которая возникает от одной перенумерации: взаимозаменяемым
    бригадам решатель раздаёт те же маршруты, но под другими именами. Обмен целиком
    геометрию маршрутов не меняет, поэтому проходит по цели чаще одиночного переноса.
    """
    current_key = tail.objective_key({})
    improved = True
    while improved:
        improved = False
        for left, right in combinations(tail.order, 2):
            if perf_counter() >= deadline:
                return
            left_sequence = tail.sequences[left]
            right_sequence = tail.sequences[right]
            if not left_sequence and not right_sequence:
                continue
            returned = (
                tail.kept_with_previous(left, right_sequence)
                + tail.kept_with_previous(right, left_sequence)
                - tail.kept_with_previous(left, left_sequence)
                - tail.kept_with_previous(right, right_sequence)
            )
            if returned <= 0:
                continue
            left_route = tail.materialize(left, right_sequence)
            right_route = tail.materialize(right, left_sequence)
            if left_route is None or right_route is None:
                continue
            candidate_key = tail.objective_key({left: left_route, right: right_route})
            if candidate_key > current_key:
                continue
            tail.sequences[left], tail.sequences[right] = right_sequence, left_sequence
            tail.routes[left], tail.routes[right] = left_route, right_route
            current_key = candidate_key
            improved = True


def _return_to_previous_crew(tail: _Tail, deadline: float) -> None:
    """Возвращает заявки прежним бригадам по одной, пока цель от этого не проигрывает.

    Позиция в маршруте прежней бригады перебирается целиком, берётся лучшая по цели;
    при равенстве — самая ранняя, чтобы результат не зависел от порядка перебора.
    """
    current_key = tail.objective_key({})
    holder = {
        request.id: engineer_id
        for engineer_id, sequence in tail.sequences.items()
        for request in sequence
    }
    candidates = sorted(
        (request for sequence in tail.sequences.values() for request in sequence),
        key=lambda request: (request.input_order, request.id),
    )
    for request in candidates:
        if perf_counter() >= deadline:
            return
        source = holder[request.id]
        target = tail.previous_engineer.get(request.id)
        if target is None or target == source or target not in tail.engineer_by_id:
            continue
        source_sequence = [item for item in tail.sequences[source] if item.id != request.id]
        source_route = tail.materialize(source, source_sequence)
        if source_route is None:
            continue
        target_sequence = tail.sequences[target]
        best_key: tuple[int, ...] | None = None
        best_route: EngineerRoute | None = None
        best_position = 0
        for position in range(len(target_sequence) + 1):
            candidate_sequence = [
                *target_sequence[:position],
                request,
                *target_sequence[position:],
            ]
            target_route = tail.materialize(target, candidate_sequence)
            if target_route is None:
                continue
            candidate_key = tail.objective_key({source: source_route, target: target_route})
            if candidate_key > current_key:
                continue
            if best_key is None or candidate_key < best_key:
                best_key, best_route, best_position = candidate_key, target_route, position
        if best_key is None or best_route is None:
            continue
        tail.sequences[source] = source_sequence
        tail.sequences[target] = [
            *target_sequence[:best_position],
            request,
            *target_sequence[best_position:],
        ]
        tail.routes[source] = source_route
        tail.routes[target] = best_route
        holder[request.id] = target
        current_key = best_key


def _stabilize_tail(
    suffix_input: PlanningInput,
    suffix_result: PlanningResult,
    base_result: PlanningResult,
    prefixes: dict[str, list[RouteStop]],
    time_limit_seconds: int,
) -> PlanningResult:
    """Убирает из пересчитанного хвоста перетряску, за которую цель не платит.

    Защищённый префикс сюда не попадает: в хвосте его заявок нет, а маршруты
    пересобираются тем же `calculate_route`, что и у решателей.
    """
    if not suffix_result.routes:
        return suffix_result
    protected_ids = {stop.request_id for prefix in prefixes.values() for stop in prefix}
    previous_engineer = {
        stop.request_id: route.engineer_id
        for route in base_result.routes
        for stop in route.stops
        if stop.request_id not in protected_ids
    }
    tail = _build_tail(
        suffix_input,
        suffix_result,
        previous_engineer,
        {engineer_id for engineer_id, prefix in prefixes.items() if prefix},
    )
    deadline = perf_counter() + max(
        STABILIZATION_MINIMUM_SECONDS,
        time_limit_seconds * STABILIZATION_TIME_SHARE,
    )
    _swap_whole_tails(tail, deadline)
    _return_to_previous_crew(tail, deadline)
    routes = [tail.routes[engineer_id] for engineer_id in tail.order]
    return suffix_result.model_copy(
        update={
            "routes": routes,
            "metrics": calculate_metrics(routes, suffix_result.unassigned),
        }
    )


def _churn_summary(
    base_result: PlanningResult,
    combined_routes: list[EngineerRoute],
) -> dict[str, int]:
    """Перетряска относительно базового плана: смены исполнителя и сдвиги времени."""
    base_engineer: dict[str, str] = {}
    base_service_start: dict[str, datetime] = {}
    for route in base_result.routes:
        for stop in route.stops:
            base_engineer[stop.request_id] = route.engineer_id
            base_service_start[stop.request_id] = stop.service_start_at
    compared = 0
    reassigned = 0
    time_shifted = 0
    for route in combined_routes:
        for stop in route.stops:
            previous = base_engineer.get(stop.request_id)
            if previous is None:
                continue
            compared += 1
            if previous != route.engineer_id:
                reassigned += 1
            drift = stop.service_start_at - base_service_start[stop.request_id]
            if abs(drift.total_seconds()) > CHURN_SHIFT_THRESHOLD_MINUTES * 60:
                time_shifted += 1
    return {
        "compared_count": compared,
        "reassigned_count": reassigned,
        "time_shifted_count": time_shifted,
        "shift_threshold_minutes": CHURN_SHIFT_THRESHOLD_MINUTES,
    }


def _combine_result(
    planning_input: PlanningInput,
    suffix_result: PlanningResult,
    base_result: PlanningResult,
    event: PlanningRequestEvent,
    prefixes: dict[str, list[RouteStop]],
) -> PlanningResult:
    suffix_by_engineer = {route.engineer_id: route for route in suffix_result.routes}
    base_by_engineer = {route.engineer_id: route for route in base_result.routes}
    combined_routes: list[EngineerRoute] = []
    for engineer in planning_input.engineers:
        prefix = prefixes.get(engineer.id, [])
        suffix = suffix_by_engineer.get(engineer.id)
        combined_stops = [stop.model_copy() for stop in prefix]
        if suffix:
            combined_stops.extend(
                stop.model_copy(update={"sequence": len(prefix) + stop.sequence})
                for stop in suffix.stops
            )
        base_route = base_by_engineer.get(engineer.id)
        prefix_distance = sum(stop.distance_meters_from_previous for stop in prefix)
        prefix_travel = sum(stop.travel_minutes_from_previous for stop in prefix)
        prefix_wait = sum(stop.wait_minutes for stop in prefix)
        request_by_id = {request.id: request for request in planning_input.requests}
        prefix_service = sum(request_by_id[stop.request_id].duration_minutes for stop in prefix)
        finish_at = (
            suffix.finish_at
            if suffix and suffix.stops
            else prefix[-1].service_end_at
            if prefix
            else max(event.event_time, engineer.shift_start)
        )
        idle_departure = min(max(event.event_time, engineer.shift_start), engineer.shift_end)
        combined_routes.append(
            EngineerRoute(
                engineer_id=engineer.id,
                engineer_name=engineer.name,
                transport=engineer.transport,
                start_location=engineer.start_location,
                departure_at=(base_route.departure_at if prefix and base_route else idle_departure),
                stops=combined_stops,
                distance_meters=prefix_distance + (suffix.distance_meters if suffix else 0),
                travel_minutes=prefix_travel + (suffix.travel_minutes if suffix else 0),
                service_minutes=prefix_service + (suffix.service_minutes if suffix else 0),
                wait_minutes=prefix_wait + (suffix.wait_minutes if suffix else 0),
                finish_at=(idle_departure if not combined_stops else finish_at),
                explanation=(
                    f"{engineer.name}: защищено визитов — {len(prefix)}, "
                    f"пересчитано — {len(suffix.stops) if suffix else 0}. "
                    "Начатые действия не изменены."
                ),
            )
        )
    old_assigned = {stop.request_id for route in base_result.routes for stop in route.stops}
    unassigned: list[UnassignedRequest] = []
    for item in suffix_result.unassigned:
        if item.request_id in old_assigned and item.request_id != event.request.id:
            unassigned.append(
                item.model_copy(
                    update={
                        "reason_code": "preempted_by_urgent",
                        "explanation": REASON_TEXT["preempted_by_urgent"],
                    }
                )
            )
        else:
            unassigned.append(item)
    metrics = calculate_metrics(combined_routes, unassigned)
    result = suffix_result.model_copy(
        update={
            "algorithm": f"replan_{suffix_result.algorithm}",
            "routes": combined_routes,
            "unassigned": unassigned,
            "metrics": metrics,
            "constraint_violations_count": 0,
            "replan_churn": _churn_summary(base_result, combined_routes),
        }
    )
    validation_input = planning_input.model_copy(
        update={"requests": [*planning_input.requests, event.request]}
    )
    violations = validate_plan(validation_input, result)
    if violations:
        raise ValueError(f"replan validator rejected result: {violations}")
    return result


def build_replan_pair(
    planning_input: PlanningInput,
    base_result: PlanningResult,
    event: PlanningRequestEvent,
    time_limit_seconds: int,
    algorithm_id: str = "ortools_gls_v1",
) -> tuple[PlanningResult, PlanningResult, list[str]]:
    event = apply_emergency_sla(event)
    if event.request.priority is Priority.NORMAL:
        inserted = _build_normal_gap_insertion(planning_input, base_result, event)
        protected_ids_list = sorted(
            stop.request_id for route in base_result.routes for stop in route.stops
        )
        return inserted, inserted.model_copy(deep=True), protected_ids_list

    request_by_id = {request.id: request for request in planning_input.requests}
    prefixes = _protected_prefixes(base_result, event.event_time)
    protected_ids = {stop.request_id for prefix in prefixes.values() for stop in prefix}
    remaining = [request for request in planning_input.requests if request.id not in protected_ids]
    remaining.append(event.request)
    anchor_engineers = _anchor_engineers(
        planning_input,
        prefixes,
        request_by_id,
        event.event_time,
    )
    suffix_input = planning_input.model_copy(
        update={
            "requests": remaining,
            "engineers": anchor_engineers,
            "revision": planning_input.revision + 1,
        }
    )
    baseline_suffix = build_baseline(suffix_input)
    if algorithm_id == "baseline_v1":
        optimized_suffix = baseline_suffix
    elif algorithm_id == "insertion_ls_v1":
        from app.planning.insertion import build_insertion

        optimized_suffix = build_insertion(
            suffix_input, time_limit_seconds=time_limit_seconds
        )
    elif algorithm_id == "ortools_gls_v1":
        optimized_suffix = build_optimized(
            suffix_input, time_limit_seconds=time_limit_seconds
        )
    else:
        raise ValueError(f"unknown replan algorithm: {algorithm_id}")
    if algorithm_id != "baseline_v1":
        # baseline остаётся честной точкой отсчёта — стабилизируем только рабочий хвост
        optimized_suffix = _stabilize_tail(
            suffix_input,
            optimized_suffix,
            base_result,
            prefixes,
            time_limit_seconds,
        )
    baseline = _combine_result(planning_input, baseline_suffix, base_result, event, prefixes)
    optimized = _combine_result(planning_input, optimized_suffix, base_result, event, prefixes)
    return baseline, optimized, sorted(protected_ids)


def _build_normal_gap_insertion(
    planning_input: PlanningInput,
    base_result: PlanningResult,
    event: PlanningRequestEvent,
) -> PlanningResult:
    """Add a non-emergency arrival only into a free gap of the published plan.

    Правило выбора места (`normal_insertion_policy`):
    - hard: минимальный прирост времени в пути, при равенстве — расчётного
      расстояния; плечо дольше норматива недопустимо, поэтому превышений нет;
    - advisory: минимальный прирост времени в пути, при равенстве — расчётного
      расстояния; превышение норматива только показывается и на выбор места не влияет;
    - soft: сначала место без превышения — минимальный прирост минут дороги сверх
      норматива по маршруту бригады (новое плечо и плечо до следующего визита; место,
      где превышения нет, даёт прирост 0 и всегда выигрывает у места с превышением),
      при равенстве — минимальный прирост времени в пути, затем расстояния. Так же, как
      в ключе сравнения планов, минута превышения дороже минут в пути и километров.
      Плечо длиннее потолка
      «норматив × множитель» недопустимо.
    Дальше при равенстве — порядок бригады во входе и более ранняя позиция.
    """
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    request_by_id = {request.id: request for request in planning_input.requests}
    count_excess = travel_norm_is_penalized()
    best: tuple[int, int, int, int, int, EngineerRoute] | None = None
    for route in base_result.routes:
        engineer = engineer_by_id.get(route.engineer_id)
        if engineer is None:
            continue
        existing = [request_by_id[stop.request_id] for stop in route.stops]
        for position in range(len(route.stops) + 1):
            candidate = insert_request_preserving_visits(
                engineer,
                route,
                existing,
                event.request,
                position,
            )
            if candidate is None:
                continue
            ranked = (
                (
                    route_norm_excess_minutes(candidate) - route_norm_excess_minutes(route)
                    if count_excess
                    else 0
                ),
                candidate.travel_minutes - route.travel_minutes,
                candidate.distance_meters - route.distance_meters,
                engineer.input_order,
                position,
                candidate,
            )
            if best is None or ranked[:5] < best[:5]:
                best = ranked

    routes = [route.model_copy(deep=True) for route in base_result.routes]
    unassigned = [item.model_copy(deep=True) for item in base_result.unassigned]
    if best is None:
        unassigned.append(
            make_unassigned(
                event.request,
                planning_input.engineers,
                fallback="schedule_conflict",
            )
        )
    else:
        selected_route = best[5]
        routes = [
            selected_route if route.engineer_id == selected_route.engineer_id else route
            for route in routes
        ]

    metrics = calculate_metrics(routes, unassigned)
    result = base_result.model_copy(
        update={
            "algorithm": "replan_normal_gap_insertion",
            "termination_reason": (
                "normal_request_inserted_without_reordering"
                if best is not None
                else "no_preserving_gap_found"
            ),
            "routes": routes,
            "unassigned": unassigned,
            "metrics": metrics,
            "constraint_violations_count": 0,
        }
    )
    validation_input = planning_input.model_copy(
        update={"requests": [*planning_input.requests, event.request]}
    )
    violations = validate_plan(validation_input, result)
    if violations:
        raise ValueError(f"normal event validator rejected result: {violations}")
    return result
