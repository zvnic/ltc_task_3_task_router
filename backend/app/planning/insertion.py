"""Эвристика вставки с локальным поиском.

Три исправления первой версии:
1. Стоимость вставки — прирост времени в пути маршрута (при равенстве — пробега), а не
   его полная длина.
2. Отказанные заявки пробуются заново после каждого принятого улучшения и в конце:
   перестановки освобождают временные окна.
3. Скан соседства продолжается с места остановки, а не начинается заново с первой
   бригады, поэтому за тот же лимит просматривается кратно больше ходов.

Соседство: перенос отрезка между маршрутами и внутри маршрута (or-opt), обмен двумя
визитами между маршрутами (swap) и разворот отрезка внутри маршрута (2-opt). Ход
оценивается пересчётом только затронутых маршрутов. Перебор детерминированный.

Ходы сравниваются ключом `plan_objective_key` — тем же, что выбирает лучший план.
В режиме норматива soft место вставки выбирается сперва по приросту минут дороги
сверх норматива, затем по приросту времени в пути и пробега; в режимах hard и
advisory — по приросту времени в пути, при равенстве пробега.
"""

from collections.abc import Iterator
from time import perf_counter

from app.planning.metrics import calculate_metrics, route_norm_excess_minutes
from app.planning.objective import plan_objective_key
from app.planning.policies import (
    request_priority_class,
    travel_norm_is_penalized,
    travel_norm_max_factor,
)
from app.planning.reasons import make_unassigned
from app.planning.schedule import calculate_route
from app.planning.validation import validate_plan
from app.schemas import (
    EngineerInput,
    EngineerRoute,
    PlanningInput,
    PlanningResult,
    ServiceRequestInput,
    UnassignedRequest,
)
from app.schemas.domain import coordinate_quality

# Длина отрезка, который переносится или разворачивается целиком.
MAX_SEGMENT = 3
# Доля лимита времени, зарезервированная на финальную попытку пристроить отказы.
FINAL_ATTEMPT_SHARE = 0.1

Proposal = dict[str, list[ServiceRequestInput]]


def _request_order(
    request: ServiceRequestInput,
    engineers: list[EngineerInput],
) -> tuple[int, int, object, int]:
    eligible_count = sum(
        request.required_skill in engineer.skills
        and (request.required_transport is None or request.required_transport == engineer.transport)
        for engineer in engineers
    )
    priority_rank = {
        "emergency": 0,
        "connection": 1,
        "routine": 2,
    }
    return (
        priority_rank[request_priority_class(request).value],
        eligible_count,
        request.window_end,
        request.input_order,
    )


class _Search:
    """Состояние поиска: назначения, кэш маршрутов, отказы и значение цели."""

    def __init__(self, engineers: list[EngineerInput], buffer_minutes: int = 0) -> None:
        self.engineers = engineers
        self.buffer_minutes = buffer_minutes
        # Штрафуемое превышение бывает только в soft с множителем больше 1. В остальных
        # случаях его не считаем, чтобы ранжирование вставок и скорость перебора были как
        # в hard: иначе soft ×1.0 медленнее и на границе лимита времени расходится с hard.
        # В advisory превышение есть, но цель его не штрафует — ранжировать по нему нельзя.
        self.count_excess = travel_norm_is_penalized() and travel_norm_max_factor() > 1.0
        self.engineer_by_id = {engineer.id: engineer for engineer in engineers}
        self.assigned: Proposal = {engineer.id: [] for engineer in engineers}
        self.routes: dict[str, EngineerRoute] = {}
        for engineer in engineers:
            route = calculate_route(engineer, [], buffer_minutes)
            if route is None:
                raise ValueError(f"пустой маршрут недопустим: {engineer.id}")
            self.routes[engineer.id] = route
        self.unassigned_requests: list[ServiceRequestInput] = []
        self.unassigned: list[UnassignedRequest] = []
        self.objective = self.evaluate({})

    def route_list(
        self,
        replacement: dict[str, EngineerRoute] | None = None,
    ) -> list[EngineerRoute]:
        chosen = replacement or {}
        return [chosen.get(engineer.id, self.routes[engineer.id]) for engineer in self.engineers]

    def evaluate(self, replacement: dict[str, EngineerRoute]) -> tuple[int, ...]:
        return plan_objective_key(
            calculate_metrics(self.route_list(replacement), self.unassigned)
        )

    def route_excess(self, engineer_id: str) -> int:
        """Минуты дороги сверх норматива в текущем маршруте бригады (0 в режиме hard)."""
        if not self.count_excess:
            return 0
        return route_norm_excess_minutes(self.routes[engineer_id])

    def apply(self, proposal: Proposal) -> bool:
        """Принимает ход, только если цель строго улучшилась."""
        replacement: dict[str, EngineerRoute] = {}
        for engineer_id, sequence in proposal.items():
            route = calculate_route(
                self.engineer_by_id[engineer_id], sequence, self.buffer_minutes
            )
            if route is None:
                return False
            replacement[engineer_id] = route
        if self.evaluate(replacement) >= self.objective:
            return False
        for engineer_id, sequence in proposal.items():
            self.assigned[engineer_id] = sequence
            self.routes[engineer_id] = replacement[engineer_id]
        self.objective = self.evaluate({})
        return True

    def set_unassigned(self, requests: list[ServiceRequestInput]) -> None:
        self.unassigned_requests = requests
        self.unassigned = [
            make_unassigned(request, self.engineers, fallback="not_selected_within_limit")
            for request in requests
        ]
        self.objective = self.evaluate({})


def _cheapest_in_route(
    engineer: EngineerInput,
    sequence: list[ServiceRequestInput],
    request: ServiceRequestInput,
    buffer_minutes: int = 0,
    *,
    count_excess: bool = False,
) -> tuple[int, int, int, int] | None:
    """(минуты сверх норматива, время в пути, пробег маршрута со вставкой, позиция).

    Порядок тот же, что в ключе сравнения планов: минуты превышения норматива, затем
    время в пути, затем километры. При `count_excess=False` (режимы hard и advisory)
    минуты превышения всегда 0.
    """
    best: tuple[int, int, int, int] | None = None
    for position in range(len(sequence) + 1):
        route = calculate_route(
            engineer,
            [*sequence[:position], request, *sequence[position:]],
            buffer_minutes,
        )
        if route is None:
            continue
        excess = route_norm_excess_minutes(route) if count_excess else 0
        candidate = (excess, route.travel_minutes, route.distance_meters, position)
        if best is None or candidate < best:
            best = candidate
    return best


def _best_insertion(search: _Search, request: ServiceRequestInput) -> tuple[str, int] | None:
    """Самая дешёвая допустимая вставка: прирост минут превышения, времени в пути, пробега."""
    best: tuple[tuple[int, int, int, int], int, int] | None = None
    best_engineer = ""
    for engineer in search.engineers:
        current = search.assigned[engineer.id]
        placement = _cheapest_in_route(
            engineer,
            current,
            request,
            search.buffer_minutes,
            count_excess=search.count_excess,
        )
        if placement is None:
            continue
        excess, travel, distance, position = placement
        route = search.routes[engineer.id]
        ranked = (
            (
                excess - search.route_excess(engineer.id),
                int(not current),
                travel - route.travel_minutes,
                distance - route.distance_meters,
            ),
            engineer.input_order,
            position,
        )
        if best is None or ranked < best:
            best = ranked
            best_engineer = engineer.id
    if best is None:
        return None
    return best_engineer, best[2]


def _insert(search: _Search, request: ServiceRequestInput) -> bool:
    """Ставит заявку в найденное место и обновляет кэш маршрута."""
    insertion = _best_insertion(search, request)
    if insertion is None:
        return False
    engineer_id, position = insertion
    current = search.assigned[engineer_id]
    sequence = [*current[:position], request, *current[position:]]
    route = calculate_route(
        search.engineer_by_id[engineer_id], sequence, search.buffer_minutes
    )
    if route is None:
        return False
    search.assigned[engineer_id] = sequence
    search.routes[engineer_id] = route
    return True


def _ejection_insert(search: _Search, request: ServiceRequestInput, deadline: float) -> bool:
    """Освобождает место для отказанной заявки, переселяя один мешающий визит.

    Варианты выселения перебираются от самого дешёвого по приросту минут сверх
    норматива дороги, затем времени в пути и пробега.
    """
    candidates: list[tuple[int, int, int, int, str, int, int]] = []
    for engineer in search.engineers:
        sequence = search.assigned[engineer.id]
        base_travel = search.routes[engineer.id].travel_minutes
        base_distance = search.routes[engineer.id].distance_meters
        base_excess = search.route_excess(engineer.id)
        for ejected in range(len(sequence)):
            if perf_counter() >= deadline:
                return False
            reduced = [*sequence[:ejected], *sequence[ejected + 1 :]]
            placement = _cheapest_in_route(
                engineer, reduced, request, count_excess=search.count_excess
            )
            if placement is None:
                continue
            excess, travel, distance, position = placement
            candidates.append(
                (
                    excess - base_excess,
                    travel - base_travel,
                    distance - base_distance,
                    engineer.input_order,
                    engineer.id,
                    ejected,
                    position,
                )
            )
    for _, _, _, _, engineer_id, ejected, position in sorted(candidates):
        if perf_counter() >= deadline:
            return False
        sequence = search.assigned[engineer_id]
        reduced = [*sequence[:ejected], *sequence[ejected + 1 :]]
        filled = [*reduced[:position], request, *reduced[position:]]
        route = calculate_route(
            search.engineer_by_id[engineer_id], filled, search.buffer_minutes
        )
        if route is None:
            continue
        saved_route = search.routes[engineer_id]
        search.assigned[engineer_id] = filled
        search.routes[engineer_id] = route
        if _insert(search, sequence[ejected]):
            return True
        search.assigned[engineer_id] = sequence
        search.routes[engineer_id] = saved_route
    return False


def _retry_unassigned(search: _Search, deadline: float, *, eject: bool = False) -> bool:
    """Повторная попытка для отказанных заявок: перестановки освобождают окна."""
    if not search.unassigned_requests:
        return False
    rejected: list[ServiceRequestInput] = []
    placed = False
    for request in search.unassigned_requests:
        if perf_counter() >= deadline:
            rejected.append(request)
            continue
        if _insert(search, request) or (eject and _ejection_insert(search, request, deadline)):
            placed = True
            continue
        rejected.append(request)
    if placed:
        search.set_unassigned(rejected)
    return placed


def _shrink_fleet(search: _Search, deadline: float) -> bool:
    """Пробует расселить визиты самой лёгкой бригады: занятых бригад станет меньше."""
    order = sorted(
        (engineer for engineer in search.engineers if search.assigned[engineer.id]),
        key=lambda engineer: (len(search.assigned[engineer.id]), engineer.input_order),
    )
    for engineer in order:
        if perf_counter() >= deadline:
            return False
        empty = calculate_route(engineer, [], search.buffer_minutes)
        if empty is None:
            continue
        before = search.objective
        saved_assigned = {key: list(value) for key, value in search.assigned.items()}
        saved_routes = dict(search.routes)
        moved = search.assigned[engineer.id]
        search.assigned[engineer.id] = []
        search.routes[engineer.id] = empty
        if all(_insert(search, request) for request in moved):
            search.objective = search.evaluate({})
            if search.objective < before:
                return True
        search.assigned = saved_assigned
        search.routes = saved_routes
        search.objective = before
    return False


def _cross_moves(
    source: list[ServiceRequestInput],
    target: list[ServiceRequestInput],
    source_id: str,
    target_id: str,
) -> Iterator[Proposal]:
    """Перенос отрезка в другой маршрут и обмен двумя визитами между маршрутами."""
    for length in range(1, MAX_SEGMENT + 1):
        for start in range(len(source) - length + 1):
            segment = source[start : start + length]
            rest = [*source[:start], *source[start + length :]]
            for position in range(len(target) + 1):
                yield {
                    source_id: rest,
                    target_id: [*target[:position], *segment, *target[position:]],
                }
    for source_position, source_request in enumerate(source):
        for target_position, target_request in enumerate(target):
            moved_source = list(source)
            moved_target = list(target)
            moved_source[source_position] = target_request
            moved_target[target_position] = source_request
            yield {source_id: moved_source, target_id: moved_target}


def _inner_moves(sequence: list[ServiceRequestInput], engineer_id: str) -> Iterator[Proposal]:
    """Перестановка отрезка (or-opt) и разворот отрезка (2-opt) внутри маршрута."""
    for length in range(1, MAX_SEGMENT + 1):
        for start in range(len(sequence) - length + 1):
            segment = sequence[start : start + length]
            rest = [*sequence[:start], *sequence[start + length :]]
            for position in range(len(rest) + 1):
                if position == start:
                    continue
                yield {engineer_id: [*rest[:position], *segment, *rest[position:]]}
    for start in range(len(sequence) - 1):
        for end in range(start + 2, len(sequence) + 1):
            yield {
                engineer_id: [
                    *sequence[:start],
                    *sequence[start:end][::-1],
                    *sequence[end:],
                ]
            }


def _scan_pair(search: _Search, source_id: str, target_id: str, deadline: float) -> bool:
    """Просматривает соседство одной пары бригад до первого улучшения."""
    source = search.assigned[source_id]
    target = search.assigned[target_id]
    moves = (
        _inner_moves(source, source_id)
        if source_id == target_id
        else _cross_moves(source, target, source_id, target_id)
    )
    for proposal in moves:
        if perf_counter() >= deadline:
            return False
        if search.apply(proposal):
            return True
    return False


def _local_search(search: _Search, deadline: float) -> None:
    """Круговой обход пар бригад; после улучшения скан идёт дальше, а не с начала."""
    pairs = [
        (source.id, target.id) for source in search.engineers for target in search.engineers
    ]
    cursor = 0
    stagnation = 0
    while stagnation < len(pairs) and perf_counter() < deadline:
        source_id, target_id = pairs[cursor]
        cursor = (cursor + 1) % len(pairs)
        if not _scan_pair(search, source_id, target_id, deadline):
            stagnation += 1
            continue
        stagnation = 0
        _retry_unassigned(search, deadline)


def build_insertion(
    planning_input: PlanningInput,
    time_limit_seconds: int = 10,
) -> PlanningResult:
    started_at = perf_counter()
    budget = max(0.05, min(time_limit_seconds, 20))
    deadline = started_at + budget
    engineers = sorted(planning_input.engineers, key=lambda item: item.input_order)
    requests = sorted(
        planning_input.requests,
        key=lambda item: _request_order(item, engineers),
    )
    search = _Search(engineers, planning_input.buffer_minutes)
    rejected: list[ServiceRequestInput] = []
    for request in requests:
        if not _insert(search, request):
            rejected.append(request)
    search.set_unassigned(rejected)

    # Сначала добираем отказы выселением: число отказов в цели старше дороги,
    # а оставшееся время локальный поиск тратит на сокращение времени в пути и пробега.
    search_deadline = deadline - budget * FINAL_ATTEMPT_SHARE
    _retry_unassigned(search, search_deadline, eject=True)
    exhausted = False
    while perf_counter() < search_deadline:
        _local_search(search, search_deadline)
        if _retry_unassigned(search, search_deadline, eject=True):
            continue
        if not _shrink_fleet(search, search_deadline):
            # исчерпано, только если вышли до срока, а не потому что срок наступил
            exhausted = perf_counter() < search_deadline
            break
    _retry_unassigned(search, deadline, eject=True)

    routes = search.route_list()
    result = PlanningResult(
        algorithm="insertion_ls_v1",
        solution_status="feasible",
        termination_reason="local_neighborhood_exhausted" if exhausted else "time_limit",
        elapsed_ms=max(0, round((perf_counter() - started_at) * 1000)),
        route_estimation_method=planning_input.route_estimation_method,
        coordinate_quality=coordinate_quality(
            request.coordinate_source for request in requests
        ),
        routes=routes,
        unassigned=search.unassigned,
        metrics=calculate_metrics(routes, search.unassigned),
        constraint_violations_count=0,
    )
    violations = validate_plan(planning_input, result)
    if violations:
        raise ValueError(f"insertion validator rejected result: {violations}")
    return result
