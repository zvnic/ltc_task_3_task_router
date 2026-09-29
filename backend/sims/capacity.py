"""Замер нехватки бригад: сколько резервных бригад нужно, чтобы закрыть день зоны.

Запуск внутри контейнера backend_tools:
    python -m sims.capacity --zone yugo_vostok --time-limit 10
    python -m sims.capacity --zone all --travel-norm-mode hard
    python -m sims.capacity --zone all --without-travel-norm

Скрипт ничего не пишет в БД: берёт сидированный набор зоны, добавляет в память по одной
синтетической бригаде и пересчитывает день лучшим решателем (`sims.sim.plan_with` со всеми
алгоритмами), пока не закроются все заявки, не встанет прирост или не упрёмся в предел
`PlanningInput.engineers` (max_length в схеме). Печатает JSON по шагам.

Резервная бригада — клон реальной: тот же офис, смена, транспорт и оснащение, уникальный
id и имя «Резерв N». Навыки — дефицитные среди оставшихся отказов. Какую реальную бригаду
клонировать, решает детерминированное правило `choose_template`: берётся та, чей клон в
одиночку может обслужить больше всего отказанных заявок (тот же `calculate_route`, что у
решателей), при равенстве — меньше суммарной дороги из офиса, длиннее смена, раньше в
списке. Случайности нет; недетерминированным может быть только сам решатель с лимитом
времени (OR-Tools и локальный поиск останавливаются по часам).

Против этого разброса два предохранителя (`best_plan`): на шаг делается `--repeats`
свежих расчётов, и в выборе участвует план прошлого шага — на расширенном входе он
допустим, новая бригада в нём просто не выезжает. Поэтому число отказов по шагам не
растёт, а все сырые попытки печатаются рядом.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter
from typing import Any

from app.core.config import get_settings
from app.db import dispose_engine
from app.planning import validate_plan
from app.planning.objective import plan_objective_key
from app.planning.policies import travel_norm_mode
from app.planning.registry import SOLVERS
from app.planning.routing import effective_edge_metrics, haversine_meters
from app.planning.schedule import calculate_route
from app.schemas import (
    EngineerInput,
    PlanningInput,
    PlanningResult,
    RequiredSkill,
    ServiceRequestInput,
)
from sims.sim import load_input, plan_with

ZONES = ("vostok", "yugo_vostok", "yugocenter")
RESERVE_PREFIX = "reserve-"
# Фиксированное пространство имён: id резервных бригад одинаковы от прогона к прогону.
RESERVE_NAMESPACE = uuid.UUID("6f1d3c52-3b7e-5a0e-9d64-0c2b8d1e2a40")


def engineers_cap() -> int:
    """Предел числа бригад во входе решателя — берётся из самой схемы, а не константой."""
    for item in PlanningInput.model_fields["engineers"].metadata:
        max_length = getattr(item, "max_length", None)
        if isinstance(max_length, int):
            return max_length
    raise RuntimeError("у PlanningInput.engineers не задан max_length")


def is_reserve(engineer: EngineerInput) -> bool:
    return engineer.external_id.startswith(RESERVE_PREFIX)


def deficit_skills(
    refused: list[ServiceRequestInput],
) -> list[RequiredSkill]:
    """Навыки отказанных заявок, самые частые первыми (при равенстве — по имени)."""
    counts = Counter(request.required_skill for request in refused)
    return sorted(counts, key=lambda skill: (-counts[skill], skill.value))


def make_clone(
    template: EngineerInput,
    skills: list[RequiredSkill],
    number: int,
    zone: str,
    input_order: int,
) -> EngineerInput:
    return EngineerInput(
        id=str(uuid.uuid5(RESERVE_NAMESPACE, f"{zone}:reserve:{number}")),
        external_id=f"{RESERVE_PREFIX}{number:02d}",
        input_order=input_order,
        name=f"Резерв {number}",
        start_location=template.start_location,
        shift_start=template.shift_start,
        shift_end=template.shift_end,
        skills=skills,
        transport=template.transport,
        equipment_inventory=dict(template.equipment_inventory),
        is_synthetic=True,
    )


def choose_template(
    planning_input: PlanningInput,
    refused: list[ServiceRequestInput],
    skills: list[RequiredSkill],
    number: int,
    zone: str,
) -> tuple[EngineerInput, dict[str, Any]]:
    """Выбирает реальную бригаду-образец и возвращает готовый клон и факты выбора.

    Кандидаты — реальные бригады с разными (офис, смена, транспорт, оснащение).
    Охват — сколько отказанных заявок клон может обслужить отдельным визитом по тем же
    правилам расписания, что у решателей (окно, смена, пешее плечо, в режиме hard —
    норматив дороги). Максимум охвата, затем меньше дороги из офиса до охваченных,
    затем длиннее смена, затем раньше во входном порядке.
    """
    next_order = max(engineer.input_order for engineer in planning_input.engineers) + 1
    seen: set[tuple[Any, ...]] = set()
    ranked: list[tuple[tuple[int, int, int, int], EngineerInput, dict[str, Any]]] = []
    for template in sorted(planning_input.engineers, key=lambda item: item.input_order):
        if is_reserve(template):
            continue
        signature = (
            template.start_location.latitude,
            template.start_location.longitude,
            template.shift_start,
            template.shift_end,
            template.transport,
            tuple(sorted(template.equipment_inventory.items())),
        )
        if signature in seen:
            continue
        seen.add(signature)
        clone = make_clone(template, skills, number, zone, next_order)
        reachable = [
            request
            for request in refused
            if calculate_route(clone, [request], planning_input.buffer_minutes) is not None
        ]
        travel = sum(
            effective_edge_metrics(clone.start_location, request.coordinates, clone.transport)[1]
            for request in reachable
        )
        shift_minutes = round((template.shift_end - template.shift_start).total_seconds() / 60)
        key = (-len(reachable), travel, -shift_minutes, template.input_order)
        facts = {
            "образец": template.name,
            "офис": [template.start_location.latitude, template.start_location.longitude],
            "смена": f"{template.shift_start:%H:%M}–{template.shift_end:%H:%M}",
            "транспорт": template.transport.value,
            "навыки": [skill.value for skill in skills],
            "охват_отказов_одиночным_визитом": len(reachable),
        }
        ranked.append((key, clone, facts))
    if not ranked:
        raise RuntimeError("нет реальных бригад для клонирования")
    _, clone, facts = min(ranked, key=lambda item: item[0])
    facts["кандидатов_образцов"] = len(ranked)
    return clone, facts


def with_engineer(planning_input: PlanningInput, engineer: EngineerInput) -> PlanningInput:
    """Новый вход с добавленной бригадой — через валидацию схемы, а не model_copy.

    Так предел max_length у `engineers` проверяет сама доменная схема: обойти его в
    замере нельзя даже случайно.
    """
    payload = planning_input.model_dump()
    payload["engineers"] = [item.model_dump() for item in [*planning_input.engineers, engineer]]
    return PlanningInput.model_validate(payload)


def without_travel_norm(planning_input: PlanningInput) -> PlanningInput:
    """Снимает норматив дороги в памяти: у заявок пропадает `travel_norm_minutes`.

    Без норматива его нечем ни запрещать (hard), ни штрафовать (soft) — это замер
    «норматив снят», с которым сравнивается штрафуемый режим.
    """
    payload = planning_input.model_dump()
    for request in payload["requests"]:
        enrichment = request["source_fields"].get("_enrichment")
        if isinstance(enrichment, dict):
            request["source_fields"]["_enrichment"] = {
                key: value for key, value in enrichment.items() if key != "travel_norm_minutes"
            }
    return PlanningInput.model_validate(payload)


def refused_requests(
    planning_input: PlanningInput, result: PlanningResult
) -> list[ServiceRequestInput]:
    request_by_id = {request.id: request for request in planning_input.requests}
    return [request_by_id[item.request_id] for item in result.unassigned]


def idle_substitutes(
    planning_input: PlanningInput, result: PlanningResult
) -> dict[str, str | None]:
    """Может ли простаивающая реальная бригада взять маршрут резервной целиком.

    Для каждой резервной бригады с визитами ищется реальная бригада без визитов, которая
    проходит ровно тот же маршрут (`calculate_route`: навыки, окна, смена, пешее плечо,
    в режиме hard — норматив). Если такая есть, резерв не был нужен: тот же охват
    достижим штатным составом, и отказ был промахом поиска, а не нехваткой людей.
    """
    request_by_id = {request.id: request for request in planning_input.requests}
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    busy = {route.engineer_id for route in result.routes if route.stops}
    idle = [
        engineer
        for engineer in planning_input.engineers
        if not is_reserve(engineer) and engineer.id not in busy
    ]
    taken: set[str] = set()
    substitutes: dict[str, str | None] = {}
    for route in result.routes:
        engineer = engineer_by_id[route.engineer_id]
        if not is_reserve(engineer) or not route.stops:
            continue
        requests = [request_by_id[stop.request_id] for stop in route.stops]
        substitute = next(
            (
                candidate
                for candidate in idle
                if candidate.id not in taken
                and calculate_route(candidate, requests, planning_input.buffer_minutes) is not None
            ),
            None,
        )
        if substitute is not None:
            taken.add(substitute.id)
        substitutes[engineer.name] = substitute.name if substitute is not None else None
    return substitutes


def step_summary(
    planning_input: PlanningInput,
    algorithm: str,
    result: PlanningResult,
    elapsed_ms: int,
) -> dict[str, Any]:
    metrics = result.metrics
    request_by_id = {request.id: request for request in planning_input.requests}
    offices = {
        (engineer.start_location.latitude, engineer.start_location.longitude): engineer
        for engineer in planning_input.engineers
    }
    visits_by_engineer = {route.engineer_id: len(route.stops) for route in result.routes}
    refusals = []
    for item in result.unassigned:
        request = request_by_id[item.request_id]
        nearest_office_km = min(
            haversine_meters(engineer.start_location, request.coordinates)
            for engineer in offices.values()
        )
        refusals.append(
            {
                "заявка": item.external_id,
                "навык": request.required_skill.value,
                "класс": item.priority_class.value,
                "причина": item.reason_code,
                "окно": f"{request.window_start:%H:%M}–{request.window_end:%H:%M}",
                "длительность_мин": request.duration_minutes,
                "до_ближайшего_офиса_км": round(nearest_office_km / 1000, 1),
            }
        )
    return {
        "бригад": len(planning_input.engineers),
        "из_них_резерв": sum(is_reserve(engineer) for engineer in planning_input.engineers),
        "алгоритм": algorithm,
        "назначено": metrics.assigned_count,
        "отказов": metrics.unassigned_count,
        "отказов_по_классам": {
            "аварии": metrics.emergency_unassigned_count,
            "подключения": metrics.connection_unassigned_count,
            "ремонты": metrics.routine_unassigned_count,
        },
        "занято_бригад": metrics.used_engineers_count,
        "пробег_км": round(metrics.total_distance_meters / 1000, 1),
        "время_в_пути_мин": metrics.total_travel_minutes,
        "превышений_норматива": metrics.norm_violation_count,
        "визитов_у_резерва": {
            engineer.name: visits_by_engineer.get(engineer.id, 0)
            for engineer in planning_input.engineers
            if is_reserve(engineer)
        },
        "простаивает_штатных": sum(
            not is_reserve(engineer) and not visits_by_engineer.get(engineer.id, 0)
            for engineer in planning_input.engineers
        ),
        "резерв_заменим_простаивающей": idle_substitutes(planning_input, result),
        "причины_отказов": dict(
            sorted(Counter(item.reason_code for item in result.unassigned).items())
        ),
        "отказы": refusals,
        "расчёт_мс": elapsed_ms,
    }


def timed_plan(planning_input: PlanningInput, time_limit: int) -> tuple[str, PlanningResult, int]:
    started = time.perf_counter()
    algorithm, result = plan_with(planning_input, list(SOLVERS), time_limit)
    return algorithm, result, round((time.perf_counter() - started) * 1000)


def best_plan(
    planning_input: PlanningInput,
    time_limit: int,
    repeats: int,
    carried: tuple[str, PlanningResult] | None,
) -> tuple[str, PlanningResult, int, list[dict[str, Any]], str]:
    """Лучший план шага: `repeats` свежих расчётов плюс план предыдущего шага.

    Решатели останавливаются по часам, и под нагрузкой один и тот же вход даёт разные
    планы: без страховки лишняя бригада «ухудшала» день. План предыдущего шага на
    расширенном входе остаётся допустимым — новая бригада в нём просто не выезжает, —
    поэтому он участвует в выборе, но только после проверки валидатором. Все свежие
    попытки печатаются, чтобы разброс решателя был виден в отчёте.
    """
    attempts: list[dict[str, Any]] = []
    candidates: list[tuple[tuple[int, ...], int, str, PlanningResult, str]] = []
    elapsed_total = 0
    for attempt in range(repeats):
        algorithm, result, elapsed = timed_plan(planning_input, time_limit)
        elapsed_total += elapsed
        key = plan_objective_key(result.metrics)
        attempts.append(
            {
                "алгоритм": algorithm,
                "отказов": result.metrics.unassigned_count,
                "ключ_цели": list(key),
                "расчёт_мс": elapsed,
            }
        )
        candidates.append((key, attempt, algorithm, result, "свежий расчёт"))
    if carried is not None:
        algorithm, result = carried
        violations = validate_plan(planning_input, result)
        if violations:
            raise RuntimeError(f"план прошлого шага не прошёл валидатор: {violations[:5]}")
        candidates.append(
            (plan_objective_key(result.metrics), repeats, algorithm, result, "план прошлого шага")
        )
    key, _, algorithm, result, source = min(candidates, key=lambda item: (item[0], item[1]))
    return algorithm, result, elapsed_total, attempts, source


def progress(message: str) -> None:
    # ход прогона — в stderr, чтобы stdout оставался чистым JSON
    print(message, file=sys.stderr, flush=True)


def measure_zone(
    zone: str,
    planning_input: PlanningInput,
    time_limit: int,
    patience: int,
    repeats: int,
) -> dict[str, Any]:
    cap = engineers_cap()
    algorithm, result, elapsed, attempts, source = best_plan(
        planning_input, time_limit, repeats, None
    )
    steps: list[dict[str, Any]] = [
        {
            "шаг": 0,
            "добавлена": None,
            "взят": source,
            "попытки": attempts,
            **step_summary(planning_input, algorithm, result, elapsed),
        }
    ]
    progress(
        f"{zone}: шаг 0, бригад {len(planning_input.engineers)}, "
        f"отказов {result.metrics.unassigned_count}, попытки "
        f"{[item['отказов'] for item in attempts]}"
    )
    stalled = 0
    stop: str
    number = 0
    while True:
        refused = refused_requests(planning_input, result)
        if not refused:
            stop = "день закрыт: отказов нет"
            break
        if len(planning_input.engineers) >= cap:
            stop = (
                f"упёрлись в предел PlanningInput.engineers max_length={cap}: "
                "больше бригад решателю не подать"
            )
            break
        number += 1
        skills = deficit_skills(refused)
        clone, facts = choose_template(planning_input, refused, skills, number, zone)
        planning_input = with_engineer(planning_input, clone)
        previous = result.metrics.unassigned_count
        algorithm, result, elapsed, attempts, source = best_plan(
            planning_input, time_limit, repeats, (algorithm, result)
        )
        gain = previous - result.metrics.unassigned_count
        steps.append(
            {
                "шаг": number,
                "добавлена": {"имя": clone.name, **facts},
                "прирост_назначений": gain,
                "взят": source,
                "попытки": attempts,
                **step_summary(planning_input, algorithm, result, elapsed),
            }
        )
        progress(
            f"{zone}: шаг {number}, бригад {len(planning_input.engineers)}, "
            f"отказов {result.metrics.unassigned_count} (прирост {gain}, {source}), попытки "
            f"{[item['отказов'] for item in attempts]}"
        )
        stalled = stalled + 1 if gain <= 0 else 0
        if stalled >= patience:
            stop = f"прирост встал: {stalled} шаг(а) подряд без новых назначений"
            break

    first, last = steps[0], steps[-1]
    return {
        "зона": zone,
        "заявок": len(planning_input.requests),
        "итог": {
            "остановка": stop,
            "бригад_было": first["бригад"],
            "бригад_стало": last["бригад"],
            "отказов_было": first["отказов"],
            "отказов_стало": last["отказов"],
            "пробег_км_было": first["пробег_км"],
            "пробег_км_стало": last["пробег_км"],
            "время_в_пути_мин_было": first["время_в_пути_мин"],
            "время_в_пути_мин_стало": last["время_в_пути_мин"],
        },
        "шаги": steps,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Замер нехватки бригад по зонам")
    parser.add_argument("--zone", default="yugo_vostok", choices=[*ZONES, "all"])
    parser.add_argument("--time-limit", type=int, default=10)
    parser.add_argument(
        "--patience",
        type=int,
        default=2,
        help="сколько шагов подряд без прироста считать остановкой",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=2,
        help="сколько свежих расчётов на шаг (берётся лучший по plan_objective_key)",
    )
    parser.add_argument(
        "--travel-norm-mode",
        choices=["soft", "hard"],
        default=None,
        help="переопределить TRAVEL_NORM_MODE на время прогона",
    )
    parser.add_argument(
        "--without-travel-norm",
        action="store_true",
        help="снять норматив дороги в памяти: у заявок пропадает travel_norm_minutes",
    )
    args = parser.parse_args()

    if args.travel_norm_mode is not None:
        os.environ["TRAVEL_NORM_MODE"] = args.travel_norm_mode
        get_settings.cache_clear()

    zones = list(ZONES) if args.zone == "all" else [args.zone]
    inputs = {zone: await load_input(zone) for zone in zones}
    await dispose_engine()
    if args.without_travel_norm:
        inputs = {zone: without_travel_norm(value) for zone, value in inputs.items()}

    payload: dict[str, Any] = {
        "замер": "нехватка бригад",
        "режим_норматива": "снят в памяти" if args.without_travel_norm else travel_norm_mode(),
        "цель": get_settings().objective_mode,
        "лимит_решателя_с": args.time_limit,
        "расчётов_на_шаг": args.repeats,
        "терпение_шагов": args.patience,
        "алгоритмы": list(SOLVERS),
        "предел_бригад": engineers_cap(),
        "зоны": [
            measure_zone(zone, inputs[zone], args.time_limit, args.patience, args.repeats)
            for zone in zones
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
