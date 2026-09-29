"""Сравнение insertion_ls_v1 и исправленной v2 на всех трёх зонах."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.db import dispose_engine
from app.planning import build_insertion
from app.planning.contracts import SolverConfig
from app.planning.metrics import objective_tuple
from app.planning.registry import SOLVERS
from sims.insertion_v2 import build_insertion_v2
from sims.sim import kpis, load_input, simulate_execution


async def main() -> None:
    payload: dict[str, Any] = {}
    for zone in ("vostok", "yugo_vostok", "yugocenter"):
        planning_input = await load_input(zone)
        zone_result: dict[str, Any] = {
            "заявок": len(planning_input.requests),
            "инженеров": len(planning_input.engineers),
        }
        variants = {
            "insertion_v1": build_insertion(planning_input, time_limit_seconds=10),
            "insertion_v2": build_insertion_v2(planning_input, time_limit_seconds=10),
            "ortools_10s": SOLVERS["ortools_gls_v1"].solve(
                planning_input, SolverConfig(time_limit_seconds=10)
            ),
        }
        for name, result in variants.items():
            zone_result[name] = {
                "objective": list(objective_tuple(result.metrics)),
                "elapsed_ms": result.elapsed_ms,
                **kpis(planning_input, result),
                "срыв_окон_при_дороге_+25%": simulate_execution(
                    planning_input, result, 1.25, 1.0
                ).summary()["window_missed"],
            }
        payload[zone] = zone_result
    await dispose_engine()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
