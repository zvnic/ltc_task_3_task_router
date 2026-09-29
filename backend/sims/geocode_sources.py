"""Координаты адресов исходных выгрузок по OpenStreetMap — разовый прогон.

Запуск внутри контейнера backend_tools (каталог /data смонтирован только на чтение,
поэтому результат пишется в /artifacts и переносится в data/fixtures вручную):
    python -m sims.geocode_sources --out /artifacts/geocoded_addresses.json

Для каждого уникального адреса трёх зон и адреса офиса зоны скрипт строит запросы
от точного к грубому (``app.imports.address_query``): дом с корпусом, дом без корпуса,
улица. Поиск ограничен рамкой вокруг центра района из ``service_zones.json``, чтобы
одноимённая улица в другом городе не подменила нужную. Ответ принимается, только если
номер дома совпал (для точности «дом») и точка не дальше предела от центра района.
Не нашлось ничего — адрес попадает в ``not_found`` и при загрузке остаётся на
демо-точке района с пометкой ``synthetic``.

Nominatim разрешает разовое геокодирование небольших наборов при темпе не больше
запроса в секунду; результат хранится в репозитории, повторный запуск досчитывает
только недостающие адреса.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.imports.address_query import AddressQuery, house_base_key, house_key, parse_address
from app.imports.csv_reader import normalize_building_address
from app.planning.routing import haversine_meters
from app.routing.geocoding import GEOCODER_ATTRIBUTION, GEOCODER_PROVIDER, search_nominatim
from app.schemas import Coordinates
from app.services.datasets import FIXTURE_ROOT, SOURCE_ROOT, service_zone_configs

# Рамка поиска вокруг центра района, градусы: ~13 км по широте, ~12 км по долготе.
VIEWBOX_HALF_LATITUDE = 0.12
VIEWBOX_HALF_LONGITUDE = 0.2
# Дальше этого от центра района ответ считается чужим адресом.
MAX_KM_FROM_CENTER_MOSCOW = 8.0
MAX_KM_FROM_CENTER_OBLAST = 15.0


def _viewbox(center: Coordinates) -> str:
    return ",".join(
        f"{value:.4f}"
        for value in (
            center.longitude - VIEWBOX_HALF_LONGITUDE,
            center.latitude + VIEWBOX_HALF_LATITUDE,
            center.longitude + VIEWBOX_HALF_LONGITUDE,
            center.latitude - VIEWBOX_HALF_LATITUDE,
        )
    )


def _accept(
    candidate: dict[str, Any],
    *,
    precision: str,
    query: AddressQuery,
    center: Coordinates,
    max_km: float,
) -> dict[str, Any] | None:
    try:
        point = Coordinates(latitude=float(candidate["lat"]), longitude=float(candidate["lon"]))
    except (KeyError, TypeError, ValueError):
        return None
    km = haversine_meters(center, point) / 1000
    if km > max_km:
        return None
    address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
    house_number = str(address.get("house_number") or "")
    if precision == "house":
        if not house_number or house_key(house_number) != house_key(query.house or ""):
            return None
    elif precision == "house_base":
        if not house_number or house_base_key(house_number) != house_base_key(
            query.house_base or ""
        ):
            return None
    elif not address.get("road") and candidate.get("category") != "highway":
        return None
    return {
        "latitude": round(point.latitude, 6),
        "longitude": round(point.longitude, 6),
        "precision": precision,
        "osm_display_name": str(candidate.get("display_name") or ""),
        "osm_ref": f"{candidate.get('osm_type', '')}/{candidate.get('osm_id', '')}",
        "km_from_district_center": round(km, 2),
    }


async def _geocode_one(
    client: httpx.AsyncClient,
    *,
    address: str,
    district: str,
    center: Coordinates,
    max_km: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    settings = get_settings()
    query = parse_address(address, district)
    tried: list[str] = []
    for precision, text in query.variants():
        tried.append(text)
        candidates = await search_nominatim(
            client=client,
            query=text,
            settings=settings,
            viewbox=_viewbox(center),
            bounded=True,
        )
        for candidate in candidates:
            accepted = _accept(
                candidate, precision=precision, query=query, center=center, max_km=max_km
            )
            if accepted is not None:
                return {**accepted, "query": text}, tried
    return None, tried


def _source_addresses() -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for zone in service_zone_configs():
        text = (SOURCE_ROOT / zone["synthetic_file"]).read_text(encoding="utf-8-sig")
        for row in csv.DictReader(io.StringIO(text), delimiter=";"):
            address = (row.get("Адрес") or "").strip()
            district = (row.get("Район") or "").strip()
            if not address or not district:
                continue
            key = normalize_building_address(address)
            items.setdefault(
                key,
                {"key": key, "address_raw": address, "zone": zone["code"], "district": district},
            )
    return list(items.values())


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="не больше N новых адресов")
    parser.add_argument(
        "--retry-coarse",
        action="store_true",
        help="заново искать адреса и офисы, найденные не до дома, и ненайденные",
    )
    args = parser.parse_args()

    existing_path = args.out if args.out.exists() else FIXTURE_ROOT / "geocoded_addresses.json"
    result: dict[str, Any] = (
        json.loads(existing_path.read_text(encoding="utf-8"))
        if existing_path.exists()
        else {}
    )
    result.update(
        {
            "schema_version": 1,
            "source": GEOCODER_PROVIDER,
            "attribution": f"{GEOCODER_ATTRIBUTION}, лицензия ODbL",
            "geocoded_on": result.get("geocoded_on") or date.today().isoformat(),
            "method": (
                "Адрес выгрузки приведён к записи OpenStreetMap (app.imports.address_query); "
                "поиск в рамке вокруг центра района; принят первый ответ с совпавшим номером "
                "дома (precision=house — с корпусом или строением, house_base — без них) или "
                "улица (precision=street), не дальше 8 км от центра района в Москве и 15 км "
                "в городах области."
            ),
        }
    )
    addresses: dict[str, Any] = result.setdefault("addresses", {})
    offices: dict[str, Any] = result.setdefault("offices", {})
    not_found: dict[str, Any] = {item["key"]: item for item in result.get("not_found", [])}
    if args.retry_coarse:
        for key in [key for key, item in addresses.items() if item["precision"] != "house"]:
            del addresses[key]
        for code in [code for code, item in offices.items() if item["precision"] != "house"]:
            del offices[code]
        not_found.clear()

    zones = {zone["code"]: zone for zone in service_zone_configs()}
    pending = [
        item
        for item in _source_addresses()
        if item["key"] not in addresses and item["key"] not in not_found
    ]
    if args.limit:
        pending = pending[: args.limit]
    async with httpx.AsyncClient(timeout=get_settings().geocoder_timeout_seconds) as client:
        for code, zone in zones.items():
            if code in offices:
                continue
            office_center = Coordinates(**zone["office"])
            found, tried = await _geocode_one(
                client,
                address=zone["office_address"],
                district="",
                center=office_center,
                max_km=MAX_KM_FROM_CENTER_MOSCOW,
            )
            if found is not None:
                offices[code] = {"address_raw": zone["office_address"], **found}
            print(f"office {code}: {found['precision'] if found else 'NOT FOUND'} {tried[-1]}")
        for index, item in enumerate(pending, start=1):
            zone = zones[item["zone"]]
            center_raw = zone["district_centers"].get(item["district"])
            if center_raw is None:
                not_found[item["key"]] = {**item, "reason": "district_without_center"}
                continue
            oblast = item["district"] in ("Домодедово", "Кашира", "Ступино")
            found, tried = await _geocode_one(
                client,
                address=item["address_raw"],
                district=item["district"],
                center=Coordinates(**center_raw),
                max_km=MAX_KM_FROM_CENTER_OBLAST if oblast else MAX_KM_FROM_CENTER_MOSCOW,
            )
            if found is None:
                not_found[item["key"]] = {**item, "reason": "not_found", "tried": tried}
            else:
                addresses[item["key"]] = {**item, **found}
            status = found["precision"] if found else "NOT FOUND"
            print(f"{index}/{len(pending)} {status}: {item['address_raw']}")
            if index % 10 == 0:
                _write(args.out, result, not_found)
    _write(args.out, result, not_found)
    precisions: dict[str, int] = {}
    for item in addresses.values():
        precisions[item["precision"]] = precisions.get(item["precision"], 0) + 1
    print(f"addresses: {len(addresses)} {precisions}; not found: {len(not_found)}")


def _write(path: Path, result: dict[str, Any], not_found: dict[str, Any]) -> None:
    # Сортированная копия: словарь addresses в result пополняется дальше по ссылке.
    payload = {
        **result,
        "addresses": dict(sorted(result["addresses"].items())),
        "not_found": sorted(not_found.values(), key=lambda item: item["key"]),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
