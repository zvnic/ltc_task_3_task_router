"""Жизненный статус заявки решает, попадёт ли она в новый снимок планирования."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Dataset, ServiceRequest
from app.routers.api import RequestPatch, patch_request
from app.schemas import RequestStatus
from app.services.datasets import (
    INACTIVE_REQUEST_STATUSES,
    is_request_plannable,
    planning_input_from_dataset,
)

# Спецификация пункта 7: перенесённая заявка исключается наравне с закрытыми.
EXPECTED_INACTIVE = {"completed", "cancelled", "rescheduled"}


def _request_row(external_id: str, status: str, input_order: int) -> ServiceRequest:
    return ServiceRequest(
        id=uuid4(),
        dataset_id=uuid4(),
        external_id=external_id,
        input_order=input_order,
        address_raw="Москва",
        address_normalized="москва",
        district="Москва",
        coordinates={"latitude": 55.75, "longitude": 37.61},
        coordinate_source="synthetic",
        duration_minutes=30,
        expected_duration_minutes=30,
        window_start=datetime.fromisoformat("2026-08-17T10:00:00+03:00"),
        window_end=datetime.fromisoformat("2026-08-17T12:00:00+03:00"),
        required_skill="local",
        required_transport=None,
        required_equipment={},
        priority="normal",
        status=status,
        completion_deadline=None,
        source_fields={},
        enrichment_rule_version="1",
    )


def _dataset(requests: list[ServiceRequest], revision: int = 1) -> Any:
    # Снимку достаточно этих полей; ORM-сессия для него не нужна.
    return SimpleNamespace(
        id=uuid4(),
        revision=revision,
        planning_date=date(2026, 8, 17),
        timezone="Europe/Moscow",
        office={"latitude": 55.7558, "longitude": 37.6173},
        requests=requests,
        engineers=[],
    )


def test_inactive_statuses_are_known_request_statuses() -> None:
    known = {status.value for status in RequestStatus}
    assert INACTIVE_REQUEST_STATUSES == EXPECTED_INACTIVE
    assert INACTIVE_REQUEST_STATUSES <= known


@pytest.mark.parametrize("status", list(RequestStatus))
def test_every_status_has_one_interpretation(status: RequestStatus) -> None:
    expected = status.value not in EXPECTED_INACTIVE
    # Строка из БД и член перечисления из PATCH трактуются одинаково.
    assert is_request_plannable(status.value) is expected
    assert is_request_plannable(status) is expected


def test_rescheduled_request_is_excluded_from_planning_input() -> None:
    rows = [
        _request_row("new", "new", 0),
        _request_row("assigned", "assigned", 1),
        _request_row("issue", "issue", 2),
        _request_row("rescheduled", "rescheduled", 3),
        _request_row("completed", "completed", 4),
        _request_row("cancelled", "cancelled", 5),
    ]

    snapshot = planning_input_from_dataset(_dataset(rows))

    assert [request.external_id for request in snapshot.requests] == [
        "new",
        "assigned",
        "issue",
    ]


def test_all_active_statuses_reach_planning_input() -> None:
    rows = [
        _request_row(status.value, status.value, index)
        for index, status in enumerate(RequestStatus)
    ]

    snapshot = planning_input_from_dataset(_dataset(rows))

    assert {request.external_id for request in snapshot.requests} == (
        {status.value for status in RequestStatus} - EXPECTED_INACTIVE
    )


class _FakeSession:
    """Минимум AsyncSession, который нужен patch_request: get и commit."""

    def __init__(self, request_row: ServiceRequest, dataset: Any) -> None:
        self.request_row = request_row
        self.dataset = dataset
        self.commits = 0

    async def get(self, model: type[Any], key: UUID, with_for_update: bool = False) -> Any:
        if model is ServiceRequest and key == self.request_row.id:
            return self.request_row
        if model is Dataset and key == self.dataset.id:
            return self.dataset
        return None

    async def commit(self) -> None:
        self.commits += 1


async def test_patch_to_rescheduled_bumps_revision_and_drops_request_from_next_snapshot() -> None:
    kept = _request_row("kept", "assigned", 0)
    moved = _request_row("moved", "assigned", 1)
    dataset = _dataset([kept, moved], revision=7)
    moved.dataset_id = dataset.id
    # План опубликован по revision 7 и содержит обе заявки.
    published_input_revision = dataset.revision
    session = _FakeSession(moved, dataset)

    response = await patch_request(
        moved.id,
        RequestPatch(expected_revision=7, status=RequestStatus.RESCHEDULED),
        cast(AsyncSession, session),
    )

    assert session.commits == 1
    assert response["revision"] == 8
    assert response["request"]["status"] == "rescheduled"
    # Так list_requests вычисляет plan_is_stale: план по revision 7 устарел.
    assert published_input_revision != dataset.revision
    snapshot = planning_input_from_dataset(dataset)
    assert snapshot.revision == 8
    assert [request.external_id for request in snapshot.requests] == ["kept"]
