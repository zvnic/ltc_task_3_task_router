import json
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models import Dataset
from app.schemas import Priority, RequiredSkill, ServiceRequestInput

FIXTURE_PATH = Path("/data/fixtures/enrichment_rules.json")
NORM_TITLES = {
    "connection_basic": "Подключение клиентов Базовая",
    "tkd_emergency": "Аварий на ТКД",
    "add_order": "Дозаказ оборудования",
    "local_repair": "Локальная заявка/ремонт у клиента",
}
NORM_ORDER = ("connection_basic", "tkd_emergency", "add_order", "local_repair")


def _fixture() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8")),
    )


def default_norms_config() -> dict[str, Any]:
    fixture = _fixture()
    rows_by_id: dict[str, dict[str, Any]] = {}
    for rule in fixture["work_types"].values():
        norm_id = str(rule["norm_id"])
        rows_by_id[norm_id] = {
            "norm_id": norm_id,
            "title": NORM_TITLES[norm_id],
            "travel_minutes": int(rule["travel_norm_minutes"]),
            "technical_minutes": int(rule["technical_minutes"]),
            "paperwork_minutes": int(rule["paperwork_minutes"]),
            "expected_service_minutes": int(rule["technical_minutes"])
            + int(rule["paperwork_minutes"]),
            "base_minutes": int(rule["travel_norm_minutes"])
            + int(rule["technical_minutes"])
            + int(rule["paperwork_minutes"]),
        }
    return {
        "version": str(fixture["version"]),
        "source_name": "Нормативы (1).xlsx",
        "source_sha256": str(fixture["source"]).rsplit(":", 1)[-1],
        "source_kind": "customer_file",
        "rows": [rows_by_id[norm_id] for norm_id in NORM_ORDER],
    }


def current_norms_config(dataset: Dataset) -> dict[str, Any]:
    assumptions = dataset.assumptions if isinstance(dataset.assumptions, dict) else {}
    stored = assumptions.get("norms_config")
    if isinstance(stored, dict) and isinstance(stored.get("rows"), list):
        normalized = dict(stored)
        normalized["rows"] = [
            {
                **row,
                "expected_service_minutes": int(
                    row.get(
                        "expected_service_minutes",
                        int(row["technical_minutes"]) + int(row["paperwork_minutes"]),
                    )
                ),
            }
            for row in stored["rows"]
        ]
        return cast(dict[str, Any], normalized)
    return default_norms_config()


def bind_request_to_current_norm(
    request: ServiceRequestInput,
    config: dict[str, Any],
) -> ServiceRequestInput:
    enrichment = request.source_fields.get("_enrichment")
    existing_norm_id = (
        str(enrichment.get("norm_id") or "") if isinstance(enrichment, dict) else ""
    )
    if existing_norm_id in NORM_ORDER:
        norm_id = existing_norm_id
    elif request.priority is Priority.URGENT or request.required_skill is RequiredSkill.EMERGENCY:
        norm_id = "tkd_emergency"
    elif request.required_skill is RequiredSkill.CONNECTION:
        norm_id = "connection_basic"
    else:
        norm_id = "local_repair"
    row = next(item for item in config["rows"] if item["norm_id"] == norm_id)
    current_enrichment = enrichment if isinstance(enrichment, dict) else {}
    safe_service_minutes = int(row["technical_minutes"]) + int(row["paperwork_minutes"])
    expected_service_minutes = min(
        request.expected_duration_minutes
        if request.expected_duration_minutes is not None
        else int(row["expected_service_minutes"]),
        safe_service_minutes,
    )
    return request.model_copy(
        update={
            "duration_minutes": safe_service_minutes,
            "expected_duration_minutes": expected_service_minutes,
            "source_fields": {
                **request.source_fields,
                "_enrichment": {
                    **current_enrichment,
                    "norm_id": norm_id,
                    "norm_version": config["version"],
                    "norm_source": config["source_kind"],
                    "technical_minutes": int(row["technical_minutes"]),
                    "paperwork_minutes": int(row["paperwork_minutes"]),
                    "expected_service_minutes": expected_service_minutes,
                    "travel_norm_minutes": int(row["travel_minutes"]),
                    "work_class": (
                        "emergency"
                        if norm_id == "tkd_emergency"
                        else "connection"
                        if norm_id == "connection_basic"
                        else "add_order"
                        if norm_id == "add_order"
                        else "local"
                    ),
                },
            },
        }
    )


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows_by_id = {str(row.get("norm_id")): row for row in rows}
    if set(rows_by_id) != set(NORM_ORDER):
        raise DomainError(
            "invalid_norm_catalog",
            "Набор нормативов должен содержать ровно четыре строки из исходного справочника.",
            status_code=422,
        )
    normalized: list[dict[str, Any]] = []
    for norm_id in NORM_ORDER:
        row = rows_by_id[norm_id]
        travel_value = row.get("travel_minutes")
        technical_value = row.get("technical_minutes")
        paperwork_value = row.get("paperwork_minutes")
        expected_value = row.get("expected_service_minutes")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (travel_value, technical_value, paperwork_value, expected_value)
        ):
            raise DomainError(
                "invalid_norm_value",
                "Все компоненты норматива должны быть целыми минутами.",
                status_code=422,
                details={"norm_id": norm_id},
            )
        assert isinstance(travel_value, int)
        assert isinstance(technical_value, int)
        assert isinstance(paperwork_value, int)
        assert isinstance(expected_value, int)
        if not 1 <= travel_value <= 240:
            raise DomainError(
                "invalid_travel_norm",
                "Норматив дороги должен быть от 1 до 240 минут.",
                status_code=422,
                details={"norm_id": norm_id},
            )
        if not 0 <= technical_value <= 720 or not 0 <= paperwork_value <= 720:
            raise DomainError(
                "invalid_service_norm",
                "Технические работы и документы должны быть от 0 до 720 минут.",
                status_code=422,
                details={"norm_id": norm_id},
            )
        service_minutes = technical_value + paperwork_value
        if service_minutes <= 0 or service_minutes > 720:
            raise DomainError(
                "invalid_service_norm",
                "Суммарное обслуживание должно быть от 1 до 720 минут.",
                status_code=422,
                details={"norm_id": norm_id},
            )
        if not 1 <= expected_value <= service_minutes:
            raise DomainError(
                "invalid_expected_service_norm",
                "Ожидаемое обслуживание должно быть от 1 минуты до безопасного норматива.",
                status_code=422,
                details={"norm_id": norm_id},
            )
        normalized.append(
            {
                "norm_id": norm_id,
                "title": NORM_TITLES[norm_id],
                "travel_minutes": travel_value,
                "technical_minutes": technical_value,
                "paperwork_minutes": paperwork_value,
                "expected_service_minutes": expected_value,
                "base_minutes": travel_value + service_minutes,
            }
        )
    return normalized


async def update_norms_config(
    session: AsyncSession,
    dataset: Dataset,
    *,
    expected_revision: int,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if dataset.revision != expected_revision:
        raise DomainError("stale_input", "Набор уже изменён.", status_code=409)
    normalized = _normalize_rows(rows)
    next_revision = dataset.revision + 1
    version = f"dispatcher_norms_r{next_revision}"
    by_id = {row["norm_id"]: row for row in normalized}
    affected_requests = 0
    for request in dataset.requests:
        source_fields = request.source_fields if isinstance(request.source_fields, dict) else {}
        enrichment = source_fields.get("_enrichment")
        if not isinstance(enrichment, dict):
            continue
        norm_id = str(enrichment.get("norm_id") or "")
        row = by_id.get(norm_id)
        if row is None:
            continue
        request.duration_minutes = row["technical_minutes"] + row["paperwork_minutes"]
        request.expected_duration_minutes = row["expected_service_minutes"]
        request.enrichment_rule_version = version
        request.source_fields = {
            **source_fields,
            "_enrichment": {
                **enrichment,
                "norm_version": version,
                "norm_source": "dispatcher_settings",
                "technical_minutes": row["technical_minutes"],
                "paperwork_minutes": row["paperwork_minutes"],
                "expected_service_minutes": row["expected_service_minutes"],
                "travel_norm_minutes": row["travel_minutes"],
            },
        }
        affected_requests += 1

    config = {
        "version": version,
        "source_name": "Нормативы (1).xlsx",
        "source_sha256": default_norms_config()["source_sha256"],
        "source_kind": "dispatcher_settings",
        "rows": normalized,
    }
    assumptions = dict(dataset.assumptions)
    assumptions["norms_config"] = config
    assumptions["norm_version"] = version
    assumptions["durations"] = (
        "Технические работы + документы из текущего справочника; дорога проверяется "
        "отдельным нормативом для каждого плеча"
    )
    dataset.assumptions = assumptions
    dataset.revision = next_revision
    await session.commit()
    return config | {"revision": dataset.revision, "affected_requests": affected_requests}
