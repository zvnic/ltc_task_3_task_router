from datetime import timedelta

from app.planning.metrics import calculate_metrics
from app.planning.policies import (
    effective_service_start_deadline,
    engineer_has_equipment_for_requests,
    request_priority_class,
    request_travel_norm_minutes,
    travel_norm_policy,
)
from app.planning.routing import (
    ACCEPTED_ROUTE_ESTIMATION_METHODS,
    ROUTE_ESTIMATION_METHOD,
    edge_is_feasible,
    edge_metrics,
    effective_edge_metrics,
    travel_norm_excess_minutes,
)
from app.schemas import PlanningInput, PlanningResult, Transport

# Маршруты, посчитанные прежней моделью (v4 — общественный транспорт до калибровки,
# v3 — ещё и без загородной скорости), остаются в планах как обещанное: защищённый
# префикс, прежние визиты при вставке. Плечо проверяется по той версии модели,
# которой его посчитали (`routing_method` в фактах визита).


def validate_plan(planning_input: PlanningInput, result: PlanningResult) -> list[str]:
    errors: list[str] = []
    request_by_id = {request.id: request for request in planning_input.requests}
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    seen: set[str] = set()
    seen_engineers: set[str] = set()
    norm_policy = travel_norm_policy()
    # Превышения норматива дороги валидатор считает сам — и число визитов, и минуты
    # сверх норматива — по нормативу заявки и заново посчитанной дороге, а не по
    # фактам, которые записал решатель.
    counted_norm_violations = 0
    counted_norm_excess_minutes = 0

    for route in result.routes:
        if route.engineer_id in seen_engineers:
            errors.append(f"duplicate_engineer_route:{route.engineer_id}")
        seen_engineers.add(route.engineer_id)
        engineer = engineer_by_id.get(route.engineer_id)
        if engineer is None:
            errors.append(f"unknown_engineer:{route.engineer_id}")
            continue
        if route.engineer_name != engineer.name:
            errors.append(f"engineer_name_mismatch:{route.engineer_id}")
        if route.transport != engineer.transport:
            errors.append(f"route_transport_mismatch:{route.engineer_id}")
        if route.routing_method not in ACCEPTED_ROUTE_ESTIMATION_METHODS:
            errors.append(f"route_routing_method_mismatch:{route.engineer_id}")
        if route.routing_quality.value != "estimated":
            errors.append(f"route_routing_quality_mismatch:{route.engineer_id}")
        if route.start_location != engineer.start_location:
            errors.append(f"route_start_location_mismatch:{route.engineer_id}")
        current_location = engineer.start_location
        current_time = route.departure_at
        if current_time < engineer.shift_start or current_time > engineer.shift_end:
            errors.append(f"invalid_departure:{route.engineer_id}")
        distance_total = 0
        travel_total = 0
        service_total = 0
        wait_total = 0
        route_requests = []
        for sequence, stop in enumerate(route.stops, start=1):
            request = request_by_id.get(stop.request_id)
            if request is None:
                errors.append(f"unknown_request:{stop.request_id}")
                continue
            if request.id in seen:
                errors.append(f"duplicate_assignment:{request.id}")
            seen.add(request.id)
            route_requests.append(request)
            if stop.sequence != sequence:
                errors.append(f"invalid_sequence:{request.id}")
            if stop.location is not None and stop.location != request.coordinates:
                errors.append(f"stop_location_mismatch:{request.id}")
            if request.required_skill not in engineer.skills:
                errors.append(f"skill_mismatch:{request.id}")
            if (
                request.required_transport is not None
                and request.required_transport != engineer.transport
            ):
                errors.append(f"transport_mismatch:{request.id}")
            if stop.departure_at < current_time:
                errors.append(f"departure_before_available:{request.id}")
            current_time = stop.departure_at
            recorded_transport = stop.facts.get("travel_mode_from_previous")
            method = stop.facts.get("routing_method") or ROUTE_ESTIMATION_METHOD
            if recorded_transport is None:
                # Plans stored before the leg-mode policy retain their original,
                # primary-transport schedule during protected-prefix replanning.
                distance_meters, travel_minutes = edge_metrics(
                    current_location,
                    request.coordinates,
                    engineer.transport,
                    method=method,
                )
                effective_transport = engineer.transport
            else:
                distance_meters, travel_minutes, effective_transport = effective_edge_metrics(
                    current_location,
                    request.coordinates,
                    engineer.transport,
                    method=method,
                )
            arrival = current_time + timedelta(minutes=travel_minutes)
            service_start = max(arrival, request.window_start)
            service_end = service_start + timedelta(minutes=request.duration_minutes)
            expected_service_end = service_start + timedelta(
                minutes=request.expected_service_minutes
            )
            reserve_minutes = request.duration_minutes - request.expected_service_minutes
            wait_minutes = round((service_start - arrival).total_seconds() / 60)
            expected = (
                arrival,
                service_start,
                service_end,
                stop.departure_at,
                wait_minutes,
                travel_minutes,
                distance_meters,
            )
            actual = (
                stop.arrival_at,
                stop.service_start_at,
                stop.service_end_at,
                stop.departure_at,
                stop.wait_minutes,
                stop.travel_minutes_from_previous,
                stop.distance_meters_from_previous,
            )
            if actual != expected:
                errors.append(f"schedule_mismatch:{request.id}")
            if (
                stop.expected_service_end_at is not None
                and stop.expected_service_end_at != expected_service_end
            ):
                errors.append(f"expected_service_mismatch:{request.id}")
            if stop.reserve_minutes != reserve_minutes:
                errors.append(f"service_reserve_mismatch:{request.id}")
            if recorded_transport is not None and recorded_transport != effective_transport.value:
                errors.append(f"travel_mode_mismatch:{request.id}")
            travel_norm_minutes = request_travel_norm_minutes(request)
            if engineer.transport is Transport.WALKING and not edge_is_feasible(
                engineer.transport,
                distance_meters,
            ):
                errors.append(f"walking_leg_too_long:{request.id}")
            # Велосипедный предел проверяется у визитов, посчитанных под ним: плечи,
            # выданные до предела, остаются в плане как обещанное (защищённый префикс,
            # прежние визиты при вставке).
            if (
                engineer.transport is Transport.BICYCLE
                and stop.facts.get("leg_limit_meters") is not None
                and not edge_is_feasible(engineer.transport, distance_meters)
            ):
                errors.append(f"bicycle_leg_too_long:{request.id}")
            norm_excess = travel_norm_excess_minutes(travel_minutes, travel_norm_minutes)
            norm_exceeded = norm_excess > 0
            if norm_exceeded:
                counted_norm_violations += 1
                counted_norm_excess_minutes += norm_excess
            # hard: плечо сверх норматива недопустимо; soft: допустимо до потолка
            # «норматив × множитель» и обязано быть честно посчитано в метриках плана
            # (сверка после цикла), а плечо длиннее потолка недопустимо, как в hard;
            # advisory: потолка нет, превышение только считается и показывается
            travel_limit = norm_policy.limit(travel_norm_minutes)
            if travel_limit is not None and travel_minutes > travel_limit:
                errors.append(
                    f"travel_norm_exceeded:{request.id}"
                    if norm_policy.hard
                    else f"travel_norm_ceiling_exceeded:{request.id}"
                )
            recorded_norm_flag = stop.facts.get("travel_norm_exceeded")
            if recorded_norm_flag is not None and recorded_norm_flag != norm_exceeded:
                errors.append(f"travel_norm_flag_mismatch:{request.id}")
            recorded_norm_excess = stop.facts.get("travel_norm_excess_minutes")
            if recorded_norm_excess is not None and recorded_norm_excess != norm_excess:
                errors.append(f"travel_norm_excess_mismatch:{request.id}")
            if not request.window_start <= service_start <= effective_service_start_deadline(
                request
            ):
                errors.append(f"window_violation:{request.id}")
            if (
                request.completion_deadline is not None
                and service_end > request.completion_deadline
            ):
                errors.append(f"completion_deadline_violation:{request.id}")
            if service_end > engineer.shift_end:
                errors.append(f"shift_violation:{request.id}")
            current_location = request.coordinates
            current_time = service_end
            distance_total += distance_meters
            travel_total += travel_minutes
            service_total += request.duration_minutes
            wait_total += wait_minutes
        if not engineer_has_equipment_for_requests(engineer, route_requests):
            errors.append(f"equipment_capacity_violation:{route.engineer_id}")
        if route.distance_meters != distance_total:
            errors.append(f"route_distance_mismatch:{route.engineer_id}")
        if route.travel_minutes != travel_total:
            errors.append(f"route_travel_mismatch:{route.engineer_id}")
        if route.service_minutes != service_total:
            errors.append(f"route_service_mismatch:{route.engineer_id}")
        if route.wait_minutes != wait_total:
            errors.append(f"route_wait_mismatch:{route.engineer_id}")
        if route.finish_at != current_time:
            errors.append(f"route_finish_mismatch:{route.engineer_id}")

    unassigned_ids: set[str] = set()
    for item in result.unassigned:
        if item.request_id in unassigned_ids:
            errors.append(f"duplicate_unassigned:{item.request_id}")
        unassigned_ids.add(item.request_id)
        request = request_by_id.get(item.request_id)
        if request is None:
            errors.append(f"unknown_unassigned:{item.request_id}")
        elif item.priority != request.priority:
            errors.append(f"unassigned_priority_mismatch:{item.request_id}")
        elif item.priority_class != request_priority_class(request):
            errors.append(f"unassigned_priority_class_mismatch:{item.request_id}")
    if seen & unassigned_ids:
        errors.append("assigned_and_unassigned_overlap")
    expected_ids = set(request_by_id)
    if seen | unassigned_ids != expected_ids:
        errors.append("request_coverage_mismatch")
    expected_metrics = calculate_metrics(result.routes, result.unassigned)
    if result.metrics != expected_metrics:
        errors.append("plan_metrics_mismatch")
    # Счёт метрик опирается на факты визитов; план, который занизил или завысил число
    # превышений норматива или их минуты, не публикуется независимо от того, что
    # записано в фактах.
    if result.metrics.norm_violation_count != counted_norm_violations:
        errors.append("norm_violation_count_mismatch")
    if result.metrics.norm_excess_minutes != counted_norm_excess_minutes:
        errors.append("norm_excess_minutes_mismatch")
    return errors
