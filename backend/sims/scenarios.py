"""Стресс-сценарии поверх сидированного набора: много срочных заявок с узкими окнами.

Запуск внутри контейнера backend_tools:
    python -m sims.scenarios --zone vostok

Скрипт ничего не пишет в БД: берёт готовый набор, пересобирает вход и печатает JSON.
Нужен потому, что в исходных данных срочность почти не проверяется: срочный приоритет
получает только подтип «Авария», а его окно либо совпадает с обычным, либо растянуто
на весь день. Верхний уровень целевой функции при этом ничего не решает.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from typing import Any

from app.db import dispose_engine
from app.planning import evaluate_result, validate_plan
from app.planning.contracts import SolverConfig
from app.planning.registry import SOLVERS
from app.schemas import PlanningInput, Priority, ServiceRequestInput
from sims.sim import load_input

MINUTE = timedelta(minutes=1)
DEFAULT_SEED = 20260817
DEFAULT_WINDOW_MINUTES = 60
DEFAULT_SHARES = (0.10, 0.25, 0.40)


@dataclass(frozen=True)
class Scenario:
    """Одна точка стресс-теста: доля срочных и ширина их окна."""

    title: str
    urgent_share: float
    window_minutes: int
    seed: int


def _minutes(delta: timedelta) -> int:
    return round(delta.total_seconds() / 60)


def _narrow_window(
    request: ServiceRequestInput,
    day_start: datetime,
    day_end: datetime,
    width_minutes: int,
    rng: Random,
) -> tuple[datetime, datetime]:
    """Сужает окно до width_minutes внутри исходного окна и рабочего дня.

    Правая граница окна — последнее допустимое начало работ, поэтому от конца дня
    отнимается длительность визита. Если сужать нечего, окно остаётся прежним.
    """
    earliest = max(request.window_start, day_start)
    latest = min(request.window_end, day_end - request.duration_minutes * MINUTE)
    if latest <= earliest:
        return request.window_start, request.window_end
    span = _minutes(latest - earliest)
    width = min(width_minutes, span)
    offset = rng.randint(0, span - width)
    start = earliest + offset * MINUTE
    return start, start + width * MINUTE


def build_stress_input(
    planning_input: PlanningInput,
    urgent_share: float,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    seed: int = DEFAULT_SEED,
) -> PlanningInput:
    """Делает из набора более жёсткий вход: ровно urgent_share срочных с узким окном.

    Выборка детерминирована seed. Заявки вне выборки становятся обычными, поэтому доля
    срочных получается точной, а не «исходные плюс добавленные». Состав заявок, адреса,
    координаты, длительности и бригады не меняются — меняются приоритет и окно.
    """
    if not 0 <= urgent_share <= 1:
        raise ValueError("urgent_share должна быть в пределах 0..1")
    rng = Random(seed)
    day_start = min(engineer.shift_start for engineer in planning_input.engineers)
    day_end = max(engineer.shift_end for engineer in planning_input.engineers)
    urgent_count = round(urgent_share * len(planning_input.requests))
    urgent_ids = set(
        rng.sample([request.id for request in planning_input.requests], urgent_count)
    )

    requests: list[ServiceRequestInput] = []
    for request in planning_input.requests:
        payload = request.model_dump()
        if request.id in urgent_ids:
            window_start, window_end = _narrow_window(
                request, day_start, day_end, window_minutes, rng
            )
            payload["priority"] = Priority.URGENT
            payload["window_start"] = window_start
            payload["window_end"] = window_end
        else:
            payload["priority"] = Priority.NORMAL
        requests.append(ServiceRequestInput.model_validate(payload))
    return planning_input.model_copy(update={"requests": requests})


def _solver_report(
    stress_input: PlanningInput,
    algorithm_id: str,
    time_limit: int,
) -> dict[str, Any]:
    result = SOLVERS[algorithm_id].solve(
        stress_input, SolverConfig(time_limit_seconds=time_limit)
    )
    violations = validate_plan(stress_input, result)
    kpi = evaluate_result(stress_input, result, len(violations))
    assigned = {stop.request_id for route in result.routes for stop in route.stops}
    urgent_ids = {
        request.id for request in stress_input.requests if request.priority is Priority.URGENT
    }
    normal_ids = {request.id for request in stress_input.requests} - urgent_ids
    return {
        "алгоритм_в_плане": result.algorithm,
        "срочных_назначено": len(assigned & urgent_ids),
        "обычных_назначено": len(assigned & normal_ids),
        "срочных_не_назначено": result.metrics.urgent_unassigned_count,
        "пробег_км": round(result.metrics.total_distance_meters / 1000, 1),
        "длина_дня_мин": kpi["makespan_minutes"],
        "время_в_пути_мин": result.metrics.total_travel_minutes,
        "бригад_занято": result.metrics.used_engineers_count,
        "валидатор": "ок" if not violations else violations[:3],
        "elapsed_ms": result.elapsed_ms,
    }


def run_scenario(
    planning_input: PlanningInput,
    scenario: Scenario,
    time_limit: int,
) -> dict[str, Any]:
    stress_input = build_stress_input(
        planning_input,
        urgent_share=scenario.urgent_share,
        window_minutes=scenario.window_minutes,
        seed=scenario.seed,
    )
    urgent = [
        request for request in stress_input.requests if request.priority is Priority.URGENT
    ]
    windows = [_minutes(request.window_end - request.window_start) for request in urgent]
    return {
        "срочных_всего": len(urgent),
        "обычных_всего": len(stress_input.requests) - len(urgent),
        "окно_срочных_мин": {"мин": min(windows, default=0), "макс": max(windows, default=0)},
        "seed": scenario.seed,
        "алгоритмы": {
            algorithm_id: _solver_report(stress_input, algorithm_id, time_limit)
            for algorithm_id in SOLVERS
        },
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Стресс-сценарии по срочным заявкам")
    parser.add_argument("--zone", default="vostok")
    parser.add_argument("--time-limit", type=int, default=10)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    args = parser.parse_args()

    planning_input = await load_input(args.zone)
    await dispose_engine()

    scenarios = [
        Scenario(
            title=f"{round(share * 100)} % срочных",
            urgent_share=share,
            window_minutes=args.window_minutes,
            seed=args.seed,
        )
        for share in DEFAULT_SHARES
    ]
    payload: dict[str, Any] = {
        "зона": args.zone,
        "заявок": len(planning_input.requests),
        "бригад": len(planning_input.engineers),
        "лимит_решателя_с": args.time_limit,
        "сценарии": {
            scenario.title: run_scenario(planning_input, scenario, args.time_limit)
            for scenario in scenarios
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
