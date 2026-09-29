import argparse
import asyncio
import json
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, cast
from uuid import UUID

import httpx

from app.core.config import get_settings
from app.core.errors import DomainError
from app.db import get_session
from app.imports.csv_reader import audit_sources
from app.planning import (
    evaluate_results,
    experiment_metadata,
    run_solvers,
    select_best,
    selection_reason,
    validate_plan,
)
from app.planning.metrics import objective_tuple
from app.schemas import PlanningInput
from app.services.datasets import (
    FIXTURE_ROOT,
    SOURCE_ROOT,
    create_dataset_from_csv,
    get_dataset_with_inputs,
    import_reference,
    planning_input_from_dataset,
    seed_service_zones,
    service_zone_configs,
)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


async def command_seed() -> None:
    async for session in get_session():
        datasets = await seed_service_zones(session)
        emit(
            {
                "status": "ok",
                "message": "Демонстрационные наборы трёх зон готовы.",
                "zones": [
                    {
                        "dataset_id": str(dataset.id),
                        "zone": dataset.assumptions.get("service_zone"),
                        "title": dataset.title,
                        "requests": len(dataset.requests),
                        "engineers": len(dataset.engineers),
                    }
                    for dataset in datasets
                ],
            }
        )


async def command_data_audit(output: Path) -> None:
    report = {
        "zones": {
            zone["code"]: {
                "name": zone["name"],
                "geography_note": zone["geography_note"],
                **audit_sources(
                    (SOURCE_ROOT / zone["synthetic_file"]).read_bytes(),
                    (SOURCE_ROOT / zone["control_file"]).read_bytes(),
                ),
            }
            for zone in service_zone_configs()
        }
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    emit({"status": "ok", "message": "Аудит данных завершён.", "output": str(output), **report})


async def command_import(path: Path) -> None:
    async for session in get_session():
        dataset = await create_dataset_from_csv(
            session,
            path.read_bytes(),
            title=f"Импорт · {path.name}",
        )
        emit({"status": "ok", "dataset_id": str(dataset.id), "requests": len(dataset.requests)})


async def command_import_reference(path: Path, dataset_id: UUID) -> None:
    async for session in get_session():
        dataset = await get_dataset_with_inputs(session, dataset_id)
        report = await import_reference(session, dataset, path.read_bytes())
        emit({"status": "ok", "dataset_id": str(dataset.id), **report})


async def _auth_login(client: httpx.AsyncClient, api_root: str) -> None:
    """Obtain a session cookie when password auth is enabled."""
    settings = get_settings()
    if not settings.auth_enabled:
        return
    login = await client.post(
        f"{api_root.rstrip('/')}/api/v1/auth/login",
        json={"password": settings.app_password},
    )
    login.raise_for_status()


async def post_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=55) as client:
        api_root = url.split("/api/v1/", maxsplit=1)[0]
        await _auth_login(client, api_root)
        response = await client.post(url, json=payload)
    if response.is_error:
        raise DomainError(
            "api_request_failed",
            f"API вернул HTTP {response.status_code}: {response.text[:500]}",
            status_code=response.status_code,
        )
    return cast(dict[str, Any], response.json())


async def command_plan(dataset_id: UUID, api_url: str) -> None:
    emit(await post_json(f"{api_url}/api/v1/datasets/{dataset_id}/plans"))


async def command_replan(plan_id: UUID, event_path: Path, api_url: str) -> None:
    payload = json.loads(event_path.read_text(encoding="utf-8"))
    emit(await post_json(f"{api_url}/api/v1/plans/{plan_id}/replan", payload))


async def command_smoke(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=90) as client:
        ready = await client.get("/api/v1/health/ready")
        ready.raise_for_status()
        await _auth_login(client, base_url.rstrip('/'))
        datasets = await client.get("/api/v1/datasets")
        datasets.raise_for_status()
        items = datasets.json()
        if not items:
            raise DomainError("smoke_no_dataset", "Smoke не нашёл демонстрационный набор.")
        dataset_id = items[0]["id"]
        requests = await client.get(f"/api/v1/datasets/{dataset_id}/requests?limit=1")
        requests.raise_for_status()
        if requests.json()["total"] < 1:
            raise DomainError("smoke_no_requests", "В демонстрационном наборе нет заявок.")
        plan = await client.post(f"/api/v1/datasets/{dataset_id}/plans")
        plan.raise_for_status()
        plan_payload = plan.json()
        selected = await client.get(f"/api/v1/plans/{plan_payload['plan_id']}")
        selected.raise_for_status()
        selected_payload = selected.json()
        road_routes_by_transport: dict[str, dict[str, Any]] = {}
        for route in selected_payload["result"]["routes"]:
            transport = str(route["transport"])
            if not route["stops"] or transport == "public_transport":
                continue
            current = road_routes_by_transport.get(transport)
            if current is None or len(route["stops"]) > len(current["stops"]):
                road_routes_by_transport[transport] = route
        if not road_routes_by_transport:
            raise DomainError(
                "smoke_no_road_route",
                "Smoke не нашёл непустой автомобильный, пеший или велосипедный маршрут.",
            )
        route_checks: list[dict[str, Any]] = []
        for transport, road_route in sorted(road_routes_by_transport.items()):
            detailed = await client.get(
                f"/api/v1/plans/{plan_payload['plan_id']}"
                f"/engineers/{road_route['engineer_id']}/route"
            )
            if detailed.is_error:
                raise DomainError(
                    "smoke_routing_unavailable",
                    f"Геометрия маршрута {transport} недоступна: "
                    f"HTTP {detailed.status_code} {detailed.text[:500]}",
                    status_code=detailed.status_code,
                )
            detailed_payload = detailed.json()
            coordinate_count = len(detailed_payload["geometry"]["coordinates"])
            segment_count = len(detailed_payload["segments"])
            if coordinate_count < 2 or segment_count != len(road_route["stops"]):
                raise DomainError(
                    "smoke_routing_invalid",
                    f"Геометрия маршрута {transport} не соответствует остановкам плана.",
                    details={
                        "coordinates": coordinate_count,
                        "segments": segment_count,
                        "stops": len(road_route["stops"]),
                    },
                )
            route_checks.append(
                {
                    "transport": transport,
                    "routing_method": detailed_payload["routing_method"],
                    "segments": segment_count,
                    "coordinates": coordinate_count,
                }
            )
        html = await client.get("/")
        html.raise_for_status()
        if "Task Router Диспетчерская" not in html.text:
            raise DomainError("smoke_web_invalid", "Web origin не содержит приложение.")
    emit(
        {
            "status": "ok",
            "message": "API, БД, UI-origin, расчёт и дорожная геометрия доступны.",
            "dataset_id": dataset_id,
            "plan_id": plan_payload["plan_id"],
            "checked_routes": route_checks,
        }
    )


def _expanded_input(source: PlanningInput) -> PlanningInput:
    requests = [request.model_copy(deep=True) for request in source.requests]
    for index, original_request in enumerate(source.requests[:34], start=len(requests)):
        requests.append(
            original_request.model_copy(
                deep=True,
                update={
                    "id": f"benchmark-request-{index}",
                    "external_id": f"BENCH-{index:03d}",
                    "input_order": index,
                },
            )
        )
    engineers = [engineer.model_copy(deep=True) for engineer in source.engineers]
    for index, original_engineer in enumerate(source.engineers[:3], start=len(engineers)):
        engineers.append(
            original_engineer.model_copy(
                deep=True,
                update={
                    "id": f"benchmark-engineer-{index}",
                    "external_id": f"BENCH-ENG-{index:02d}",
                    "input_order": index,
                    "name": f"Тестовая бригада {index + 1}",
                },
            )
        )
    return source.model_copy(
        deep=True,
        update={"dataset_id": "benchmark-100-15", "requests": requests, "engineers": engineers},
    )


def _measure(planning_input: PlanningInput, runs: int, time_limit_seconds: int) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for run_number in range(1, runs + 1):
        started_at = perf_counter()
        results = run_solvers(
            planning_input,
            ["baseline_v1", "ortools_gls_v1", "insertion_ls_v1"],
            time_limit_seconds,
        )
        selected_algorithm, selected = select_best(results)
        evaluations = evaluate_results(planning_input, results)
        baseline = results["baseline_v1"]
        wall_ms = round((perf_counter() - started_at) * 1000)
        violations = validate_plan(planning_input, selected)
        if violations:
            raise DomainError(
                "benchmark_invalid_plan",
                "Validator отклонил результат benchmark.",
                details={"violations": violations},
            )
        samples.append(
            {
                "run": run_number,
                "wall_ms": wall_ms,
                "algorithm": selected.algorithm,
                "selected_algorithm": selected_algorithm,
                "termination_reason": selected.termination_reason,
                "baseline_tuple": objective_tuple(baseline.metrics),
                "selected_tuple": objective_tuple(selected.metrics),
                "selection_reason": selection_reason(selected_algorithm, results),
                "metrics": selected.metrics.model_dump(mode="json"),
                "algorithm_results": {
                    algorithm_id: {
                        "objective_tuple": objective_tuple(result.metrics),
                        "elapsed_ms": result.elapsed_ms,
                        "termination_reason": result.termination_reason,
                        "metrics": result.metrics.model_dump(mode="json"),
                        "kpis": evaluations[algorithm_id],
                    }
                    for algorithm_id, result in results.items()
                },
            }
        )
    return {
        "request_count": len(planning_input.requests),
        "engineer_count": len(planning_input.engineers),
        "experiment": experiment_metadata(
            planning_input,
            ["baseline_v1", "ortools_gls_v1", "insertion_ls_v1"],
            time_limit_seconds,
        ),
        "runs": samples,
        "median_wall_ms": median(item["wall_ms"] for item in samples),
        "max_wall_ms": max(item["wall_ms"] for item in samples),
    }


async def _measure_gets(api_url: str) -> dict[str, Any]:
    samples: list[float] = []
    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:
        login = await client.post(
            "/api/v1/auth/login",
            json={"password": get_settings().app_password},
        )
        login.raise_for_status()
        for _ in range(20):
            started_at = perf_counter()
            response = await client.get("/api/v1/datasets")
            response.raise_for_status()
            samples.append((perf_counter() - started_at) * 1000)
    ordered = sorted(samples)
    return {
        "runs": 20,
        "median_ms": round(median(samples), 2),
        "p95_ms": round(ordered[18], 2),
        "max_ms": round(max(samples), 2),
    }


async def command_benchmark(output_dir: Path, api_url: str) -> None:
    async for session in get_session():
        datasets = await seed_service_zones(session)
        dataset = next(
            item for item in datasets if item.assumptions.get("service_zone") == "vostok"
        )
        current_input = planning_input_from_dataset(dataset)
        original_requests = [
            request
            for request in current_input.requests
            if "Тип заявки BK" in request.source_fields
        ][:66]
        demo_payload = current_input.model_dump(mode="json")
        demo_payload.update({"revision": 1, "requests": original_requests})
        demo_input = PlanningInput.model_validate(demo_payload)
        expanded_input = _expanded_input(demo_input)
        zone_inputs = {}
        for item in datasets:
            zone_input = planning_input_from_dataset(item)
            zone_inputs[str(item.assumptions.get("service_zone"))] = zone_input.model_copy(
                update={
                    "requests": [
                        request
                        for request in zone_input.requests
                        if "Тип заявки BK" in request.source_fields
                    ]
                }
            )
    fixture = cast(
        dict[str, Any],
        json.loads((FIXTURE_ROOT / "constraint_cases.json").read_text(encoding="utf-8")),
    )
    conflict_input = PlanningInput.model_validate(fixture["strict_improvement"])
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "solvers": ["baseline_v1", "ortools_gls_v1", "insertion_ls_v1"],
            "architecture": platform.machine(),
            "cpu_count_visible": os.cpu_count(),
            "solver_limit_seconds": 3,
            "request_timeout_seconds": 45,
        },
        "demo_66_12": _measure(demo_input, runs=5, time_limit_seconds=3),
        "zones": {
            zone_code: _measure(zone_input, runs=5, time_limit_seconds=3)
            for zone_code, zone_input in zone_inputs.items()
        },
        "scale_100_15": _measure(expanded_input, runs=5, time_limit_seconds=3),
        "strict_improvement": _measure(conflict_input, runs=1, time_limit_seconds=2),
        "datasets_get_20": await _measure_gets(api_url),
    }
    conflict_run = report["strict_improvement"]["runs"][0]
    if tuple(conflict_run["selected_tuple"]) >= tuple(conflict_run["baseline_tuple"]):
        raise DomainError(
            "benchmark_no_strict_improvement",
            "Контрольный fixture не показал строгое улучшение.",
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "results.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(
        {
            "status": "ok",
            "output": str(output_path),
            "demo_66_12_median_ms": report["demo_66_12"]["median_wall_ms"],
            "scale_100_15_median_ms": report["scale_100_15"]["median_wall_ms"],
            "strict_improvement": {
                "baseline_tuple": conflict_run["baseline_tuple"],
                "selected_tuple": conflict_run["selected_tuple"],
            },
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Управление прототипом маршрутов")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed")
    audit_parser = subparsers.add_parser("data-audit")
    audit_parser.add_argument("--output", type=Path, required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--file", type=Path, required=True)
    reference_parser = subparsers.add_parser("import-reference")
    reference_parser.add_argument("--file", type=Path, required=True)
    reference_parser.add_argument("--dataset-id", type=UUID, required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--dataset-id", type=UUID, required=True)
    plan_parser.add_argument("--api-url", default="http://api:8000")
    replan_parser = subparsers.add_parser("replan")
    replan_parser.add_argument("--plan-id", type=UUID, required=True)
    replan_parser.add_argument("--event-file", type=Path, required=True)
    replan_parser.add_argument("--api-url", default="http://api:8000")
    smoke_parser = subparsers.add_parser("smoke")
    smoke_parser.add_argument("--base-url", required=True)
    benchmark_parser = subparsers.add_parser("benchmark")
    benchmark_parser.add_argument("--output-dir", type=Path, required=True)
    benchmark_parser.add_argument("--api-url", default="http://api:8000")
    return parser


async def run(args: argparse.Namespace) -> None:
    match args.command:
        case "seed":
            await command_seed()
        case "data-audit":
            await command_data_audit(args.output)
        case "import":
            await command_import(args.file)
        case "import-reference":
            await command_import_reference(args.file, args.dataset_id)
        case "plan":
            await command_plan(args.dataset_id, args.api_url)
        case "replan":
            await command_replan(args.plan_id, args.event_file, args.api_url)
        case "smoke":
            await command_smoke(args.base_url)
        case "benchmark":
            await command_benchmark(args.output_dir, args.api_url)
        case _:
            raise DomainError("unknown_command", "Неизвестная команда.")


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except DomainError as exc:
        print(f"Ошибка [{exc.code}]: {exc.message}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
