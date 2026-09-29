import hashlib
import json
from math import sqrt
from typing import Any

from app.planning.metrics import objective_tuple
from app.planning.objective import (
    COST,
    PLAN_KEY_LABELS,
    lexicographic_plan_key,
    objective_mode,
    objective_passport,
    plan_cost,
    plan_objective_key,
)
from app.planning.policies import travel_norm_max_factor, travel_norm_mode
from app.planning.road_matrix import road_matrix
from app.planning.routing import (
    EARTH_RADIUS_METERS,
    MAX_BICYCLE_LEG_METERS,
    MAX_CAR_LOCAL_WALK_METERS,
    MAX_WALKING_LEG_METERS,
    ROUTE_ESTIMATION_METHOD,
    SUBURBAN_BOARDING_MIN_KM,
    SUBURBAN_BOARDING_MINUTES,
    SUBURBAN_SPEED_KMH,
    TRANSPORT_ACCESS_MINUTES,
    TRANSPORT_MIN_TRAVEL_MINUTES,
    TRANSPORT_PARAMETERS,
)
from app.schemas import PlanningInput, PlanningResult


def _canonical_hash(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def experiment_metadata(
    planning_input: PlanningInput,
    algorithm_ids: list[str],
    time_limit_seconds: int,
) -> dict[str, Any]:
    snapshot = planning_input.model_dump(mode="json")
    matrix_inputs = {
        "method": planning_input.route_estimation_method,
        # справочник расстояний по дорогам меняет плечи v6 так же, как параметры модели
        "road_matrix_sha256": (
            road_matrix().sha256
            if planning_input.route_estimation_method == ROUTE_ESTIMATION_METHOD
            else None
        ),
        "earth_radius_meters": EARTH_RADIUS_METERS,
        "max_car_local_walk_meters": MAX_CAR_LOCAL_WALK_METERS,
        "max_walking_leg_meters": MAX_WALKING_LEG_METERS,
        "max_bicycle_leg_meters": MAX_BICYCLE_LEG_METERS,
        "transport_parameters": {
            transport.value: {
                "speed_kmh": values[0],
                "path_factor": values[1],
                "access_minutes": TRANSPORT_ACCESS_MINUTES[transport],
                "min_travel_minutes": TRANSPORT_MIN_TRAVEL_MINUTES[transport],
                "suburban_speed_kmh": SUBURBAN_SPEED_KMH.get(transport),
                "suburban_boarding_minutes": SUBURBAN_BOARDING_MINUTES.get(transport),
            }
            for transport, values in TRANSPORT_PARAMETERS.items()
        },
        "suburban_boarding_min_km": SUBURBAN_BOARDING_MIN_KM,
        "requests": [
            {
                "id": request.id,
                "coordinates": request.coordinates.model_dump(mode="json"),
            }
            for request in planning_input.requests
        ],
        "engineers": [
            {
                "id": engineer.id,
                "start_location": engineer.start_location.model_dump(mode="json"),
                "transport": engineer.transport.value,
                "equipment_inventory": engineer.equipment_inventory,
            }
            for engineer in planning_input.engineers
        ],
        "request_travel_norms": {
            request.id: (
                request.source_fields.get("_enrichment", {}).get("travel_norm_minutes")
                if isinstance(request.source_fields.get("_enrichment"), dict)
                else None
            )
            for request in planning_input.requests
        },
    }
    norm_versions = sorted(
        {
            str(enrichment["norm_version"])
            for request in planning_input.requests
            if isinstance((enrichment := request.source_fields.get("_enrichment")), dict)
            and enrichment.get("norm_version")
        }
    )
    # Режим норматива дороги и множитель потолка меняют допустимые плечи и цель,
    # поэтому входят в отпечаток конфигурации наравне с решателями и лимитом.
    config = {
        "algorithm_ids": algorithm_ids,
        "time_limit_seconds": time_limit_seconds,
        "random_seed": None,
        "travel_norm_mode": travel_norm_mode(),
        "travel_norm_max_factor": travel_norm_max_factor(),
    }
    return {
        "snapshot_sha256": _canonical_hash(snapshot),
        "matrix_sha256": _canonical_hash(matrix_inputs),
        "config_sha256": _canonical_hash(config),
        "route_estimation_method": planning_input.route_estimation_method,
        "norm_versions": norm_versions,
        "request_count": len(planning_input.requests),
        "engineer_count": len(planning_input.engineers),
        **config,
    }


def _percent(numerator: int | float, denominator: int | float) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def _percent_or_none(numerator: int | float, denominator: int | float) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


def evaluate_result(
    planning_input: PlanningInput,
    result: PlanningResult,
    validator_violations: int = 0,
) -> dict[str, int | float | None]:
    request_by_id = {request.id: request for request in planning_input.requests}
    assigned_ids = {stop.request_id for route in result.routes for stop in route.stops}
    urgent_ids = {
        request.id for request in planning_input.requests if request.priority.value == "urgent"
    }
    assigned_urgent = len(assigned_ids & urgent_ids)
    total_service = sum(route.service_minutes for route in result.routes)
    total_wait = sum(route.wait_minutes for route in result.routes)
    total_travel = result.metrics.total_travel_minutes
    available_shift = round(
        sum(
            (engineer.shift_end - engineer.shift_start).total_seconds() / 60
            for engineer in planning_input.engineers
        )
    )
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    used_routes = [route for route in result.routes if route.stops]
    used_shift = round(
        sum(
            (
                engineer_by_id[route.engineer_id].shift_end
                - engineer_by_id[route.engineer_id].shift_start
            ).total_seconds()
            / 60
            for route in used_routes
        )
    )
    workloads = [route.service_minutes + route.travel_minutes for route in used_routes]
    workload_mean = sum(workloads) / len(workloads) if workloads else 0.0
    workload_variance = (
        sum((value - workload_mean) ** 2 for value in workloads) / len(workloads)
        if workloads
        else 0.0
    )
    workload_cv = sqrt(workload_variance) / workload_mean if workload_mean else 0.0
    # длительность маршрута — от фактического выезда к первому визиту, а не от начала
    # смены: к первому визиту бригада выезжает в последний момент (schedule.py)
    makespan = max(
        (
            round((route.finish_at - route.stops[0].departure_at).total_seconds() / 60)
            for route in used_routes
        ),
        default=0,
    )
    assigned_service = sum(
        request_by_id[request_id].duration_minutes
        for request_id in assigned_ids
        if request_id in request_by_id
    )
    return {
        "completion_rate_percent": _percent(len(assigned_ids), len(planning_input.requests)),
        "urgent_completion_rate_percent": _percent_or_none(assigned_urgent, len(urgent_ids)),
        "distance_per_assigned_meters": (
            round(result.metrics.total_distance_meters / len(assigned_ids))
            if assigned_ids
            else None
        ),
        "travel_minutes_per_assigned": (
            round(total_travel / len(assigned_ids), 2) if assigned_ids else None
        ),
        "productive_utilization_percent": _percent(total_service, available_shift),
        "used_engineer_utilization_percent": _percent(total_service, used_shift),
        "travel_share_percent": _percent(
            total_travel, total_service + total_travel + total_wait
        ),
        "workload_cv": round(workload_cv, 4),
        "workload_spread_minutes": max(workloads) - min(workloads) if workloads else 0,
        "total_service_minutes": total_service,
        "assigned_service_minutes": assigned_service,
        "total_wait_minutes": total_wait,
        "available_shift_minutes": available_shift,
        "makespan_minutes": makespan,
        "requests_per_used_engineer": (
            round(len(assigned_ids) / len(used_routes), 2) if used_routes else None
        ),
        "validator_violations": validator_violations,
        "travel_gap_to_best_known_percent": None,
        "distance_gap_to_best_known_percent": None,
    }


def evaluate_results(
    planning_input: PlanningInput,
    results: dict[str, PlanningResult],
) -> dict[str, dict[str, int | float | None]]:
    evaluations = {
        algorithm_id: evaluate_result(planning_input, result)
        for algorithm_id, result in results.items()
    }
    # Разрыв честен только между планами, равными по всем старшим ступеням ключа
    # сравнения: по времени в пути — при равных охвате, минутах сверх норматива и числе
    # бригад; по пробегу — при равном ещё и времени в пути.
    gaps = (
        ("travel_gap_to_best_known_percent", 2, "total_travel_minutes"),
        ("distance_gap_to_best_known_percent", 1, "total_distance_meters"),
    )
    for field, tail, metric in gaps:
        best_by_priority: dict[tuple[int, ...], int] = {}
        for result in results.values():
            key = lexicographic_plan_key(result.metrics)[:-tail]
            value = int(getattr(result.metrics, metric))
            best_by_priority[key] = min(value, best_by_priority.get(key, value))
        for algorithm_id, result in results.items():
            best = best_by_priority[lexicographic_plan_key(result.metrics)[:-tail]]
            value = int(getattr(result.metrics, metric))
            evaluations[algorithm_id][field] = (
                round((value - best) / best * 100, 2) if best else 0.0
            )
    return evaluations


def selection_reason(
    selected_algorithm: str,
    results: dict[str, PlanningResult],
) -> dict[str, Any]:
    """Почему выбран план: решающая метрика против каждого конкурента.

    Решающая метрика ищется по тому же ключу, которым планы сравнивались
    (`plan_objective_key`, подписи `PLAN_KEY_LABELS`), а не по паспортному
    пятиэлементному кортежу: иначе в режиме soft, где решили минуты сверх норматива
    дороги, паспорт назвал бы следующую ступень (например, число бригад).
    Паспортные `selected_tuple` и `objective_tuple` остаются для совместимости.
    """
    mode = objective_mode()
    selected_metrics = results[selected_algorithm].metrics
    selected_key = plan_objective_key(selected_metrics)
    selected_cost = plan_cost(selected_metrics) if mode == COST else None
    competitors: list[dict[str, Any]] = []
    for algorithm_id, result in results.items():
        if algorithm_id == selected_algorithm:
            continue
        candidate_key = plan_objective_key(result.metrics)
        entry: dict[str, Any] = {
            "algorithm_id": algorithm_id,
            "objective_tuple": objective_tuple(result.metrics),
            "plan_key": list(candidate_key),
        }
        if mode == COST:
            candidate_cost = plan_cost(result.metrics)
            entry["cost_units"] = candidate_cost
            entry["decisive_metric"] = "cost" if candidate_cost != selected_cost else "tie"
        else:
            decisive_index = next(
                (
                    index
                    for index, (selected_value, candidate_value) in enumerate(
                        zip(selected_key, candidate_key, strict=True)
                    )
                    if selected_value != candidate_value
                ),
                None,
            )
            entry["decisive_metric"] = (
                PLAN_KEY_LABELS[decisive_index] if decisive_index is not None else "tie"
            )
        competitors.append(entry)
    payload: dict[str, Any] = {
        "selected_algorithm": selected_algorithm,
        "selected_tuple": objective_tuple(selected_metrics),
        "selected_plan_key": list(selected_key),
        "competitors": competitors,
        **objective_passport(),
    }
    if selected_cost is not None:
        payload["selected_cost_units"] = selected_cost
    return payload


MANUAL_ASSIGNMENT_REASON = "manual_assignment"


def manual_selection_reason(
    algorithm: str,
    result: PlanningResult,
    parent_algorithm: str,
    parent_result: PlanningResult,
) -> dict[str, Any]:
    """Паспорт выбора плана с ручным назначением: решил диспетчер, а не цель.

    Конкурент один — исходный план; его решающая метрика `manual_assignment`, потому
    что назначение сделано вручную, а не выбрано сравнением по ключу. Ключи обоих
    планов записаны, чтобы было видно, чем ручное решение отличается по цели.
    """
    return {
        "selected_algorithm": algorithm,
        "selected_tuple": objective_tuple(result.metrics),
        "selected_plan_key": list(plan_objective_key(result.metrics)),
        "decided_by": "dispatcher",
        "reason": MANUAL_ASSIGNMENT_REASON,
        "reason_text": "ручное назначение диспетчером",
        "competitors": [
            {
                "algorithm_id": parent_algorithm,
                "objective_tuple": objective_tuple(parent_result.metrics),
                "plan_key": list(plan_objective_key(parent_result.metrics)),
                "decisive_metric": MANUAL_ASSIGNMENT_REASON,
            }
        ],
        **objective_passport(),
    }
