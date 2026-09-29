from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import Dataset, ServiceRequest
from app.schemas import (
    Coordinates,
    Priority,
    RequiredSkill,
    ServiceRequestInput,
)
from app.services.norms import (
    bind_request_to_current_norm,
    default_norms_config,
    update_norms_config,
)


@pytest.fixture
async def session():
    settings = get_settings()
    engine = create_async_engine(settings.test_database_url or settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        try:
            yield db
            await db.rollback()
        finally:
            await db.close()
    await engine.dispose()


def test_customer_workbook_norms_are_loaded_exactly() -> None:
    config = default_norms_config()
    rows = {row["norm_id"]: row for row in config["rows"]}

    assert config["source_sha256"] == (
        "b82b3f1062be039835086c0471fd0247e0bf2de42d8c3e5cf9a59f4cf8c8482b"
    )
    assert rows["connection_basic"]["base_minutes"] == 90
    assert rows["tkd_emergency"]["base_minutes"] == 100
    assert rows["add_order"]["base_minutes"] == 40
    assert rows["local_repair"]["base_minutes"] == 50


def test_dispatcher_request_is_bound_to_current_emergency_norm() -> None:
    request = ServiceRequestInput(
        id="urgent",
        external_id="urgent",
        input_order=0,
        address="Москва",
        district="Тест",
        coordinates=Coordinates(latitude=55.75, longitude=37.62),
        duration_minutes=1,
        window_start=datetime(2026, 8, 17, 10, tzinfo=UTC),
        window_end=datetime(2026, 8, 17, 12, tzinfo=UTC),
        required_skill=RequiredSkill.EMERGENCY,
        priority=Priority.URGENT,
    )

    bound = bind_request_to_current_norm(request, default_norms_config())

    assert bound.duration_minutes == 80
    assert bound.expected_duration_minutes == 80
    assert bound.source_fields["_enrichment"]["norm_id"] == "tkd_emergency"
    assert bound.source_fields["_enrichment"]["travel_norm_minutes"] == 20


@pytest.mark.asyncio
async def test_updating_norms_changes_bound_requests_and_revision(session: AsyncSession) -> None:
    dataset = Dataset(
        id=uuid4(),
        title="Нормативы test",
        planning_date=date(2026, 8, 17),
        timezone="Europe/Moscow",
        revision=1,
        office={"latitude": 55.75, "longitude": 37.62},
        import_report={},
        assumptions={},
        source_fingerprint=f"norms-test-{uuid4()}",
    )
    request = ServiceRequest(
        id=uuid4(),
        dataset=dataset,
        external_id="request-1",
        input_order=0,
        address_raw="Москва",
        address_normalized="москва",
        district="Тест",
        coordinates={"latitude": 55.75, "longitude": 37.62},
        coordinate_source="synthetic",
        duration_minutes=20,
        window_start=datetime(2026, 8, 17, 10, tzinfo=UTC),
        window_end=datetime(2026, 8, 17, 12, tzinfo=UTC),
        required_skill="connection",
        required_transport=None,
        required_equipment={},
        priority="normal",
        status="new",
        source_fields={"_enrichment": {"norm_id": "add_order"}},
        enrichment_rule_version="customer_norms_v2",
    )
    session.add_all([dataset, request])
    await session.flush()
    rows = default_norms_config()["rows"]
    changed_rows = [
        {
            **row,
            "travel_minutes": 25 if row["norm_id"] == "add_order" else row["travel_minutes"],
            "technical_minutes": 12 if row["norm_id"] == "add_order" else row["technical_minutes"],
            "paperwork_minutes": 8 if row["norm_id"] == "add_order" else row["paperwork_minutes"],
            "expected_service_minutes": (
                15 if row["norm_id"] == "add_order" else row["expected_service_minutes"]
            ),
        }
        for row in rows
    ]

    result = await update_norms_config(
        session,
        dataset,
        expected_revision=1,
        rows=changed_rows,
    )

    assert result["revision"] == 2
    assert result["affected_requests"] == 1
    assert request.duration_minutes == 20
    assert request.expected_duration_minutes == 15
    assert request.source_fields["_enrichment"]["travel_norm_minutes"] == 25
    assert request.enrichment_rule_version == "dispatcher_norms_r2"
