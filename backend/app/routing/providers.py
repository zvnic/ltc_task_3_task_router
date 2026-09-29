import asyncio
import logging
from datetime import datetime
from typing import Any, cast

import httpx

from app.core.config import Settings
from app.core.errors import DomainError
from app.schemas import (
    Coordinates,
    DetailedEngineerRoute,
    DetailedRouteSegment,
    RouteGeometry,
    RoutingQuality,
    Transport,
)

VALHALLA_COSTING: dict[Transport, str] = {
    Transport.CAR: "auto",
    Transport.WALKING: "pedestrian",
    Transport.BICYCLE: "bicycle",
}
OSRM_PROFILE: dict[Transport, str] = {
    Transport.CAR: "driving",
    Transport.WALKING: "walking",
    Transport.BICYCLE: "cycling",
}
OSRM_SERVER: dict[Transport, str] = {
    Transport.CAR: "routed-car",
    Transport.WALKING: "routed-foot",
    Transport.BICYCLE: "routed-bike",
}

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
logger = logging.getLogger(__name__)
_valhalla_rate_lock = asyncio.Lock()
_valhalla_last_request_at = 0.0


def decode_polyline(value: str, *, precision: int) -> list[tuple[float, float]]:
    """Decode an encoded polyline into GeoJSON coordinate order (longitude, latitude)."""
    coordinates: list[tuple[float, float]] = []
    latitude = 0
    longitude = 0
    index = 0
    scale = 10**precision
    while index < len(value):
        deltas: list[int] = []
        for _axis in range(2):
            result = 0
            shift = 0
            while True:
                if index >= len(value):
                    raise ValueError("invalid encoded polyline")
                item = ord(value[index]) - 63
                index += 1
                result |= (item & 0x1F) << shift
                shift += 5
                if item < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        coordinates.append((longitude / scale, latitude / scale))
    return coordinates


def _merge_coordinates(
    current: list[tuple[float, float]],
    addition: list[tuple[float, float]],
) -> None:
    if current and addition and current[-1] == addition[0]:
        current.extend(addition[1:])
    else:
        current.extend(addition)


def _provider_error(transport: Transport, *, unavailable: bool = False) -> DomainError:
    if unavailable:
        return DomainError(
            "routing_provider_unconfigured",
            "Для общественного транспорта требуется настроенный OpenTripPlanner с OSM и GTFS.",
            status_code=503,
            details={"transport_type": transport.value, "required_provider": "otp"},
        )
    return DomainError(
        "routing_provider_unavailable",
        "Точная геометрия маршрута временно недоступна; прямой отрезок не показан.",
        status_code=503,
        details={"transport_type": transport.value},
    )


async def _post_valhalla(
    *,
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    settings: Settings,
    retry_attempts: int | None = None,
    request_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    global _valhalla_last_request_at

    attempts = (settings.routing_retry_attempts if retry_attempts is None else retry_attempts) + 1
    for attempt in range(attempts):
        try:
            async with _valhalla_rate_lock:
                elapsed = asyncio.get_running_loop().time() - _valhalla_last_request_at
                wait_seconds = settings.valhalla_min_interval_seconds - elapsed
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                _valhalla_last_request_at = asyncio.get_running_loop().time()
            # Pace request starts, but never hold the global limiter while a
            # public provider is responding. Otherwise one slow request blocks
            # geometry for every engineer route handled by this API process.
            request_kwargs: dict[str, Any] = {
                "json": payload,
                "headers": {"X-Client-Id": settings.valhalla_client_id},
            }
            if request_timeout_seconds is not None:
                request_kwargs["timeout"] = request_timeout_seconds
            response = await client.post(
                f"{settings.valhalla_url.rstrip('/')}/route",
                **request_kwargs,
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
        except httpx.TimeoutException:
            # A second attempt against the same stalled public endpoint spends
            # the route budget without adding resilience. The caller either
            # falls back from a grouped request to paced legs or switches the
            # whole route to exact OSRM.
            raise
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
    raise RuntimeError("unreachable Valhalla retry state")


async def _get_osrm(
    *,
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
) -> dict[str, Any]:
    attempts = settings.routing_retry_attempts + 1
    for attempt in range(attempts):
        try:
            response = await client.get(
                url,
                params={"overview": "full", "geometries": "geojson", "steps": "true"},
                headers={"X-Client-Id": settings.valhalla_client_id},
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
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
    raise RuntimeError("unreachable OSRM retry state")


async def _osrm_group(
    *,
    client: httpx.AsyncClient,
    start_sequence: int,
    points: list[Coordinates],
    transport: Transport,
    settings: Settings,
) -> list[DetailedRouteSegment]:
    if not settings.osrm_fallback_url:
        raise httpx.ConnectError("OSRM fallback is not configured")
    coordinates = ";".join(
        f"{point.longitude},{point.latitude}" for point in points
    )
    body = await _get_osrm(
        client=client,
        url=(
            f"{settings.osrm_fallback_url.rstrip('/')}/{OSRM_SERVER[transport]}"
            f"/route/v1/driving/{coordinates}"
        ),
        settings=settings,
    )
    route = body["routes"][0]
    legs = route["legs"]
    if len(legs) != len(points) - 1:
        raise ValueError("OSRM route response has unexpected leg count")
    segments: list[DetailedRouteSegment] = []
    for offset, leg in enumerate(legs):
        route_coordinates: list[tuple[float, float]] = []
        for step in leg["steps"]:
            step_coordinates = [
                (float(item[0]), float(item[1]))
                for item in step["geometry"]["coordinates"]
            ]
            _merge_coordinates(route_coordinates, step_coordinates)
        if len(route_coordinates) < 2:
            raise ValueError("OSRM route leg has no geometry")
        segments.append(
            DetailedRouteSegment(
                sequence=start_sequence + offset,
                duration_seconds=round(float(leg["duration"])),
                distance_meters=round(float(leg["distance"])),
                geometry=RouteGeometry(coordinates=route_coordinates),
            )
        )
    return segments


async def _osrm_route_segments(
    *,
    client: httpx.AsyncClient,
    points: list[Coordinates],
    segment_transports: list[Transport],
    settings: Settings,
) -> list[DetailedRouteSegment]:
    segments: list[DetailedRouteSegment] = []
    start = 0
    while start < len(segment_transports):
        transport = segment_transports[start]
        end = start + 1
        while end < len(segment_transports) and segment_transports[end] is transport:
            end += 1
        segments.extend(
            await _osrm_group(
                client=client,
                start_sequence=start + 1,
                points=points[start : end + 1],
                transport=transport,
                settings=settings,
            )
        )
        start = end
    return segments


async def _valhalla_segment(
    *,
    client: httpx.AsyncClient,
    sequence: int,
    origin: Coordinates,
    destination: Coordinates,
    departure_at: datetime,
    transport: Transport,
    settings: Settings,
) -> DetailedRouteSegment:
    payload = {
        "locations": [
            {
                "lat": origin.latitude,
                "lon": origin.longitude,
                "type": "break",
            },
            {
                "lat": destination.latitude,
                "lon": destination.longitude,
                "type": "break",
            },
        ],
        "costing": VALHALLA_COSTING[transport],
        "date_time": {
            "type": 1,
            "value": departure_at.strftime("%Y-%m-%dT%H:%M"),
        },
        "units": "kilometers",
    }
    body = await _post_valhalla(
        client=client,
        payload=payload,
        settings=settings,
    )
    legs = body["trip"]["legs"]
    if len(legs) != 1:
        raise ValueError("Valhalla segment response must contain one leg")
    leg = legs[0]
    coordinates = decode_polyline(str(leg["shape"]), precision=6)
    if len(coordinates) < 2:
        raise ValueError("route leg has no geometry")
    summary = leg["summary"]
    return DetailedRouteSegment(
        sequence=sequence,
        duration_seconds=round(float(summary["time"])),
        distance_meters=round(float(summary["length"]) * 1000),
        geometry=RouteGeometry(coordinates=coordinates),
    )


async def _valhalla_grouped_segments(
    *,
    client: httpx.AsyncClient,
    start_sequence: int,
    points: list[Coordinates],
    departure_at: datetime,
    transport: Transport,
    settings: Settings,
) -> list[DetailedRouteSegment]:
    payload = {
        "locations": [
            {
                "lat": point.latitude,
                "lon": point.longitude,
                "type": "break",
            }
            for point in points
        ],
        "costing": VALHALLA_COSTING[transport],
        "date_time": {
            "type": 1,
            "value": departure_at.strftime("%Y-%m-%dT%H:%M"),
        },
        "units": "kilometers",
    }
    body = await _post_valhalla(
        client=client,
        payload=payload,
        settings=settings,
        retry_attempts=0,
        request_timeout_seconds=min(5.0, settings.routing_timeout_seconds),
    )
    legs = body["trip"]["legs"]
    if len(legs) != len(points) - 1:
        raise ValueError("Valhalla grouped response leg count does not match route points")
    segments: list[DetailedRouteSegment] = []
    for sequence, leg in enumerate(legs, start=start_sequence):
        coordinates = decode_polyline(str(leg["shape"]), precision=6)
        if len(coordinates) < 2:
            raise ValueError("route leg has no geometry")
        summary = leg["summary"]
        segments.append(
            DetailedRouteSegment(
                sequence=sequence,
                duration_seconds=round(float(summary["time"])),
                distance_meters=round(float(summary["length"]) * 1000),
                geometry=RouteGeometry(coordinates=coordinates),
            )
        )
    return segments


async def _valhalla_route(
    *,
    engineer_id: str,
    transport: Transport,
    points: list[Coordinates],
    departure_times: list[datetime],
    segment_transports: list[Transport],
    settings: Settings,
) -> DetailedEngineerRoute:
    segments: list[DetailedRouteSegment] = []
    route_coordinates: list[tuple[float, float]] = []
    routing_provider = "valhalla"
    try:
        async with httpx.AsyncClient(timeout=settings.routing_timeout_seconds) as client:
            try:
                start = 0
                while start < len(segment_transports):
                    group_transport = segment_transports[start]
                    end = start + 1
                    while (
                        end < len(segment_transports)
                        and segment_transports[end] is group_transport
                    ):
                        end += 1
                    grouped_segments: list[DetailedRouteSegment] = []
                    if end - start > 1:
                        try:
                            grouped_segments = await _valhalla_grouped_segments(
                                client=client,
                                start_sequence=start + 1,
                                points=points[start : end + 1],
                                departure_at=departure_times[start],
                                transport=group_transport,
                                settings=settings,
                            )
                        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                            status_code = (
                                exc.response.status_code
                                if isinstance(exc, httpx.HTTPStatusError)
                                else None
                            )
                            logger.info(
                                "Valhalla grouped route unavailable, falling back to paced legs: "
                                "error=%s status=%s segments=%s",
                                type(exc).__name__,
                                status_code,
                                end - start,
                            )
                    if grouped_segments:
                        segments.extend(grouped_segments)
                    else:
                        for index in range(start, end):
                            segments.append(
                                await _valhalla_segment(
                                    client=client,
                                    sequence=index + 1,
                                    origin=points[index],
                                    destination=points[index + 1],
                                    departure_at=departure_times[index],
                                    transport=segment_transports[index],
                                    settings=settings,
                                )
                            )
                    start = end
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                status_code = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                logger.warning(
                    "Valhalla unavailable, switching whole route to exact OSRM fallback: "
                    "error=%s status=%s completed=%s segments=%s",
                    type(exc).__name__,
                    status_code,
                    len(segments),
                    len(segment_transports),
                )
                routing_provider = "osrm"
                segments = await _osrm_route_segments(
                    client=client,
                    points=points,
                    segment_transports=segment_transports,
                    settings=settings,
                )
    except (TimeoutError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning(
            "Valhalla route geometry failed: error=%s status=%s completed=%s segments=%s",
            type(exc).__name__,
            status_code,
            len(segments),
            len(segment_transports),
        )
        raise _provider_error(transport) from exc

    for segment in segments:
        _merge_coordinates(route_coordinates, segment.geometry.coordinates)
    only_transport = segment_transports[0]
    if routing_provider == "valhalla":
        routing_method = (
            f"valhalla_{VALHALLA_COSTING[only_transport]}_v1"
            if all(item is only_transport for item in segment_transports)
            else "valhalla_mixed_leg_profiles_v1"
        )
    else:
        routing_method = (
            f"osrm_{OSRM_PROFILE[only_transport]}_fallback_v1"
            if all(item is only_transport for item in segment_transports)
            else "osrm_mixed_profiles_fallback_v1"
        )
    return DetailedEngineerRoute(
        engineer_id=engineer_id,
        transport_type=transport,
        routing_method=routing_method,
        routing_quality=RoutingQuality.EXACT,
        total_duration_seconds=sum(item.duration_seconds for item in segments),
        total_distance_meters=sum(item.distance_meters for item in segments),
        geometry=RouteGeometry(coordinates=route_coordinates),
        segments=segments,
    )


async def _otp_segment(
    *,
    origin: Coordinates,
    destination: Coordinates,
    departure_at: datetime,
    settings: Settings,
    sequence: int,
) -> DetailedRouteSegment:
    query = """
    {
      planConnection(
        origin: { location: { coordinate: { latitude: ORIGIN_LAT, longitude: ORIGIN_LON } } }
        destination: {
          location: { coordinate: { latitude: DESTINATION_LAT, longitude: DESTINATION_LON } }
        }
        dateTime: { earliestDeparture: "DEPARTURE_AT" }
        modes: {
          direct: [WALK]
          transit: { transit: [{ mode: BUS }, { mode: RAIL }, { mode: TRAM }] }
        }
      ) {
        edges { node { start end legs { mode distance duration legGeometry { points } } } }
      }
    }
    """
    query = (
        query.replace("ORIGIN_LAT", str(origin.latitude))
        .replace("ORIGIN_LON", str(origin.longitude))
        .replace("DESTINATION_LAT", str(destination.latitude))
        .replace("DESTINATION_LON", str(destination.longitude))
        .replace("DEPARTURE_AT", departure_at.isoformat())
    )
    try:
        async with httpx.AsyncClient(timeout=settings.routing_timeout_seconds) as client:
            response = await client.post(settings.otp_url or "", json={"query": query})
            response.raise_for_status()
            body: dict[str, Any] = response.json()
        if body.get("errors"):
            raise ValueError("OTP returned GraphQL errors")
        nodes = body["data"]["planConnection"]["edges"]
        if not nodes:
            raise ValueError("OTP returned no itinerary")
        legs = nodes[0]["node"]["legs"]
        coordinates: list[tuple[float, float]] = []
        duration_seconds = 0
        distance_meters = 0
        for leg in legs:
            leg_coordinates = decode_polyline(leg["legGeometry"]["points"], precision=5)
            _merge_coordinates(coordinates, leg_coordinates)
            duration_seconds += round(float(leg["duration"]))
            distance_meters += round(float(leg["distance"]))
        if len(coordinates) < 2:
            raise ValueError("OTP itinerary has no geometry")
        return DetailedRouteSegment(
            sequence=sequence,
            duration_seconds=duration_seconds,
            distance_meters=distance_meters,
            geometry=RouteGeometry(coordinates=coordinates),
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise _provider_error(Transport.PUBLIC_TRANSPORT) from exc


async def build_detailed_route(
    *,
    engineer_id: str,
    transport: Transport,
    points: list[Coordinates],
    departure_times: list[datetime],
    segment_transports: list[Transport] | None = None,
    settings: Settings,
) -> DetailedEngineerRoute:
    if len(points) < 2 or len(departure_times) != len(points) - 1:
        raise ValueError("route points and departure times do not match")
    effective_segment_transports = segment_transports or [transport] * (len(points) - 1)
    if len(effective_segment_transports) != len(points) - 1:
        raise ValueError("segment transports and route points do not match")
    try:
        async with asyncio.timeout(settings.routing_route_timeout_seconds):
            if transport is not Transport.PUBLIC_TRANSPORT:
                return await _valhalla_route(
                    engineer_id=engineer_id,
                    transport=transport,
                    points=points,
                    departure_times=departure_times,
                    segment_transports=effective_segment_transports,
                    settings=settings,
                )
            if not settings.otp_url:
                raise _provider_error(transport, unavailable=True)
            segments = [
                await _otp_segment(
                    origin=points[index],
                    destination=points[index + 1],
                    departure_at=departure_times[index],
                    settings=settings,
                    sequence=index + 1,
                )
                for index in range(len(points) - 1)
            ]
            coordinates: list[tuple[float, float]] = []
            for segment in segments:
                _merge_coordinates(coordinates, segment.geometry.coordinates)
            return DetailedEngineerRoute(
                engineer_id=engineer_id,
                transport_type=transport,
                routing_method="open_trip_planner_transit_v1",
                routing_quality=RoutingQuality.EXACT,
                total_duration_seconds=sum(item.duration_seconds for item in segments),
                total_distance_meters=sum(item.distance_meters for item in segments),
                geometry=RouteGeometry(coordinates=coordinates),
                segments=segments,
            )
    except TimeoutError as exc:
        logger.warning(
            "Detailed route deadline exceeded: transport=%s segments=%s timeout=%s",
            transport.value,
            len(effective_segment_transports),
            settings.routing_route_timeout_seconds,
        )
        raise DomainError(
            "routing_provider_timeout",
            "Точный маршрут не получен за отведённое время. Повторите запрос.",
            status_code=504,
            details={
                "transport_type": transport.value,
                "timeout_seconds": settings.routing_route_timeout_seconds,
            },
        ) from exc
