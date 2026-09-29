from datetime import datetime, timedelta

from app.planning.policies import (
    effective_service_start_deadline,
    engineer_has_equipment_for_requests,
    request_travel_norm_minutes,
    travel_norm_policy,
)
from app.planning.routing import (
    LEG_LIMIT_METERS,
    ROUTE_ESTIMATION_METHOD,
    edge_is_feasible,
    effective_edge_metrics,
    travel_exceeds_norm,
    travel_norm_excess_minutes,
)
from app.schemas import EngineerInput, EngineerRoute, RouteStop, ServiceRequestInput, Transport

# Почему плечо пройдено не основным транспортом бригады: машина паркуется и идёт
# к соседнему дому, бригада на общественном транспорте идёт пешком, когда так не дольше.
SHORT_WALK_REASONS = {
    Transport.CAR: "short_car_leg_walk",
    Transport.PUBLIC_TRANSPORT: "short_transit_leg_walk",
}


def _travel_mode_reason(engineer: EngineerInput, effective_transport: Transport) -> str:
    if effective_transport is Transport.WALKING and engineer.transport in SHORT_WALK_REASONS:
        return SHORT_WALK_REASONS[engineer.transport]
    return "primary_transport"


# Код объяснения визита, доехавшего дольше норматива дороги. Появляется в режимах
# TRAVEL_NORM_MODE=advisory и soft (плечо в пределах потолка): в режиме hard такое
# плечо до визита не доходит.
NORM_EXCEEDED_CODE = "travel_norm_exceeded"


def _with_norm_code(codes: list[str], exceeded: bool) -> list[str]:
    """Коды объяснения визита с пометкой о превышении норматива или без неё."""
    kept = [code for code in codes if code != NORM_EXCEEDED_CODE]
    return [*kept, NORM_EXCEEDED_CODE] if exceeded else kept


def _is_eligible(engineer: EngineerInput, request: ServiceRequestInput) -> bool:
    if request.required_skill not in engineer.skills:
        return False
    return request.required_transport is None or request.required_transport == engineer.transport


def _latest_departure(
    engineer: EngineerInput,
    requests: list[ServiceRequestInput],
    buffer_minutes: int,
) -> datetime:
    """Самый поздний выезд, при котором последовательность ещё укладывается в окна.

    Обратный проход: для каждого визита считается самое позднее допустимое начало
    работ с учётом дороги и буфера после него; от первого визита вычитается дорога
    из стартовой точки. Раньше начала смены бригада не выезжает.

    Дорога считается тем же `effective_edge_metrics`, что и в прямом проходе: иначе
    короткие плечи, которые автобригада проходит пешком, дадут разные времена, и
    валидатор отвергнет план.
    """
    last = requests[-1]
    latest_start = min(
        effective_service_start_deadline(last),
        engineer.shift_end - timedelta(minutes=last.duration_minutes),
    )
    for index in range(len(requests) - 2, -1, -1):
        request = requests[index]
        _, travel_minutes, _ = effective_edge_metrics(
            request.coordinates,
            requests[index + 1].coordinates,
            engineer.transport,
        )
        latest_start = min(
            effective_service_start_deadline(request),
            latest_start
            - timedelta(minutes=travel_minutes + buffer_minutes + request.duration_minutes),
        )
    _, travel_first, _ = effective_edge_metrics(
        engineer.start_location,
        requests[0].coordinates,
        engineer.transport,
    )
    return max(engineer.shift_start, latest_start - timedelta(minutes=travel_first))


def calculate_route(
    engineer: EngineerInput,
    requests: list[ServiceRequestInput],
    buffer_minutes: int = 0,
) -> EngineerRoute | None:
    if not engineer_has_equipment_for_requests(engineer, requests):
        return None
    # режим норматива и потолок читаются один раз на маршрут
    norm_policy = travel_norm_policy()
    buffer = buffer_minutes
    current_location = engineer.start_location
    current_time = engineer.shift_start
    if buffer > 0 and requests:
        # буфер имеет смысл только вместе с поздним стартом: иначе запас съедает ожидание
        if not all(_is_eligible(engineer, request) for request in requests):
            return None
        current_time = _latest_departure(engineer, requests, buffer)
    route_departure = current_time
    finish_at = current_time
    stops: list[RouteStop] = []
    distance_total = 0
    travel_total = 0
    wait_total = 0
    service_total = 0

    for sequence, request in enumerate(requests, start=1):
        if not _is_eligible(engineer, request):
            return None
        departure_at = current_time
        distance_meters, travel_minutes, effective_transport = effective_edge_metrics(
            current_location,
            request.coordinates,
            engineer.transport,
        )
        travel_norm_minutes = request_travel_norm_minutes(request)
        if not edge_is_feasible(
            engineer.transport,
            distance_meters,
            travel_minutes=travel_minutes,
            max_travel_minutes=norm_policy.limit(travel_norm_minutes),
        ):
            return None
        arrival_at = departure_at + timedelta(minutes=travel_minutes)
        service_start_at = max(arrival_at, request.window_start)
        if sequence == 1 and buffer == 0 and service_start_at > arrival_at:
            # Выехать к началу смены, чтобы часами ждать открытия окна у двери клиента,
            # диспетчер бригаде не поручит: первый выезд сдвигается на последний момент,
            # когда бригада успевает к началу работ. Время работ не меняется. Маршрут
            # (`route_departure`) по-прежнему начинается, когда бригада свободна: на этом
            # держатся вставка новой заявки в утренний интервал и защита при событии —
            # бригада, ещё не выехавшая к первому визиту, не считается в пути.
            departure_at = service_start_at - timedelta(minutes=travel_minutes)
            arrival_at = service_start_at
        if service_start_at > effective_service_start_deadline(request):
            return None
        service_end_at = service_start_at + timedelta(minutes=request.duration_minutes)
        expected_service_end_at = service_start_at + timedelta(
            minutes=request.expected_service_minutes
        )
        if (
            request.completion_deadline is not None
            and service_end_at > request.completion_deadline
        ):
            return None
        if service_end_at > engineer.shift_end:
            return None
        wait_minutes = round((service_start_at - arrival_at).total_seconds() / 60)
        norm_exceeded = travel_exceeds_norm(travel_minutes, travel_norm_minutes)
        travel_mode_reason = _travel_mode_reason(engineer, effective_transport)
        explanation_codes = [
            "skill_match",
            "transport_match",
            f"transport_profile_{effective_transport.value}",
            (
                travel_mode_reason
                if travel_mode_reason != "primary_transport"
                else "walking_leg_within_1000m"
                if engineer.transport.value == "walking"
                else "motorized_leg_allowed"
            ),
            "routing_estimated",
            "time_window_feasible",
        ]
        if norm_exceeded:
            explanation_codes.append(NORM_EXCEEDED_CODE)
        stops.append(
            RouteStop(
                request_id=request.id,
                sequence=sequence,
                location=request.coordinates,
                arrival_at=arrival_at,
                service_start_at=service_start_at,
                expected_service_end_at=expected_service_end_at,
                service_end_at=service_end_at,
                departure_at=departure_at,
                wait_minutes=wait_minutes,
                travel_minutes_from_previous=travel_minutes,
                distance_meters_from_previous=distance_meters,
                reserve_minutes=request.duration_minutes
                - request.expected_service_minutes,
                explanation_codes=explanation_codes,
                facts={
                    "required_skill": request.required_skill.value,
                    "transport": engineer.transport.value,
                    "travel_mode_from_previous": effective_transport.value,
                    "travel_mode_reason": travel_mode_reason,
                    "routing_method": ROUTE_ESTIMATION_METHOD,
                    "routing_quality": "estimated",
                    # предел плеча, под которым визит посчитан: по нему валидатор
                    # отличает новые велосипедные плечи от обещанных до предела
                    "leg_limit_meters": LEG_LIMIT_METERS.get(engineer.transport),
                    "window_end": request.window_end.isoformat(),
                    "completion_deadline": (
                        request.completion_deadline.isoformat()
                        if request.completion_deadline
                        else None
                    ),
                    "shift_end": engineer.shift_end.isoformat(),
                    "expected_service_minutes": request.expected_service_minutes,
                    "safe_service_minutes": request.duration_minutes,
                    "required_equipment": request.required_equipment,
                    "travel_norm_minutes": travel_norm_minutes,
                    "travel_norm_exceeded": norm_exceeded,
                    "travel_norm_excess_minutes": travel_norm_excess_minutes(
                        travel_minutes, travel_norm_minutes
                    ),
                },
            )
        )
        distance_total += distance_meters
        travel_total += travel_minutes
        wait_total += wait_minutes
        service_total += request.duration_minutes
        current_location = request.coordinates
        finish_at = service_end_at
        # выезд на следующий визит не раньше, чем кончится буфер после этого
        current_time = service_end_at + timedelta(minutes=buffer)

    explanation = (
        f"{engineer.name}: {len(stops)} визитов, расчётный пробег "
        f"{distance_total / 1000:.1f} км; возврат в стартовую точку не учитывается."
    )
    return EngineerRoute(
        engineer_id=engineer.id,
        engineer_name=engineer.name,
        transport=engineer.transport,
        routing_method=ROUTE_ESTIMATION_METHOD,
        routing_quality="estimated",
        start_location=engineer.start_location,
        departure_at=route_departure,
        stops=stops,
        distance_meters=distance_total,
        travel_minutes=travel_total,
        service_minutes=service_total,
        wait_minutes=wait_total,
        finish_at=finish_at,
        explanation=explanation,
    )


def insert_request_preserving_visits(
    engineer: EngineerInput,
    route: EngineerRoute,
    existing_requests: list[ServiceRequestInput],
    request: ServiceRequestInput,
    position: int,
) -> EngineerRoute | None:
    """Insert a normal arrival into a real free gap without moving existing service times."""
    if request.required_skill not in engineer.skills:
        return None
    if request.required_transport is not None and request.required_transport != engineer.transport:
        return None
    if not engineer_has_equipment_for_requests(engineer, [*existing_requests, request]):
        return None
    if position < 0 or position > len(route.stops):
        return None

    request_by_id = {item.id: item for item in existing_requests}
    previous_location = engineer.start_location
    previous_end = route.departure_at
    if position:
        previous_stop = route.stops[position - 1]
        previous_request = request_by_id[previous_stop.request_id]
        previous_location = previous_request.coordinates
        previous_end = previous_stop.service_end_at

    distance, travel, effective_transport = effective_edge_metrics(
        previous_location,
        request.coordinates,
        engineer.transport,
    )
    norm_policy = travel_norm_policy()
    travel_norm_minutes = request_travel_norm_minutes(request)
    if not edge_is_feasible(
        engineer.transport,
        distance,
        travel_minutes=travel,
        max_travel_minutes=norm_policy.limit(travel_norm_minutes),
    ):
        return None
    norm_exceeded = travel_exceeds_norm(travel, travel_norm_minutes)
    departure = previous_end
    arrival = previous_end + timedelta(minutes=travel)
    service_start = max(arrival, request.window_start)
    if position == 0 and service_start > arrival:
        # как в calculate_route: к первому визиту дня выезжают в последний момент
        departure = service_start - timedelta(minutes=travel)
        arrival = service_start
    service_end = service_start + timedelta(minutes=request.duration_minutes)
    expected_service_end = service_start + timedelta(
        minutes=request.expected_service_minutes
    )
    if service_start > effective_service_start_deadline(request):
        return None
    if request.completion_deadline is not None and service_end > request.completion_deadline:
        return None
    if service_end > engineer.shift_end:
        return None

    inserted = RouteStop(
        request_id=request.id,
        sequence=position + 1,
        location=request.coordinates,
        arrival_at=arrival,
        service_start_at=service_start,
        expected_service_end_at=expected_service_end,
        service_end_at=service_end,
        departure_at=departure,
        wait_minutes=round((service_start - arrival).total_seconds() / 60),
        travel_minutes_from_previous=travel,
        distance_meters_from_previous=distance,
        reserve_minutes=request.duration_minutes - request.expected_service_minutes,
        explanation_codes=_with_norm_code(
            [
                "gap_insertion",
                "existing_visits_preserved",
                f"transport_profile_{effective_transport.value}",
                "routing_estimated",
            ],
            norm_exceeded,
        ),
        facts={
            "required_skill": request.required_skill.value,
            "transport": engineer.transport.value,
            "travel_mode_from_previous": effective_transport.value,
            "travel_mode_reason": _travel_mode_reason(engineer, effective_transport),
            "routing_method": ROUTE_ESTIMATION_METHOD,
            "routing_quality": "estimated",
            "leg_limit_meters": LEG_LIMIT_METERS.get(engineer.transport),
            "required_equipment": request.required_equipment,
            "travel_norm_minutes": travel_norm_minutes,
            "travel_norm_exceeded": norm_exceeded,
            "travel_norm_excess_minutes": travel_norm_excess_minutes(travel, travel_norm_minutes),
            "expected_service_minutes": request.expected_service_minutes,
            "safe_service_minutes": request.duration_minutes,
        },
    )

    stops = [stop.model_copy() for stop in route.stops]
    if position < len(stops):
        next_stop = stops[position]
        next_request = request_by_id[next_stop.request_id]
        next_distance, next_travel, next_transport = effective_edge_metrics(
            request.coordinates,
            next_request.coordinates,
            engineer.transport,
        )
        next_travel_norm_minutes = request_travel_norm_minutes(next_request)
        if not edge_is_feasible(
            engineer.transport,
            next_distance,
            travel_minutes=next_travel,
            max_travel_minutes=norm_policy.limit(next_travel_norm_minutes),
        ):
            return None
        # плечо до следующего визита теперь начинается у новой заявки: превышение его
        # норматива считается заново, прежняя пометка к новому плечу не относится
        next_norm_exceeded = travel_exceeds_norm(next_travel, next_travel_norm_minutes)
        next_arrival = service_end + timedelta(minutes=next_travel)
        next_service_start = max(next_arrival, next_request.window_start)
        next_service_end = next_service_start + timedelta(minutes=next_request.duration_minutes)
        if (
            next_service_start != next_stop.service_start_at
            or next_service_end != next_stop.service_end_at
        ):
            return None
        stops[position] = next_stop.model_copy(
            update={
                "departure_at": service_end,
                "arrival_at": next_arrival,
                "wait_minutes": round(
                    (next_service_start - next_arrival).total_seconds() / 60
                ),
                "travel_minutes_from_previous": next_travel,
                "distance_meters_from_previous": next_distance,
                "explanation_codes": _with_norm_code(
                    next_stop.explanation_codes, next_norm_exceeded
                ),
                "facts": {
                    **next_stop.facts,
                    # плечо пересчитано текущей моделью, даже если визит из старого плана
                    "routing_method": ROUTE_ESTIMATION_METHOD,
                    "travel_mode_from_previous": next_transport.value,
                    "travel_mode_reason": _travel_mode_reason(engineer, next_transport),
                    "travel_norm_minutes": next_travel_norm_minutes,
                    "travel_norm_exceeded": next_norm_exceeded,
                    "travel_norm_excess_minutes": travel_norm_excess_minutes(
                        next_travel, next_travel_norm_minutes
                    ),
                },
            }
        )

    stops.insert(position, inserted)
    stops = [stop.model_copy(update={"sequence": index}) for index, stop in enumerate(stops, 1)]
    return EngineerRoute(
        engineer_id=route.engineer_id,
        engineer_name=route.engineer_name,
        transport=route.transport,
        routing_method=route.routing_method,
        routing_quality=route.routing_quality,
        start_location=route.start_location,
        departure_at=route.departure_at,
        stops=stops,
        distance_meters=sum(stop.distance_meters_from_previous for stop in stops),
        travel_minutes=sum(stop.travel_minutes_from_previous for stop in stops),
        service_minutes=sum(
            request_by_id.get(stop.request_id, request).duration_minutes for stop in stops
        ),
        wait_minutes=sum(stop.wait_minutes for stop in stops),
        finish_at=stops[-1].service_end_at,
        explanation=(
            f"{engineer.name}: новая обычная заявка вставлена в свободный интервал; "
            "исполнители, порядок и время обслуживания прежних визитов сохранены."
        ),
    )
