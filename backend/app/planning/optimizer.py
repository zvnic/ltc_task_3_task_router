from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from zoneinfo import ZoneInfo

from ortools.constraint_solver import pywrapcp, routing_enums_pb2  # type: ignore[import-untyped]

from app.planning.baseline import build_baseline
from app.planning.metrics import calculate_metrics
from app.planning.objective import (
    COST,
    COST_SCALE,
    TRAVEL_MINUTE_ARC_WEIGHT,
    calculate_cost_weights,
    cost_weights,
    objective_mode,
    plan_objective_key,
)
from app.planning.policies import (
    effective_service_start_deadline,
    engineer_has_equipment_for_requests,
    request_priority_class,
    request_travel_norm_minutes,
    travel_norm_policy,
)
from app.planning.reasons import make_unassigned
from app.planning.routing import (
    edge_is_feasible,
    effective_edge_metrics,
    travel_norm_excess_minutes,
)
from app.planning.schedule import calculate_route
from app.planning.validation import validate_plan
from app.schemas import PlanningInput, PlanningResult, ServiceRequestInput, Transport


def _minute_of_day(value: datetime, timezone_name: str) -> int:
    local_value = value.astimezone(ZoneInfo(timezone_name))
    return local_value.hour * 60 + local_value.minute


def lexicographic_edge_cost(travel_minutes: int, distance_meters: int) -> int:
    """Цена дуги по времени и пробегу: минута весит TRAVEL_MINUTE_ARC_WEIGHT, км — 1."""
    return travel_minutes * TRAVEL_MINUTE_ARC_WEIGHT + distance_meters // 1000


def distance_edge_cost(travel_minutes: int, distance_meters: int) -> int:
    """Цена дуги по пробегу, в метрах: второй проход поиска."""
    return distance_meters


# Цена дуги — подсказка локальному поиску, а не цель: план сравнивается по
# `plan_objective_key`. Замер 29.09 на трёх зонах: с ценой по минутам поиск находит
# план без отказа на Востоке и на 79 минут в пути короче на Югоцентре, с ценой по
# метрам — 8 отказов вместо 12 на Юго-востоке; ни одна цена не лучше везде. Поиск
# сходится за 5 с, поэтому лимит делится между двумя проходами, и в результат идёт
# лучший по ключу плана.
EDGE_COSTS: tuple[Callable[[int, int], int], ...] = (
    lexicographic_edge_cost,
    distance_edge_cost,
)


def calculate_objective_weights(
    request_count: int,
    vehicle_count: int,
    maximum_edge_cost: int,
    *,
    norm_excess_bound_minutes: int = 0,
) -> dict[str, int]:
    """Веса лексикографической цели: каждая ступень дороже всей суммы младших.

    `maximum_edge_cost` — самая дорогая допустимая дуга по цене дуги прохода
    (`lexicographic_edge_cost` или `distance_edge_cost`).

    `norm_excess_bound_minutes` — сколько минут превышения норматива дороги может
    набрать план в худшем случае (режим soft: сумма по заявкам «потолок − норматив»).
    Больше нуля — между бригадами и пропусками заявок встаёт ступень минут превышения,
    тот же порядок, что у `plan_objective_key`. При 0 (режим hard или множитель 1,0)
    веса остаются ровно прежними.
    """
    travel_cost_bound = request_count * maximum_edge_cost
    engineer_weight = travel_cost_bound + 1
    route_bound = vehicle_count * engineer_weight + travel_cost_bound
    # Минута превышения дороже всех бригад и всей дороги вместе; превышение
    # бывает только на дуге, ведущей в заявку, и не больше её «потолок − норматив».
    norm_excess_weight = route_bound + 1 if norm_excess_bound_minutes else 0
    norm_bound = norm_excess_bound_minutes * norm_excess_weight + route_bound
    routine_drop_weight = norm_bound + 1
    routine_bound = request_count * routine_drop_weight + norm_bound
    connection_drop_weight = routine_bound + 1
    connection_bound = request_count * connection_drop_weight + routine_bound
    emergency_drop_weight = connection_bound + 1
    objective_upper_bound = request_count * emergency_drop_weight + connection_bound
    if objective_upper_bound >= 2**63:
        raise ValueError("planning objective exceeds int64")
    weights = {
        "maximum_edge_cost": maximum_edge_cost,
        "travel_cost_bound": travel_cost_bound,
        "used_engineer": engineer_weight,
        "routine_unassigned": routine_drop_weight,
        "connection_unassigned": connection_drop_weight,
        "emergency_unassigned": emergency_drop_weight,
        "objective_upper_bound": objective_upper_bound,
    }
    if norm_excess_bound_minutes:
        weights["norm_excess_bound_minutes"] = norm_excess_bound_minutes
        weights["norm_excess_minute"] = norm_excess_weight
    return weights


@dataclass
class _TransportEdges:
    """Плечи одного вида транспорта: [узел-откуда][заявка-куда] и колбэки решателя."""

    distance: list[list[int]] = field(default_factory=list)
    travel: list[list[int]] = field(default_factory=list)
    feasible: list[list[bool]] = field(default_factory=list)
    time_callback: int = -1
    arc_cost_callback: int = -1


def build_optimized(planning_input: PlanningInput, time_limit_seconds: int = 10) -> PlanningResult:
    """OR-Tools GLS; в лексикографическом режиме — два прохода с разной ценой дуги."""
    started_at = perf_counter()
    time_limit_milliseconds = max(1, min(time_limit_seconds, 20)) * 1000
    if objective_mode() == COST:
        # у стоимостного режима своя цена дуги: метры и минуты в деньгах
        return _solve(planning_input, time_limit_milliseconds, lexicographic_edge_cost)
    results = [
        _solve(planning_input, time_limit_milliseconds // len(EDGE_COSTS), edge_cost)
        for edge_cost in EDGE_COSTS
    ]
    # при равенстве ключа остаётся первый проход — по времени в пути
    best_pass = min(
        range(len(results)), key=lambda index: plan_objective_key(results[index].metrics)
    )
    best = results[best_pass]
    if best.algorithm == "ortools":
        best.objective_weights["arc_cost_by_travel_minutes"] = int(
            EDGE_COSTS[best_pass] is lexicographic_edge_cost
        )
    best.elapsed_ms = round((perf_counter() - started_at) * 1000)
    return best


def _solve(
    planning_input: PlanningInput,
    time_limit_milliseconds: int,
    edge_cost: Callable[[int, int], int],
) -> PlanningResult:
    started_at = perf_counter()
    baseline = build_baseline(planning_input)
    engineers = sorted(planning_input.engineers, key=lambda item: item.input_order)
    requests = sorted(planning_input.requests, key=lambda item: item.input_order)
    buffer_minutes = planning_input.buffer_minutes
    mode = objective_mode()
    # режим норматива дороги и потолок читаются один раз на запуск решателя
    norm_policy = travel_norm_policy()
    meter_weight = 0
    travel_minute_weight = 0
    if mode == COST:
        prices = cost_weights()
        meter_weight = prices["cost_per_km"]
        travel_minute_weight = prices["cost_per_travel_minute"] * COST_SCALE
    if not requests or not engineers:
        baseline.algorithm = "baseline_fallback"
        baseline.termination_reason = "empty_solver_input"
        return baseline

    eligible_requests: list[ServiceRequestInput] = []
    candidates_by_request: dict[str, list[int]] = {}
    prefiltered_unassigned = []
    for request in requests:
        candidates = [
            index
            for index, engineer in enumerate(engineers)
            if request.required_skill in engineer.skills
            and (
                request.required_transport is None
                or request.required_transport == engineer.transport
            )
            and engineer_has_equipment_for_requests(engineer, [request])
            and calculate_route(engineer, [request]) is not None
        ]
        if candidates:
            eligible_requests.append(request)
            candidates_by_request[request.id] = candidates
        else:
            prefiltered_unassigned.append(
                make_unassigned(request, engineers, fallback="not_selected_within_limit")
            )
    if not eligible_requests:
        baseline.algorithm = "baseline_fallback"
        baseline.termination_reason = "no_eligible_requests"
        return baseline

    request_count = len(eligible_requests)
    vehicle_count = len(engineers)
    starts = [request_count + vehicle for vehicle in range(vehicle_count)]
    ends = [request_count + vehicle_count + vehicle for vehicle in range(vehicle_count)]
    manager = pywrapcp.RoutingIndexManager(
        request_count + 2 * vehicle_count,
        vehicle_count,
        starts,
        ends,
    )
    node_count = request_count + 2 * vehicle_count
    model_parameters = pywrapcp.DefaultRoutingModelParameters()
    # Колбэки дуг кешируются в матрицу: решатель не зовёт Python на каждой пробе хода
    # и за тот же лимит времени перебирает больше ходов.
    model_parameters.max_callback_cache_size = node_count
    # Кеш заполняется при регистрации колбэка по всем парам узлов, включая дуги из
    # финишей, которых в маршрутах не бывает: колбэки обязаны их переносить.
    routing = pywrapcp.RoutingModel(manager, model_parameters)

    # Дуга в заявку длиннее предела (hard — норматив, soft — потолок «норматив ×
    # множитель») недопустима. В режиме soft дуга в пределах потолка стоит тем дороже,
    # чем больше минут сверх норматива. Если превысить норматив нигде нельзя (режим
    # hard, множитель 1,0 или нет нормативов), модель совпадает с режимом hard.
    norm_by_node = [request_travel_norm_minutes(request) for request in eligible_requests]
    limit_by_node = [norm_policy.limit(norm) for norm in norm_by_node]
    max_excess_by_node = [norm_policy.max_excess(norm) for norm in norm_by_node]
    norm_excess_bound_minutes = sum(max_excess_by_node)
    use_norm_penalty = norm_excess_bound_minutes > 0

    # Плечи считаются один раз на вид транспорта: узлы 0..R-1 — заявки, R..R+V-1 —
    # старты бригад; дуги ведут только в заявки (в старт не входят, в финиш — даром).
    points = [
        *(request.coordinates for request in eligible_requests),
        *(engineer.start_location for engineer in engineers),
    ]
    durations = [request.duration_minutes for request in eligible_requests]
    edges: dict[Transport, _TransportEdges] = {}
    for transport in sorted({engineer.transport for engineer in engineers}):
        edge = _TransportEdges()
        for origin in points:
            distance_row: list[int] = []
            travel_row: list[int] = []
            feasible_row: list[bool] = []
            for to_node in range(request_count):
                distance, travel, _ = effective_edge_metrics(
                    origin, eligible_requests[to_node].coordinates, transport
                )
                distance_row.append(distance)
                travel_row.append(travel)
                feasible_row.append(
                    edge_is_feasible(
                        transport,
                        distance,
                        travel_minutes=travel,
                        max_travel_minutes=limit_by_node[to_node],
                    )
                )
            edge.distance.append(distance_row)
            edge.travel.append(travel_row)
            edge.feasible.append(feasible_row)
        edges[transport] = edge

    maximum_distance = 0
    maximum_travel_minutes = 0
    maximum_edge_cost = 0
    for vehicle, engineer in enumerate(engineers):
        edge = edges[engineer.transport]
        # Верхние границы для весов цели — по дугам, доступным бригаде: из её старта и
        # между заявками. В режиме soft с потолком дуги длиннее потолка недостижимы и в
        # оценку не входят: это держит ступень минут превышения в пределах int64.
        for from_node in (*range(request_count), request_count + vehicle):
            for to_node in range(request_count):
                distance_meters = edge.distance[from_node][to_node]
                travel_minutes = edge.travel[from_node][to_node]
                feasible = (
                    edge.feasible[from_node][to_node]
                    if use_norm_penalty
                    else edge_is_feasible(engineer.transport, distance_meters)
                )
                if feasible:
                    maximum_distance = max(maximum_distance, distance_meters)
                    maximum_travel_minutes = max(maximum_travel_minutes, travel_minutes)
                    maximum_edge_cost = max(
                        maximum_edge_cost,
                        edge_cost(travel_minutes, distance_meters),
                    )

    request_upper_bound = len(requests)
    if mode == COST:
        objective_weights = calculate_cost_weights(
            request_upper_bound,
            vehicle_count,
            maximum_distance,
            maximum_travel_minutes,
            maximum_norm_excess_minutes=max(max_excess_by_node, default=0),
        )
    else:
        objective_weights = calculate_objective_weights(
            request_upper_bound,
            vehicle_count,
            maximum_edge_cost,
            norm_excess_bound_minutes=norm_excess_bound_minutes,
        )
    baseline.objective_weights = objective_weights
    # Колбэки регистрируются после весов: кеш решателя заполняется при регистрации,
    # и цена минуты превышения должна быть уже известна.
    norm_excess_weight = objective_weights["norm_excess_minute"] if use_norm_penalty else 0

    def norm_penalty(to_node: int, travel_minutes: int) -> int:
        """Штраф дуги, ведущей в заявку, за минуты сверх её норматива дороги."""
        excess = travel_norm_excess_minutes(travel_minutes, norm_by_node[to_node])
        return excess * norm_excess_weight

    for edge in edges.values():

        def time_callback(from_index: int, to_index: int, *, edge: _TransportEdges = edge) -> int:
            from_node: int = manager.IndexToNode(from_index)
            to_node: int = manager.IndexToNode(to_index)
            service_minutes = durations[from_node] if from_node < request_count else 0
            if to_node >= request_count or from_node >= request_count + vehicle_count:
                return service_minutes
            if not edge.feasible[from_node][to_node]:
                # Larger than the complete Time dimension capacity, making
                # this transport-specific arc impossible for OR-Tools.
                return 2_881
            # буфер резервируется после визита, перед выездом на следующий
            buffer = buffer_minutes if from_node < request_count else 0
            return service_minutes + edge.travel[from_node][to_node] + buffer

        def arc_cost_callback(
            from_index: int, to_index: int, *, edge: _TransportEdges = edge
        ) -> int:
            from_node: int = manager.IndexToNode(from_index)
            to_node: int = manager.IndexToNode(to_index)
            if to_node >= request_count or from_node >= request_count + vehicle_count:
                return 0
            edge_distance = edge.distance[from_node][to_node]
            edge_travel = edge.travel[from_node][to_node]
            if mode == COST:
                # стоимостной режим: дуга стоит и метры, и минуты за рулём
                arc_cost = edge_distance * meter_weight + edge_travel * travel_minute_weight
            else:
                # лексикографический режим: цена дуги этого прохода
                arc_cost = edge_cost(edge_travel, edge_distance)
            if use_norm_penalty:
                arc_cost += norm_penalty(to_node, edge_travel)
            return arc_cost

        edge.time_callback = routing.RegisterTransitCallback(time_callback)
        edge.arc_cost_callback = routing.RegisterTransitCallback(arc_cost_callback)

    time_callbacks: list[int] = []
    for vehicle, engineer in enumerate(engineers):
        edge = edges[engineer.transport]
        time_callbacks.append(edge.time_callback)
        routing.SetArcCostEvaluatorOfVehicle(edge.arc_cost_callback, vehicle)

    engineer_weight = objective_weights["used_engineer"]
    routine_drop_weight = objective_weights["routine_unassigned"]
    connection_drop_weight = objective_weights["connection_unassigned"]
    emergency_drop_weight = objective_weights["emergency_unassigned"]

    for vehicle in range(vehicle_count):
        routing.SetFixedCostOfVehicle(engineer_weight, vehicle)

    for request_index, request in enumerate(eligible_requests):
        routing_index = manager.NodeToIndex(request_index)
        allowed = set(candidates_by_request[request.id])
        for vehicle in range(vehicle_count):
            if vehicle not in allowed:
                routing.VehicleVar(routing_index).RemoveValue(vehicle)
        penalty = {
            "emergency": emergency_drop_weight,
            "connection": connection_drop_weight,
            "routine": routine_drop_weight,
        }[request_priority_class(request).value]
        routing.AddDisjunction([routing_index], penalty)

    equipment_codes = sorted(
        {code for request in eligible_requests for code in request.required_equipment}
    )
    for equipment_index, code in enumerate(equipment_codes):

        def equipment_demand(from_index: int, *, equipment_code: str = code) -> int:
            node = manager.IndexToNode(from_index)
            if node >= request_count:
                return 0
            return int(eligible_requests[node].required_equipment.get(equipment_code, 0))

        demand_index = routing.RegisterUnaryTransitCallback(equipment_demand)
        routing.AddDimensionWithVehicleCapacity(
            demand_index,
            0,
            [engineer.equipment_inventory.get(code, 0) for engineer in engineers],
            True,
            f"Equipment_{equipment_index}",
        )

    routing.AddDimensionWithVehicleTransits(
        time_callbacks,
        1440,
        2880,
        False,
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")
    for request_index, request in enumerate(eligible_requests):
        time_dimension.CumulVar(manager.NodeToIndex(request_index)).SetRange(
            _minute_of_day(request.window_start, planning_input.timezone),
            _minute_of_day(effective_service_start_deadline(request), planning_input.timezone),
        )
    for vehicle, engineer in enumerate(engineers):
        time_dimension.CumulVar(routing.Start(vehicle)).SetRange(
            _minute_of_day(engineer.shift_start, planning_input.timezone),
            _minute_of_day(engineer.shift_end, planning_input.timezone),
        )
        time_dimension.CumulVar(routing.End(vehicle)).SetRange(
            _minute_of_day(engineer.shift_start, planning_input.timezone),
            _minute_of_day(engineer.shift_end, planning_input.timezone),
        )

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    search.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search.time_limit.FromMilliseconds(time_limit_milliseconds)
    solution = routing.SolveWithParameters(search)
    if solution is None:
        baseline.algorithm = "baseline_fallback"
        baseline.termination_reason = "solver_no_solution"
        baseline.elapsed_ms = round((perf_counter() - started_at) * 1000)
        return baseline

    request_order_by_engineer: dict[str, list[ServiceRequestInput]] = {
        engineer.id: [] for engineer in engineers
    }
    selected_ids: set[str] = set()
    for vehicle, engineer in enumerate(engineers):
        index = routing.Start(vehicle)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node < request_count:
                request = eligible_requests[node]
                request_order_by_engineer[engineer.id].append(request)
                selected_ids.add(request.id)
            index = solution.Value(routing.NextVar(index))

    routes = []
    for engineer in engineers:
        route = calculate_route(
            engineer,
            request_order_by_engineer[engineer.id],
            buffer_minutes,
        )
        if route is None:
            baseline.algorithm = "baseline_fallback"
            baseline.termination_reason = "solver_schedule_rejected"
            baseline.elapsed_ms = round((perf_counter() - started_at) * 1000)
            return baseline
        routes.append(route)
    dropped = [request for request in eligible_requests if request.id not in selected_ids]
    unassigned = [
        *prefiltered_unassigned,
        *[
            make_unassigned(request, engineers, fallback="not_selected_within_limit")
            for request in dropped
        ],
    ]
    metrics = calculate_metrics(routes, unassigned)
    candidate = PlanningResult(
        algorithm="ortools",
        solution_status="feasible",
        termination_reason="time_limit_or_local_optimum",
        elapsed_ms=round((perf_counter() - started_at) * 1000),
        route_estimation_method=planning_input.route_estimation_method,
        coordinate_quality=baseline.coordinate_quality,
        objective_weights=objective_weights,
        routes=routes,
        unassigned=unassigned,
        metrics=metrics,
        constraint_violations_count=0,
    )
    violations = validate_plan(planning_input, candidate)
    candidate_key = plan_objective_key(candidate.metrics)
    if violations or candidate_key >= plan_objective_key(baseline.metrics):
        baseline.algorithm = "baseline_fallback"
        baseline.termination_reason = (
            "validator_rejected_solver" if violations else "solver_did_not_improve_tuple"
        )
        baseline.elapsed_ms = round((perf_counter() - started_at) * 1000)
        return baseline
    return candidate
