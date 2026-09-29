"""Замер режима норматива дороги: advisory, hard и soft с потолком ×1,5 / ×2,0 / ×3,0.

Запуск внутри контейнера backend_tools (один, без параллельных прогонов — лимит решателя
считается по часам, и чужая нагрузка портит план):
    python -m sims.norm_sweep
    python -m sims.norm_sweep --zones yugo_vostok --configs soft:2.0 --repeats 1
    python -m sims.norm_sweep --out /artifacts/norm_sweep.json

Скрипт ничего не пишет в БД: читает сидированные наборы зон, а дальше для каждой зоны и
конфигурации `--repeats` раз подряд считает лучший план всеми решателями
(`sims.sim.plan_with` — тот же выбор по `plan_objective_key`, что у приложения, с
проверкой валидатором). Конфигурация ставится через os.environ и
`get_settings.cache_clear()` внутри одного процесса; после переключения режим читается
обратно через `app.planning.policies`, и при расхождении прогон останавливается —
молча посчитать hard под видом soft нельзя.

Превышение норматива пересчитывается заново по самим заявкам
(`request_travel_norm_minutes`) и дороге остановок и сверяется с `PlanMetrics`: при
расхождении прогон тоже останавливается. Печатает JSON в stdout, ход — в stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.db import dispose_engine
from app.planning.policies import (
    request_travel_norm_minutes,
    travel_norm_max_factor,
    travel_norm_mode,
    travel_norm_policy,
)
from app.planning.registry import SOLVERS
from app.planning.routing import haversine_meters, travel_norm_excess_minutes
from app.schemas import EngineerInput, PlanningInput, PlanningResult
from sims.sim import load_input, plan_with

ZONES = ("vostok", "yugo_vostok", "yugocenter")
# Корзины превышения одного плеча, минуты сверх норматива (включительно).
EXCESS_BUCKETS = ((1, 5), (6, 10), (11, 20), (21, 40), (41, 10_000))


@dataclass(frozen=True)
class NormConfig:
    mode: str
    factor: float

    @property
    def label(self) -> str:
        return self.mode if self.mode in {"hard", "advisory"} else f"soft ×{self.factor:g}"


DEFAULT_CONFIGS = (
    NormConfig("advisory", 2.0),
    NormConfig("hard", 2.0),
    NormConfig("soft", 1.5),
    NormConfig("soft", 2.0),
    NormConfig("soft", 3.0),
)


def parse_config(text: str) -> NormConfig:
    """`advisory`, `hard` или `soft:<множитель>`."""
    if text in {"hard", "advisory"}:
        return NormConfig(text, 2.0)
    mode, _, factor = text.partition(":")
    if mode != "soft" or not factor:
        raise argparse.ArgumentTypeError(
            f"ожидается advisory, hard или soft:<множитель>, а не {text!r}"
        )
    return NormConfig("soft", float(factor))


def apply_config(config: NormConfig) -> None:
    """Ставит режим в окружение процесса и проверяет, что приложение его видит."""
    os.environ["TRAVEL_NORM_MODE"] = config.mode
    os.environ["TRAVEL_NORM_MAX_FACTOR"] = repr(config.factor)
    get_settings.cache_clear()
    if travel_norm_mode() != config.mode or travel_norm_max_factor() != config.factor:
        raise SystemExit(
            f"режим не переключился: хотели {config.label}, приложение видит "
            f"{travel_norm_mode()} ×{travel_norm_max_factor()}"
        )


def progress(message: str) -> None:
    # ход прогона — в stderr, чтобы stdout оставался чистым JSON
    print(message, file=sys.stderr, flush=True)


def load_average() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def bucket_label(low: int, high: int) -> str:
    return f"{low}+" if high >= 10_000 else f"{low}–{high}"


def office_key(engineer: EngineerInput) -> str:
    return f"{engineer.start_location.latitude:.4f},{engineer.start_location.longitude:.4f}"


def refusals(planning_input: PlanningInput, result: PlanningResult) -> list[dict[str, Any]]:
    """Отказы плана с расстоянием по прямой до ближайшего офиса (любой бригады)."""
    request_by_id = {request.id: request for request in planning_input.requests}
    out = []
    for item in result.unassigned:
        request = request_by_id[item.request_id]
        meters, office = min(
            (haversine_meters(engineer.start_location, request.coordinates), office_key(engineer))
            for engineer in planning_input.engineers
        )
        out.append(
            {
                "заявка": item.external_id,
                "класс": item.priority_class.value,
                "причина": item.reason_code,
                "окно": f"{request.window_start:%H:%M}–{request.window_end:%H:%M}",
                "ближайший_офис": office,
                "до_ближайшего_офиса_км": round(meters / 1000, 1),
            }
        )
    return out


def leg_summary(planning_input: PlanningInput, result: PlanningResult) -> dict[str, Any]:
    """Плечи плана: превышения норматива, пересчитанные по заявкам, и самое длинное плечо."""
    request_by_id = {request.id: request for request in planning_input.requests}
    engineer_by_id = {engineer.id: engineer for engineer in planning_input.engineers}
    count = 0
    minutes = 0
    worst_excess = 0
    worst_excess_leg: dict[str, Any] | None = None
    longest = 0
    longest_leg: dict[str, Any] | None = None
    buckets: Counter[str] = Counter()
    for route in result.routes:
        engineer = engineer_by_id[route.engineer_id]
        for stop in route.stops:
            request = request_by_id[stop.request_id]
            norm = request_travel_norm_minutes(request)
            travel = stop.travel_minutes_from_previous
            leg = {
                "заявка": request.external_id,
                "бригада": route.engineer_name,
                "транспорт": route.transport.value,
                "дорога_мин": travel,
                "норматив_мин": norm,
                "км": round(stop.distance_meters_from_previous / 1000, 1),
                "номер_в_маршруте": stop.sequence,
                "визитов_в_маршруте": len(route.stops),
                "офис_бригады": office_key(engineer),
                "от_офиса_бригады_по_прямой_км": round(
                    haversine_meters(engineer.start_location, request.coordinates) / 1000, 1
                ),
            }
            if travel > longest:
                longest, longest_leg = travel, leg
            excess = travel_norm_excess_minutes(travel, norm)
            if excess <= 0:
                continue
            count += 1
            minutes += excess
            for low, high in EXCESS_BUCKETS:
                if low <= excess <= high:
                    buckets[bucket_label(low, high)] += 1
                    break
            if excess > worst_excess:
                worst_excess, worst_excess_leg = excess, leg
    metrics = result.metrics
    if (count, minutes) != (metrics.norm_violation_count, metrics.norm_excess_minutes):
        raise SystemExit(
            f"превышения не сходятся с метриками плана: пересчёт {count} шт / {minutes} мин, "
            f"в плане {metrics.norm_violation_count} / {metrics.norm_excess_minutes}"
        )
    return {
        "превышений": count,
        "минут_превышения": minutes,
        "макс_превышение_плеча_мин": worst_excess,
        "плечо_с_макс_превышением": worst_excess_leg,
        "превышения_по_величине": {
            bucket_label(low, high): buckets.get(bucket_label(low, high), 0)
            for low, high in EXCESS_BUCKETS
        },
        "самое_длинное_плечо_мин": longest,
        "самое_длинное_плечо": longest_leg,
    }


def one_run(planning_input: PlanningInput, time_limit: int) -> dict[str, Any]:
    load_before = load_average()
    started = time.perf_counter()
    algorithm, result = plan_with(planning_input, list(SOLVERS), time_limit)
    wall_ms = round((time.perf_counter() - started) * 1000)
    metrics = result.metrics
    return {
        "решатель": algorithm,
        "назначено": metrics.assigned_count,
        "отказов": metrics.unassigned_count,
        "отказов_по_классам": {
            "аварии": metrics.emergency_unassigned_count,
            "подключения": metrics.connection_unassigned_count,
            "обычные": metrics.routine_unassigned_count,
        },
        "причины_отказов": dict(
            sorted(Counter(item.reason_code for item in result.unassigned).items())
        ),
        "отказы": refusals(planning_input, result),
        **leg_summary(planning_input, result),
        "бригад_занято": metrics.used_engineers_count,
        "пробег_км": round(metrics.total_distance_meters / 1000, 1),
        "время_в_пути_мин": metrics.total_travel_minutes,
        "расчёт_решателя_мс": result.elapsed_ms,
        "расчёт_всех_решателей_мс": wall_ms,
        "нагрузка_1мин_до": load_before,
    }


def spread(runs: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [run[key] for run in runs]
    return {
        "мин": min(values),
        "макс": max(values),
        "медиана": statistics.median(values),
        "значения": values,
    }


def config_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: spread(runs, key)
        for key in (
            "отказов",
            "превышений",
            "минут_превышения",
            "макс_превышение_плеча_мин",
            "самое_длинное_плечо_мин",
            "пробег_км",
            "время_в_пути_мин",
            "расчёт_всех_решателей_мс",
        )
    } | {"решатели": dict(Counter(run["решатель"] for run in runs))}


def totals(zones: list[dict[str, Any]], configs: list[NormConfig]) -> dict[str, Any]:
    """Итог по зонам: суммы по номеру повтора и суммы лучших/худших по зонам."""
    out: dict[str, Any] = {}
    for config in configs:
        per_zone = [
            next(item for item in zone["конфигурации"] if item["конфигурация"] == config.label)
            for zone in zones
        ]
        repeats = min(len(item["прогоны"]) for item in per_zone)
        by_repeat = [
            {
                "повтор": index + 1,
                "отказов": sum(item["прогоны"][index]["отказов"] for item in per_zone),
                "превышений": sum(item["прогоны"][index]["превышений"] for item in per_zone),
                "минут_превышения": sum(
                    item["прогоны"][index]["минут_превышения"] for item in per_zone
                ),
                "макс_превышение_плеча_мин": max(
                    item["прогоны"][index]["макс_превышение_плеча_мин"] for item in per_zone
                ),
                "пробег_км": round(
                    sum(item["прогоны"][index]["пробег_км"] for item in per_zone), 1
                ),
                "время_в_пути_мин": sum(
                    item["прогоны"][index]["время_в_пути_мин"] for item in per_zone
                ),
            }
            for index in range(repeats)
        ]
        out[config.label] = {
            "заявок": sum(zone["заявок"] for zone in zones),
            "по_повторам": by_repeat,
            "отказов_сумма_лучших_по_зонам": sum(
                item["сводка"]["отказов"]["мин"] for item in per_zone
            ),
            "отказов_сумма_худших_по_зонам": sum(
                item["сводка"]["отказов"]["макс"] for item in per_zone
            ),
            "макс_превышение_плеча_мин": max(
                item["сводка"]["макс_превышение_плеча_мин"]["макс"] for item in per_zone
            ),
        }
    return out


def measure_zone(
    zone: str,
    planning_input: PlanningInput,
    configs: list[NormConfig],
    repeats: int,
    time_limit: int,
) -> dict[str, Any]:
    norms = Counter(request_travel_norm_minutes(request) for request in planning_input.requests)
    items: list[dict[str, Any]] = []
    for config in configs:
        apply_config(config)
        policy = travel_norm_policy()
        runs: list[dict[str, Any]] = []
        for index in range(repeats):
            run = one_run(planning_input, time_limit)
            runs.append(run)
            progress(
                f"{zone} {config.label} #{index + 1}: {run['решатель']}, отказов "
                f"{run['отказов']}, превышений {run['превышений']} / "
                f"{run['минут_превышения']} мин, худшее плечо {run['самое_длинное_плечо_мин']} "
                f"мин, {run['расчёт_всех_решателей_мс']} мс, нагрузка {run['нагрузка_1мин_до']}"
            )
        items.append(
            {
                "конфигурация": config.label,
                "режим_в_приложении": travel_norm_mode(),
                "множитель_в_приложении": travel_norm_max_factor(),
                "потолок_плеча_по_нормативам": {
                    str(norm): policy.limit(norm) for norm in sorted(n for n in norms if n)
                },
                "прогоны": runs,
                "сводка": config_summary(runs),
            }
        )
    return {
        "зона": zone,
        "заявок": len(planning_input.requests),
        "бригад": len(planning_input.engineers),
        "нормативы_мин": {str(key): value for key, value in sorted(norms.items(), key=str)},
        "конфигурации": items,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Замер режима норматива дороги")
    parser.add_argument("--zones", nargs="+", default=list(ZONES), choices=ZONES)
    parser.add_argument(
        "--configs",
        nargs="+",
        type=parse_config,
        default=list(DEFAULT_CONFIGS),
        help="hard и/или soft:<множитель>, по умолчанию hard soft:1.5 soft:2.0 soft:3.0",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--time-limit", type=int, default=10)
    parser.add_argument("--out", default=None, help="дополнительно записать JSON в файл")
    args = parser.parse_args()

    inputs = {zone: await load_input(zone) for zone in args.zones}
    await dispose_engine()

    started = datetime.now(UTC)
    zones = [
        measure_zone(zone, inputs[zone], args.configs, args.repeats, args.time_limit)
        for zone in args.zones
    ]
    payload: dict[str, Any] = {
        "замер": "режим норматива дороги",
        "начато_utc": started.isoformat(timespec="seconds"),
        "закончено_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "цель": get_settings().objective_mode,
        "лимит_решателя_с": args.time_limit,
        "повторов": args.repeats,
        "алгоритмы": list(SOLVERS),
        "cpu_контейнера": os.cpu_count(),
        "зоны": zones,
        "итог": totals(zones, args.configs),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    # сначала stdout: ошибка пути в --out не должна стоить получаса расчёта (Git Bash
    # переписывает /artifacts/... в C:/Program Files/Git/...; звать с MSYS_NO_PATHCONV=1)
    print(text, flush=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")


if __name__ == "__main__":
    asyncio.run(main())
