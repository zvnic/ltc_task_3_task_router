"""Симуляции модели распределения маршрутов.

Запуск внутри контейнера backend_tools:
    python -m sims.sim all --zone vostok

Скрипт ничего не пишет в БД: читает набор, считает планы в памяти и печатает JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db import dispose_engine, get_session
from app.models import Dataset
from app.planning import build_insertion, evaluate_result, validate_plan
from app.planning.contracts import SolverConfig
from app.planning.metrics import calculate_metrics, objective_tuple
from app.planning.registry import SOLVERS, run_solvers, select_best
from app.planning.routing import effective_edge_metrics
from app.planning.schedule import _latest_departure, calculate_route
from app.schemas import PlanningInput, PlanningResult
from app.services.datasets import get_dataset_with_inputs, planning_input_from_dataset

MINUTE = timedelta(minutes=1)


# --------------------------------------------------------------------------- данные


async def load_input(zone: str) -> PlanningInput:
    async for session in get_session():
        rows = (await session.execute(select(Dataset))).scalars().all()
        chosen: Dataset | None = None
        for row in rows:
            if (row.assumptions or {}).get("service_zone") == zone:
                chosen = row
                break
        if chosen is None:
            raise SystemExit(f"Нет набора для зоны {zone}. Есть: "
                             f"{[ (r.assumptions or {}).get('service_zone') for r in rows ]}")
        dataset = await get_dataset_with_inputs(session, UUID(str(chosen.id)))
        return planning_input_from_dataset(dataset)
    raise SystemExit("нет сессии БД")


def plan_with(
    planning_input: PlanningInput,
    algorithm_ids: list[str],
    time_limit: int,
) -> tuple[str, PlanningResult]:
    results = run_solvers(planning_input, algorithm_ids, time_limit)
    return select_best(results)


def kpis(planning_input: PlanningInput, result: PlanningResult) -> dict[str, Any]:
    values = evaluate_result(planning_input, result)
    keep = (
        "completion_rate_percent",
        "urgent_completion_rate_percent",
        "makespan_minutes",
        "workload_cv",
        "workload_spread_minutes",
        "total_wait_minutes",
        "travel_share_percent",
        "used_engineer_utilization_percent",
    )
    out: dict[str, Any] = {key: values.get(key) for key in keep}
    out["assigned"] = result.metrics.assigned_count
    out["unassigned"] = result.metrics.unassigned_count
    out["used_engineers"] = result.metrics.used_engineers_count
    out["distance_km"] = round(result.metrics.total_distance_meters / 1000, 1)
    out["travel_minutes"] = result.metrics.total_travel_minutes
    return out


# ------------------------------------------------------- симулятор исполнения дня


@dataclass
class ExecutionReport:
    visits: int = 0
    window_missed: int = 0
    urgent_missed: int = 0
    beyond_shift: int = 0
    delays: list[int] = field(default_factory=list)
    worst_route_overrun: int = 0
    routes_broken: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "visits": self.visits,
            "window_missed": self.window_missed,
            "window_missed_percent": round(100 * self.window_missed / self.visits, 1)
            if self.visits
            else 0.0,
            "urgent_missed": self.urgent_missed,
            "beyond_shift": self.beyond_shift,
            "routes_broken": self.routes_broken,
            "delay_median_min": round(statistics.median(self.delays)) if self.delays else 0,
            "delay_p90_min": round(
                statistics.quantiles(self.delays, n=10)[8]
            ) if len(self.delays) >= 10 else (max(self.delays) if self.delays else 0),
            "delay_max_min": max(self.delays) if self.delays else 0,
            "worst_route_overrun_min": self.worst_route_overrun,
        }


def simulate_execution(
    planning_input: PlanningInput,
    result: PlanningResult,
    travel_factor: float = 1.0,
    service_factor: float = 1.0,
) -> ExecutionReport:
    """Проигрывает готовый план с более медленной дорогой/работой.

    Бригада едет по той же последовательности; опоздание копится по цепочке.
    Считаем визиты, у которых начало работы вышло за окно, и выходы за смену.
    """
    request_by_id = {request.id: request for request in planning_input.requests}
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    report = ExecutionReport()

    for route in result.routes:
        if not route.stops:
            continue
        engineer = engineer_by_id[route.engineer_id]
        location = engineer.start_location
        clock = route.departure_at
        route_broken = False
        for stop in route.stops:
            request = request_by_id[stop.request_id]
            _, travel_minutes, _ = effective_edge_metrics(
                location, request.coordinates, engineer.transport
            )
            travel_real = math.ceil(travel_minutes * travel_factor)
            arrival = clock + travel_real * MINUTE
            service_start = max(arrival, request.window_start)
            service_real = math.ceil(request.duration_minutes * service_factor)
            service_end = service_start + service_real * MINUTE

            report.visits += 1
            delay = round((service_start - stop.service_start_at) / MINUTE)
            report.delays.append(max(0, delay))
            if service_start > request.window_end:
                report.window_missed += 1
                route_broken = True
                if request.priority.value == "urgent":
                    report.urgent_missed += 1
            if service_end > engineer.shift_end:
                report.beyond_shift += 1
                route_broken = True
                overrun = round((service_end - engineer.shift_end) / MINUTE)
                report.worst_route_overrun = max(report.worst_route_overrun, overrun)

            location = request.coordinates
            clock = service_end
        if route_broken:
            report.routes_broken += 1
    return report


# ----------------------------------------------------- поздний старт (сжатие ожидания)


def compact_waiting(
    planning_input: PlanningInput,
    result: PlanningResult,
) -> PlanningResult:
    """Сдвигает выезд как можно позже без нарушения окон, SLA и смены.

    Последовательность визитов не меняется — меняется только время выезда. Своей
    арифметики расписания здесь нет: самый поздний выезд считает
    `schedule._latest_departure`, а маршрут от него строит `calculate_route` — тот же
    код, что у решателей. Иначе копия правил разъезжается с приложением (пешие плечи
    автобригады, SLA, защитный резерв), и валидатор отвергает план.
    """
    request_by_id = {request.id: request for request in planning_input.requests}
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    compacted = result.model_copy(deep=True)
    routes = []
    for route in compacted.routes:
        if not route.stops:
            routes.append(route)
            continue
        engineer = engineer_by_id[route.engineer_id]
        requests = [request_by_id[stop.request_id] for stop in route.stops]
        departure = _latest_departure(engineer, requests, 0)
        late_engineer = engineer.model_copy(update={"shift_start": departure})
        rebuilt = calculate_route(late_engineer, requests)
        routes.append(rebuilt if rebuilt is not None else route)
    compacted.routes = routes
    compacted.metrics = calculate_metrics(compacted.routes, compacted.unassigned)
    return compacted


# ----------------------------------------------------------------- эксперименты


def experiment_fragility(planning_input: PlanningInput, result: PlanningResult) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, travel, service in (
        ("план как есть", 1.0, 1.0),
        ("дорога +10%", 1.1, 1.0),
        ("дорога +25%", 1.25, 1.0),
        ("дорога +50%", 1.5, 1.0),
        ("работа +15%", 1.0, 1.15),
        ("дорога +25% и работа +15%", 1.25, 1.15),
    ):
        out[label] = simulate_execution(planning_input, result, travel, service).summary()
    return out


def experiment_compaction(
    planning_input: PlanningInput, result: PlanningResult
) -> dict[str, Any]:
    compacted = compact_waiting(planning_input, result)
    violations = validate_plan(planning_input, compacted)
    return {
        "валидатор_после_сжатия": violations[:5] if violations else "ок",
        "ожидание_до_мин": result.metrics.per_engineer
        and sum(item.wait_minutes for item in result.metrics.per_engineer),
        "ожидание_после_мин": sum(
            item.wait_minutes for item in compacted.metrics.per_engineer
        ),
        "устойчивость_до": simulate_execution(planning_input, result, 1.25, 1.0).summary(),
        "устойчивость_после": simulate_execution(
            planning_input, compacted, 1.25, 1.0
        ).summary(),
    }


def experiment_objective(planning_input: PlanningInput, time_limit: int) -> dict[str, Any]:
    """Сравнивает текущую цель с вариантом без экономии инженеров."""
    from app.planning import insertion as insertion_module

    original = insertion_module.objective_tuple
    base = build_insertion(planning_input, time_limit_seconds=time_limit)

    def spread_objective(metrics: Any) -> tuple[int, int, int, int]:
        # инженеров не экономим: вместо их числа штрафуем перекос по времени в маршрутах
        loads = [
            item.travel_minutes + item.service_minutes + item.wait_minutes
            for item in metrics.per_engineer
        ]
        spread = max(loads) - min(loads) if loads else 0
        return (
            metrics.urgent_unassigned_count,
            metrics.normal_unassigned_count,
            spread,
            metrics.total_distance_meters,
        )

    insertion_module.objective_tuple = spread_objective  # type: ignore[assignment]
    try:
        variant = build_insertion(planning_input, time_limit_seconds=time_limit)
    finally:
        insertion_module.objective_tuple = original  # type: ignore[assignment]

    return {
        "текущая_цель": kpis(planning_input, base),
        "без_экономии_инженеров": kpis(planning_input, variant),
    }


def experiment_determinism(
    planning_input: PlanningInput, algorithm_id: str, repeats: int, time_limit: int
) -> dict[str, Any]:
    runs = []
    for _ in range(repeats):
        result = SOLVERS[algorithm_id].solve(
            planning_input, SolverConfig(time_limit_seconds=time_limit)
        )
        runs.append(
            {
                "objective": list(objective_tuple(result.metrics)),
                "distance_km": round(result.metrics.total_distance_meters / 1000, 1),
                "assigned": result.metrics.assigned_count,
            }
        )
    identical = all(run["objective"] == runs[0]["objective"] for run in runs)
    return {"algorithm": algorithm_id, "runs": runs, "совпали": identical}


def experiment_time_limit(
    planning_input: PlanningInput, algorithm_id: str, limits: list[int]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for limit in limits:
        result = SOLVERS[algorithm_id].solve(
            planning_input, SolverConfig(time_limit_seconds=limit)
        )
        out[f"{limit}с"] = {
            "objective": list(objective_tuple(result.metrics)),
            **kpis(planning_input, result),
        }
    return out


def experiment_algorithms(planning_input: PlanningInput, time_limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for algorithm_id in SOLVERS:
        result = SOLVERS[algorithm_id].solve(
            planning_input, SolverConfig(time_limit_seconds=time_limit)
        )
        violations = validate_plan(planning_input, result)
        out[algorithm_id] = {
            "objective": list(objective_tuple(result.metrics)),
            "валидатор": "ок" if not violations else violations[:3],
            "elapsed_ms": result.elapsed_ms,
            **kpis(planning_input, result),
        }
    return out


# ----------------------------------------------------------------------- запуск


async def main() -> None:
    parser = argparse.ArgumentParser(description="Симуляции модели маршрутизации")
    parser.add_argument(
        "experiment",
        choices=["all", "fragility", "compaction", "objective", "determinism", "limits", "algos"],
    )
    parser.add_argument("--zone", default="vostok")
    parser.add_argument("--time-limit", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    planning_input = await load_input(args.zone)
    await dispose_engine()

    payload: dict[str, Any] = {
        "зона": args.zone,
        "заявок": len(planning_input.requests),
        "инженеров": len(planning_input.engineers),
    }

    need = (
        {"fragility", "compaction", "objective", "determinism", "limits", "algos"}
        if args.experiment == "all"
        else {args.experiment}
    )

    if need & {"fragility", "compaction"}:
        algorithm, result = plan_with(
            planning_input, list(SOLVERS), args.time_limit
        )
        payload["опорный_план"] = {"алгоритм": algorithm, **kpis(planning_input, result)}
        if "fragility" in need:
            payload["устойчивость"] = experiment_fragility(planning_input, result)
        if "compaction" in need:
            payload["поздний_старт"] = experiment_compaction(planning_input, result)

    if "algos" in need:
        payload["алгоритмы"] = experiment_algorithms(planning_input, args.time_limit)
    if "objective" in need:
        payload["целевая_функция"] = experiment_objective(planning_input, args.time_limit)
    if "determinism" in need:
        payload["повторяемость"] = experiment_determinism(
            planning_input, "ortools_gls_v1", args.repeats, args.time_limit
        )
    if "limits" in need:
        payload["лимит_времени"] = experiment_time_limit(
            planning_input, "ortools_gls_v1", [5, 10, 30, 60]
        )

    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
