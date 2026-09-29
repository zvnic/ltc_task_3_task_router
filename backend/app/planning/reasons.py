from app.planning.policies import (
    engineer_has_equipment_for_requests,
    request_priority_class,
    request_travel_norm_minutes,
    travel_norm_policy,
)
from app.planning.routing import MAX_BICYCLE_LEG_METERS, edge_is_feasible, effective_edge_metrics
from app.planning.schedule import calculate_route
from app.schemas import (
    EngineerInput,
    Priority,
    ServiceRequestInput,
    Transport,
    UnassignedRequest,
)

REASON_TEXT = {
    "no_required_skill": "Нет инженера с требуемым навыком.",
    "no_required_transport": "Среди инженеров с нужным навыком нет требуемого транспорта.",
    "no_required_equipment": "У подходящих инженеров нет необходимого запаса оборудования.",
    "no_feasible_time": "Даже отдельный визит не помещается во временное окно или смену.",
    "walking_leg_too_long": (
        "Заявка находится дальше допустимого пешего плеча от стартов подходящих инженеров."
    ),
    "bicycle_leg_too_long": (
        "Заявка находится дальше допустимого велосипедного плеча от стартов подходящих "
        "инженеров."
    ),
    # При TRAVEL_NORM_MODE=hard — дорога дольше норматива; при soft — дольше потолка
    # «норматив × множитель»: превышение в пределах потолка заявку в отказ не
    # отправляет, а считается и штрафуется в цели.
    "travel_norm_exceeded": (
        "Расчётное время дороги превышает действующий норматив для этого вида работ "
        "(в режиме soft — его допустимый потолок)."
    ),
    "schedule_conflict": "Подходящие инженеры есть, но визит конфликтует с их расписанием.",
    "baseline_order_conflict": (
        "Заявку нельзя добавить в конец маршрута при фиксированном входном порядке baseline."
    ),
    "not_selected_within_limit": "Допустимое назначение не найдено за ограниченное время поиска.",
    "preempted_by_urgent": "Обычная заявка уступила место срочной при перепланировании.",
}


def reason_for_request(
    request: ServiceRequestInput,
    engineers: list[EngineerInput],
    *,
    fallback: str,
) -> str:
    skilled = [engineer for engineer in engineers if request.required_skill in engineer.skills]
    if not skilled:
        return "no_required_skill"
    transported = [
        engineer
        for engineer in skilled
        if request.required_transport is None or engineer.transport == request.required_transport
    ]
    if not transported:
        return "no_required_transport"
    equipped = [
        engineer
        for engineer in transported
        if engineer_has_equipment_for_requests(engineer, [request])
    ]
    if not equipped:
        return "no_required_equipment"
    if not any(
        edge_is_feasible(
            engineer.transport,
            effective_edge_metrics(
                engineer.start_location, request.coordinates, engineer.transport
            )[0],
        )
        for engineer in equipped
    ):
        # предел плеча есть только у пеших и велосипедных бригад
        if any(engineer.transport is Transport.BICYCLE for engineer in equipped):
            return "bicycle_leg_too_long"
        return "walking_leg_too_long"
    # Норматив дороги объясняет отказ там, где он запрещает плечо: в hard — сам
    # норматив, в soft — потолок «норматив × множитель». В advisory не запрещает ничего.
    travel_limit = travel_norm_policy().limit(request_travel_norm_minutes(request))
    if travel_limit is not None and not any(
        effective_edge_metrics(
            engineer.start_location, request.coordinates, engineer.transport
        )[1]
        <= travel_limit
        for engineer in equipped
    ):
        return "travel_norm_exceeded"
    if not any(calculate_route(engineer, [request]) is not None for engineer in equipped):
        return "no_feasible_time"
    return fallback


def _reason_details(
    request: ServiceRequestInput,
    engineers: list[EngineerInput],
    code: str,
) -> tuple[str, dict[str, object]]:
    base: dict[str, object] = {
        "required_skill": request.required_skill.value,
        "required_transport": (
            request.required_transport.value if request.required_transport else None
        ),
        "window_start": request.window_start.isoformat(),
        "window_end": request.window_end.isoformat(),
    }
    skilled = [engineer for engineer in engineers if request.required_skill in engineer.skills]
    transported = [
        engineer
        for engineer in skilled
        if request.required_transport is None or engineer.transport == request.required_transport
    ]
    equipped = [
        engineer
        for engineer in transported
        if engineer_has_equipment_for_requests(engineer, [request])
    ]
    if code == "no_required_skill":
        return (
            f"Требуется навык «{request.required_skill.value}», но ни у одного "
            "инженера зоны его нет.",
            base | {"engineers_checked": len(engineers)},
        )
    if code == "no_required_transport":
        required = request.required_transport.value if request.required_transport else "не задан"
        available = sorted({engineer.transport.value for engineer in skilled})
        return (
            f"Требуется транспорт «{required}». У инженеров с нужным навыком доступны: "
            f"{', '.join(available) or 'нет вариантов'}.",
            base | {"available_transports": available},
        )
    if code == "no_required_equipment":
        return (
            "Ни у одного инженера с нужным навыком и транспортом нет требуемого остатка "
            f"оборудования: {request.required_equipment}.",
            base | {"required_equipment": request.required_equipment},
        )
    edge_facts = [
        {
            "engineer_id": engineer.id,
            "engineer_name": engineer.name,
            "transport": engineer.transport.value,
            "effective_transport": metrics[2].value,
            "distance_meters": metrics[0],
            "travel_minutes": metrics[1],
        }
        for engineer in equipped
        for metrics in [
            effective_edge_metrics(
                engineer.start_location, request.coordinates, engineer.transport
            )
        ]
    ]
    if code == "walking_leg_too_long":
        nearest = min(
            (int(item["distance_meters"]) for item in edge_facts),
            default=0,
        )
        return (
            f"Ближайший допустимый по навыку инженер должен пройти {nearest / 1000:.1f} км "
            "от своей стартовой точки. Лимит одного пешего плеча — 1,0 км.",
            base | {"walking_limit_meters": 1000, "candidate_edges": edge_facts},
        )
    if code == "bicycle_leg_too_long":
        nearest = min(
            (
                int(item["distance_meters"])
                for item in edge_facts
                if item["transport"] == Transport.BICYCLE.value
            ),
            default=0,
        )
        return (
            f"Ближайший допустимый по навыку инженер на велосипеде должен проехать "
            f"{nearest / 1000:.1f} км от своей стартовой точки. Лимит одного велосипедного "
            f"плеча — {MAX_BICYCLE_LEG_METERS // 1000} км.",
            base
            | {"bicycle_limit_meters": MAX_BICYCLE_LEG_METERS, "candidate_edges": edge_facts},
        )
    if code == "travel_norm_exceeded":
        norm_policy = travel_norm_policy()
        travel_norm = request_travel_norm_minutes(request)
        travel_limit = norm_policy.limit(travel_norm)
        best = min((int(item["travel_minutes"]) for item in edge_facts), default=0)
        # потолок называется, только когда он выше норматива: при множителе 1,0 текст
        # тот же, что в режиме hard
        limit_text = (
            f", допустимый потолок — {travel_limit} мин "
            f"(норматив × {norm_policy.max_factor:g})"
            if norm_policy.max_excess(travel_norm) > 0
            else ""
        )
        return (
            f"Минимальная расчётная дорога от стартов подходящих инженеров — {best} мин, "
            f"норматив для заявки — {travel_norm} мин{limit_text}.",
            base
            | {
                "travel_norm_minutes": travel_norm,
                "travel_limit_minutes": travel_limit,
                "best_travel_minutes": best,
                "candidate_edges": edge_facts,
            },
        )
    if code == "no_feasible_time":
        return (
            f"Визит длительностью {request.duration_minutes} мин не помещается в окно "
            f"{request.window_start:%H:%M}–{request.window_end:%H:%M} с учётом дороги и смен.",
            base | {"duration_minutes": request.duration_minutes, "candidate_edges": edge_facts},
        )
    if code == "baseline_order_conflict":
        # в soft с потолком выше норматива запрещает не норматив, а потолок; при
        # множителе 1,0 текст тот же, что в режиме hard; в advisory норматив не запрещает
        norm_policy = travel_norm_policy()
        travel_norm = request_travel_norm_minutes(request)
        limits = (
            "окна или смены"
            if norm_policy.limit(travel_norm) is None
            else "окна, смены или потолка норматива дороги"
            if norm_policy.max_excess(travel_norm) > 0
            else "окна, смены или норматива дороги"
        )
        return (
            "При сохранении входного порядка заявку нельзя добавить в конец ни одного "
            f"допустимого маршрута без нарушения {limits}.",
            base | {"candidate_edges": edge_facts},
        )
    if code == "preempted_by_urgent":
        return REASON_TEXT[code], base
    if code == "not_selected_within_limit":
        return (
            "Решатель проверил допустимые назначения, но не включил заявку в лучший найденный "
            "план в пределах лимита времени.",
            base | {"candidate_edges": edge_facts},
        )
    return (
        "Подходящие инженеры существуют, но заявка конфликтует с уже занятыми интервалами, "
        "порядком визитов или действующими нормативами.",
        base | {"candidate_edges": edge_facts},
    )


def make_unassigned(
    request: ServiceRequestInput,
    engineers: list[EngineerInput],
    *,
    fallback: str,
) -> UnassignedRequest:
    code = reason_for_request(request, engineers, fallback=fallback)
    explanation, details = _reason_details(request, engineers, code)
    return UnassignedRequest(
        request_id=request.id,
        external_id=request.external_id,
        priority=Priority(request.priority),
        priority_class=request_priority_class(request),
        reason_code=code,
        explanation=explanation,
        details=details,
    )
