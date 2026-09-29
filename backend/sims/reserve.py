"""Замер защитного резерва: что даёт разница между безопасным слотом и ожидаемой работой.

Запуск внутри контейнера backend_tools:
    python -m sims.reserve --zone all --time-limit 10

Скрипт ничего не пишет в БД. В сидированных наборах резерв нулевой: ожидаемое время
работ совпадает с безопасным слотом (`expected_service_minutes == duration_minutes`).
Здесь он включается в памяти так, как задумано нормативами: ожидаемое время = технические
минуты норматива (`_enrichment.technical_minutes`, справочник
data/fixtures/enrichment_rules.json), безопасный слот не трогается. Резервом становится
разница — у подключения и дозаказа это минуты оформления документов.

Три варианта на одной зоне:
  * «резерв нулевой» — план по безопасному слоту, ожидаемое = безопасному (как в данных);
  * «резерв включён» — ТОТ ЖЕ план (исполнители, порядок, время), пересобранный
    `calculate_route` на входе с ожидаемым = техническим: резерв записан в остановки
    так же, как в приложении, валидатор это проверяет;
  * «слот без резерва» — контрфакт для цены резерва: решатель планирует прямо по
    техническим минутам, то есть резерв из слота убран.

Исполнение дня проигрывается `sims.sim.simulate_execution` через обёртку
`simulate_expected`: реальная работа длится ОЖИДАЕМОЕ время × k (1.0, 1.15, 1.3), а не
безопасный слот. Случайности нет; недетерминированным может быть только решатель с
лимитом времени.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Any

from app.core.config import get_settings
from app.db import dispose_engine
from app.planning import validate_plan
from app.planning.contracts import SolverConfig
from app.planning.metrics import calculate_metrics
from app.planning.objective import plan_objective_key
from app.planning.policies import travel_norm_mode
from app.planning.registry import SOLVERS
from app.planning.schedule import calculate_route
from app.schemas import EngineerRoute, PlanningInput, PlanningResult, ServiceRequestInput
from app.services.norms import default_norms_config
from sims.sim import ExecutionReport, load_input, plan_with, simulate_execution

ZONES = ("vostok", "yugo_vostok", "yugocenter")
DEFAULT_FACTORS = (1.0, 1.15, 1.3)


# ------------------------------------------------------------------ ожидаемое время


def _enrichment(request: ServiceRequestInput) -> dict[str, Any]:
    value = request.source_fields.get("_enrichment")
    return value if isinstance(value, dict) else {}


def technical_catalog() -> dict[str, int]:
    """Технические минуты по видам работ из справочника нормативов (fixture)."""
    return {
        str(row["norm_id"]): int(row["technical_minutes"]) for row in default_norms_config()["rows"]
    }


def technical_minutes(request: ServiceRequestInput, catalog: dict[str, int]) -> int:
    """Ожидаемое время работ по нормативу: технические минуты вида работ заявки.

    Берётся из норматива, привязанного к заявке (`_enrichment`), при его отсутствии — из
    справочника по norm_id. Как и в приложении, ожидаемое не больше безопасного и не
    меньше минуты. Заявка без норматива остаётся без резерва.
    """
    enrichment = _enrichment(request)
    value = enrichment.get("technical_minutes")
    if isinstance(value, bool) or not isinstance(value, int):
        value = catalog.get(str(enrichment.get("norm_id") or ""))
    if value is None:
        return request.duration_minutes
    return max(1, min(value, request.duration_minutes))


def replace_requests(
    planning_input: PlanningInput, updates: dict[str, dict[str, int]]
) -> PlanningInput:
    """Копия входа с подменёнными полями заявок — через валидацию доменной схемы."""
    payload = planning_input.model_dump()
    payload["requests"] = [
        {**request, **updates.get(str(request["id"]), {})} for request in payload["requests"]
    ]
    return PlanningInput.model_validate(payload)


# ----------------------------------------------------------------- план и исполнение


def rematerialize(planning_input: PlanningInput, result: PlanningResult) -> PlanningResult:
    """Тот же план (исполнители и порядок визитов) на другом входе.

    Маршрут строит `calculate_route` — тот же код, что у решателей, поэтому
    `reserve_minutes` и `expected_service_end_at` в остановках заполнены так же, как в
    приложении, и независимый валидатор может их сверить.
    """
    request_by_id = {request.id: request for request in planning_input.requests}
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    routes: list[EngineerRoute] = []
    for route in result.routes:
        if not route.stops:
            routes.append(route)
            continue
        engineer = engineer_by_id[route.engineer_id]
        requests = [request_by_id[stop.request_id] for stop in route.stops]
        rebuilt = calculate_route(engineer, requests, planning_input.buffer_minutes)
        if rebuilt is None:
            raise RuntimeError(f"маршрут {route.engineer_name} не пересобрался на новом входе")
        routes.append(rebuilt)
    rebuilt_result = result.model_copy(deep=True)
    rebuilt_result.routes = routes
    rebuilt_result.metrics = calculate_metrics(routes, rebuilt_result.unassigned)
    return rebuilt_result


def schedule_signature(result: PlanningResult) -> list[tuple[str, str, datetime, datetime]]:
    return sorted(
        (route.engineer_id, stop.request_id, stop.service_start_at, stop.service_end_at)
        for route in result.routes
        for stop in route.stops
    )


def simulate_expected(
    planning_input: PlanningInput,
    result: PlanningResult,
    service_factor: float,
    travel_factor: float = 1.0,
) -> ExecutionReport:
    """Обёртка над `sims.sim.simulate_execution`: работа длится ожидаемое время × k.

    `simulate_execution` растягивает `duration_minutes`, то есть безопасный слот. Здесь ему
    подаётся копия входа, где длительность каждой заявки подменена ожидаемой. Копия нужна
    только проигрывателю: план, окна и смены берутся как есть, решателю она не подаётся,
    поэтому схема здесь сознательно не перепроверяется.
    """
    actual = planning_input.model_copy(
        update={
            "requests": [
                request.model_copy(update={"duration_minutes": request.expected_service_minutes})
                for request in planning_input.requests
            ]
        }
    )
    return simulate_execution(actual, result, travel_factor, service_factor)


def factor_label(factor: float) -> str:
    return f"работа ×{factor:g}"


def variant_summary(
    title: str,
    planning_input: PlanningInput,
    algorithm: str,
    result: PlanningResult,
    factors: tuple[float, ...],
    travel_factor: float,
) -> dict[str, Any]:
    metrics = result.metrics
    stops = [stop for route in result.routes for stop in route.stops]
    return {
        "вариант": title,
        "алгоритм": algorithm,
        "назначено": metrics.assigned_count,
        "отказов": metrics.unassigned_count,
        "занято_бригад": metrics.used_engineers_count,
        "пробег_км": round(metrics.total_distance_meters / 1000, 1),
        "время_в_пути_мин": metrics.total_travel_minutes,
        "превышений_норматива": metrics.norm_violation_count,
        "резерв_в_плане_мин": sum(stop.reserve_minutes for stop in stops),
        "визитов_с_резервом": sum(stop.reserve_minutes > 0 for stop in stops),
        "исполнение": {
            factor_label(factor): simulate_expected(
                planning_input, result, factor, travel_factor
            ).summary()
            for factor in factors
        },
    }


def reserve_by_work_type(
    planning_input: PlanningInput, technical: dict[str, int]
) -> list[dict[str, Any]]:
    """Сколько заявок какого вида работ и какой резерв у них появляется."""
    counts: Counter[tuple[str, int, int]] = Counter(
        (
            str(_enrichment(request).get("norm_id") or "без_норматива"),
            request.duration_minutes,
            technical[request.id],
        )
        for request in planning_input.requests
    )
    return [
        {
            "вид_работ": norm_id,
            "заявок": count,
            "безопасный_слот_мин": safe,
            "ожидаемое_мин": expected,
            "резерв_мин": safe - expected,
        }
        for (norm_id, safe, expected), count in sorted(counts.items())
    ]


# ----------------------------------------------------------------------- замер зоны


def progress(message: str) -> None:
    # ход прогона — в stderr, чтобы stdout оставался чистым JSON
    print(message, file=sys.stderr, flush=True)


def best_of(
    planning_input: PlanningInput, time_limit: int, repeats: int
) -> tuple[str, PlanningResult, list[int]]:
    """Лучший из `repeats` расчётов всеми решателями по `plan_objective_key`.

    Решатели останавливаются по часам, и под нагрузкой один вход даёт разные планы;
    лучший из нескольких расчётов меньше зависит от случайной загрузки машины. Число
    отказов каждой попытки возвращается, чтобы разброс был виден в отчёте.
    """
    runs = [plan_with(planning_input, list(SOLVERS), time_limit) for _ in range(repeats)]
    best = min(
        range(len(runs)), key=lambda index: (plan_objective_key(runs[index][1].metrics), index)
    )
    algorithm, result = runs[best]
    return algorithm, result, [run[1].metrics.unassigned_count for run in runs]


def measure_zone(
    zone: str,
    zero_input: PlanningInput,
    time_limit: int,
    factors: tuple[float, ...],
    travel_factor: float,
    repeats: int,
) -> dict[str, Any]:
    catalog = technical_catalog()
    technical = {request.id: technical_minutes(request, catalog) for request in zero_input.requests}
    reserve_in_data = sum(
        request.duration_minutes - request.expected_service_minutes
        for request in zero_input.requests
    )

    algorithm, zero_plan, zero_attempts = best_of(zero_input, time_limit, repeats)
    progress(f"{zone}: план по безопасному слоту готов ({algorithm}, отказы {zero_attempts})")

    reserve_input = replace_requests(
        zero_input,
        {
            request_id: {"expected_duration_minutes": value}
            for request_id, value in technical.items()
        },
    )
    reserve_plan = rematerialize(reserve_input, zero_plan)
    violations = validate_plan(reserve_input, reserve_plan)
    if violations:
        raise RuntimeError(f"{zone}: валидатор отверг план с резервом: {violations[:5]}")

    tight_input = replace_requests(
        zero_input,
        {
            request_id: {"duration_minutes": value, "expected_duration_minutes": value}
            for request_id, value in technical.items()
        },
    )
    tight_algorithm, tight_plan, tight_attempts = best_of(tight_input, time_limit, repeats)
    progress(
        f"{zone}: план по техническим минутам готов ({tight_algorithm}, отказы {tight_attempts})"
    )

    variants = [
        variant_summary(
            "резерв нулевой: план по безопасному слоту, работа = слот × k",
            zero_input,
            algorithm,
            zero_plan,
            factors,
            travel_factor,
        ),
        variant_summary(
            "резерв включён: тот же план, работа = технические минуты × k",
            reserve_input,
            algorithm,
            reserve_plan,
            factors,
            travel_factor,
        ),
        variant_summary(
            "слот без резерва: план по техническим минутам, работа = технические × k",
            tight_input,
            tight_algorithm,
            tight_plan,
            factors,
            travel_factor,
        ),
    ]

    as_planned = simulate_expected(zero_input, zero_plan, 1.0, 1.0)
    # Решатели не читают ожидаемое время: оно только заполняет поля остановки
    # (schedule.py). Детерминированный baseline_v1 это подтверждает: на входе с резервом
    # и без него расписание обязано совпасть до минуты.
    baseline_zero = SOLVERS["baseline_v1"].solve(zero_input, SolverConfig(time_limit))
    baseline_reserve = SOLVERS["baseline_v1"].solve(reserve_input, SolverConfig(time_limit))
    checks: dict[str, Any] = {
        "резерв_в_данных_мин": reserve_in_data,
        "валидатор_плана_с_резервом": "ок",
        "расписание_с_резервом_совпало_с_нулевым": schedule_signature(zero_plan)
        == schedule_signature(reserve_plan),
        "baseline_не_зависит_от_ожидаемого": schedule_signature(baseline_zero)
        == schedule_signature(baseline_reserve),
        "отказы_попыток_слот_безопасный": zero_attempts,
        "отказы_попыток_слот_технический": tight_attempts,
        # проигрыватель честен, только если «план как есть» даёт ноль срывов
        "самопроверка_план_как_есть": {
            "срывов_окон": as_planned.window_missed,
            "за_смену": as_planned.beyond_shift,
        },
    }

    return {
        "зона": zone,
        "заявок": len(zero_input.requests),
        "бригад": len(zero_input.engineers),
        "резерв_по_видам_работ": reserve_by_work_type(zero_input, technical),
        "проверки": checks,
        "варианты": variants,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Замер защитного резерва по зонам")
    parser.add_argument("--zone", default="all", choices=[*ZONES, "all"])
    parser.add_argument("--time-limit", type=int, default=10)
    parser.add_argument(
        "--factors",
        type=float,
        nargs="+",
        default=list(DEFAULT_FACTORS),
        help="во сколько раз реальная работа длиннее ожидаемой",
    )
    parser.add_argument(
        "--travel-factor",
        type=float,
        default=1.0,
        help="во сколько раз реальная дорога длиннее расчётной",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=2,
        help="сколько расчётов на план (берётся лучший по plan_objective_key)",
    )
    parser.add_argument(
        "--travel-norm-mode",
        choices=["soft", "hard"],
        default=None,
        help="переопределить TRAVEL_NORM_MODE на время прогона",
    )
    args = parser.parse_args()

    if args.travel_norm_mode is not None:
        os.environ["TRAVEL_NORM_MODE"] = args.travel_norm_mode
        get_settings.cache_clear()

    zones = list(ZONES) if args.zone == "all" else [args.zone]
    inputs = {zone: await load_input(zone) for zone in zones}
    await dispose_engine()

    factors = tuple(args.factors)
    payload: dict[str, Any] = {
        "замер": "защитный резерв",
        "режим_норматива": travel_norm_mode(),
        "цель": get_settings().objective_mode,
        "лимит_решателя_с": args.time_limit,
        "расчётов_на_план": args.repeats,
        "коэффициенты_работы": list(factors),
        "коэффициент_дороги": args.travel_factor,
        "зоны": [
            measure_zone(
                zone,
                inputs[zone],
                args.time_limit,
                factors,
                args.travel_factor,
                args.repeats,
            )
            for zone in zones
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
