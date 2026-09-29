import asyncio
from datetime import datetime
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import DomainError
from app.routing import build_detailed_route, decode_polyline
from app.schemas import Coordinates, Transport


def _encode_polyline(points: list[tuple[float, float]], precision: int) -> str:
    factor = 10**precision
    previous_latitude = 0
    previous_longitude = 0
    output: list[str] = []
    for latitude, longitude in points:
        values = [
            round(latitude * factor) - previous_latitude,
            round(longitude * factor) - previous_longitude,
        ]
        previous_latitude = round(latitude * factor)
        previous_longitude = round(longitude * factor)
        for value in values:
            encoded = ~(value << 1) if value < 0 else value << 1
            while encoded >= 0x20:
                output.append(chr((0x20 | (encoded & 0x1F)) + 63))
                encoded >>= 5
            output.append(chr(encoded + 63))
    return "".join(output)


def test_decode_polyline_returns_geojson_coordinate_order() -> None:
    value = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    assert decode_polyline(value, precision=5) == [
        (-120.2, 38.5),
        (-120.95, 40.7),
        (-126.453, 43.252),
    ]


@pytest.mark.parametrize(
    ("transport", "costing"),
    [
        (Transport.CAR, "auto"),
        (Transport.WALKING, "pedestrian"),
        (Transport.BICYCLE, "bicycle"),
    ],
)
async def test_valhalla_profiles_return_network_geometry(
    monkeypatch: pytest.MonkeyPatch,
    transport: Transport,
    costing: str,
) -> None:
    shape = _encode_polyline([(55.75, 37.61), (55.751, 37.615), (55.752, 37.62)], 6)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "trip": {
                    "legs": [
                        {
                            "shape": shape,
                            "summary": {"time": 123.4, "length": 1.25},
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> FakeResponse:
            assert kwargs["json"]["costing"] == costing
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    detailed = await build_detailed_route(
        engineer_id="engineer-1",
        transport=transport,
        points=[
            Coordinates(latitude=55.75, longitude=37.61),
            Coordinates(latitude=55.752, longitude=37.62),
        ],
        departure_times=[datetime.fromisoformat("2026-09-23T08:00:00+03:00")],
        settings=Settings(valhalla_min_interval_seconds=0),
    )
    assert detailed.routing_quality.value == "exact"
    assert detailed.routing_method == f"valhalla_{costing}_v1"
    assert detailed.total_duration_seconds == 123
    assert detailed.total_distance_meters == 1250
    assert detailed.geometry.coordinates[1] == (37.615, 55.751)


async def test_public_transport_requires_otp_with_gtfs() -> None:
    with pytest.raises(DomainError) as error:
        await build_detailed_route(
            engineer_id="engineer-1",
            transport=Transport.PUBLIC_TRANSPORT,
            points=[
                Coordinates(latitude=55.75, longitude=37.61),
                Coordinates(latitude=55.752, longitude=37.62),
            ],
            departure_times=[datetime.fromisoformat("2026-09-23T08:00:00+03:00")],
            settings=Settings(otp_url=None, valhalla_min_interval_seconds=0),
        )
    assert error.value.code == "routing_provider_unconfigured"


async def test_valhalla_uses_bounded_grouped_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shapes = [
        _encode_polyline([(55.75, 37.61), (55.751, 37.615)], 6),
        _encode_polyline([(55.751, 37.615), (55.752, 37.62)], 6),
    ]
    payload_sizes: list[int] = []
    request_timeouts: list[float | None] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "trip": {
                    "legs": [
                        {
                            "shape": shape,
                            "summary": {"time": 60, "length": 0.5},
                        }
                        for shape in shapes
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.calls = 0

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> FakeResponse:
            payload_sizes.append(len(kwargs["json"]["locations"]))
            request_timeouts.append(kwargs.get("timeout"))
            self.calls += 1
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    detailed = await build_detailed_route(
        engineer_id="engineer-1",
        transport=Transport.WALKING,
        points=[
            Coordinates(latitude=55.75, longitude=37.61),
            Coordinates(latitude=55.751, longitude=37.615),
            Coordinates(latitude=55.752, longitude=37.62),
        ],
        departure_times=[
            datetime.fromisoformat("2026-09-23T08:00:00+03:00"),
            datetime.fromisoformat("2026-09-23T08:20:00+03:00"),
        ],
        settings=Settings(valhalla_min_interval_seconds=0),
    )
    assert payload_sizes == [3]
    assert request_timeouts == [5.0]
    assert len(detailed.segments) == 2
    assert detailed.total_distance_meters == 1_000


async def test_grouped_timeout_falls_back_to_independent_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shapes = [
        _encode_polyline([(55.75, 37.61), (55.751, 37.615)], 6),
        _encode_polyline([(55.751, 37.615), (55.752, 37.62)], 6),
    ]
    payload_sizes: list[int] = []

    class FakeResponse:
        def __init__(self, shape: str) -> None:
            self.shape = shape

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "trip": {
                    "legs": [
                        {
                            "shape": self.shape,
                            "summary": {"time": 60, "length": 0.5},
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.calls = 0

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> FakeResponse:
            payload_sizes.append(len(kwargs["json"]["locations"]))
            if self.calls == 0:
                self.calls += 1
                request = httpx.Request("POST", "https://routing.example/route")
                raise httpx.ReadTimeout("grouped route timed out", request=request)
            response = FakeResponse(shapes[self.calls - 1])
            self.calls += 1
            return response

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    detailed = await build_detailed_route(
        engineer_id="engineer-1",
        transport=Transport.WALKING,
        points=[
            Coordinates(latitude=55.75, longitude=37.61),
            Coordinates(latitude=55.751, longitude=37.615),
            Coordinates(latitude=55.752, longitude=37.62),
        ],
        departure_times=[
            datetime.fromisoformat("2026-09-23T08:00:00+03:00"),
            datetime.fromisoformat("2026-09-23T08:20:00+03:00"),
        ],
        settings=Settings(valhalla_min_interval_seconds=0),
    )

    assert payload_sizes == [3, 2, 2]
    assert len(detailed.segments) == 2


async def test_valhalla_uses_effective_profile_for_each_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _encode_polyline([(55.75, 37.61), (55.751, 37.615)], 6)
    costings: list[str] = []
    payload_sizes: list[int] = []

    class FakeResponse:
        def __init__(self, leg_count: int) -> None:
            self.leg_count = leg_count

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "trip": {
                    "legs": [
                        {
                            "shape": shape,
                            "summary": {"time": 60, "length": 0.1},
                        }
                        for _index in range(self.leg_count)
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> FakeResponse:
            costings.append(str(kwargs["json"]["costing"]))
            payload_size = len(kwargs["json"]["locations"])
            payload_sizes.append(payload_size)
            return FakeResponse(payload_size - 1)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    detailed = await build_detailed_route(
        engineer_id="engineer-1",
        transport=Transport.CAR,
        points=[
            Coordinates(latitude=55.75, longitude=37.61),
            Coordinates(latitude=55.751, longitude=37.615),
            Coordinates(latitude=55.752, longitude=37.62),
            Coordinates(latitude=55.753, longitude=37.625),
            Coordinates(latitude=55.754, longitude=37.63),
        ],
        departure_times=[
            datetime.fromisoformat("2026-09-23T08:00:00+03:00"),
            datetime.fromisoformat("2026-09-23T08:20:00+03:00"),
            datetime.fromisoformat("2026-09-23T08:40:00+03:00"),
            datetime.fromisoformat("2026-09-23T09:00:00+03:00"),
        ],
        segment_transports=[
            Transport.CAR,
            Transport.WALKING,
            Transport.WALKING,
            Transport.CAR,
        ],
        settings=Settings(valhalla_min_interval_seconds=0),
    )
    assert costings == ["auto", "pedestrian", "auto"]
    assert payload_sizes == [2, 3, 2]
    assert detailed.routing_method == "valhalla_mixed_leg_profiles_v1"


async def test_valhalla_retries_temporary_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _encode_polyline([(55.75, 37.61), (55.751, 37.615)], 6)
    calls = 0

    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code < 400:
                return
            request = httpx.Request("POST", "https://routing.example/route")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "temporary routing failure",
                request=request,
                response=response,
            )

        def json(self) -> dict[str, Any]:
            return {
                "trip": {
                    "legs": [
                        {
                            "shape": shape,
                            "summary": {"time": 60, "length": 0.1},
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **_kwargs: Any) -> FakeResponse:
            nonlocal calls
            calls += 1
            return FakeResponse(503 if calls == 1 else 200)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    detailed = await build_detailed_route(
        engineer_id="engineer-1",
        transport=Transport.CAR,
        points=[
            Coordinates(latitude=55.75, longitude=37.61),
            Coordinates(latitude=55.751, longitude=37.615),
        ],
        departure_times=[datetime.fromisoformat("2026-09-23T08:00:00+03:00")],
        settings=Settings(
            routing_retry_attempts=1,
            routing_retry_backoff_seconds=0,
            valhalla_min_interval_seconds=0,
        ),
    )

    assert calls == 2
    assert len(detailed.geometry.coordinates) == 2


async def test_valhalla_rate_limiter_does_not_hold_lock_during_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _encode_polyline([(55.75, 37.61), (55.751, 37.615)], 6)
    both_started = asyncio.Event()
    release_responses = asyncio.Event()
    started = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "trip": {
                    "legs": [
                        {
                            "shape": shape,
                            "summary": {"time": 60, "length": 0.1},
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **_kwargs: Any) -> FakeResponse:
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await release_responses.wait()
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    settings = Settings(valhalla_min_interval_seconds=0)
    points = [
        Coordinates(latitude=55.75, longitude=37.61),
        Coordinates(latitude=55.751, longitude=37.615),
    ]
    departure_times = [datetime.fromisoformat("2026-09-23T08:00:00+03:00")]
    first = asyncio.create_task(
        build_detailed_route(
            engineer_id="engineer-1",
            transport=Transport.CAR,
            points=points,
            departure_times=departure_times,
            settings=settings,
        )
    )
    second = asyncio.create_task(
        build_detailed_route(
            engineer_id="engineer-2",
            transport=Transport.CAR,
            points=points,
            departure_times=departure_times,
            settings=settings,
        )
    )
    try:
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
    finally:
        release_responses.set()
        await asyncio.gather(first, second)

    assert started == 2


async def test_route_deadline_returns_controlled_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **_kwargs: Any) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    settings = Settings(valhalla_min_interval_seconds=0)
    settings.routing_route_timeout_seconds = 0.05

    with pytest.raises(DomainError) as error:
        await build_detailed_route(
            engineer_id="engineer-1",
            transport=Transport.CAR,
            points=[
                Coordinates(latitude=55.75, longitude=37.61),
                Coordinates(latitude=55.751, longitude=37.615),
            ],
            departure_times=[datetime.fromisoformat("2026-09-23T08:00:00+03:00")],
            settings=settings,
        )

    assert error.value.code == "routing_provider_timeout"
    assert error.value.status_code == 504


async def test_valhalla_timeout_switches_to_osrm_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_calls = 0
    get_calls = 0

    class OsrmResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "routes": [
                    {
                        "legs": [
                            {
                                "distance": 500,
                                "duration": 120,
                                "steps": [
                                    {
                                        "geometry": {
                                            "coordinates": [
                                                [37.61, 55.75],
                                                [37.615, 55.751],
                                            ]
                                        }
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **_kwargs: Any) -> None:
            nonlocal post_calls
            post_calls += 1
            request = httpx.Request("POST", "https://routing.example/route")
            raise httpx.ReadTimeout("Valhalla timed out", request=request)

        async def get(self, _url: str, **_kwargs: Any) -> OsrmResponse:
            nonlocal get_calls
            get_calls += 1
            return OsrmResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    detailed = await build_detailed_route(
        engineer_id="engineer-1",
        transport=Transport.CAR,
        points=[
            Coordinates(latitude=55.75, longitude=37.61),
            Coordinates(latitude=55.751, longitude=37.615),
        ],
        departure_times=[datetime.fromisoformat("2026-09-23T08:00:00+03:00")],
        settings=Settings(
            routing_retry_attempts=5,
            routing_retry_backoff_seconds=0,
            valhalla_min_interval_seconds=0,
        ),
    )

    assert post_calls == 1
    assert get_calls == 1
    assert detailed.routing_method == "osrm_driving_fallback_v1"


async def test_exact_osrm_fallback_is_used_when_valhalla_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_url = ""
    get_calls = 0

    class FailedValhallaResponse:
        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "https://routing.example/route")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError(
                "Valhalla unavailable",
                request=request,
                response=response,
            )

    class OsrmResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "code": "Ok",
                "routes": [
                    {
                        "legs": [
                            {
                                "distance": 840.4,
                                "duration": 370.2,
                                "steps": [
                                    {
                                        "geometry": {
                                            "coordinates": [
                                                [37.61, 55.75],
                                                [37.615, 55.751],
                                                [37.62, 55.752],
                                            ]
                                        }
                                    }
                                ],
                            },
                            {
                                "distance": 120.2,
                                "duration": 50.1,
                                "steps": [
                                    {
                                        "geometry": {
                                            "coordinates": [
                                                [37.62, 55.752],
                                                [37.625, 55.753],
                                            ]
                                        }
                                    }
                                ],
                            },
                        ]
                    }
                ],
            }

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **_kwargs: Any) -> FailedValhallaResponse:
            return FailedValhallaResponse()

        async def get(self, url: str, **_kwargs: Any) -> OsrmResponse:
            nonlocal get_calls, requested_url
            get_calls += 1
            requested_url = url
            return OsrmResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    detailed = await build_detailed_route(
        engineer_id="engineer-1",
        transport=Transport.BICYCLE,
        points=[
            Coordinates(latitude=55.75, longitude=37.61),
            Coordinates(latitude=55.752, longitude=37.62),
            Coordinates(latitude=55.753, longitude=37.625),
        ],
        departure_times=[
            datetime.fromisoformat("2026-09-23T08:00:00+03:00"),
            datetime.fromisoformat("2026-09-23T08:20:00+03:00"),
        ],
        settings=Settings(
            routing_retry_attempts=0,
            valhalla_min_interval_seconds=0,
            osrm_fallback_url="https://fallback.example",
        ),
    )

    assert "/routed-bike/route/v1/driving/" in requested_url
    assert get_calls == 1
    assert detailed.routing_quality.value == "exact"
    assert detailed.routing_method == "osrm_cycling_fallback_v1"
    assert detailed.total_distance_meters == 960
    assert len(detailed.segments) == 2
    assert len(detailed.geometry.coordinates) == 4
