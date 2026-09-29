from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[str]: JSONB}


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200))
    planning_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    office: Mapped[dict[str, Any]] = mapped_column(JSONB)
    import_report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    requests: Mapped[list["ServiceRequest"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )
    engineers: Mapped[list["Engineer"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class ServiceRequest(Base):
    __tablename__ = "service_requests"
    __table_args__ = (
        UniqueConstraint("dataset_id", "external_id"),
        Index("ix_service_requests_dataset", "dataset_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(120))
    input_order: Mapped[int] = mapped_column(Integer)
    address_raw: Mapped[str] = mapped_column(String(600))
    address_normalized: Mapped[str] = mapped_column(String(600))
    district: Mapped[str] = mapped_column(String(160))
    coordinates: Mapped[dict[str, Any]] = mapped_column(JSONB)
    coordinate_source: Mapped[str] = mapped_column(String(24))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    expected_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    required_skill: Mapped[str] = mapped_column(String(32))
    required_transport: Mapped[str | None] = mapped_column(String(32), nullable=True)
    required_equipment: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    status: Mapped[str] = mapped_column(String(24), default="new")
    completion_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_fields: Mapped[dict[str, Any]] = mapped_column(JSONB)
    enrichment_rule_version: Mapped[str] = mapped_column(String(32), default="1")

    dataset: Mapped[Dataset] = relationship(back_populates="requests")


class Engineer(Base):
    __tablename__ = "engineers"
    __table_args__ = (
        UniqueConstraint("dataset_id", "external_id"),
        Index("ix_engineers_dataset", "dataset_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(120))
    input_order: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    start_location: Mapped[dict[str, Any]] = mapped_column(JSONB)
    shift_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    shift_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    skills: Mapped[list[str]] = mapped_column(JSONB)
    transport: Mapped[str] = mapped_column(String(32))
    equipment_inventory: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_synthetic: Mapped[bool] = mapped_column(default=True)

    dataset: Mapped[Dataset] = relationship(back_populates="engineers")


class ReferenceAssignment(Base):
    __tablename__ = "reference_assignments"
    __table_args__ = (Index("ix_reference_assignments_dataset", "dataset_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("service_requests.id", ondelete="SET NULL"), nullable=True
    )
    reference_external_id: Mapped[str] = mapped_column(String(120))
    team_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reference_status: Mapped[str] = mapped_column(String(80))
    match_status: Mapped[str] = mapped_column(String(24))
    source_fields: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (Index("ix_plans_dataset", "dataset_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    input_revision: Mapped[int] = mapped_column(Integer)
    parent_plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    baseline_plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    model_info: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class PlanningEvent(Base):
    __tablename__ = "planning_events"
    __table_args__ = (UniqueConstraint("dataset_id", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    base_plan_id: Mapped[UUID] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(40), default="urgent_request")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    result_plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
