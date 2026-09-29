from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.planning.evaluator import evaluate_results, experiment_metadata, selection_reason
from app.planning.metrics import objective_tuple
from app.planning.objective import (
    OBJECTIVE_LABELS,
    PLAN_KEY_LABELS,
    calculate_cost_weights,
    objective_mode,
    objective_passport,
    plan_cost,
    plan_objective_key,
)
from app.planning.registry import select_best
from app.schemas import PlanMetrics, PlanningInput, PlanningResult
from app.services.plan_selection import select_latest_published_plan


def test_select_latest_published_prefers_replan_over_pair_baseline() -> None:
    ts = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
    plans = [
        {"id": "1", "kind": "optimized", "created_at": ts - timedelta(hours=1)},
        {"id": "2", "kind": "replan_baseline", "created_at": ts},
        {"id": "3", "kind": "replan_optimized", "created_at": ts},
    ]
    selected = select_latest_published_plan(plans)
    assert selected is not None
    assert selected["id"] == "3"
    assert selected["kind"] == "replan_optimized"


def test_select_latest_published_uses_created_at() -> None:
    ts = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
    plans = [
        {"id": "old", "kind": "replan_optimized", "created_at": ts - timedelta(minutes=5)},
        {"id": "new", "kind": "replan_baseline_fallback", "created_at": ts},
        {"id": "base", "kind": "replan_baseline", "created_at": ts},
    ]
    selected = select_latest_published_plan(plans)
    assert selected is not None
    assert selected["id"] == "new"


def test_select_latest_published_ignores_raw_baseline() -> None:
    ts = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
    plans = [
        {"id": "b", "kind": "baseline", "created_at": ts},
        {"id": "o", "kind": "optimized", "created_at": ts - timedelta(seconds=1)},
    ]
    selected = select_latest_published_plan(plans)
    assert selected is not None
    assert selected["kind"] == "optimized"


@pytest.fixture(autouse=True)
def _settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def use_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def result_with(
    *,
    used_engineers: int,
    distance_meters: int,
    travel_minutes: int = 0,
    routine_unassigned: int = 0,
    connection_unassigned: int = 0,
    emergency_unassigned: int = 0,
    norm_violations: int = 0,
    norm_excess_minutes: int = 0,
) -> PlanningResult:
    metrics = PlanMetrics(
        assigned_count=0,
        unassigned_count=routine_unassigned + connection_unassigned + emergency_unassigned,
        urgent_unassigned_count=emergency_unassigned,
        normal_unassigned_count=routine_unassigned + connection_unassigned,
        emergency_unassigned_count=emergency_unassigned,
        connection_unassigned_count=connection_unassigned,
        routine_unassigned_count=routine_unassigned,
        norm_violation_count=norm_violations,
        norm_excess_minutes=norm_excess_minutes,
        used_engineers_count=used_engineers,
        total_distance_meters=distance_meters,
        total_travel_minutes=travel_minutes,
    )
    return PlanningResult(
        algorithm="test",
        solution_status="feasible",
        termination_reason="test",
        elapsed_ms=0,
        route_estimation_method="haversine_by_transport_v3",
        coordinate_quality="verified",
        routes=[],
        unassigned=[],
        metrics=metrics,
        constraint_violations_count=0,
    )


def test_default_mode_treats_travel_norm_as_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    # Организаторы: 20 минут бронируют слот в графике, при распределении их заменяет
    # расчётная дорога. По умолчанию превышение показывается, но план не хуже от него.
    monkeypatch.delenv("TRAVEL_NORM_MODE", raising=False)
    monkeypatch.delenv("TRAVEL_NORM_MAX_FACTOR", raising=False)
    get_settings.cache_clear()
    metrics = result_with(
        used_engineers=10,
        distance_meters=134_997,
        routine_unassigned=2,
        norm_violations=3,
        norm_excess_minutes=41,
    ).metrics
    assert objective_mode() == "lexicographic"
    assert plan_objective_key(metrics) == (0, 0, 2, 0, 10, 0, 134_997)
    passport = objective_passport()
    assert passport["travel_norm_mode"] == "advisory"
    assert passport["plan_key_criteria"] == list(PLAN_KEY_LABELS)


def test_advisory_mode_never_buys_norm_minutes_with_crews_or_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch, TRAVEL_NORM_MODE="advisory")
    long_legs = result_with(
        used_engineers=8, distance_meters=90_000, norm_violations=4, norm_excess_minutes=300
    )
    extra_crew = result_with(used_engineers=9, distance_meters=80_000)
    assert select_best({"baseline_v1": extra_crew, "ortools_gls_v1": long_legs})[0] == (
        "ortools_gls_v1"
    )
    use_settings(monkeypatch, OBJECTIVE_MODE="cost")
    clean = result_with(used_engineers=2, distance_meters=10_000, travel_minutes=30)
    violating = result_with(
        used_engineers=2,
        distance_meters=10_000,
        travel_minutes=30,
        norm_violations=2,
        norm_excess_minutes=7,
    )
    assert plan_cost(violating.metrics) == plan_cost(clean.metrics)


def test_soft_mode_keeps_lexicographic_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, TRAVEL_NORM_MODE="soft")
    monkeypatch.delenv("TRAVEL_NORM_MAX_FACTOR", raising=False)
    get_settings.cache_clear()
    metrics = result_with(
        used_engineers=10,
        distance_meters=134_997,
        travel_minutes=1_200,
        routine_unassigned=2,
        norm_violations=3,
        norm_excess_minutes=41,
    ).metrics
    assert objective_mode() == "lexicographic"
    key = plan_objective_key(metrics)
    # паспортный кортеж не меняется, ключ сравнения вставляет ступень норматива —
    # минуты превышения, а не число визитов с превышением
    assert len(objective_tuple(metrics)) == 6
    assert key == (0, 0, 2, 41, 10, 1_200, 134_997)
    assert key[:3] + key[4:] == objective_tuple(metrics)
    assert len(key) == len(PLAN_KEY_LABELS)
    assert PLAN_KEY_LABELS[3] == "norm_excess_minutes"
    assert [label for label in PLAN_KEY_LABELS if label != "norm_excess_minutes"] == list(
        OBJECTIVE_LABELS
    )
    passport = objective_passport()
    assert passport["objective_mode"] == "lexicographic"
    assert passport["travel_norm_mode"] == "soft"
    assert passport["travel_norm_max_factor"] == 2.0
    assert passport["plan_key_criteria"] == list(PLAN_KEY_LABELS)


def test_norm_excess_is_worse_than_extra_crew_but_better_than_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch, TRAVEL_NORM_MODE="soft")
    violating = result_with(
        used_engineers=8, distance_meters=90_000, norm_violations=1, norm_excess_minutes=1
    )
    extra_crew = result_with(used_engineers=9, distance_meters=140_000)
    assert select_best({"baseline_v1": violating, "ortools_gls_v1": extra_crew})[0] == (
        "ortools_gls_v1"
    )
    many_minutes = result_with(
        used_engineers=8, distance_meters=90_000, norm_violations=7, norm_excess_minutes=140
    )
    refusal = result_with(used_engineers=8, distance_meters=80_000, routine_unassigned=1)
    assert select_best({"baseline_v1": refusal, "ortools_gls_v1": many_minutes})[0] == (
        "ortools_gls_v1"
    )


def test_key_prefers_fewer_excess_minutes_not_fewer_violations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch, TRAVEL_NORM_MODE="soft")
    # Одно плечо на 30 мин сверх норматива хуже трёх плеч по 2 мин: решает величина.
    one_long_leg = result_with(
        used_engineers=8, distance_meters=90_000, norm_violations=1, norm_excess_minutes=30
    )
    three_short_legs = result_with(
        used_engineers=8, distance_meters=95_000, norm_violations=3, norm_excess_minutes=6
    )
    assert plan_objective_key(three_short_legs.metrics) < plan_objective_key(
        one_long_leg.metrics
    )
    selected, _ = select_best({"baseline_v1": one_long_leg, "ortools_gls_v1": three_short_legs})
    assert selected == "ortools_gls_v1"
    # минута превышения старше бригады: 5 мин и 9 бригад лучше 6 мин и 8 бригад
    fewer_minutes = result_with(used_engineers=9, distance_meters=120_000, norm_excess_minutes=5)
    fewer_crews = result_with(used_engineers=8, distance_meters=80_000, norm_excess_minutes=6)
    assert select_best({"baseline_v1": fewer_crews, "ortools_gls_v1": fewer_minutes})[0] == (
        "ortools_gls_v1"
    )


def test_hard_mode_key_orders_plans_exactly_like_the_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch, TRAVEL_NORM_MODE="hard")
    plans = [
        result_with(used_engineers=9, distance_meters=143_286),
        result_with(used_engineers=10, distance_meters=134_997),
        result_with(used_engineers=8, distance_meters=150_000, routine_unassigned=1),
        result_with(used_engineers=7, distance_meters=90_000, connection_unassigned=1),
    ]
    by_key = sorted(range(len(plans)), key=lambda index: plan_objective_key(plans[index].metrics))
    by_tuple = sorted(range(len(plans)), key=lambda index: objective_tuple(plans[index].metrics))
    assert by_key == by_tuple
    assert objective_passport()["travel_norm_mode"] == "hard"


def test_travel_time_decides_before_distance() -> None:
    # Бригада тратит на дорогу минуты смены: план короче по времени лучше, даже если
    # по километрам длиннее (загородная трасса вместо пробок в центре).
    faster = result_with(used_engineers=9, distance_meters=150_000, travel_minutes=900)
    shorter = result_with(used_engineers=9, distance_meters=140_000, travel_minutes=960)
    assert select_best({"baseline_v1": shorter, "ortools_gls_v1": faster})[0] == (
        "ortools_gls_v1"
    )
    # при равном времени решает пробег
    same_time = result_with(used_engineers=9, distance_meters=145_000, travel_minutes=900)
    assert select_best({"baseline_v1": faster, "ortools_gls_v1": same_time})[0] == (
        "ortools_gls_v1"
    )
    assert PLAN_KEY_LABELS[-2:] == ("travel_minutes", "distance_meters")


def test_lexicographic_mode_saves_crews_at_any_distance() -> None:
    results = {
        "baseline_v1": result_with(used_engineers=10, distance_meters=134_997),
        "ortools_gls_v1": result_with(used_engineers=9, distance_meters=143_286),
    }
    selected_algorithm, _ = select_best(results)
    assert selected_algorithm == "ortools_gls_v1"


def test_cost_mode_counts_distance_and_travel(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(
        monkeypatch,
        OBJECTIVE_MODE="cost",
        COST_PER_CREW_DAY="6000",
        COST_PER_KM="20",
        COST_PER_TRAVEL_MINUTE="8",
    )
    metrics = result_with(
        used_engineers=2,
        distance_meters=10_000,
        travel_minutes=30,
    ).metrics
    expected = 2 * 6_000_000 + 20 * 10_000 + 30 * 8_000
    assert plan_cost(metrics) == expected
    assert plan_objective_key(metrics) == (expected,)


def test_cost_mode_charges_each_excess_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, OBJECTIVE_MODE="cost", TRAVEL_NORM_MODE="soft")
    settings = get_settings()
    # Демонстрационная цена минуты превышения: много дороже минуты в пути, а самое
    # длинное допустимое плечо (норматив 20 мин × множитель 2,0 = 40 мин на машине:
    # 37 мин в движении на 25 км/ч — не больше 16 км) вместе с превышением дешевле
    # пропуска обычной заявки.
    assert settings.cost_per_travel_minute < settings.penalty_norm_excess_minute
    norm_minutes = 20
    max_excess = int(norm_minutes * settings.travel_norm_max_factor) - norm_minutes
    ceiling_leg_cost = (
        max_excess * settings.penalty_norm_excess_minute
        + 2 * norm_minutes * settings.cost_per_travel_minute
        + 16 * settings.cost_per_km
    )
    assert ceiling_leg_cost < settings.penalty_unassigned_routine
    clean = result_with(used_engineers=2, distance_meters=10_000, travel_minutes=30)
    violating = result_with(
        used_engineers=2,
        distance_meters=10_000,
        travel_minutes=30,
        norm_violations=2,
        norm_excess_minutes=7,
    )
    assert plan_cost(violating.metrics) - plan_cost(clean.metrics) == (
        7 * settings.penalty_norm_excess_minute * 1_000
    )
    # за штраф отвечают минуты: то же число превышений на 1 мин дешевле
    shorter = result_with(
        used_engineers=2,
        distance_meters=10_000,
        travel_minutes=30,
        norm_violations=2,
        norm_excess_minutes=6,
    )
    assert select_best({"baseline_v1": violating, "ortools_gls_v1": shorter})[0] == (
        "ortools_gls_v1"
    )
    assert objective_passport()["objective_weights"]["penalty_norm_excess_minute"] == (
        settings.penalty_norm_excess_minute
    )


def test_cost_mode_prefers_cheaper_plan_with_equal_crews(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, OBJECTIVE_MODE="cost")
    results = {
        "baseline_v1": result_with(
            used_engineers=10,
            distance_meters=180_000,
            travel_minutes=1_400,
        ),
        "ortools_gls_v1": result_with(
            used_engineers=10,
            distance_meters=135_000,
            travel_minutes=1_100,
        ),
    }
    selected_algorithm, _ = select_best(results)
    assert selected_algorithm == "ortools_gls_v1"


def test_paid_crews_make_shorter_day_cheaper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Бригады уже оплачены: их число перестаёт быть аргументом в пользу плана."""
    results = {
        "baseline_v1": result_with(
            used_engineers=12,
            distance_meters=135_000,
            travel_minutes=1_000,
        ),
        "ortools_gls_v1": result_with(
            used_engineers=9,
            distance_meters=180_000,
            travel_minutes=1_400,
        ),
    }
    assert select_best(results)[0] == "ortools_gls_v1"
    use_settings(monkeypatch, OBJECTIVE_MODE="cost", COST_PER_CREW_DAY="0")
    assert select_best(results)[0] == "baseline_v1"


def test_cost_mode_keeps_coverage_above_savings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пропуск заявки дороже и лишней бригады, и лишних километров."""
    use_settings(monkeypatch, OBJECTIVE_MODE="cost")
    settings = get_settings()
    assert settings.penalty_unassigned_routine > settings.cost_per_crew_day
    assert settings.penalty_unassigned_connection > settings.penalty_unassigned_routine
    assert settings.penalty_unassigned_emergency > settings.penalty_unassigned_connection
    results = {
        "baseline_v1": result_with(
            used_engineers=10,
            distance_meters=160_000,
            travel_minutes=1_200,
        ),
        "ortools_gls_v1": result_with(
            used_engineers=10,
            distance_meters=120_000,
            travel_minutes=1_000,
            emergency_unassigned=1,
        ),
    }
    assert select_best(results)[0] == "baseline_v1"


def test_cost_mode_tie_breaks_by_registry_order(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, OBJECTIVE_MODE="cost")
    results = {
        "ortools_gls_v1": result_with(
            used_engineers=8,
            distance_meters=120_000,
            travel_minutes=900,
        ),
        "baseline_v1": result_with(
            used_engineers=8,
            distance_meters=120_000,
            travel_minutes=900,
        ),
    }
    assert select_best(results)[0] == "baseline_v1"


def test_cost_weights_guard_int64() -> None:
    weights = calculate_cost_weights(83, 12, 250_000, 400)
    assert weights["used_engineer"] == 6_000 * 1_000
    assert weights["meter"] == 20
    assert weights["travel_minute"] == 8 * 1_000
    assert weights["objective_upper_bound"] < 2**63
    assert "norm_excess_minute" not in weights
    with pytest.raises(ValueError, match="int64"):
        calculate_cost_weights(10**9, 10**6, 10**9, 10**6)


def test_cost_weights_add_excess_minutes_to_arc_bound() -> None:
    plain = calculate_cost_weights(83, 12, 250_000, 400)
    weights = calculate_cost_weights(83, 12, 250_000, 400, maximum_norm_excess_minutes=20)
    assert weights["norm_excess_minute"] == 300 * 1_000
    assert weights["travel_minute"] < weights["norm_excess_minute"]
    # самое большое допустимое превышение одной дуги дешевле пропуска заявки
    assert 20 * weights["norm_excess_minute"] < weights["routine_unassigned"]
    assert weights["objective_upper_bound"] == (
        plain["objective_upper_bound"] + (83 + 12) * 20 * weights["norm_excess_minute"]
    )
    assert weights["objective_upper_bound"] < 2**63
    with pytest.raises(ValueError, match="int64"):
        calculate_cost_weights(10**9, 10**6, 10**9, 10**6, maximum_norm_excess_minutes=20)


def test_selection_reason_records_mode_and_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    results = {
        "baseline_v1": result_with(used_engineers=10, distance_meters=180_000),
        "ortools_gls_v1": result_with(used_engineers=10, distance_meters=135_000),
    }
    lexicographic = selection_reason("ortools_gls_v1", results)
    assert lexicographic["objective_mode"] == "lexicographic"
    assert lexicographic["objective_criteria"][0] == "emergency_unassigned"
    assert lexicographic["objective_criteria"][4] == "travel_minutes"
    assert lexicographic["objective_criteria"][5] == "distance_meters"
    assert lexicographic["competitors"][0]["decisive_metric"] == "distance_meters"

    use_settings(monkeypatch, OBJECTIVE_MODE="cost", TRAVEL_NORM_MAX_FACTOR="2.0")
    cost = selection_reason("ortools_gls_v1", results)
    assert cost["objective_mode"] == "cost"
    assert cost["objective_weights"]["cost_per_km"] == 20
    assert cost["cost_scale"] == 1_000
    assert cost["selected_cost_units"] == plan_cost(results["ortools_gls_v1"].metrics)
    assert cost["competitors"][0]["decisive_metric"] == "cost"
    assert cost["competitors"][0]["cost_units"] == plan_cost(results["baseline_v1"].metrics)
    assert cost["travel_norm_max_factor"] == 2.0


def test_soft_passport_names_excess_minutes_when_they_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch, TRAVEL_NORM_MODE="soft", TRAVEL_NORM_MAX_FACTOR="1.5")
    # Выбранный план везёт на одну бригаду больше, но на 18 мин меньше превышений.
    # Паспортный кортеж без ступени норматива назвал бы решающей
    # «used_engineers» — неверную причину.
    results = {
        "baseline_v1": result_with(
            used_engineers=8, distance_meters=90_000, norm_violations=2, norm_excess_minutes=30
        ),
        "ortools_gls_v1": result_with(
            used_engineers=9, distance_meters=140_000, norm_violations=2, norm_excess_minutes=12
        ),
    }
    selected, _ = select_best(results)
    assert selected == "ortools_gls_v1"

    reason = selection_reason(selected, results)

    competitor = reason["competitors"][0]
    assert competitor["decisive_metric"] == "norm_excess_minutes"
    assert competitor["plan_key"] == [0, 0, 0, 30, 8, 0, 90_000]
    assert reason["selected_plan_key"] == [0, 0, 0, 12, 9, 0, 140_000]
    # паспортный кортеж — без ступени норматива
    assert reason["selected_tuple"] == (0, 0, 0, 9, 0, 140_000)
    assert reason["travel_norm_mode"] == "soft"
    assert reason["travel_norm_max_factor"] == 1.5
    assert reason["plan_key_criteria"] == list(PLAN_KEY_LABELS)


def test_distance_gap_compares_only_plans_equal_on_the_comparison_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch, TRAVEL_NORM_MODE="soft")
    planning_input = PlanningInput(
        dataset_id="dataset",
        revision=1,
        planning_date=date(2026, 8, 17),
        requests=[],
        engineers=[],
    )
    # Короче на 20 км, но с превышением норматива: по ключу сравнения планы не равны,
    # и разрыв по пробегу между ними не считается.
    results = {
        "baseline_v1": result_with(used_engineers=8, distance_meters=100_000),
        "ortools_gls_v1": result_with(
            used_engineers=8, distance_meters=80_000, norm_violations=1, norm_excess_minutes=5
        ),
        "insertion_ls_v1": result_with(used_engineers=8, distance_meters=110_000),
    }

    evaluations = evaluate_results(planning_input, results)

    assert evaluations["baseline_v1"]["distance_gap_to_best_known_percent"] == 0.0
    assert evaluations["ortools_gls_v1"]["distance_gap_to_best_known_percent"] == 0.0
    assert evaluations["insertion_ls_v1"]["distance_gap_to_best_known_percent"] == 10.0


def test_experiment_config_fingerprint_includes_travel_norm_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planning_input = PlanningInput(
        dataset_id="dataset",
        revision=1,
        planning_date=date(2026, 8, 17),
        requests=[],
        engineers=[],
    )
    use_settings(monkeypatch, TRAVEL_NORM_MODE="soft", TRAVEL_NORM_MAX_FACTOR="2.0")
    soft_two = experiment_metadata(planning_input, ["baseline_v1"], 1)
    assert soft_two["travel_norm_mode"] == "soft"
    assert soft_two["travel_norm_max_factor"] == 2.0
    use_settings(monkeypatch, TRAVEL_NORM_MAX_FACTOR="1.5")
    assert experiment_metadata(planning_input, ["baseline_v1"], 1)["config_sha256"] != (
        soft_two["config_sha256"]
    )


@pytest.mark.parametrize("factor", ["0.9", "10.5"])
def test_travel_norm_max_factor_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    factor: str,
) -> None:
    use_settings(monkeypatch, TRAVEL_NORM_MAX_FACTOR=factor)
    with pytest.raises(ValidationError):
        get_settings()
