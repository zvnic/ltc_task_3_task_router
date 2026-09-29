from app.schemas import (
    EngineerMetrics,
    EngineerRoute,
    PlanMetrics,
    UnassignedRequest,
    WorkPriorityClass,
)


def route_norm_excess(route: EngineerRoute) -> tuple[int, int]:
    """(визитов дольше норматива дороги, сумма минут сверх норматива) маршрута.

    Норматив визита лежит в фактах остановки (`travel_norm_minutes`), поэтому счёт
    не требует исходных заявок. Независимо, по самим заявкам и заново посчитанной
    дороге, оба числа пересчитывает валидатор: план, занизивший превышения, он
    не пропускает.
    """
    count = 0
    minutes = 0
    for stop in route.stops:
        norm = stop.facts.get("travel_norm_minutes")
        # type(...) is int отсекает и None, и bool; счёт идёт на каждый ход локального
        # поиска, поэтому без лишних вызовов (та же формула, что travel_norm_excess_minutes)
        if type(norm) is int and stop.travel_minutes_from_previous > norm:
            count += 1
            minutes += stop.travel_minutes_from_previous - norm
    return count, minutes


def route_norm_excess_minutes(route: EngineerRoute) -> int:
    """Сумма минут дороги сверх норматива в маршруте — ступень цели режима soft."""
    return route_norm_excess(route)[1]


def calculate_metrics(
    routes: list[EngineerRoute],
    unassigned: list[UnassignedRequest],
) -> PlanMetrics:
    excess = [route_norm_excess(route) for route in routes]
    per_engineer = [
        EngineerMetrics(
            engineer_id=route.engineer_id,
            visits=len(route.stops),
            distance_meters=route.distance_meters,
            travel_minutes=route.travel_minutes,
            service_minutes=route.service_minutes,
            wait_minutes=route.wait_minutes,
        )
        for route in routes
    ]
    return PlanMetrics(
        assigned_count=sum(len(route.stops) for route in routes),
        unassigned_count=len(unassigned),
        urgent_unassigned_count=sum(item.priority.value == "urgent" for item in unassigned),
        normal_unassigned_count=sum(item.priority.value == "normal" for item in unassigned),
        emergency_unassigned_count=sum(
            item.priority_class is WorkPriorityClass.EMERGENCY for item in unassigned
        ),
        connection_unassigned_count=sum(
            item.priority_class is WorkPriorityClass.CONNECTION for item in unassigned
        ),
        routine_unassigned_count=sum(
            item.priority_class is WorkPriorityClass.ROUTINE for item in unassigned
        ),
        norm_violation_count=sum(count for count, _ in excess),
        norm_excess_minutes=sum(minutes for _, minutes in excess),
        used_engineers_count=sum(bool(route.stops) for route in routes),
        total_distance_meters=sum(route.distance_meters for route in routes),
        total_travel_minutes=sum(route.travel_minutes for route in routes),
        per_engineer=per_engineer,
    )


def objective_tuple(metrics: PlanMetrics) -> tuple[int, int, int, int, int, int]:
    return (
        metrics.emergency_unassigned_count,
        metrics.connection_unassigned_count,
        metrics.routine_unassigned_count,
        metrics.used_engineers_count,
        metrics.total_travel_minutes,
        metrics.total_distance_meters,
    )
