from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import DomainError
from app.routing import geocode_address, geocoding

NOMINATIM_ITEMS: list[dict[str, Any]] = [
    {
        "lat": "55.7078213",
        "lon": "37.7518635",
        "display_name": "97 к1, Волгоградский проспект, район Кузьминки, Москва, Россия",
        "address": {"suburb": "район Кузьминки", "city": "Москва"},
    },
    {
        "lat": "54.8337",
        "lon": "38.1519",
        "display_name": "Кашира, городской округ Кашира, Московская область, Россия",
        "address": {"town": "Кашира", "county": "городской округ Кашира"},
    },
    {"lat": "not-a-number", "lon": "37.0", "display_name": "битая запись"},
]


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    geocoding._geocoder_cache.clear()


def _fake_client(
    calls: list[dict[str, Any]],
    *,
    body: Any = None,
    error: Exception | None = None,
) -> type:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return NOMINATIM_ITEMS if body is None else body

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> FakeResponse:
            calls.append({"url": url, **kwargs})
            if error is not None:
                raise error
            return FakeResponse()

    return FakeClient


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "geocoder_min_interval_seconds": 0,
        "routing_retry_backoff_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


async def test_geocode_returns_candidates_with_zone_style_district(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(calls))

    result = await geocode_address("  Волгоградский   97к1 ", settings=_settings())

    assert result.query == "Волгоградский 97к1"
    assert [item.district for item in result.items] == ["Кузьминки", "Кашира"]
    assert result.items[0].coordinates.latitude == pytest.approx(55.7078213)
    assert result.items[0].coordinates.longitude == pytest.approx(37.7518635)
    # Запись без числовой координаты отброшена, а не превращена в (0, 0).
    assert len(result.items) == 2
    request = calls[0]
    assert request["url"] == "https://nominatim.openstreetmap.org/search"
    assert request["params"]["q"] == "Волгоградский 97к1"
    assert request["params"]["countrycodes"] == "ru"
    assert request["headers"]["User-Agent"].startswith("task-router-local-demo")


async def test_geocode_caches_repeated_query(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(calls))

    first = await geocode_address("Волгоградский 97к1", settings=_settings())
    second = await geocode_address("волгоградский  97К1", settings=_settings())

    assert len(calls) == 1
    assert second.items == first.items


async def test_geocode_not_found_is_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(calls, body=[]))

    result = await geocode_address("улица, которой нет", settings=_settings())

    assert result.items == []


async def test_geocode_unavailable_is_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client(calls, error=httpx.ConnectError("offline")),
    )

    with pytest.raises(DomainError) as error:
        await geocode_address("Волгоградский 97к1", settings=_settings(routing_retry_attempts=1))

    assert error.value.code == "geocoder_unavailable"
    assert error.value.status_code == 503
    assert len(calls) == 2
    # Сбой не кешируется: следующий запрос снова идёт в сервис.
    assert geocoding._geocoder_cache == {}


async def test_geocode_disabled_without_url() -> None:
    with pytest.raises(DomainError) as error:
        await geocode_address("Волгоградский 97к1", settings=_settings(geocoder_url=None))

    assert error.value.code == "geocoder_unconfigured"
    assert error.value.status_code == 503
