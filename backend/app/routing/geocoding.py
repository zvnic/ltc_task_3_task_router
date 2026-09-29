import asyncio
import logging
from collections import OrderedDict
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import DomainError
from app.schemas import Coordinates, GeocodeCandidate, GeocodeResult

GEOCODER_PROVIDER = "OpenStreetMap Nominatim"
GEOCODER_ATTRIBUTION = "© участники OpenStreetMap"
GEOCODER_RESULT_LIMIT = 5
# Nominatim просит кешировать повторные запросы: диспетчер ищет один и тот же адрес
# по нескольку раз, пока проигрывает аварию.
GEOCODER_CACHE_SIZE = 256
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# Приставки районов в ответе OSM: «район Кузьминки» → «Кузьминки», как в заявках.
DISTRICT_PREFIXES = ("район ", "поселение ", "городской округ ", "муниципальный округ ")

logger = logging.getLogger(__name__)
_geocoder_rate_lock = asyncio.Lock()
_geocoder_last_request_at = 0.0
_geocoder_cache: OrderedDict[str, list[GeocodeCandidate]] = OrderedDict()


def normalize_geocode_query(query: str) -> str:
    return " ".join(query.split())


def _district(address: dict[str, Any]) -> str:
    """Район для списка заявок: район города, иначе населённый пункт, иначе округ."""
    for key in ("suburb", "city_district", "town", "city", "village", "municipality", "county"):
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            for prefix in DISTRICT_PREFIXES:
                if name.lower().startswith(prefix):
                    return name[len(prefix):].strip() or name
            return name
    return "Вне справочника зоны"


def _candidate(item: dict[str, Any]) -> GeocodeCandidate | None:
    try:
        coordinates = Coordinates(latitude=float(item["lat"]), longitude=float(item["lon"]))
    except (KeyError, TypeError, ValueError):
        return None
    label = str(item.get("display_name") or "").strip()
    if not label:
        return None
    address = item.get("address")
    return GeocodeCandidate(
        address=label,
        district=_district(address if isinstance(address, dict) else {}),
        coordinates=coordinates,
    )


async def search_nominatim(
    *,
    client: httpx.AsyncClient,
    query: str,
    settings: Settings,
    viewbox: str | None = None,
    bounded: bool = False,
) -> list[dict[str, Any]]:
    """Сырой ответ Nominatim на один запрос — с темпом не чаще раза в интервал.

    ``viewbox`` и ``bounded`` сужают поиск: инструмент геокодирования исходных
    адресов ищет дом только вокруг его района (``sims.geocode_sources``).
    """
    global _geocoder_last_request_at

    url = f"{str(settings.geocoder_url).rstrip('/')}/search"
    params: dict[str, str | int] = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": GEOCODER_RESULT_LIMIT,
        "countrycodes": "ru",
        "accept-language": "ru",
        "viewbox": viewbox or settings.geocoder_viewbox,
        "bounded": 1 if bounded else 0,
    }
    attempts = settings.routing_retry_attempts + 1
    for attempt in range(attempts):
        try:
            async with _geocoder_rate_lock:
                elapsed = asyncio.get_running_loop().time() - _geocoder_last_request_at
                wait_seconds = settings.geocoder_min_interval_seconds - elapsed
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                _geocoder_last_request_at = asyncio.get_running_loop().time()
            response = await client.get(
                url,
                params=params,
                headers={"User-Agent": settings.geocoder_user_agent},
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, list):
                raise ValueError("geocoder response is not a list")
            return [item for item in body if isinstance(item, dict)]
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in RETRYABLE_STATUS_CODES
            if not retryable or attempt + 1 >= attempts:
                raise
        except httpx.TransportError:
            if attempt + 1 >= attempts:
                raise
        delay = settings.routing_retry_backoff_seconds * (2**attempt)
        if delay:
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable geocoder retry state")


async def geocode_address(query: str, *, settings: Settings) -> GeocodeResult:
    """Координаты адреса новой заявки: до пяти вариантов, лучший первым.

    Пустой список — адрес не найден; это не ошибка, диспетчер уточнит адрес или
    введёт координаты сам. Недоступный сервис — ошибка 503: подставлять координату
    «на глаз» (центр района, офис) нельзя, заявка уехала бы не туда.
    """
    normalized = normalize_geocode_query(query)
    if not settings.geocoder_url:
        raise DomainError(
            "geocoder_unconfigured",
            "Поиск адреса выключен (GEOCODER_URL не задан). Выберите адрес из справочника "
            "или введите координаты.",
            status_code=503,
        )
    cache_key = normalized.casefold()
    cached = _geocoder_cache.get(cache_key)
    if cached is None:
        try:
            async with httpx.AsyncClient(timeout=settings.geocoder_timeout_seconds) as client:
                raw_items = await search_nominatim(
                    client=client, query=normalized, settings=settings
                )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("geocoder request failed: %s", exc.__class__.__name__)
            raise DomainError(
                "geocoder_unavailable",
                "Сервис поиска адреса не ответил. Повторите через минуту или введите "
                "координаты вручную.",
                status_code=503,
            ) from exc
        cached = [
            candidate
            for candidate in (_candidate(item) for item in raw_items)
            if candidate is not None
        ]
        _geocoder_cache[cache_key] = cached
        while len(_geocoder_cache) > GEOCODER_CACHE_SIZE:
            _geocoder_cache.popitem(last=False)
    else:
        _geocoder_cache.move_to_end(cache_key)
    return GeocodeResult(
        query=normalized,
        provider=GEOCODER_PROVIDER,
        attribution=GEOCODER_ATTRIBUTION,
        items=list(cached),
    )
