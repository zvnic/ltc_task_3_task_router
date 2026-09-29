"""Матрица расстояний по дорогам OpenStreetMap между точками зон — разовый прогон.

Запуск внутри контейнера backend_tools (каталог /data смонтирован только на чтение,
поэтому результат пишется в /artifacts и переносится в data/fixtures вручную):
    python -m sims.road_matrix --out /artifacts/road_distances.json

Точки зоны — офис и адреса заявок из исходной выгрузки ровно с теми координатами,
которые получит набор при загрузке (`app.services.datasets._coordinate_for`): найденные
в OpenStreetMap или демо-точки районов. Для каждой зоны и профиля (машина, пешком,
велосипед) — один запрос table к OSRM на сервере FOSSGIS: не чаще раза в секунду,
как требуют правила сервера.

Справочник только дополняется. Уже посчитанные пары не пересчитываются: по ним
валидатор перепроверяет обещанные плечи опубликованных планов. Новые точки
добавляются в конец списка зоны, для них запрашивается полная матрица, а значения
прежних пар берутся из старого файла.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import statistics
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.planning.road_matrix import ROAD_MATRIX_PATH
from app.planning.routing import haversine_meters
from app.schemas import Coordinates
from app.services.datasets import (
    FIXTURE_ROOT,
    SOURCE_ROOT,
    _coordinate_for,
    _load_json,
    _office_start_location,
    get_service_zone,
    service_zone_configs,
)

# (сервер OSRM, профиль в пути запроса) — те же, что у детального маршрута на карте.
OSRM_PROFILES: dict[str, tuple[str, str]] = {
    "car": ("routed-car", "driving"),
    "walking": ("routed-foot", "walking"),
    "bicycle": ("routed-bike", "cycling"),
}
MIN_INTERVAL_SECONDS = 1.1


def zone_points(code: str) -> list[list[float]]:
    """Офис и уникальные точки заявок зоны в порядке выгрузки."""
    zone = get_service_zone(code)
    offset = float(_load_json(FIXTURE_ROOT / "service_zones.json")["stable_offset_degrees"])
    office = _office_start_location(zone)
    points = [[office["latitude"], office["longitude"]]]
    seen = {tuple(points[0])}
    text = (SOURCE_ROOT / zone["synthetic_file"]).read_text(encoding="utf-8-sig")
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        address = (row.get("Адрес") or "").strip()
        district = (row.get("Район") or "").strip()
        if not address or district not in zone["district_centers"]:
            continue
        coordinates, _source = _coordinate_for(address, district, zone, offset)
        point = (coordinates["latitude"], coordinates["longitude"])
        if point not in seen:
            seen.add(point)
            points.append([point[0], point[1]])
    return points


async def osrm_table(
    client: httpx.AsyncClient, base_url: str, profile: str, points: list[list[float]]
) -> list[list[int | None]]:
    server, mode = OSRM_PROFILES[profile]
    locations = ";".join(f"{longitude:.6f},{latitude:.6f}" for latitude, longitude in points)
    response = await client.get(
        f"{base_url}/{server}/table/v1/{mode}/{locations}",
        params={"annotations": "distance"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "Ok":
        raise SystemExit(f"OSRM {profile}: {payload.get('code')} {payload.get('message')}")
    rows = payload["distances"]
    if len(rows) != len(points) or any(len(row) != len(points) for row in rows):
        raise SystemExit(f"OSRM {profile}: матрица не {len(points)}×{len(points)}")
    return [[None if value is None else round(value) for value in row] for row in rows]


def detour_ratios(points: list[list[float]], matrix: list[list[int | None]]) -> list[float]:
    """Отношение «по дороге / по прямой» для пар дальше километра."""
    ratios = []
    for row, origin in enumerate(points):
        for column, destination in enumerate(points):
            value = matrix[row][column]
            if row == column or value is None:
                continue
            straight = haversine_meters(
                Coordinates(latitude=origin[0], longitude=origin[1]),
                Coordinates(latitude=destination[0], longitude=destination[1]),
            )
            if straight >= 1_000:
                ratios.append(value / straight)
    return ratios


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    settings = get_settings()
    base_url = (settings.osrm_fallback_url or "https://routing.openstreetmap.de").rstrip("/")
    existing_path = args.out if args.out.exists() else ROAD_MATRIX_PATH
    previous: dict[str, Any] = (
        json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else {}
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "source": f"OSRM {base_url} (FOSSGIS)",
        "attribution": "© участники OpenStreetMap, лицензия ODbL",
        "built_on": previous.get("built_on") or date.today().isoformat(),
        "method": (
            "Матрица table/v1 с annotations=distance по профилям car (routed-car), foot "
            "(routed-foot) и bike (routed-bike). Метры по дорогам между точками, притянутыми "
            "к сети; в расчёте плеча берётся не меньше прямой."
        ),
        "zones": {},
    }
    stats: dict[str, Any] = {}
    last_request = 0.0
    headers = {"User-Agent": settings.geocoder_user_agent}
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        for zone in service_zone_configs():
            code = zone["code"]
            old = previous.get("zones", {}).get(code, {})
            old_points: list[list[float]] = old.get("points", [])
            fresh = zone_points(code)
            old_set = {tuple(point) for point in old_points}
            points = [*old_points, *[point for point in fresh if tuple(point) not in old_set]]
            item: dict[str, Any] = {"points": points}
            for profile in OSRM_PROFILES:
                old_matrix = old.get(profile)
                if old_matrix is not None and len(old_points) == len(points):
                    item[profile] = old_matrix
                    continue
                wait = MIN_INTERVAL_SECONDS - (time.monotonic() - last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                matrix = await osrm_table(client, base_url, profile, points)
                last_request = time.monotonic()
                if old_matrix is not None:
                    # прежние пары не меняются: по ним уже обещаны плечи
                    for row in range(len(old_points)):
                        matrix[row][: len(old_points)] = old_matrix[row]
                item[profile] = matrix
                print(f"{code} {profile}: {len(points)}×{len(points)}", flush=True)
            result["zones"][code] = item
            stats[code] = {
                "points": len(points),
                **{
                    profile: {
                        "unreachable_pairs": sum(
                            value is None for row in item[profile] for value in row
                        ),
                        "median_detour_ratio": round(
                            statistics.median(detour_ratios(points, item[profile]) or [0.0]), 3
                        ),
                    }
                    for profile in OSRM_PROFILES
                },
            }
    args.out.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
