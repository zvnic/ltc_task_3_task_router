from time import perf_counter

from app.planning.metrics import calculate_metrics
from app.planning.reasons import make_unassigned
from app.planning.schedule import calculate_route
from app.planning.validation import validate_plan
from app.schemas import EngineerRoute, PlanningInput, PlanningResult, ServiceRequestInput
from app.schemas.domain import coordinate_quality


def build_baseline(planning_input: PlanningInput) -> PlanningResult:
    started_at = perf_counter()
    ordered_engineers = sorted(planning_input.engineers, key=lambda item: item.input_order)
    ordered_requests = sorted(planning_input.requests, key=lambda item: item.input_order)
    assigned_by_engineer: dict[str, list[ServiceRequestInput]] = {
        engineer.id: [] for engineer in ordered_engineers
    }
    buffer_minutes = planning_input.buffer_minutes
    routes_by_engineer: dict[str, EngineerRoute] = {}
    unassigned = []

    for request in ordered_requests:
        selected = False
        for engineer in ordered_engineers:
            candidate_requests = [*assigned_by_engineer[engineer.id], request]
            candidate_route = calculate_route(engineer, candidate_requests, buffer_minutes)
            if candidate_route is None:
                continue
            assigned_by_engineer[engineer.id] = candidate_requests
            routes_by_engineer[engineer.id] = candidate_route
            selected = True
            break
        if not selected:
            unassigned.append(
                make_unassigned(request, ordered_engineers, fallback="baseline_order_conflict")
            )

    routes = [
        routes_by_engineer.get(engineer.id) or calculate_route(engineer, [], buffer_minutes)
        for engineer in ordered_engineers
    ]
    checked_routes = [route for route in routes if route is not None]
    metrics = calculate_metrics(checked_routes, unassigned)
    result = PlanningResult(
        algorithm="baseline",
        solution_status="feasible",
        termination_reason="input_order_completed",
        elapsed_ms=max(0, round((perf_counter() - started_at) * 1000)),
        route_estimation_method=planning_input.route_estimation_method,
        coordinate_quality=coordinate_quality(
            request.coordinate_source for request in ordered_requests
        ),
        routes=checked_routes,
        unassigned=unassigned,
        metrics=metrics,
        constraint_violations_count=0,
    )
    violations = validate_plan(planning_input, result)
    if violations:
        raise ValueError(f"baseline validator rejected result: {violations}")
    return result
