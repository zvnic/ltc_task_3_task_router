from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from app.core.config import get_settings
from app.planning import (
    RouteSolver,
    algorithm_catalog,
    build_baseline,
    build_insertion,
    build_optimized,
    build_replan_pair,
    evaluate_results,
    experiment_metadata,
    optimizer,
    schedule,
    validate_plan,
)
from app.planning.metrics import calculate_metrics
from app.planning.objective import plan_objective_key
from app.planning.optimizer import (
    _minute_of_day,
    calculate_objective_weights,
    distance_edge_cost,
    lexicographic_edge_cost,
)
from app.planning.registry import SOLVERS
from app.planning.replan import (
    NORMAL_INSERTION_POLICY_HARD,
    NORMAL_INSERTION_POLICY_SOFT,
    _anchor_engineers,
    _protected_prefixes,
    _stabilize_tail,
    normal_insertion_policy,
)
from app.planning.routing import (
    PREVIOUS_ROUTE_ESTIMATION_METHOD,
    ROUTE_ESTIMATION_METHOD,
    effective_edge_metrics,
)
from app.planning.schedule import calculate_route, insert_request_preserving_visits
from app.schemas import (
    Coordinates,
    EngineerInput,
    PlanningInput,
    PlanningRequestEvent,
    PlanningResult,
    Priority,
    RequiredSkill,
    ServiceRequestInput,
    Transport,
    UrgentRequestEvent,
    apply_emergency_sla,
)


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"2026-08-17T{hour:02d}:{minute:02d}:00+03:00")


def engineer(
    identifier: str = "engineer-1",
    *,
    skills: list[RequiredSkill] | None = None,
    transport: Transport = Transport.CAR,
) -> EngineerInput:
    return EngineerInput(
        id=identifier,
        external_id=identifier,
        input_order=int(identifier[-1]) if identifier[-1].isdigit() else 0,
        name=identifier,
        start_location=Coordinates(latitude=55.70, longitude=37.70),
        shift_start=dt(8),
        shift_end=dt(18),
        skills=skills or [RequiredSkill.LOCAL],
        transport=transport,
    )


def service_request(
    identifier: str,
    order: int,
    start: int,
    end: int,
    *,
    duration: int = 60,
    expected_duration: int | None = None,
    skill: RequiredSkill = RequiredSkill.LOCAL,
    transport: Transport | None = None,
    priority: Priority = Priority.NORMAL,
    source_fields: dict[str, object] | None = None,
    required_equipment: dict[str, int] | None = None,
    completion_deadline: datetime | None = None,
) -> ServiceRequestInput:
    return ServiceRequestInput(
        id=identifier,
        external_id=identifier,
        input_order=order,
        address=f"Адрес {identifier}",
        district="Тест",
        coordinates=Coordinates(latitude=55.70 + order * 0.001, longitude=37.70),
        duration_minutes=duration,
        expected_duration_minutes=expected_duration,
        window_start=dt(start),
        window_end=dt(end),
        required_skill=skill,
        required_transport=transport,
        required_equipment=required_equipment or {},
        priority=priority,
        completion_deadline=completion_deadline,
        source_fields=source_fields or {},
    )


def planning(
    requests: list[ServiceRequestInput],
    engineers: list[EngineerInput],
    *,
    buffer_minutes: int = 0,
) -> PlanningInput:
    return PlanningInput(
        dataset_id="dataset",
        revision=1,
        planning_date=dt(8).date(),
        requests=requests,
        engineers=engineers,
        buffer_minutes=buffer_minutes,
    )


def test_solver_minutes_are_normalized_to_dataset_timezone() -> None:
    moscow_time = dt(14, 30)
    assert _minute_of_day(moscow_time, "Europe/Moscow") == 14 * 60 + 30
    assert _minute_of_day(moscow_time.astimezone(UTC), "Europe/Moscow") == 14 * 60 + 30


def test_registered_solvers_implement_common_contract() -> None:
    assert set(SOLVERS) == {"baseline_v1", "ortools_gls_v1", "insertion_ls_v1"}
    assert all(isinstance(solver, RouteSolver) for solver in SOLVERS.values())


def test_experiment_fingerprints_and_kpis_are_reproducible() -> None:
    input_data = planning(
        [
            service_request("request-1", 0, 8, 12),
            service_request("request-2", 1, 10, 14),
        ],
        [engineer()],
    )
    first = experiment_metadata(input_data, ["baseline_v1"], 1)
    second = experiment_metadata(input_data, ["baseline_v1"], 1)
    assert first == second
    changed_request = input_data.requests[0].model_copy(
        update={"coordinates": Coordinates(latitude=55.75, longitude=37.75)}
    )
    changed = input_data.model_copy(
        update={"requests": [changed_request, input_data.requests[1]]}
    )
    assert experiment_metadata(changed, ["baseline_v1"], 1)["matrix_sha256"] != first[
        "matrix_sha256"
    ]
    results = {"baseline_v1": build_baseline(input_data)}
    kpis = evaluate_results(input_data, results)["baseline_v1"]
    assert kpis["completion_rate_percent"] == 100
    assert kpis["distance_per_assigned_meters"] is not None
    assert kpis["validator_violations"] == 0


def test_objective_weights_preserve_lexicographic_business_priority() -> None:
    weights = calculate_objective_weights(83, 12, 250_000)
    assert weights["used_engineer"] > weights["travel_cost_bound"]
    route_bound = 12 * weights["used_engineer"] + weights["travel_cost_bound"]
    assert weights["routine_unassigned"] > route_bound
    routine_bound = 83 * weights["routine_unassigned"] + route_bound
    assert weights["connection_unassigned"] > routine_bound
    connection_bound = 83 * weights["connection_unassigned"] + routine_bound
    assert weights["emergency_unassigned"] > connection_bound
    assert weights["objective_upper_bound"] < 2**63
    assert "norm_excess_minute" not in weights


def test_objective_weights_put_norm_excess_minutes_between_crews_and_drops() -> None:
    plain = calculate_objective_weights(83, 12, 250_000)
    # 83 заявки с нормативом 20 мин при множителе 2,0: до 20 мин превышения на каждой
    bound = 83 * 20
    weights = calculate_objective_weights(83, 12, 250_000, norm_excess_bound_minutes=bound)
    assert weights["used_engineer"] == plain["used_engineer"]
    route_bound = 12 * weights["used_engineer"] + weights["travel_cost_bound"]
    # одна минута превышения дороже всех бригад и всей дороги
    assert weights["norm_excess_minute"] > route_bound
    norm_bound = bound * weights["norm_excess_minute"] + route_bound
    # один пропуск дороже любого допустимого превышения во всём плане
    assert weights["routine_unassigned"] > norm_bound
    routine_bound = 83 * weights["routine_unassigned"] + norm_bound
    assert weights["connection_unassigned"] > routine_bound
    assert weights["norm_excess_bound_minutes"] == bound
    assert weights["objective_upper_bound"] < 2**63
    # худший случай по пределам схемы (100 заявок, 15 бригад, норматив 20 мин,
    # множитель 10, плечо 110 км и 200 минут) ещё помещается в int64
    worst_edge = lexicographic_edge_cost(200, 110_000)
    worst = calculate_objective_weights(100, 15, worst_edge, norm_excess_bound_minutes=100 * 180)
    assert worst["objective_upper_bound"] < 2**63
    with pytest.raises(ValueError, match="int64"):
        calculate_objective_weights(100, 15, 10**9, norm_excess_bound_minutes=100 * 20)


def test_edge_cost_orders_by_minutes_then_kilometres() -> None:
    # минута дороги дороже любой разницы в километрах на одной дуге
    assert lexicographic_edge_cost(30, 5_000) < lexicographic_edge_cost(31, 1_000)
    assert lexicographic_edge_cost(90, 100_000) < lexicographic_edge_cost(91, 1_000)
    # при равных минутах решает пробег
    assert lexicographic_edge_cost(30, 4_000) < lexicographic_edge_cost(30, 9_000)


def test_optimizer_keeps_the_better_of_two_edge_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    input_data = planning(north_south_requests(), [engineer("engineer-1"), engineer("engineer-2")])
    base = build_baseline(input_data)
    calls: list[tuple[int, object]] = []

    def fake_solve(
        planning_input: PlanningInput, time_limit_milliseconds: int, edge_cost: object
    ) -> PlanningResult:
        calls.append((time_limit_milliseconds, edge_cost))
        # проход по метрам нашёл план короче по времени — он и должен победить
        minutes = 50 if edge_cost is distance_edge_cost else 60
        metrics = base.metrics.model_copy(update={"total_travel_minutes": minutes})
        return base.model_copy(
            update={"algorithm": "ortools", "metrics": metrics, "objective_weights": {}}
        )

    monkeypatch.setattr(optimizer, "_solve", fake_solve)
    result = build_optimized(input_data, time_limit_seconds=10)

    assert calls == [(5_000, lexicographic_edge_cost), (5_000, distance_edge_cost)]
    assert result.metrics.total_travel_minutes == 50
    assert result.objective_weights["arc_cost_by_travel_minutes"] == 0


def test_window_limits_start_not_service_end() -> None:
    request = service_request("request-1", 0, 10, 12, duration=40)
    plan = build_baseline(planning([request], [engineer()]))
    stop = plan.routes[0].stops[0]
    assert stop.service_start_at == dt(10)
    assert stop.service_end_at == dt(10, 40)
    assert validate_plan(planning([request], [engineer()]), plan) == []


def test_safe_duration_protects_following_visit_and_exposes_expected_finish() -> None:
    first = service_request(
        "first",
        0,
        10,
        10,
        duration=60,
        expected_duration=35,
    )
    second = service_request("second", 1, 11, 11, duration=30).model_copy(
        update={"coordinates": first.coordinates}
    )
    input_data = planning([first, second], [engineer()])

    result = build_baseline(input_data)

    first_stop, second_stop = result.routes[0].stops
    assert first_stop.expected_service_end_at == dt(10, 35)
    assert first_stop.service_end_at == dt(11)
    assert first_stop.reserve_minutes == 25
    assert second_stop.service_start_at == dt(11)
    assert validate_plan(input_data, result) == []


def test_service_may_finish_after_window_but_not_after_shift() -> None:
    allowed = service_request("allowed", 0, 10, 10, duration=60)
    too_late = service_request("too-late", 1, 18, 18, duration=30)
    result = build_baseline(planning([allowed, too_late], [engineer()]))
    assert result.routes[0].stops[0].service_end_at == dt(11)
    assert result.unassigned[0].reason_code == "no_feasible_time"


def test_baseline_preserves_order_and_reports_missing_transport() -> None:
    first = service_request("request-1", 0, 12, 14)
    second = service_request("request-2", 1, 10, 12)
    transport_limited = service_request("request-3", 2, 10, 14, transport=Transport.BICYCLE)
    result = build_baseline(planning([first, second, transport_limited], [engineer()]))
    assert [stop.request_id for stop in result.routes[0].stops] == ["request-1"]
    reasons = {item.request_id: item.reason_code for item in result.unassigned}
    assert reasons["request-2"] == "baseline_order_conflict"
    assert reasons["request-3"] == "no_required_transport"


def test_missing_skill_is_explicit() -> None:
    request = service_request("emergency", 0, 10, 12, skill=RequiredSkill.EMERGENCY)
    result = build_baseline(planning([request], [engineer()]))
    assert result.unassigned[0].reason_code == "no_required_skill"


def test_walking_engineer_is_not_assigned_a_cross_city_request() -> None:
    request = service_request("far", 0, 10, 16).model_copy(
        update={"coordinates": Coordinates(latitude=55.76, longitude=37.70)}
    )
    walker = engineer(transport=Transport.WALKING)

    for result in (
        build_baseline(planning([request], [walker])),
        build_insertion(planning([request], [walker]), time_limit_seconds=1),
        build_optimized(planning([request], [walker]), time_limit_seconds=1),
    ):
        assert not result.routes[0].stops
        assert result.unassigned[0].reason_code == "walking_leg_too_long"
        assert validate_plan(planning([request], [walker]), result) == []


def test_solver_cannot_chain_two_individually_near_but_distant_walking_visits() -> None:
    north = service_request("north", 0, 9, 16).model_copy(
        update={"coordinates": Coordinates(latitude=55.707, longitude=37.70)}
    )
    south = service_request("south", 1, 9, 16).model_copy(
        update={"coordinates": Coordinates(latitude=55.693, longitude=37.70)}
    )
    walker = engineer(transport=Transport.WALKING)
    input_data = planning([north, south], [walker])

    result = build_optimized(input_data, time_limit_seconds=1)

    assert result.metrics.assigned_count == 1
    assert result.metrics.unassigned_count == 1
    assert validate_plan(input_data, result) == []


def test_validator_rejects_walking_leg_over_limit() -> None:
    near = service_request("near", 0, 9, 16)
    walker = engineer(transport=Transport.WALKING)
    valid_input = planning([near], [walker])
    result = build_baseline(valid_input)
    far = near.model_copy(
        update={"coordinates": Coordinates(latitude=55.76, longitude=37.70)}
    )

    violations = validate_plan(planning([far], [walker]), result)

    assert "walking_leg_too_long:near" in violations


def test_bicycle_crew_is_not_sent_beyond_the_leg_limit() -> None:
    # 11 км по прямой — 13,4 расчётных км, больше предела 12 км
    far = service_request("far", 0, 10, 16).model_copy(
        update={"coordinates": Coordinates(latitude=55.80, longitude=37.70)}
    )
    input_data = planning([far], [engineer(transport=Transport.BICYCLE)])

    for result in all_solver_results(input_data):
        assert result.metrics.assigned_count == 0
        assert result.unassigned[0].reason_code == "bicycle_leg_too_long"
        assert "12 км" in result.unassigned[0].explanation
        assert validate_plan(input_data, result) == []


def test_bicycle_crew_takes_a_leg_within_the_limit() -> None:
    # 6,7 км по прямой — 8 расчётных км
    near = service_request("near", 0, 10, 16).model_copy(
        update={"coordinates": Coordinates(latitude=55.76, longitude=37.70)}
    )
    input_data = planning([near], [engineer(transport=Transport.BICYCLE)])

    for result in all_solver_results(input_data):
        assert result.metrics.assigned_count == 1
        assert result.routes[0].stops[0].facts["leg_limit_meters"] == 12_000
        assert validate_plan(input_data, result) == []


def test_validator_checks_bicycle_leg_only_for_visits_planned_under_the_limit() -> None:
    near = service_request("near", 0, 9, 16)
    cyclist = engineer(transport=Transport.BICYCLE)
    result = build_baseline(planning([near], [cyclist]))
    far = near.model_copy(
        update={"coordinates": Coordinates(latitude=55.80, longitude=37.70)}
    )
    far_input = planning([far], [cyclist])

    assert "bicycle_leg_too_long:near" in validate_plan(far_input, result)

    # визит, выданный до предела, — без отметки: его плечо остаётся обещанным
    stop = result.routes[0].stops[0]
    legacy_stop = stop.model_copy(
        update={"facts": {k: v for k, v in stop.facts.items() if k != "leg_limit_meters"}}
    )
    legacy = result.model_copy(
        update={"routes": [result.routes[0].model_copy(update={"stops": [legacy_stop]})]}
    )
    assert "bicycle_leg_too_long:near" not in validate_plan(far_input, legacy)


@pytest.fixture
def hard_norm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Норматив дороги как запрет плеча — поведение до 24.09."""
    monkeypatch.setenv("TRAVEL_NORM_MODE", "hard")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def soft_norm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Норматив дороги как штрафуемое превышение с потолком «норматив × 2,0»."""
    monkeypatch.setenv("TRAVEL_NORM_MODE", "soft")
    monkeypatch.setenv("TRAVEL_NORM_MAX_FACTOR", "2.0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def use_norm(monkeypatch: pytest.MonkeyPatch, mode: str, factor: str = "2.0") -> None:
    """Переключает режим норматива и множитель потолка посреди теста."""
    monkeypatch.setenv("TRAVEL_NORM_MODE", mode)
    monkeypatch.setenv("TRAVEL_NORM_MAX_FACTOR", factor)
    get_settings.cache_clear()


def with_travel_norm(request: ServiceRequestInput, minutes: int) -> ServiceRequestInput:
    return request.model_copy(
        update={
            "source_fields": {
                "_enrichment": {
                    "norm_id": "local_repair",
                    "norm_version": "test",
                    "travel_norm_minutes": minutes,
                    "work_class": "local",
                }
            }
        }
    )


def strict_travel_input() -> PlanningInput:
    """Одна заявка в 111 м от старта: дорога 3 мин при нормативе 2 мин."""
    request = with_travel_norm(
        service_request("strict-travel", 0, 9, 16).model_copy(
            update={"coordinates": Coordinates(latitude=55.701, longitude=37.70)}
        ),
        2,
    )
    return planning([request], [engineer(transport=Transport.CAR)])


# Север и юг от общего старта бригад: плечи «старт → заявка» по 8 мин укладываются
# в норматив 10 мин, а переезд «север → юг» (13 мин) его превышает.
NORTH = Coordinates(latitude=55.7135, longitude=37.70)
SOUTH = Coordinates(latitude=55.6865, longitude=37.70)
SPLIT_NORM_MINUTES = 10


def north_south_requests() -> list[ServiceRequestInput]:
    north = service_request("north", 0, 9, 17).model_copy(update={"coordinates": NORTH})
    south = service_request("south", 1, 9, 17).model_copy(update={"coordinates": SOUTH})
    return [
        with_travel_norm(north, SPLIT_NORM_MINUTES),
        with_travel_norm(south, SPLIT_NORM_MINUTES),
    ]


def test_north_south_fixture_splits_legs_around_the_norm() -> None:
    start = engineer().start_location
    assert effective_edge_metrics(start, NORTH, Transport.CAR)[1] <= SPLIT_NORM_MINUTES
    assert effective_edge_metrics(start, SOUTH, Transport.CAR)[1] <= SPLIT_NORM_MINUTES
    assert effective_edge_metrics(NORTH, SOUTH, Transport.CAR)[1] > SPLIT_NORM_MINUTES
    assert effective_edge_metrics(SOUTH, NORTH, Transport.CAR)[1] > SPLIT_NORM_MINUTES


def all_solver_results(input_data: PlanningInput) -> list[PlanningResult]:
    return [
        build_baseline(input_data),
        build_insertion(input_data, time_limit_seconds=1),
        build_optimized(input_data, time_limit_seconds=1),
    ]


@pytest.mark.usefixtures("hard_norm")
def test_hard_mode_solvers_drop_a_leg_over_travel_norm() -> None:
    input_data = strict_travel_input()

    for result in all_solver_results(input_data):
        assert result.metrics.assigned_count == 0
        assert result.metrics.norm_violation_count == 0
        assert result.unassigned[0].reason_code == "travel_norm_exceeded"
        assert "3 мин" in result.unassigned[0].explanation
        assert validate_plan(input_data, result) == []


@pytest.mark.usefixtures("hard_norm")
def test_hard_mode_single_crew_cannot_chain_north_and_south() -> None:
    input_data = planning(north_south_requests(), [engineer()])

    for result in all_solver_results(input_data):
        assert result.metrics.assigned_count == 1
        assert result.metrics.norm_violation_count == 0
        assert validate_plan(input_data, result) == []


@pytest.mark.usefixtures("soft_norm")
def test_soft_mode_solvers_assign_a_leg_over_norm_and_count_it() -> None:
    input_data = strict_travel_input()

    for result in all_solver_results(input_data):
        assert result.metrics.assigned_count == 1
        assert result.metrics.unassigned_count == 0
        assert result.metrics.norm_violation_count == 1
        assert result.metrics.norm_excess_minutes == 1
        stop = result.routes[0].stops[0]
        assert stop.travel_minutes_from_previous == 3
        assert stop.facts["travel_norm_minutes"] == 2
        assert stop.facts["travel_norm_exceeded"] is True
        assert stop.facts["travel_norm_excess_minutes"] == 1
        assert "travel_norm_exceeded" in stop.explanation_codes
        # ступень норматива в ключе — минуты превышения, а не число визитов
        assert plan_objective_key(result.metrics)[3] == 1
        assert validate_plan(input_data, result) == []


@pytest.mark.usefixtures("soft_norm")
def test_soft_mode_single_crew_serves_both_with_one_violation() -> None:
    input_data = planning(north_south_requests(), [engineer()])

    for result in all_solver_results(input_data):
        assert result.metrics.assigned_count == 2
        assert result.metrics.norm_violation_count == 1
        # переезд север → юг 13 мин при нормативе 10
        assert result.metrics.norm_excess_minutes == 3
        assert validate_plan(input_data, result) == []


@pytest.mark.usefixtures("soft_norm")
def test_soft_mode_prefers_extra_crew_over_norm_violation() -> None:
    input_data = planning(
        north_south_requests(),
        [engineer("engineer-1"), engineer("engineer-2")],
    )

    for result in (
        build_insertion(input_data, time_limit_seconds=1),
        build_optimized(input_data, time_limit_seconds=2),
    ):
        assert result.metrics.assigned_count == 2
        assert result.metrics.norm_violation_count == 0
        assert result.metrics.used_engineers_count == 2
        assert validate_plan(input_data, result) == []
    # baseline честно остаётся наивным: первый допустимый инженер, пусть и с превышением
    baseline = build_baseline(input_data)
    assert baseline.metrics.used_engineers_count == 1
    assert baseline.metrics.norm_violation_count == 1


@pytest.mark.usefixtures("soft_norm")
def test_soft_mode_ortools_prices_excess_minutes_between_crews_and_drops() -> None:
    input_data = planning(north_south_requests(), [engineer()])

    result = build_optimized(input_data, time_limit_seconds=1)

    weights = result.objective_weights
    route_bound = weights["used_engineer"] + weights["travel_cost_bound"]
    # две заявки с нормативом 10 мин и потолком 20: до 10 мин превышения на каждой
    assert weights["norm_excess_bound_minutes"] == 20
    assert weights["norm_excess_minute"] > route_bound
    assert weights["routine_unassigned"] > 20 * weights["norm_excess_minute"] + route_bound
    assert weights["objective_upper_bound"] < 2**63


@pytest.mark.usefixtures("hard_norm")
def test_hard_mode_ortools_weights_have_no_norm_tier() -> None:
    input_data = planning(north_south_requests(), [engineer()])

    result = build_optimized(input_data, time_limit_seconds=1)

    assert "norm_excess_minute" not in result.objective_weights


@pytest.mark.usefixtures("soft_norm")
def test_validator_rejects_understated_norm_violation_count() -> None:
    input_data = strict_travel_input()
    result = build_baseline(input_data)
    assert result.metrics.norm_violation_count == 1

    lying_metrics = result.model_copy(
        update={"metrics": result.metrics.model_copy(update={"norm_violation_count": 0})}
    )
    violations = validate_plan(input_data, lying_metrics)
    assert "norm_violation_count_mismatch" in violations
    assert "plan_metrics_mismatch" in violations

    # Подделаны и факты визита, и метрики — согласованно между собой. Сверка метрик
    # с фактами молчит, но независимый счёт валидатора ловит подлог.
    stop = result.routes[0].stops[0]
    forged_facts = {**stop.facts, "travel_norm_minutes": None, "travel_norm_exceeded": False}
    forged_stop = stop.model_copy(update={"facts": forged_facts})
    forged_route = result.routes[0].model_copy(update={"stops": [forged_stop]})
    forged = result.model_copy(
        update={
            "routes": [forged_route],
            "metrics": calculate_metrics([forged_route], result.unassigned),
        }
    )
    assert forged.metrics.norm_violation_count == 0
    violations = validate_plan(input_data, forged)
    assert "plan_metrics_mismatch" not in violations
    assert "norm_violation_count_mismatch" in violations
    assert "travel_norm_flag_mismatch:strict-travel" in violations


def test_validator_in_hard_mode_rejects_a_soft_plan_with_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAVEL_NORM_MODE", "soft")
    get_settings.cache_clear()
    try:
        input_data = strict_travel_input()
        soft_plan = build_baseline(input_data)
        assert validate_plan(input_data, soft_plan) == []
        monkeypatch.setenv("TRAVEL_NORM_MODE", "hard")
        get_settings.cache_clear()
        assert "travel_norm_exceeded:strict-travel" in validate_plan(input_data, soft_plan)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("mode", ["advisory", "soft", "hard"])
def test_walking_leg_limit_stays_a_prohibition_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("TRAVEL_NORM_MODE", mode)
    get_settings.cache_clear()
    try:
        far = with_travel_norm(
            service_request("far", 0, 10, 16).model_copy(
                update={"coordinates": Coordinates(latitude=55.76, longitude=37.70)}
            ),
            600,
        )
        input_data = planning([far], [engineer(transport=Transport.WALKING)])
        for result in all_solver_results(input_data):
            assert result.metrics.assigned_count == 0
            assert result.unassigned[0].reason_code == "walking_leg_too_long"
            assert validate_plan(input_data, result) == []
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("soft_norm")
def test_soft_gap_insertion_recounts_the_following_leg() -> None:
    # Базовый маршрут «север → юг» превышает норматив на втором плече. Вставка между
    # ними заявки у старта делит длинный переезд на два коротких: пометка превышения
    # у следующего визита должна сняться, а не остаться от прежнего плеча.
    north, south = north_south_requests()
    north = north.model_copy(update={"window_start": dt(9), "window_end": dt(9)})
    south = south.model_copy(update={"window_start": dt(14), "window_end": dt(14)})
    middle = with_travel_norm(
        service_request("middle", 2, 8, 18).model_copy(
            update={"coordinates": engineer().start_location}
        ),
        SPLIT_NORM_MINUTES,
    )
    worker = engineer()
    route = calculate_route(worker, [north, south])
    assert route is not None
    assert route.stops[1].facts["travel_norm_exceeded"] is True

    inserted = insert_request_preserving_visits(worker, route, [north, south], middle, 1)

    assert inserted is not None
    assert [stop.request_id for stop in inserted.stops] == ["north", "middle", "south"]
    following = inserted.stops[2]
    assert following.facts["travel_norm_exceeded"] is False
    assert "travel_norm_exceeded" not in following.explanation_codes
    assert following.service_start_at == route.stops[1].service_start_at
    input_data = planning([north, south, middle], [worker])
    plan = build_baseline(planning([north, south], [worker])).model_copy(
        update={
            "routes": [inserted],
            "unassigned": [],
            "metrics": calculate_metrics([inserted], []),
        }
    )
    assert plan.metrics.norm_violation_count == 0
    assert validate_plan(input_data, plan) == []


@pytest.mark.usefixtures("soft_norm")
def test_soft_normal_event_inserts_over_norm_instead_of_refusing() -> None:
    north = with_travel_norm(
        service_request("north", 0, 9, 9, duration=60).model_copy(update={"coordinates": NORTH}),
        SPLIT_NORM_MINUTES,
    )
    input_data = planning([north], [engineer()])
    base = build_baseline(input_data)
    incoming = with_travel_norm(
        service_request("south", 1, 11, 16, duration=60).model_copy(
            update={"coordinates": SOUTH}
        ),
        SPLIT_NORM_MINUTES,
    )
    event = PlanningRequestEvent(
        event_id="soft-normal",
        idempotency_key="soft-normal",
        event_time=dt(8),
        expected_revision=1,
        request=incoming,
    )

    _baseline, result, _protected = build_replan_pair(input_data, base, event, 1)

    assert [stop.request_id for stop in result.routes[0].stops] == ["north", "south"]
    assert result.metrics.norm_violation_count == 1
    assert result.routes[0].stops[1].facts["travel_norm_exceeded"] is True


@pytest.mark.usefixtures("hard_norm")
def test_hard_normal_event_refuses_a_gap_over_norm() -> None:
    north = with_travel_norm(
        service_request("north", 0, 9, 9, duration=60).model_copy(update={"coordinates": NORTH}),
        SPLIT_NORM_MINUTES,
    )
    input_data = planning([north], [engineer()])
    base = build_baseline(input_data)
    incoming = with_travel_norm(
        service_request("south", 1, 11, 16, duration=60).model_copy(
            update={"coordinates": SOUTH}
        ),
        SPLIT_NORM_MINUTES,
    )
    event = PlanningRequestEvent(
        event_id="hard-normal",
        idempotency_key="hard-normal",
        event_time=dt(8),
        expected_revision=1,
        request=incoming,
    )

    _baseline, result, _protected = build_replan_pair(input_data, base, event, 1)

    assert [stop.request_id for stop in result.routes[0].stops] == ["north"]
    assert result.unassigned[0].request_id == "south"
    assert result.metrics.norm_violation_count == 0


# --- потолок превышения и штраф за минуты (TRAVEL_NORM_MAX_FACTOR) ------------------


def result_signature(result: PlanningResult) -> tuple[object, ...]:
    """Всё, что решает план, без времени расчёта: маршруты, отказы, метрики, веса."""
    return (
        [
            (
                route.engineer_id,
                [(stop.request_id, stop.service_start_at) for stop in route.stops],
            )
            for route in result.routes
        ],
        [(item.request_id, item.reason_code, item.explanation) for item in result.unassigned],
        result.metrics,
        result.objective_weights,
    )


def factor_one_inputs() -> list[PlanningInput]:
    return [
        strict_travel_input(),
        planning(north_south_requests(), [engineer()]),
        planning(north_south_requests(), [engineer("engineer-1"), engineer("engineer-2")]),
        car_or_walker_input(),
    ]


def test_soft_mode_with_factor_one_behaves_exactly_like_hard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        for input_data in factor_one_inputs():
            use_norm(monkeypatch, "hard")
            hard = [result_signature(result) for result in all_solver_results(input_data)]
            use_norm(monkeypatch, "soft", "1.0")
            soft_results = all_solver_results(input_data)
            assert [result_signature(result) for result in soft_results] == hard
            for result in soft_results:
                assert result.metrics.norm_excess_minutes == 0
                assert "norm_excess_minute" not in result.objective_weights
                assert validate_plan(input_data, result) == []
    finally:
        get_settings.cache_clear()


def test_soft_mode_forbids_a_leg_over_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = engineer()
    north, south = north_south_requests()
    input_data = planning([north, south], [worker])
    try:
        # переезд 13 мин при нормативе 10: потолок 12 (× 1,2) его запрещает,
        # потолок 13 (× 1,3) — ровно допускает
        use_norm(monkeypatch, "soft", "1.2")
        assert calculate_route(worker, [north, south]) is None
        for result in all_solver_results(input_data):
            assert result.metrics.assigned_count == 1
            assert result.metrics.norm_excess_minutes == 0
            assert validate_plan(input_data, result) == []
        use_norm(monkeypatch, "soft", "1.3")
        route = calculate_route(worker, [north, south])
        assert route is not None
        assert route.stops[1].facts["travel_norm_excess_minutes"] == 3
        for result in all_solver_results(input_data):
            assert result.metrics.assigned_count == 2
            assert result.metrics.norm_excess_minutes == 3
            assert validate_plan(input_data, result) == []
    finally:
        get_settings.cache_clear()


def test_soft_refusal_over_the_ceiling_names_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    north = with_travel_norm(
        service_request("north", 0, 9, 17).model_copy(update={"coordinates": NORTH}), 5
    )
    input_data = planning([north], [engineer()])
    try:
        # дорога от старта 8 мин при нормативе 5: потолок floor(5 × 1,4) = 7 плечо
        # запрещает, потолок floor(5 × 1,6) = 8 — допускает с превышением 3 мин
        use_norm(monkeypatch, "soft", "1.4")
        for result in all_solver_results(input_data):
            assert result.metrics.assigned_count == 0
            refusal = result.unassigned[0]
            assert refusal.reason_code == "travel_norm_exceeded"
            assert "норматив для заявки — 5 мин" in refusal.explanation
            assert "допустимый потолок — 7 мин" in refusal.explanation
            assert refusal.details["travel_limit_minutes"] == 7
            assert validate_plan(input_data, result) == []
        use_norm(monkeypatch, "soft", "1.6")
        for result in all_solver_results(input_data):
            assert result.metrics.assigned_count == 1
            assert result.metrics.norm_excess_minutes == 3
    finally:
        get_settings.cache_clear()


def test_validator_rejects_a_leg_over_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    input_data = planning(north_south_requests(), [engineer()])
    try:
        use_norm(monkeypatch, "soft", "2.0")
        plan = build_baseline(input_data)
        assert plan.metrics.norm_excess_minutes == 3
        assert validate_plan(input_data, plan) == []
        use_norm(monkeypatch, "soft", "1.2")
        violations = validate_plan(input_data, plan)
        assert "travel_norm_ceiling_exceeded:south" in violations
        assert "travel_norm_exceeded:south" not in violations
        use_norm(monkeypatch, "hard")
        assert "travel_norm_exceeded:south" in validate_plan(input_data, plan)
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("soft_norm")
def test_validator_rejects_understated_norm_excess_minutes() -> None:
    input_data = planning(north_south_requests(), [engineer()])
    plan = build_baseline(input_data)
    assert (plan.metrics.norm_violation_count, plan.metrics.norm_excess_minutes) == (1, 3)

    lying_metrics = plan.model_copy(
        update={"metrics": plan.metrics.model_copy(update={"norm_excess_minutes": 1})}
    )
    violations = validate_plan(input_data, lying_metrics)
    assert "norm_excess_minutes_mismatch" in violations
    assert "plan_metrics_mismatch" in violations
    assert "norm_violation_count_mismatch" not in violations

    # Факты и метрики подделаны согласованно: число превышений то же (1), а минут
    # меньше (норматив «поднят» до 12). Счёт визитов сходится — ловят именно минуты.
    route = plan.routes[0]
    stop = route.stops[1]
    forged_facts = {**stop.facts, "travel_norm_minutes": 12, "travel_norm_excess_minutes": 1}
    forged_stop = stop.model_copy(update={"facts": forged_facts})
    forged_route = route.model_copy(update={"stops": [route.stops[0], forged_stop]})
    forged = plan.model_copy(
        update={
            "routes": [forged_route],
            "metrics": calculate_metrics([forged_route], plan.unassigned),
        }
    )
    assert (forged.metrics.norm_violation_count, forged.metrics.norm_excess_minutes) == (1, 1)
    violations = validate_plan(input_data, forged)
    assert "plan_metrics_mismatch" not in violations
    assert "norm_violation_count_mismatch" not in violations
    assert "norm_excess_minutes_mismatch" in violations
    assert "travel_norm_excess_mismatch:south" in violations


# Две бригады и одна заявка с нормативом 8 мин. Пеший инженер (первый во входе,
# его берёт baseline) ближе: 0,9 км, 13 мин, превышение 5. Автобригада дальше:
# 2,6 км, 10 мин, превышение 2. По числу превышений они равны, и прежняя цель
# брала пешего по пробегу; по минутам превышения выигрывает автобригада.
CAR_START = Coordinates(latitude=55.718, longitude=37.70)
WALKER_START = Coordinates(latitude=55.693, longitude=37.70)
TARGET = Coordinates(latitude=55.70, longitude=37.70)
TARGET_NORM_MINUTES = 8


def car_or_walker_input() -> PlanningInput:
    walker = engineer("engineer-1", transport=Transport.WALKING).model_copy(
        update={"start_location": WALKER_START}
    )
    car = engineer("engineer-2").model_copy(update={"start_location": CAR_START})
    target = with_travel_norm(
        service_request("target", 0, 9, 17).model_copy(update={"coordinates": TARGET}),
        TARGET_NORM_MINUTES,
    )
    return planning([target], [walker, car])


def test_car_or_walker_fixture_ties_on_count_but_not_on_minutes() -> None:
    car_distance, car_minutes, _ = effective_edge_metrics(CAR_START, TARGET, Transport.CAR)
    walk_distance, walk_minutes, _ = effective_edge_metrics(
        WALKER_START, TARGET, Transport.WALKING
    )
    assert walk_distance < car_distance
    assert TARGET_NORM_MINUTES < car_minutes < walk_minutes <= 2 * TARGET_NORM_MINUTES
    assert (car_minutes - TARGET_NORM_MINUTES, walk_minutes - TARGET_NORM_MINUTES) == (2, 5)


@pytest.mark.usefixtures("soft_norm")
def test_optimizing_solvers_prefer_fewer_excess_minutes_over_shorter_leg() -> None:
    input_data = car_or_walker_input()

    for result in (
        build_insertion(input_data, time_limit_seconds=1),
        build_optimized(input_data, time_limit_seconds=1),
    ):
        assert result.algorithm in {"insertion_ls_v1", "ortools"}
        served = [route.engineer_id for route in result.routes if route.stops]
        assert served == ["engineer-2"]
        assert result.metrics.norm_excess_minutes == 2
        assert validate_plan(input_data, result) == []


@pytest.mark.usefixtures("soft_norm")
def test_soft_normal_event_prefers_fewer_excess_minutes_then_distance() -> None:
    input_data = car_or_walker_input()
    empty_input = input_data.model_copy(update={"requests": []})
    base = build_baseline(empty_input)
    event = PlanningRequestEvent(
        event_id="soft-minutes",
        idempotency_key="soft-minutes",
        event_time=dt(8),
        expected_revision=1,
        request=input_data.requests[0],
    )

    _baseline, result, _protected = build_replan_pair(empty_input, base, event, 1)

    served = [route.engineer_id for route in result.routes if route.stops]
    assert served == ["engineer-2"]
    assert result.metrics.norm_excess_minutes == 2
    assert normal_insertion_policy() == NORMAL_INSERTION_POLICY_SOFT


@pytest.mark.usefixtures("hard_norm")
def test_hard_normal_event_policy_name_is_unchanged() -> None:
    assert normal_insertion_policy() == NORMAL_INSERTION_POLICY_HARD
    assert NORMAL_INSERTION_POLICY_HARD == "min_incremental_travel_then_distance_without_reordering"


@pytest.fixture
def advisory_norm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Норматив дороги справочный — режим по умолчанию с 28.09."""
    monkeypatch.setenv("TRAVEL_NORM_MODE", "advisory")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("advisory_norm")
def test_advisory_norm_assigns_a_long_leg_and_only_marks_it() -> None:
    # Заявка в 20 км от старта при нормативе 20 мин: в soft ×2 (потолок 40 мин) она
    # ушла бы в отказ. Организаторы: дальняя поездка — не ошибка, 20 минут — слот.
    far = with_travel_norm(
        service_request("far-town", 0, 9, 17).model_copy(
            update={"coordinates": Coordinates(latitude=55.52, longitude=37.70)}
        ),
        20,
    )
    input_data = planning([far], [engineer(transport=Transport.CAR)])
    for result in all_solver_results(input_data):
        assert result.metrics.assigned_count == 1
        stop = result.routes[0].stops[0]
        assert stop.travel_minutes_from_previous > 40
        assert stop.facts["travel_norm_exceeded"] is True
        assert "travel_norm_exceeded" in stop.explanation_codes
        assert result.metrics.norm_violation_count == 1
        assert validate_plan(input_data, result) == []
    assert normal_insertion_policy() == NORMAL_INSERTION_POLICY_HARD


def test_all_solvers_mark_short_car_leg_as_local_walk() -> None:
    request = service_request("nearby", 1, 9, 16)
    input_data = planning([request], [engineer(transport=Transport.CAR)])

    for result in (
        build_baseline(input_data),
        build_insertion(input_data, time_limit_seconds=1),
        build_optimized(input_data, time_limit_seconds=1),
    ):
        stop = result.routes[0].stops[0]
        assert stop.facts["travel_mode_from_previous"] == "walking"
        assert stop.facts["travel_mode_reason"] == "short_car_leg_walk"
        assert validate_plan(input_data, result) == []


def test_all_solvers_mark_short_transit_leg_as_walk() -> None:
    # 333 м по прямой: пешком 7 мин, на транспорте с подходом и ожиданием — 17
    request = service_request("nearby", 3, 9, 16)
    input_data = planning([request], [engineer(transport=Transport.PUBLIC_TRANSPORT)])

    for result in (
        build_baseline(input_data),
        build_insertion(input_data, time_limit_seconds=1),
        build_optimized(input_data, time_limit_seconds=1),
    ):
        stop = result.routes[0].stops[0]
        assert stop.facts["travel_mode_from_previous"] == "walking"
        assert stop.facts["travel_mode_reason"] == "short_transit_leg_walk"
        assert "short_transit_leg_walk" in stop.explanation_codes
        assert validate_plan(input_data, result) == []


def test_equal_coordinates_still_produce_two_service_visits() -> None:
    first = service_request("same-1", 0, 10, 12, duration=30)
    second = service_request("same-2", 0, 10, 12, duration=30).model_copy(update={"input_order": 1})
    result = build_baseline(planning([first, second], [engineer()]))
    assert [stop.request_id for stop in result.routes[0].stops] == ["same-1", "same-2"]
    assert result.routes[0].stops[1].distance_meters_from_previous == 0


def test_last_service_is_inside_shift_and_no_return_is_added() -> None:
    request = service_request("request-1", 0, 17, 18, duration=60)
    result = build_baseline(planning([request], [engineer()]))
    route = result.routes[0]
    assert route.finish_at == dt(18)
    assert route.service_minutes == 60
    assert route.distance_meters == route.stops[0].distance_meters_from_previous


def test_optimizer_never_publishes_worse_tuple() -> None:
    requests = [
        service_request("request-1", 0, 12, 14),
        service_request("request-2", 1, 10, 12),
    ]
    input_data = planning(requests, [engineer()])
    baseline = build_baseline(input_data)
    optimized = build_optimized(input_data, time_limit_seconds=1)
    baseline_tuple = (
        baseline.metrics.urgent_unassigned_count,
        baseline.metrics.normal_unassigned_count,
        baseline.metrics.used_engineers_count,
        baseline.metrics.total_distance_meters,
    )
    optimized_tuple = (
        optimized.metrics.urgent_unassigned_count,
        optimized.metrics.normal_unassigned_count,
        optimized.metrics.used_engineers_count,
        optimized.metrics.total_distance_meters,
    )
    assert optimized_tuple <= baseline_tuple
    assert validate_plan(input_data, optimized) == []


def test_solver_registry_and_insertion_are_switchable_and_measurable() -> None:
    ids = {item["id"] for item in algorithm_catalog()}
    assert ids == {"baseline_v1", "ortools_gls_v1", "insertion_ls_v1"}
    requests = [
        service_request("late", 0, 12, 14, duration=60),
        service_request("early", 1, 10, 10, duration=60),
    ]
    input_data = planning(requests, [engineer()])
    baseline = build_baseline(input_data)
    insertion = build_insertion(input_data, time_limit_seconds=1)
    assert baseline.metrics.assigned_count == 1
    assert insertion.metrics.assigned_count == 2
    assert [stop.request_id for stop in insertion.routes[0].stops] == ["early", "late"]
    assert validate_plan(input_data, insertion) == []


def test_optimizer_prefers_urgent_assignment_over_normal() -> None:
    normal = service_request("normal", 0, 10, 10, duration=480)
    urgent = service_request("urgent", 1, 10, 10, duration=480, priority=Priority.URGENT)
    input_data = planning([normal, urgent], [engineer()])
    optimized = build_optimized(input_data, time_limit_seconds=1)
    assigned = {stop.request_id for route in optimized.routes for stop in route.stops}
    assert "urgent" in assigned
    assert optimized.metrics.urgent_unassigned_count == 0


def test_optimizer_prefers_connection_over_routine_work() -> None:
    routine = service_request(
        "routine",
        0,
        10,
        10,
        duration=480,
        source_fields={"_enrichment": {"work_class": "local"}},
    )
    connection = service_request(
        "connection",
        1,
        10,
        10,
        duration=480,
        skill=RequiredSkill.CONNECTION,
        source_fields={"_enrichment": {"work_class": "connection"}},
    )
    worker = engineer(skills=[RequiredSkill.LOCAL, RequiredSkill.CONNECTION])
    result = build_optimized(planning([routine, connection], [worker]), time_limit_seconds=1)
    assigned = {stop.request_id for route in result.routes for stop in route.stops}
    assert assigned == {"connection"}
    assert result.metrics.connection_unassigned_count == 0
    assert result.metrics.routine_unassigned_count == 1


def test_equipment_inventory_is_a_route_capacity() -> None:
    first = service_request(
        "equipment-1",
        0,
        9,
        12,
        duration=30,
        required_equipment={"terminal": 1},
    )
    second = service_request(
        "equipment-2",
        1,
        10,
        14,
        duration=30,
        required_equipment={"terminal": 1},
    )
    worker = engineer().model_copy(update={"equipment_inventory": {"terminal": 1}})
    result = build_optimized(planning([first, second], [worker]), time_limit_seconds=1)
    assert result.metrics.assigned_count == 1
    assert result.metrics.unassigned_count == 1
    assert validate_plan(planning([first, second], [worker]), result) == []


def test_missing_equipment_is_reported_explicitly() -> None:
    request = service_request(
        "equipment",
        0,
        10,
        12,
        required_equipment={"terminal": 1},
    )
    result = build_baseline(planning([request], [engineer()]))
    assert result.unassigned[0].reason_code == "no_required_equipment"


def test_emergency_sla_starts_at_arrival_and_requires_completion_within_100_minutes() -> None:
    raw = PlanningRequestEvent(
        event_id="event-sla",
        idempotency_key="event-sla",
        event_time=dt(10),
        expected_revision=1,
        request=service_request(
            "urgent-sla",
            1,
            9,
            14,
            duration=80,
            skill=RequiredSkill.EMERGENCY,
            priority=Priority.URGENT,
        ),
    )
    normalized = apply_emergency_sla(raw)
    assert normalized.request.window_start == dt(10)
    assert normalized.request.completion_deadline == dt(11, 40)
    assert normalized.request.source_fields["_sla"]["minutes"] == 100


def test_normal_event_only_fills_a_gap_and_preserves_existing_visits() -> None:
    first = service_request("first", 0, 10, 10, duration=30)
    second = service_request("second", 1, 12, 12, duration=30)
    input_data = planning([first, second], [engineer()])
    base = build_baseline(input_data)
    event = PlanningRequestEvent(
        event_id="normal-event",
        idempotency_key="normal-event",
        event_time=dt(9),
        expected_revision=1,
        request=service_request("gap", 2, 11, 11, duration=30),
    )
    baseline, optimized, protected = build_replan_pair(input_data, base, event, 1)
    assert set(protected) == {"first", "second"}
    for result in (baseline, optimized):
        route = result.routes[0]
        assert [stop.request_id for stop in route.stops] == ["first", "gap", "second"]
        old_by_id = {stop.request_id: stop for stop in base.routes[0].stops}
        for stop in route.stops:
            if stop.request_id in old_by_id:
                assert stop.service_start_at == old_by_id[stop.request_id].service_start_at
                assert stop.service_end_at == old_by_id[stop.request_id].service_end_at


def test_normal_event_chooses_engineer_with_minimum_incremental_distance() -> None:
    first_engineer = engineer("engineer-1").model_copy(
        update={"start_location": Coordinates(latitude=55.60, longitude=37.40)}
    )
    second_engineer = engineer("engineer-2").model_copy(
        update={"start_location": Coordinates(latitude=55.80, longitude=37.80)}
    )
    input_data = planning([], [first_engineer, second_engineer])
    base = build_baseline(input_data)
    incoming = service_request("near-second", 0, 9, 16, duration=30).model_copy(
        update={"coordinates": Coordinates(latitude=55.801, longitude=37.801)}
    )
    event = PlanningRequestEvent(
        event_id="normal-nearest",
        idempotency_key="normal-nearest",
        event_time=dt(9),
        expected_revision=1,
        request=incoming,
    )

    _baseline, result, _protected = build_replan_pair(input_data, base, event, 1)

    assigned_route = next(route for route in result.routes if route.stops)
    assert assigned_route.engineer_id == "engineer-2"
    assert result.termination_reason == "normal_request_inserted_without_reordering"


def test_validator_rejects_route_identity_metadata_mismatch() -> None:
    request = service_request("request", 0, 10, 12)
    input_data = planning([request], [engineer()])
    result = build_baseline(input_data)
    wrong_route = result.routes[0].model_copy(
        update={"start_location": Coordinates(latitude=55.8, longitude=37.8)}
    )
    corrupted = result.model_copy(update={"routes": [wrong_route]})
    assert "route_start_location_mismatch:engineer-1" in validate_plan(input_data, corrupted)


def test_plan_snapshots_transport_profile_and_stop_location() -> None:
    request = service_request("request", 0, 10, 12)
    for transport in Transport:
        worker = engineer(transport=transport)
        input_data = planning([request], [worker])
        result = build_baseline(input_data)
        route = result.routes[0]
        assert route.transport is transport
        assert route.routing_method == ROUTE_ESTIMATION_METHOD
        assert route.routing_quality.value == "estimated"
        assert route.stops[0].location == request.coordinates
        assert validate_plan(input_data, result) == []


def test_validator_checks_leg_by_the_model_it_was_planned_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2,2 км: бригада на общественном транспорте едет, а не идёт пешком
    request = service_request("far", 20, 10, 12)
    input_data = planning([request], [engineer(transport=Transport.PUBLIC_TRANSPORT)])
    current = build_baseline(input_data)
    with monkeypatch.context() as patch:
        patch.setattr(schedule, "ROUTE_ESTIMATION_METHOD", PREVIOUS_ROUTE_ESTIMATION_METHOD)
        patch.setattr(
            schedule,
            "effective_edge_metrics",
            partial(effective_edge_metrics, method=PREVIOUS_ROUTE_ESTIMATION_METHOD),
        )
        before = build_baseline(input_data)
    stop = before.routes[0].stops[0]
    assert stop.facts["routing_method"] == "haversine_by_transport_v4"
    current_stop = current.routes[0].stops[0]
    assert stop.travel_minutes_from_previous != current_stop.travel_minutes_from_previous
    # план до калибровки остаётся обещанным: его плечо проверяется по v4
    assert validate_plan(input_data, before) == []
    relabeled_stop = stop.model_copy(
        update={"facts": {**stop.facts, "routing_method": "haversine_by_transport_v5"}}
    )
    relabeled = before.model_copy(
        update={"routes": [before.routes[0].model_copy(update={"stops": [relabeled_stop]})]}
    )
    assert "schedule_mismatch:far" in validate_plan(input_data, relabeled)


def test_validator_rejects_changed_snapshot_location() -> None:
    request = service_request("request", 0, 10, 12)
    input_data = planning([request], [engineer()])
    result = build_baseline(input_data)
    wrong_stop = result.routes[0].stops[0].model_copy(
        update={"location": Coordinates(latitude=55.8, longitude=37.8)}
    )
    wrong_route = result.routes[0].model_copy(update={"stops": [wrong_stop]})
    corrupted = result.model_copy(update={"routes": [wrong_route]})
    assert "stop_location_mismatch:request" in validate_plan(input_data, corrupted)


def test_empty_and_no_engineer_inputs_are_explained() -> None:
    empty = build_baseline(planning([], []))
    assert empty.metrics.assigned_count == 0
    request = service_request("alone", 0, 10, 12)
    no_engineers = build_optimized(planning([request], []), time_limit_seconds=1)
    assert no_engineers.unassigned[0].reason_code == "no_required_skill"


def test_replan_preserves_started_work() -> None:
    existing = [
        service_request("request-1", 0, 8, 10),
        service_request("request-2", 1, 10, 12),
    ]
    input_data = planning(existing, [engineer()])
    base = build_baseline(input_data)
    urgent = service_request(
        "urgent-1",
        100,
        9,
        11,
        skill=RequiredSkill.LOCAL,
        priority=Priority.URGENT,
    )
    event = UrgentRequestEvent(
        event_id="event-1",
        idempotency_key="urgent-1",
        event_time=dt(8, 30),
        expected_revision=1,
        request=urgent,
    )
    baseline, optimized, protected = build_replan_pair(input_data, base, event, 1)
    assert protected == ["request-1"]
    for result in (baseline, optimized):
        first = result.routes[0].stops[0]
        assert first.request_id == base.routes[0].stops[0].request_id
        assert first.service_start_at == base.routes[0].stops[0].service_start_at
        assert first.service_end_at == base.routes[0].stops[0].service_end_at


def _urgent_event(event_time: datetime, window_start: int, window_end: int) -> UrgentRequestEvent:
    return UrgentRequestEvent(
        event_id=f"event-{event_time.isoformat()}",
        idempotency_key=f"urgent-{event_time.isoformat()}",
        event_time=event_time,
        expected_revision=1,
        request=service_request(
            "urgent-boundary",
            100,
            window_start,
            window_end,
            priority=Priority.URGENT,
        ),
    )


def test_replan_preserves_a_visit_when_engineer_is_travelling() -> None:
    request = service_request("started-trip", 1, 8, 10)
    input_data = planning([request], [engineer()])
    base = build_baseline(input_data)
    event_time = datetime.fromisoformat("2026-08-17T08:00:30+03:00")
    baseline, optimized, protected = build_replan_pair(
        input_data,
        base,
        _urgent_event(event_time, 9, 11),
        1,
    )
    assert protected == ["started-trip"]
    for result in (baseline, optimized):
        assert result.routes[0].stops[0] == base.routes[0].stops[0]


def test_replan_preserves_a_visit_while_waiting_for_client() -> None:
    # Ждать у двери бригада может только между визитами: к первому визиту она выезжает
    # в последний момент. Первый визит 8:00–9:00, второй в соседнем доме открывается в 11.
    input_data = planning(
        [service_request("done-first", 0, 8, 9), service_request("waiting", 1, 11, 12)],
        [engineer()],
    )
    base = build_baseline(input_data)
    waiting_stop = base.routes[0].stops[1]
    assert waiting_stop.wait_minutes > 0
    assert waiting_stop.arrival_at < dt(10) < waiting_stop.service_start_at
    baseline, optimized, protected = build_replan_pair(
        input_data,
        base,
        _urgent_event(dt(10), 10, 12),
        1,
    )
    assert protected == ["done-first", "waiting"]
    for result in (baseline, optimized):
        assert result.routes[0].stops[:2] == base.routes[0].stops


def test_first_visit_is_not_frozen_before_the_late_departure() -> None:
    # К окну 10–12 бригада выезжает в последний момент, а не в 8:00. В 9:30 она ещё
    # свободна, и авария может перестроить её день.
    input_data = planning([service_request("later", 1, 10, 12)], [engineer()])
    base = build_baseline(input_data)
    first = base.routes[0].stops[0]
    assert first.departure_at > dt(9, 30)
    assert first.wait_minutes == 0
    _, _, protected = build_replan_pair(input_data, base, _urgent_event(dt(9, 30), 10, 12), 1)
    assert protected == []


def test_replan_after_shift_keeps_history_and_leaves_urgent_unassigned() -> None:
    request = service_request("completed", 0, 8, 10)
    input_data = planning([request], [engineer()])
    base = build_baseline(input_data)
    baseline, optimized, protected = build_replan_pair(
        input_data,
        base,
        _urgent_event(dt(18, 30), 18, 19),
        1,
    )
    assert protected == ["completed"]
    for result in (baseline, optimized):
        assert result.routes[0].stops == base.routes[0].stops
        assert result.unassigned[0].request_id == "urgent-boundary"


def test_replan_suffix_does_not_start_before_event_after_idle_gap() -> None:
    completed = service_request("completed-before-gap", 0, 8, 9, duration=30)
    input_data = planning([completed], [engineer()])
    base = build_baseline(input_data)
    baseline, insertion, protected = build_replan_pair(
        input_data,
        base,
        _urgent_event(dt(12), 12, 14),
        1,
        "insertion_ls_v1",
    )
    assert protected == ["completed-before-gap"]
    for result in (baseline, insertion):
        assert result.routes[0].stops[1].departure_at >= dt(12)
        assert validate_plan(
            input_data.model_copy(
                update={"requests": [*input_data.requests, _urgent_event(dt(12), 12, 14).request]}
            ),
            result,
        ) == []


def test_replan_keeps_explicit_baseline_solver() -> None:
    original = service_request("original", 0, 8, 10, duration=30)
    input_data = planning([original], [engineer()])
    base = build_baseline(input_data)
    baseline, selected, _protected = build_replan_pair(
        input_data,
        base,
        _urgent_event(dt(12), 12, 14),
        1,
        "baseline_v1",
    )
    assert selected.algorithm == "replan_baseline"
    assert selected.metrics == baseline.metrics


def _midday_scenario() -> tuple[PlanningInput, PlanningResult]:
    """Две взаимозаменяемые бригады: к полудню обе заняты визитом в одной точке.

    Утренние визиты идут до 12:30, поэтому событие в 12:00 застаёт их в работе —
    они защищены, а хвост (ближний и дальний визиты) пересчитывается заново.
    """
    input_data = planning(
        [
            service_request("morning-1", 0, 10, 11, duration=150),
            service_request("morning-2", 0, 10, 11, duration=150),
            service_request("tail-near", 2, 13, 15, duration=120),
            service_request("tail-far", 50, 13, 15, duration=120),
        ],
        [engineer("engineer-1"), engineer("engineer-2")],
    )
    return input_data, build_baseline(input_data)


def test_replan_keeps_tail_with_previous_crews_and_reports_churn() -> None:
    input_data, base = _midday_scenario()
    _baseline, optimized, protected = build_replan_pair(
        input_data,
        base,
        _urgent_event(dt(12), 13, 16),
        1,
    )
    assert protected == ["morning-1", "morning-2"]
    base_engineer = {
        stop.request_id: route.engineer_id for route in base.routes for stop in route.stops
    }
    base_start = {
        stop.request_id: stop.service_start_at for route in base.routes for stop in route.stops
    }
    base_first_stop = {route.engineer_id: route.stops[0] for route in base.routes}
    holder: dict[str, str] = {}
    reassigned = 0
    time_shifted = 0
    for route in optimized.routes:
        assert route.stops[0] == base_first_stop[route.engineer_id]
        for stop in route.stops:
            if stop.request_id not in base_engineer:
                continue
            holder[stop.request_id] = route.engineer_id
            reassigned += base_engineer[stop.request_id] != route.engineer_id
            drift = stop.service_start_at - base_start[stop.request_id]
            time_shifted += abs(drift) > timedelta(minutes=15)
    assert holder["tail-near"] == "engineer-1"
    assert holder["tail-far"] == "engineer-2"
    assert optimized.replan_churn == {
        "compared_count": 4,
        "reassigned_count": reassigned,
        "time_shifted_count": time_shifted,
        "shift_threshold_minutes": 15,
    }
    assert reassigned == 0


def test_stabilization_returns_relabeled_tails_at_equal_cost() -> None:
    input_data, base = _midday_scenario()
    event_time = dt(12)
    prefixes = _protected_prefixes(base, event_time)
    request_by_id = {request.id: request for request in input_data.requests}
    anchors = _anchor_engineers(input_data, prefixes, request_by_id, event_time)
    protected = {stop.request_id for prefix in prefixes.values() for stop in prefix}
    suffix_input = input_data.model_copy(
        update={
            "requests": [
                request for request in input_data.requests if request.id not in protected
            ],
            "engineers": anchors,
            "revision": input_data.revision + 1,
        }
    )
    straight = build_baseline(suffix_input)
    assert [[stop.request_id for stop in route.stops] for route in straight.routes] == [
        ["tail-near"],
        ["tail-far"],
    ]
    first = calculate_route(anchors[0], [request_by_id["tail-far"]], 0)
    second = calculate_route(anchors[1], [request_by_id["tail-near"]], 0)
    assert first is not None
    assert second is not None
    relabeled = straight.model_copy(
        update={"routes": [first, second], "metrics": calculate_metrics([first, second], [])}
    )
    stabilized = _stabilize_tail(suffix_input, relabeled, base, prefixes, 1)
    assert [[stop.request_id for stop in route.stops] for route in stabilized.routes] == [
        ["tail-near"],
        ["tail-far"],
    ]
    assert stabilized.metrics.total_distance_meters == relabeled.metrics.total_distance_meters
    assert validate_plan(suffix_input, stabilized) == []


def _buffer_requests() -> list[ServiceRequestInput]:
    return [
        service_request("first", 0, 10, 12, duration=60),
        service_request("second", 1, 12, 14, duration=60),
    ]


def test_zero_buffer_departs_to_the_first_visit_at_the_last_moment() -> None:
    requests = _buffer_requests()
    route = calculate_route(engineer(), requests, 0)
    default_route = calculate_route(engineer(), requests)
    assert route is not None
    assert default_route is not None
    assert route == default_route
    # маршрут начинается, когда бригада свободна (начало смены); к первому визиту она
    # выезжает в последний момент и не ждёт у двери — время работ то же
    assert route.departure_at == dt(8)
    first = route.stops[0]
    assert first.service_start_at == dt(10)
    assert first.arrival_at == first.service_start_at
    assert first.wait_minutes == 0
    assert first.departure_at == first.arrival_at - timedelta(
        minutes=first.travel_minutes_from_previous
    )
    assert route.stops[1].departure_at == route.stops[0].service_end_at
    assert route.wait_minutes > 0
    input_data = planning(requests, [engineer()])
    assert validate_plan(input_data, build_baseline(input_data)) == []


def test_gap_insertion_before_the_first_visit_uses_the_free_morning() -> None:
    # Поздний выезд не отнимает утро: маршрут по-прежнему начинается с начала смены,
    # и новая заявка встаёт перед первым визитом, не сдвигая его.
    later = service_request("later", 1, 14, 16)
    base = build_baseline(planning([later], [engineer()]))
    morning = service_request("morning", 2, 9, 10)
    inserted = insert_request_preserving_visits(engineer(), base.routes[0], [later], morning, 0)
    assert inserted is not None
    assert inserted.departure_at == dt(8)
    assert [stop.request_id for stop in inserted.stops] == ["morning", "later"]
    assert inserted.stops[0].wait_minutes == 0
    assert inserted.stops[1].service_start_at == base.routes[0].stops[0].service_start_at
    full_input = planning([later, morning], [engineer()])
    result = base.model_copy(
        update={"routes": [inserted], "metrics": calculate_metrics([inserted], [])}
    )
    assert validate_plan(full_input, result) == []


def test_buffer_starts_late_and_reserves_time_between_visits() -> None:
    requests = _buffer_requests()
    route = calculate_route(engineer(), requests, 10)
    assert route is not None
    assert route.departure_at > dt(8)
    assert route.stops[1].departure_at - route.stops[0].service_end_at == timedelta(minutes=10)
    assert route.wait_minutes == 0
    for stop, request in zip(route.stops, requests, strict=True):
        assert request.window_start <= stop.service_start_at <= request.window_end


def test_buffer_plan_is_accepted_by_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANNING_BUFFER_MINUTES", "10")
    get_settings.cache_clear()
    try:
        requests = [
            service_request("late", 0, 12, 14, duration=60),
            service_request("early", 1, 10, 10, duration=60),
        ]
        input_data = planning(requests, [engineer()], buffer_minutes=10)
        baseline = build_baseline(input_data)
        optimized = build_optimized(input_data, time_limit_seconds=2)
        assert validate_plan(input_data, baseline) == []
        assert optimized.termination_reason != "solver_schedule_rejected"
        assert optimized.metrics.assigned_count == 2
        assert validate_plan(input_data, optimized) == []
        route = optimized.routes[0]
        assert route.departure_at > dt(8)
        request_by_id = {request.id: request for request in requests}
        for previous, following in zip(route.stops, route.stops[1:], strict=False):
            assert following.departure_at - previous.service_end_at >= timedelta(minutes=10)
        for stop in route.stops:
            window = request_by_id[stop.request_id]
            assert window.window_start <= stop.service_start_at <= window.window_end
    finally:
        get_settings.cache_clear()
