from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.domain import Coordinates, RoutingQuality, Transport


class RouteGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]] = Field(min_length=2)


class DetailedRouteSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    duration_seconds: int = Field(ge=0)
    distance_meters: int = Field(ge=0)
    geometry: RouteGeometry


class DetailedEngineerRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engineer_id: str
    transport_type: Transport
    routing_method: str
    routing_quality: RoutingQuality
    total_duration_seconds: int = Field(ge=0)
    total_distance_meters: int = Field(ge=0)
    geometry: RouteGeometry
    segments: list[DetailedRouteSegment] = Field(min_length=1)


class GeocodeCandidate(BaseModel):
    """Один найденный адрес: подпись, район для списка заявок и координата."""

    model_config = ConfigDict(extra="forbid")

    address: str
    district: str
    coordinates: Coordinates


class GeocodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    provider: str
    attribution: str
    items: list[GeocodeCandidate]
