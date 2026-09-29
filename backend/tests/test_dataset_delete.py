"""Dataset / zone hard-delete clears related rows without IntegrityError."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.errors import DomainError
from app.models import (
    Dataset,
    Engineer,
    Plan,
    PlanningEvent,
    ReferenceAssignment,
    ServiceRequest,
)
from app.services.datasets import delete_dataset, delete_datasets_by_service_zone, get_service_zone


@pytest.fixture
async def session():
    settings = get_settings()
    url = settings.test_database_url or settings.database_url
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        try:
            yield db
            await db.rollback()
        finally:
            await db.close()
    await engine.dispose()


async def _seed_minimal(
    session: AsyncSession,
    *,
    title: str,
    zone_code: str,
) -> Dataset:
    zone = get_service_zone(zone_code)
    zone_name = str(zone.get("name") or zone_code)
    suffix = uuid4().hex[:10]
    dataset = Dataset(
        id=uuid4(),
        title=title,
        planning_date=date(2024, 6, 1),
        timezone="Europe/Moscow",
        revision=1,
        office={"lat": 55.75, "lon": 37.62, "name": "office"},
        import_report={"rows": 1},
        assumptions={
            "service_zone": zone_code,
            "service_zone_name": zone_name,
            "workday": {"start": "09:00", "end": "18:00"},
        },
        source_fingerprint=f"test-delete-{suffix}",
    )
    session.add(dataset)
    await session.flush()

    engineer = Engineer(
        id=uuid4(),
        dataset_id=dataset.id,
        external_id=f"eng-{suffix}",
        input_order=1,
        name="Тест Инженер",
        start_location={"lat": 55.75, "lon": 37.62},
        shift_start=datetime(2024, 6, 1, 6, 0, tzinfo=UTC),
        shift_end=datetime(2024, 6, 1, 15, 0, tzinfo=UTC),
        skills=["медные"],
        transport="auto",
        is_synthetic=False,
    )
    request = ServiceRequest(
        id=uuid4(),
        dataset_id=dataset.id,
        external_id=f"req-{suffix}",
        input_order=1,
        address_raw="ул. Тестовая, 1",
        address_normalized="ул. Тестовая, 1",
        district="test",
        coordinates={"lat": 55.76, "lon": 37.63},
        coordinate_source="test",
        duration_minutes=60,
        window_start=datetime(2024, 6, 1, 6, 0, tzinfo=UTC),
        window_end=datetime(2024, 6, 1, 15, 0, tzinfo=UTC),
        required_skill="медные",
        required_transport=None,
        priority="normal",
        status="new",
        source_fields={},
    )
    session.add_all([engineer, request])
    await session.flush()

    parent = Plan(
        id=uuid4(),
        dataset_id=dataset.id,
        input_revision=1,
        kind="baseline",
        input_snapshot={},
        result={"assignments": []},
        metrics={},
        model_info={},
    )
    session.add(parent)
    await session.flush()

    child = Plan(
        id=uuid4(),
        dataset_id=dataset.id,
        input_revision=1,
        parent_plan_id=parent.id,
        baseline_plan_id=parent.id,
        kind="replan",
        event_time=datetime(2024, 6, 1, 9, 0, tzinfo=UTC),
        input_snapshot={},
        result={"assignments": []},
        metrics={},
        model_info={},
    )
    session.add(child)
    await session.flush()

    event = PlanningEvent(
        id=uuid4(),
        dataset_id=dataset.id,
        base_plan_id=parent.id,
        event_time=datetime(2024, 6, 1, 9, 0, tzinfo=UTC),
        kind="urgent_request",
        payload={},
        idempotency_key=f"evt-{suffix}",
        result_plan_id=child.id,
    )
    ref = ReferenceAssignment(
        id=uuid4(),
        dataset_id=dataset.id,
        request_id=request.id,
        reference_external_id=f"ref-{suffix}",
        team_name="team-a",
        reference_status="done",
        match_status="matched",
        source_fields={},
    )
    session.add_all([event, ref])
    await session.flush()
    return dataset


@pytest.mark.asyncio
async def test_delete_dataset_removes_children(session: AsyncSession) -> None:
    dataset = await _seed_minimal(session, title="Тест-зона A", zone_code="vostok")
    dataset_id = dataset.id

    result = await delete_dataset(session, dataset_id)
    await session.commit()

    assert result["dataset_id"] == str(dataset_id)
    assert result["deleted"]["plans"] == 2
    assert result["deleted"]["events"] == 1
    assert result["deleted"]["requests"] == 1
    assert result["deleted"]["engineers"] == 1
    assert result["deleted"]["reference_assignments"] == 1

    assert await session.get(Dataset, dataset_id) is None
    assert (
        await session.scalar(
            select(func.count()).select_from(Plan).where(Plan.dataset_id == dataset_id)
        )
    ) == 0
    assert (
        await session.scalar(
            select(func.count())
            .select_from(PlanningEvent)
            .where(PlanningEvent.dataset_id == dataset_id)
        )
    ) == 0
    assert (
        await session.scalar(
            select(func.count())
            .select_from(ServiceRequest)
            .where(ServiceRequest.dataset_id == dataset_id)
        )
    ) == 0

    with pytest.raises(DomainError) as exc:
        await delete_dataset(session, dataset_id)
    assert exc.value.code == "dataset_not_found"
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_datasets_by_service_zone(session: AsyncSession) -> None:
    d1 = await _seed_minimal(session, title="Восток тест-1", zone_code="vostok")
    d2 = await _seed_minimal(session, title="Восток тест-2", zone_code="vostok")
    other = await _seed_minimal(session, title="Югоцентр тест", zone_code="yugocenter")
    other_id = other.id

    result = await delete_datasets_by_service_zone(session, "vostok")
    await session.commit()

    assert result["zone_code"] == "vostok"
    assert result["count"] >= 2
    deleted_ids = {item["dataset_id"] for item in result["deleted_datasets"]}
    assert str(d1.id) in deleted_ids
    assert str(d2.id) in deleted_ids
    assert await session.get(Dataset, d1.id) is None
    assert await session.get(Dataset, d2.id) is None
    assert await session.get(Dataset, other_id) is not None

    await delete_dataset(session, other_id)
    await session.commit()
