"""Ручное назначение уже отказанной заявки: перебор мест и новый план manual_insert."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.auth import COOKIE_NAME, issue_session_value
from app.core.config import get_settings
from app.core.errors import DomainError
from app.db import get_session
from app.main import app
from app.models import Dataset, Plan, ServiceRequest
from app.planning import build_baseline, validate_plan
from app.planning.metrics import calculate_metrics
from app.planning.reasons import make_unassigned
from app.planning.schedule import calculate_route
from app.schemas import (
    Coordinates,
    EngineerInput,
    PlanningInput,
    PlanningResult,
    RequiredSkill,
    ServiceRequestInput,
    Transport,
)
from app.services.datasets import delete_dataset
from app.services.plans import (
    assign_unassigned_request,
    enumerate_request_placements,
    request_placements,
)

START = Coordinates(latitude=55.70, longitude=37.70)
# Норматив дороги до вставляемой заявки: 5 мин от соседних визитов укладываются,
# 9–10 мин от старта бригад и от визита второй бригады — нет.
INSERTED_TRAVEL_NORM = 6


def ident(name: str) -> str:
    """Идентификаторы в виде UUID: API принимает request_id и engineer_id как UUID."""
    return str(uuid5(NAMESPACE_URL, f"manual-assign-test:{name}"))


E1, E2 = ident("e1"), ident("e2")
A, B, C, X, Y = (ident(name) for name in ("a", "b", "c", "x", "y"))


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"2026-08-17T{hour:02d}:{minute:02d}:00+03:00")


def crew(identifier: str, order: int) -> EngineerInput:
    return EngineerInput(
        id=identifier,
        external_id=f"crew-{order}",
        input_order=order,
        name=f"Бригада {order}",
        start_location=START,
        shift_start=dt(8),
        shift_end=dt(18),
        skills=[RequiredSkill.LOCAL],
        transport=Transport.CAR,
    )


def visit(
    identifier: str,
    order: int,
    coordinates: Coordinates,
    start: int,
    end: int,
    *,
    duration: int,
    expected: int,
    skill: RequiredSkill = RequiredSkill.LOCAL,
    travel_norm: int | None = None,
) -> ServiceRequestInput:
    source_fields: dict[str, Any] = {}
    if travel_norm is not None:
        source_fields["_enrichment"] = {
            "norm_id": "local_repair",
            "norm_version": "test",
            "travel_norm_minutes": travel_norm,
            "work_class": "local",
        }
    return ServiceRequestInput(
        id=identifier,
        external_id=f"req-{order}",
        input_order=order,
        address=f"Адрес {order}",
        district="Тест",
        coordinates=coordinates,
        duration_minutes=duration,
        expected_duration_minutes=expected,
        window_start=dt(start),
        window_end=dt(end),
        required_skill=skill,
        source_fields=source_fields,
    )


def scenario() -> tuple[PlanningInput, PlanningResult]:
    """Опубликованный план: у бригады 1 визиты A (9:00) и B (12:00), у бригады 2 — C (10:00).

    X (окно 8–17) и Y (навык, которого нет ни у одной бригады) стоят в отказах.
    У A, B, C защитный резерв ненулевой: ожидаемое время меньше безопасного.
    X лежит ровно между A и B, поэтому самое дешёвое место — между ними.
    """
    requests = [
        visit(A, 0, Coordinates(latitude=55.71, longitude=37.70), 9, 9, duration=60, expected=45),
        visit(B, 1, Coordinates(latitude=55.72, longitude=37.70), 12, 12, duration=60, expected=40),
        visit(C, 2, Coordinates(latitude=55.70, longitude=37.72), 10, 10, duration=60, expected=50),
        visit(
            X,
            3,
            Coordinates(latitude=55.715, longitude=37.70),
            8,
            17,
            duration=30,
            expected=20,
            travel_norm=INSERTED_TRAVEL_NORM,
        ),
        visit(
            Y,
            4,
            Coordinates(latitude=55.705, longitude=37.71),
            8,
            17,
            duration=30,
            expected=30,
            skill=RequiredSkill.CONNECTION,
        ),
    ]
    engineers = [crew(E1, 1), crew(E2, 2)]
    planning_input = PlanningInput(
        dataset_id=str(uuid4()),
        revision=1,
        planning_date=date(2026, 8, 17),
        requests=requests,
        engineers=engineers,
    )
    by_id = {request.id: request for request in requests}
    first = calculate_route(engineers[0], [by_id[A], by_id[B]])
    second = calculate_route(engineers[1], [by_id[C]])
    assert first is not None and second is not None
    routes = [first, second]
    unassigned = [
        make_unassigned(by_id[X], engineers, fallback="schedule_conflict"),
        make_unassigned(by_id[Y], engineers, fallback="schedule_conflict"),
    ]
    result = build_baseline(planning_input).model_copy(
        update={
            "routes": routes,
            "unassigned": unassigned,
            "metrics": calculate_metrics(routes, unassigned),
            "constraint_violations_count": 0,
        }
    )
    assert validate_plan(planning_input, result) == []
    return planning_input, result


@pytest.fixture
def soft_norm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TRAVEL_NORM_MODE", "soft")
    monkeypatch.setenv("TRAVEL_NORM_MAX_FACTOR", "2.0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def hard_norm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TRAVEL_NORM_MODE", "hard")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def visit_state(result: PlanningResult | dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    """Исполнитель, порядок и время работ каждого визита — то, что ручная вставка не трогает."""
    payload = result.model_dump(mode="json") if isinstance(result, PlanningResult) else result
    return {
        stop["request_id"]: (
            route["engineer_id"],
            stop["service_start_at"],
            stop["service_end_at"],
            stop["expected_service_end_at"],
            stop["reserve_minutes"],
        )
        for route in payload["routes"]
        for stop in route["stops"]
    }


def route_order(result: dict[str, Any], keep: set[str]) -> dict[str, list[str]]:
    return {
        route["engineer_id"]: [
            stop["request_id"] for stop in route["stops"] if stop["request_id"] in keep
        ]
        for route in result["routes"]
    }


# --- перебор мест и сборка плана, без базы ---------------------------------------


@pytest.mark.usefixtures("soft_norm")
def test_placements_list_every_feasible_slot_sorted_by_added_distance() -> None:
    planning_input, base = scenario()

    placements = request_placements(planning_input, base, X).placements

    assert [(item.engineer_id, item.position) for item in placements][0] == (E1, 1)
    assert {(item.engineer_id, item.position) for item in placements} == {
        (E1, 0),
        (E1, 1),
        (E1, 2),
        (E2, 0),
        (E2, 1),
    }
    distances = [item.added_distance_meters for item in placements]
    assert distances == sorted(distances)
    # старт бригад и визит C дальше норматива X, соседи A и B — ближе
    exceeded = {(item.engineer_id, item.position) for item in placements if item.norm_exceeded}
    assert exceeded == {(E1, 0), (E2, 0), (E2, 1)}
    # у A, B, C норматива нет: прирост минут превышения — это само новое плечо до X
    assert all((item.added_norm_excess_minutes > 0) == item.norm_exceeded for item in placements)
    best = placements[0]
    assert best.engineer_name == "Бригада 1"
    assert best.service_start_at >= best.arrival_at
    assert (best.service_end_at - best.service_start_at).total_seconds() == 30 * 60
    # ровно то, что даст вставка: прирост = разница пробега маршрутов
    candidates = enumerate_request_placements(
        planning_input,
        base,
        next(item for item in planning_input.requests if item.id == X),
    )
    assert [item.to_schema() for item in candidates] == placements


@pytest.mark.usefixtures("hard_norm")
def test_hard_mode_placements_exclude_legs_over_travel_norm() -> None:
    planning_input, base = scenario()

    placements = request_placements(planning_input, base, X).placements

    assert [(item.engineer_id, item.position) for item in placements] == [(E1, 1), (E1, 2)]
    assert not any(item.norm_exceeded for item in placements)


@pytest.mark.usefixtures("soft_norm")
def test_manual_assign_keeps_existing_visits_to_the_minute() -> None:
    planning_input, base = scenario()
    before = base.model_dump(mode="json")

    result, placement = assign_unassigned_request(planning_input, base, X, E1, 1)

    after = result.model_dump(mode="json")
    old_ids = {A, B, C}
    old_state = visit_state(before)
    # резерв ненулевой — сверка ниже действительно его проверяет
    assert {state[4] for state in old_state.values()} == {15, 20, 10}
    new_state = visit_state(after)
    assert {request_id: new_state[request_id] for request_id in old_ids} == old_state
    assert route_order(after, old_ids) == route_order(before, old_ids)
    assert [stop["request_id"] for stop in after["routes"][0]["stops"]] == [A, X, B]
    inserted = after["routes"][0]["stops"][1]
    assert inserted["reserve_minutes"] == 10
    assert inserted["expected_service_end_at"] is not None
    assert placement.added_distance_meters == (
        result.routes[0].distance_meters - base.routes[0].distance_meters
    )
    assert [item.request_id for item in result.unassigned] == [Y]
    assert result.metrics == calculate_metrics(result.routes, result.unassigned)
    assert result.metrics.assigned_count == base.metrics.assigned_count + 1
    assert validate_plan(planning_input, result) == []
    # исходный план не тронут
    assert base.model_dump(mode="json") == before


@pytest.mark.usefixtures("soft_norm")
def test_manual_assign_over_norm_is_counted_in_soft_mode() -> None:
    planning_input, base = scenario()

    result, placement = assign_unassigned_request(planning_input, base, X, E2, 1)

    assert placement.norm_exceeded is True
    assert placement.added_norm_excess_minutes > 0
    assert result.metrics.norm_violation_count == base.metrics.norm_violation_count + 1
    assert result.metrics.norm_excess_minutes == (
        base.metrics.norm_excess_minutes + placement.added_norm_excess_minutes
    )
    assert validate_plan(planning_input, result) == []


@pytest.mark.usefixtures("soft_norm")
def test_manual_assign_rejects_request_nobody_can_serve() -> None:
    planning_input, base = scenario()

    assert request_placements(planning_input, base, Y).placements == []
    with pytest.raises(DomainError) as error:
        assign_unassigned_request(planning_input, base, Y, E1, 0)
    assert error.value.code == "placement_infeasible"
    assert error.value.status_code == 409


# --- API на тестовой базе --------------------------------------------------------


class _TestDatabase:
    """Движок тестовой базы, созданный в цикле событий TestClient."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine | None = None
        self.factory: async_sessionmaker[AsyncSession] | None = None

    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self.factory is None:
            self.engine = create_async_engine(self.url, pool_pre_ping=True)
            self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        return self.factory

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions()() as session:
            yield session

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


async def _seed(database: _TestDatabase) -> tuple[str, str]:
    planning_input, base = scenario()
    async with database.sessions()() as session:
        dataset = Dataset(
            id=UUID(planning_input.dataset_id),
            title="Ручное назначение (тест)",
            planning_date=planning_input.planning_date,
            timezone="Europe/Moscow",
            revision=1,
            office={"latitude": START.latitude, "longitude": START.longitude},
            import_report={},
            assumptions={},
            source_fingerprint=f"manual-assign-{uuid4().hex}",
        )
        session.add(dataset)
        await session.flush()
        plan = Plan(
            dataset_id=dataset.id,
            input_revision=1,
            kind="optimized",
            input_snapshot=planning_input.model_dump(mode="json"),
            result=base.model_dump(mode="json"),
            metrics=base.metrics.model_dump(mode="json"),
            model_info={"algorithm_id": "ortools_gls_v1"},
        )
        session.add(plan)
        await session.commit()
        return str(dataset.id), str(plan.id)


async def _add_request_row(
    database: _TestDatabase, dataset_id: str, request: ServiceRequestInput
) -> None:
    """Заявка набора в базе — так её записывает перепланирование."""
    async with database.sessions()() as session:
        session.add(
            ServiceRequest(
                id=UUID(request.id),
                dataset_id=UUID(dataset_id),
                external_id=request.external_id,
                input_order=request.input_order,
                address_raw=request.address,
                address_normalized=request.address,
                district=request.district,
                coordinates=request.coordinates.model_dump(),
                coordinate_source=request.coordinate_source.value,
                duration_minutes=request.duration_minutes,
                expected_duration_minutes=request.expected_service_minutes,
                window_start=request.window_start,
                window_end=request.window_end,
                required_skill=request.required_skill.value,
                required_transport=None,
                required_equipment=request.required_equipment,
                priority=request.priority.value,
                status=request.status.value,
                completion_deadline=request.completion_deadline,
                source_fields=request.source_fields,
                enrichment_rule_version="test",
            )
        )
        await session.commit()


async def _add_newer_plan(database: _TestDatabase, dataset_id: str, plan_id: str) -> str:
    """Копия плана как более новый текущий план на той же ревизии (второй расчёт)."""
    async with database.sessions()() as session:
        source = await session.get(Plan, UUID(plan_id))
        assert source is not None
        newer = Plan(
            dataset_id=UUID(dataset_id),
            input_revision=source.input_revision,
            kind="optimized",
            input_snapshot=source.input_snapshot,
            result=source.result,
            metrics=source.metrics,
            model_info=source.model_info,
        )
        session.add(newer)
        await session.commit()
        return str(newer.id)


async def _plan_count(database: _TestDatabase, dataset_id: str) -> int:
    async with database.sessions()() as session:
        count = await session.scalar(
            select(func.count()).select_from(Plan).where(Plan.dataset_id == UUID(dataset_id))
        )
        return int(count or 0)


async def _drop(database: _TestDatabase, dataset_id: str) -> None:
    async with database.sessions()() as session:
        await delete_dataset(session, UUID(dataset_id))
        await session.commit()


@pytest.fixture
def api(soft_norm: None) -> Iterator[tuple[TestClient, _TestDatabase, str, str]]:
    settings = get_settings()
    database = _TestDatabase(settings.test_database_url or settings.database_url)
    app.dependency_overrides[get_session] = database.session
    try:
        with TestClient(app) as client:
            portal = client.portal
            assert portal is not None
            dataset_id, plan_id = portal.call(_seed, database)
            client.cookies.set(COOKIE_NAME, issue_session_value())
            try:
                yield client, database, dataset_id, plan_id
            finally:
                portal.call(_drop, database, dataset_id)
                portal.call(database.dispose)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _plan_total(client: TestClient, database: _TestDatabase, dataset_id: str) -> int:
    portal = client.portal
    assert portal is not None
    return portal.call(_plan_count, database, dataset_id)


def _revision(client: TestClient, dataset_id: str) -> int:
    return int(client.get(f"/api/v1/datasets/{dataset_id}").json()["revision"])


def _current_plan_id(client: TestClient, dataset_id: str) -> str:
    datasets = client.get("/api/v1/datasets").json()
    return str(next(item for item in datasets if item["id"] == dataset_id)["current_plan_id"])


def _assign_best(client: TestClient, plan_id: str, expected_revision: int) -> Any:
    return client.post(
        f"/api/v1/plans/{plan_id}/assign",
        json={
            "request_id": X,
            "engineer_id": E1,
            "position": 1,
            "expected_revision": expected_revision,
        },
    )


def _replan_event(key: str, expected_revision: int) -> dict[str, Any]:
    """Внутридневная обычная заявка в дату набора."""
    request = visit(
        ident(f"event-{key}"),
        10,
        Coordinates(latitude=55.705, longitude=37.705),
        10,
        17,
        duration=30,
        expected=30,
    )
    return {
        "event_id": key,
        "idempotency_key": key,
        "event_time": dt(9).isoformat(),
        "expected_revision": expected_revision,
        "request": request.model_dump(mode="json"),
    }


def test_api_endpoints_require_session(
    api: tuple[TestClient, _TestDatabase, str, str],
) -> None:
    client, _database, _dataset_id, plan_id = api
    client.cookies.clear()

    listed = client.get(f"/api/v1/plans/{plan_id}/placements", params={"request_id": X})
    assigned = client.post(
        f"/api/v1/plans/{plan_id}/assign",
        json={"request_id": X, "engineer_id": E1, "position": 1, "expected_revision": 1},
    )

    assert listed.status_code == 401
    assert assigned.status_code == 401


def test_api_manual_assign_creates_child_plan(
    api: tuple[TestClient, _TestDatabase, str, str],
) -> None:
    client, database, dataset_id, plan_id = api
    parent_before = client.get(f"/api/v1/plans/{plan_id}").json()

    listed = client.get(f"/api/v1/plans/{plan_id}/placements", params={"request_id": X})
    assert listed.status_code == 200
    body = listed.json()
    assert body["request_id"] == X
    assert set(body["placements"][0]) == {
        "engineer_id",
        "engineer_name",
        "position",
        "arrival_at",
        "service_start_at",
        "service_end_at",
        "added_distance_meters",
        "added_travel_minutes",
        "norm_exceeded",
        "added_norm_excess_minutes",
    }
    distances = [item["added_distance_meters"] for item in body["placements"]]
    assert distances == sorted(distances)
    best = body["placements"][0]
    assert (best["engineer_id"], best["position"]) == (E1, 1)

    response = client.post(
        f"/api/v1/plans/{plan_id}/assign",
        json={
            "request_id": X,
            "engineer_id": best["engineer_id"],
            "position": best["position"],
            "expected_revision": 1,
        },
    )

    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["kind"] == "manual_insert"
    assert plan["parent_plan_id"] == plan_id
    # назначение поднимает revision набора: планы старой ревизии устарели
    assert plan["input_revision"] == 2
    assert _revision(client, dataset_id) == 2
    assert plan["model_info"]["algorithm_id"] == "ortools_gls_v1"
    assert plan["model_info"]["manual_assignment"]["norm_exceeded"] is False
    assert plan["model_info"]["manual_assignment"]["added_norm_excess_minutes"] == 0
    # паспорт: воспроизводимость и причина выбора есть и у ручного назначения
    experiment = plan["model_info"]["experiment"]
    assert experiment["algorithm_ids"] == ["manual_gap_insertion"]
    assert experiment["time_limit_seconds"] == 0
    assert experiment["travel_norm_mode"] == "soft"
    assert {"experiment_id", "snapshot_sha256", "matrix_sha256", "config_sha256"} <= set(
        experiment
    )
    reason = plan["model_info"]["selection_reason"]
    assert reason["selected_algorithm"] == "manual_gap_insertion"
    assert reason["reason_text"] == "ручное назначение диспетчером"
    [competitor] = reason["competitors"]
    assert competitor["algorithm_id"] == "ortools_gls_v1"
    assert competitor["decisive_metric"] == "manual_assignment"
    # у исходного плана в отказах X (ремонт) и Y (подключение), у нового — только Y
    assert competitor["plan_key"][:3] == [0, 1, 1]
    assert reason["selected_plan_key"][:3] == [0, 1, 0]
    assert reason["travel_norm_mode"] == "soft"
    assert reason["travel_norm_max_factor"] == 2.0
    result = plan["result"]
    assert X not in {item["request_id"] for item in result["unassigned"]}
    stops = result["routes"][0]["stops"]
    assert [stop["request_id"] for stop in stops] == [A, X, B]
    assert stops[1]["service_start_at"] == best["service_start_at"]
    assert plan["metrics"]["assigned_count"] == parent_before["metrics"]["assigned_count"] + 1
    assert plan["metrics"]["unassigned_count"] == parent_before["metrics"]["unassigned_count"] - 1
    assert (
        plan["metrics"]["total_distance_meters"]
        == parent_before["metrics"]["total_distance_meters"] + best["added_distance_meters"]
    )
    # прежние визиты не сдвинулись ни на минуту, резерв и ожидаемое окончание те же
    old_state = visit_state(parent_before["result"])
    new_state = visit_state(result)
    assert {request_id: new_state[request_id] for request_id in old_state} == old_state
    assert route_order(result, set(old_state)) == route_order(
        parent_before["result"], set(old_state)
    )
    # исходный план не переписан, новый план стал текущим планом набора
    assert client.get(f"/api/v1/plans/{plan_id}").json() == parent_before
    assert _plan_total(client, database, dataset_id) == 2
    datasets = client.get("/api/v1/datasets").json()
    current = next(item for item in datasets if item["id"] == dataset_id)
    assert current["current_plan_id"] == plan["id"]

    # Повтор по исходному плану — и из вкладки со старой ревизией, и с новой — не
    # проходит: исходный план посчитан на ревизии 1.
    for expected_revision in (1, 2):
        again = client.post(
            f"/api/v1/plans/{plan_id}/assign",
            json={
                "request_id": X,
                "engineer_id": E1,
                "position": 2,
                "expected_revision": expected_revision,
            },
        )
        assert again.status_code == 409
        assert again.json()["code"] == "stale_input"
    assert _plan_total(client, database, dataset_id) == 2


def test_api_assign_into_superseded_plan_is_stale_plan(
    api: tuple[TestClient, _TestDatabase, str, str],
) -> None:
    client, database, dataset_id, plan_id = api
    portal = client.portal
    assert portal is not None
    # второй расчёт на той же ревизии: revision он не поднимает
    newer_id = portal.call(_add_newer_plan, database, dataset_id, plan_id)

    assigned = _assign_best(client, plan_id, 1)
    replanned = client.post(f"/api/v1/plans/{plan_id}/replan", json=_replan_event("stale", 1))

    for response in (assigned, replanned):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "stale_plan"
        assert response.json()["details"]["current_plan_id"] == newer_id
    assert _plan_total(client, database, dataset_id) == 2
    assert _revision(client, dataset_id) == 1
    assert not app.state.planning_lock.locked()


def test_api_replan_rejects_a_request_number_already_in_the_dataset(
    api: tuple[TestClient, _TestDatabase, str, str],
) -> None:
    client, database, dataset_id, plan_id = api
    event = _replan_event("repeated-number", 1)
    # в наборе уже есть заявка с тем же номером — например, от прошлого события
    existing = ServiceRequestInput.model_validate(event["request"]).model_copy(
        update={"id": ident("existing-row")}
    )
    portal = client.portal
    assert portal is not None
    portal.call(_add_request_row, database, dataset_id, existing)

    response = client.post(f"/api/v1/plans/{plan_id}/replan", json=event)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "request_external_id_exists"
    assert existing.external_id in response.json()["message"]
    assert _plan_total(client, database, dataset_id) == 1
    assert not app.state.planning_lock.locked()


def test_api_replan_from_parent_after_assign_keeps_manual_assignment(
    api: tuple[TestClient, _TestDatabase, str, str],
) -> None:
    client, database, dataset_id, plan_id = api
    assigned = _assign_best(client, plan_id, 1)
    assert assigned.status_code == 200, assigned.text
    manual = assigned.json()

    # Устаревшая вкладка: исходный план и старая ревизия. Вкладка, обновившая
    # ревизию, но всё ещё на исходном плане, — тоже отказ: план посчитан на ревизии 1.
    for key, expected_revision in (("old-tab", 1), ("refreshed-revision", 2)):
        response = client.post(
            f"/api/v1/plans/{plan_id}/replan",
            json=_replan_event(key, expected_revision),
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "stale_input"

    # ручное назначение осталось текущим планом и не затёрто
    assert _plan_total(client, database, dataset_id) == 2
    assert _current_plan_id(client, dataset_id) == manual["id"]
    assert _revision(client, dataset_id) == 2
    stored = client.get(f"/api/v1/plans/{manual['id']}").json()
    served = {stop["request_id"] for route in stored["result"]["routes"] for stop in route["stops"]}
    assert X in served
    assert X not in {item["request_id"] for item in stored["result"]["unassigned"]}


def test_api_assign_is_busy_while_planning_runs(
    api: tuple[TestClient, _TestDatabase, str, str],
) -> None:
    client, database, dataset_id, plan_id = api
    portal = client.portal
    assert portal is not None
    lock = app.state.planning_lock

    # идёт расчёт или перепланирование, начатое до назначения
    portal.call(lock.acquire)
    try:
        busy = _assign_best(client, plan_id, 1)
    finally:
        portal.call(lock.release)

    assert busy.status_code == 409
    assert busy.json()["code"] == "planning_busy"
    assert _plan_total(client, database, dataset_id) == 1
    assert _revision(client, dataset_id) == 1
    # расчёт закончился — назначение проходит и отпускает блокировку
    done = _assign_best(client, plan_id, 1)
    assert done.status_code == 200, done.text
    assert not lock.locked()


def test_api_manual_assign_rejects_stale_revision(
    api: tuple[TestClient, _TestDatabase, str, str],
) -> None:
    client, database, dataset_id, plan_id = api

    response = client.post(
        f"/api/v1/plans/{plan_id}/assign",
        json={"request_id": X, "engineer_id": E1, "position": 1, "expected_revision": 2},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "stale_input"
    assert _plan_total(client, database, dataset_id) == 1


def test_api_manual_assign_rejects_already_assigned_request(
    api: tuple[TestClient, _TestDatabase, str, str],
) -> None:
    client, database, dataset_id, plan_id = api

    listed = client.get(f"/api/v1/plans/{plan_id}/placements", params={"request_id": A})
    response = client.post(
        f"/api/v1/plans/{plan_id}/assign",
        json={"request_id": A, "engineer_id": E2, "position": 0, "expected_revision": 1},
    )

    assert listed.status_code == 409
    assert listed.json()["code"] == "request_already_assigned"
    assert response.status_code == 409
    assert response.json()["code"] == "request_already_assigned"
    assert _plan_total(client, database, dataset_id) == 1


def test_api_plan_from_other_norm_mode_is_409_not_500(
    api: tuple[TestClient, _TestDatabase, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Смена режима норматива не роняет сервер на уже опубликованном плане.

    Режим — настройка процесса, а не свойство плана. План с превышением, посчитанный
    в soft, после переключения на hard недопустим, и перепланирование от него падало
    с 500 внутри решателя. Теперь — 409 с понятной причиной.
    """
    client, _database, _dataset_id, plan_id = api
    placements = client.get(
        f"/api/v1/plans/{plan_id}/placements", params={"request_id": X}
    ).json()["placements"]
    over_norm = [item for item in placements if item["norm_exceeded"]]
    # без превышения в исходном плане тест проверял бы пустое место
    assert over_norm, "в сценарии нет варианта с превышением норматива"
    choice = over_norm[0]
    assigned = client.post(
        f"/api/v1/plans/{plan_id}/assign",
        json={
            "request_id": X,
            "engineer_id": choice["engineer_id"],
            "position": choice["position"],
            "expected_revision": 1,
        },
    )
    assert assigned.status_code == 200, assigned.text
    manual = assigned.json()
    assert manual["metrics"]["norm_violation_count"] > 0

    monkeypatch.setenv("TRAVEL_NORM_MODE", "hard")
    get_settings.cache_clear()

    replan = client.post(
        f"/api/v1/plans/{manual['id']}/replan",
        json=_replan_event("after-mode-switch", 2),
    )
    assert replan.status_code == 409, replan.text
    assert replan.json()["code"] == "plan_norm_policy_mismatch"

    reassign = client.post(
        f"/api/v1/plans/{manual['id']}/assign",
        json={"request_id": X, "engineer_id": E1, "position": 1, "expected_revision": 2},
    )
    assert reassign.status_code == 409, reassign.text
    assert reassign.json()["code"] == "plan_norm_policy_mismatch"
