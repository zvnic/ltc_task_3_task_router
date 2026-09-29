"""Живая проверка ручного назначения на поднятом стенде.

Запуск внутри контейнера backend_tools (сеть compose):
    python -m sims.live_check --base-url http://web:8080
    python -m sims.live_check --base-url http://web:8080 --zone yugo_vostok
    python -m sims.live_check --base-url http://web:8080 --plan-if-missing

Что делает, по шагам:
1. readiness и вход тем же способом, что `app.cli` (пароль из get_settings, не печатается);
2. находит набор зоны и его текущий (опубликованный) план; с --plan-if-missing, если плана
   нет, считает его через POST /datasets/{id}/plans;
3. перебирает отказанные заявки плана, для каждой зовёт GET /plans/{id}/placements, пока
   не найдёт заявку хотя бы с одним вариантом вставки;
4. если вариант есть — POST /plans/{id}/assign (вариант с наименьшим приростом пробега),
   печатает kind нового плана и revision набора до и после;
5. повторяет то же назначение в исходный план со старой revision — ждёт 409;
6. проверяет, что GET /plans/{новый id} отдаёт в метриках norm_violation_count и
   norm_excess_minutes.

ВНИМАНИЕ: шаг 4 пишет в БД стенда — создаёт план manual_insert и поднимает revision набора.
Флаг --dry-run останавливается после шага 3.

Печатаются только коды HTTP, коды ошибок и числа; cookie и пароль не выводятся.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx

from app.core.config import get_settings

NORM_FIELDS = ("norm_violation_count", "norm_excess_minutes")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def error_code(response: httpx.Response) -> str | None:
    """Код доменной ошибки из тела ответа, если он там есть."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        code = body.get("code")
        return str(code) if code is not None else None
    return None


async def dataset_row(client: httpx.AsyncClient, zone: str) -> dict[str, Any]:
    response = await client.get("/api/v1/datasets")
    response.raise_for_status()
    rows: list[dict[str, Any]] = response.json()
    for row in rows:
        if row.get("service_zone") == zone:
            return row
    raise SystemExit(
        f"Нет набора для зоны {zone}. Есть: {[row.get('service_zone') for row in rows]}"
    )


def norm_metrics(plan: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = plan.get("metrics") or {}
    return {field: metrics.get(field, "НЕТ ПОЛЯ") for field in NORM_FIELDS}


async def run(
    base_url: str, zone: str, max_requests: int, dry_run: bool, plan_if_missing: bool
) -> int:
    report: dict[str, Any] = {"base_url": base_url, "zone": zone}
    checks: dict[str, bool] = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=120) as client:
        ready = await client.get("/api/v1/health/ready")
        report["ready_http"] = ready.status_code
        checks["ready_200"] = ready.status_code == 200

        login = await client.post(
            "/api/v1/auth/login",
            json={"password": get_settings().app_password},
        )
        report["login_http"] = login.status_code
        checks["login_200"] = login.status_code == 200
        if login.status_code != 200:
            emit({**report, "checks": checks})
            return 1

        dataset = await dataset_row(client, zone)
        revision_before = int(dataset["revision"])
        plan_id = dataset.get("current_plan_id")
        report["dataset"] = {
            "id": dataset["id"],
            "title": dataset.get("title"),
            "revision_before": revision_before,
            "current_plan_id": plan_id,
        }
        if plan_id is None and plan_if_missing:
            # Плана у набора нет — считаем его тем же запросом, что кнопка «Рассчитать».
            created = await client.post(f"/api/v1/datasets/{dataset['id']}/plans")
            report["plan_created_http"] = created.status_code
            if created.status_code == 200:
                plan_id = created.json()["plan_id"]
                dataset = await dataset_row(client, zone)
                revision_before = int(dataset["revision"])
                report["dataset"]["revision_before"] = revision_before
                report["dataset"]["current_plan_id"] = dataset.get("current_plan_id")
            else:
                report["plan_created_error_code"] = error_code(created)
        if plan_id is None:
            checks["published_plan_found"] = False
            emit({**report, "checks": checks})
            return 1
        checks["published_plan_found"] = True

        parent_response = await client.get(f"/api/v1/plans/{plan_id}")
        parent_response.raise_for_status()
        parent = parent_response.json()
        unassigned: list[dict[str, Any]] = parent["result"]["unassigned"]
        report["parent_plan"] = {
            "id": plan_id,
            "kind": parent["kind"],
            "input_revision": parent["input_revision"],
            "created_at": parent["created_at"],
            "unassigned_count": len(unassigned),
            "norm_metrics": norm_metrics(parent),
        }

        # Шаг 3: первая отказанная заявка, у которой есть хоть один вариант вставки.
        tried: list[dict[str, Any]] = []
        chosen_request: dict[str, Any] | None = None
        placements: list[dict[str, Any]] = []
        for item in unassigned[:max_requests]:
            response = await client.get(
                f"/api/v1/plans/{plan_id}/placements",
                params={"request_id": item["request_id"]},
            )
            count = len(response.json()["placements"]) if response.status_code == 200 else None
            tried.append(
                {
                    "external_id": item["external_id"],
                    "reason_code": item["reason_code"],
                    "placements_http": response.status_code,
                    "placements_count": count,
                    "error_code": None if response.status_code == 200 else error_code(response),
                }
            )
            if response.status_code == 200 and count:
                chosen_request = item
                placements = response.json()["placements"]
                break
        report["placements_tried"] = tried
        checks["placements_http_200"] = bool(tried) and all(
            row["placements_http"] == 200 for row in tried
        )
        if chosen_request is None:
            report["assign"] = "пропущено: ни у одной отказанной заявки нет варианта вставки"
            emit({**report, "checks": checks})
            return 0 if all(checks.values()) else 1

        best = min(
            placements,
            key=lambda row: (row["added_distance_meters"], row["added_travel_minutes"]),
        )
        report["chosen"] = {
            "external_id": chosen_request["external_id"],
            "placements_count": len(placements),
            "norm_exceeded_count": sum(1 for row in placements if row["norm_exceeded"]),
            "best": {
                "engineer_id": best["engineer_id"],
                "position": best["position"],
                "added_distance_meters": best["added_distance_meters"],
                "added_travel_minutes": best["added_travel_minutes"],
                "norm_exceeded": best["norm_exceeded"],
                "added_norm_excess_minutes": best.get("added_norm_excess_minutes"),
            },
        }
        if dry_run:
            report["assign"] = "пропущено: --dry-run"
            emit({**report, "checks": checks})
            return 0 if all(checks.values()) else 1

        # Шаг 4: ручное назначение.
        assign_payload = {
            "request_id": chosen_request["request_id"],
            "engineer_id": best["engineer_id"],
            "position": best["position"],
            "expected_revision": revision_before,
        }
        assign = await client.post(f"/api/v1/plans/{plan_id}/assign", json=assign_payload)
        report["assign_http"] = assign.status_code
        checks["assign_200"] = assign.status_code == 200
        if assign.status_code != 200:
            report["assign_error_code"] = error_code(assign)
            emit({**report, "checks": checks})
            return 1
        new_plan = assign.json()
        dataset_after = await dataset_row(client, zone)
        revision_after = int(dataset_after["revision"])
        new_assigned = new_plan["metrics"]["assigned_count"]
        parent_assigned = parent["metrics"]["assigned_count"]
        report["new_plan"] = {
            "id": new_plan["id"],
            "kind": new_plan["kind"],
            "parent_plan_id": new_plan["parent_plan_id"],
            "input_revision": new_plan["input_revision"],
            "assigned_count": f"{parent_assigned} -> {new_assigned}",
            "unassigned_count": new_plan["metrics"]["unassigned_count"],
        }
        report["revision"] = {
            "before": revision_before,
            "after": revision_after,
            "current_plan_id_after": dataset_after.get("current_plan_id"),
        }
        checks["new_plan_kind_manual_insert"] = new_plan["kind"] == "manual_insert"
        checks["revision_grew"] = revision_after == revision_before + 1
        checks["new_plan_is_current"] = dataset_after.get("current_plan_id") == new_plan["id"]
        checks["assigned_count_plus_one"] = new_assigned == parent_assigned + 1

        # Шаг 5: то же назначение в исходный план со старой revision — должно быть 409.
        repeat = await client.post(f"/api/v1/plans/{plan_id}/assign", json=assign_payload)
        report["repeat_on_parent"] = {
            "http": repeat.status_code,
            "error_code": error_code(repeat),
        }
        checks["repeat_on_parent_409"] = repeat.status_code == 409

        # Шаг 6: метрики норматива в новом плане через GET.
        fetched = await client.get(f"/api/v1/plans/{new_plan['id']}")
        report["get_new_plan_http"] = fetched.status_code
        fetched_plan = fetched.json()
        metrics: dict[str, Any] = fetched_plan.get("metrics") or {}
        report["get_new_plan_norm_metrics"] = norm_metrics(fetched_plan)
        model_info: dict[str, Any] = fetched_plan.get("model_info") or {}
        report["new_plan_model_info_keys"] = sorted(model_info)
        checks["get_new_plan_200"] = fetched.status_code == 200
        checks["norm_fields_in_metrics"] = all(field in metrics for field in NORM_FIELDS)

    emit({**report, "checks": checks})
    return 0 if all(checks.values()) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Живая проверка placements и assign")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--zone", default="yugocenter")
    parser.add_argument("--max-requests", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    # Если у набора нет опубликованного плана — посчитать его (пишет план в БД).
    parser.add_argument("--plan-if-missing", action="store_true")
    args = parser.parse_args()
    code = asyncio.run(
        run(args.base_url, args.zone, args.max_requests, args.dry_run, args.plan_if_missing)
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
