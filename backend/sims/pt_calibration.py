"""Калибровка общественного транспорта: модель дороги против Яндекс Карт.

Запуск внутри контейнера backend_tools:
    python -m sims.pt_calibration
    python -m sims.pt_calibration --legs   # ещё и каждое плечо

Скрипт ничего не пишет и не ходит ни в БД, ни в сеть. Он читает замер Яндекс Карт
(`pt_yandex_2026_09_29.csv` рядом: 128 плеч, вторник 29.09.2026, режим «общественный
транспорт», время отправления задано) и пересчитывает каждое плечо той же функцией,
что и решатель (`effective_edge_metrics` для бригады на общественном транспорте), по
текущей модели и по v4. Печатает ошибку «модель − Яндекс» по полосам расстояния.

Колонки файла: откуда и куда, время отправления, лучший вариант Яндекса (минимум по
предложенным), все варианты, расстояние по прямой. Колонки v4_min и v5_min — расчёт на
момент калибровки; скрипт сверяет с ними пересчёт, чтобы молча не разойтись с кодом.
Источники плеч (source): plan — плечи бригад на общественном транспорте из планов трёх
зон по v4, extra — добор до полос расстояния и за МКАД, near_mkad — города у МКАД от
офисов, walk_boundary — точки в 0,6–1,3 км от заявки (пар заявок на таком расстоянии
в зонах нет).
"""

from __future__ import annotations

import argparse
import csv
import statistics
from dataclasses import dataclass
from pathlib import Path

from app.planning.routing import (
    PREVIOUS_ROUTE_ESTIMATION_METHOD,
    ROUTE_ESTIMATION_METHOD,
    SUBURBAN_BOARDING_MIN_KM,
    TRANSPORT_PARAMETERS,
    effective_edge_metrics,
    haversine_meters,
    share_inside_mkad,
)
from app.schemas import Coordinates, Transport

LEGS_FILE = Path(__file__).with_name("pt_yandex_2026_09_29.csv")
# полосы по расстоянию по прямой, м; «за МКАД» — плечи, где за МКАД больше 0,5 км пути
CITY_BANDS = (
    ("до 0,5 км", 0, 500),
    ("0,5–1,5 км", 500, 1_500),
    ("1,5–4 км", 1_500, 4_000),
    ("4–7 км", 4_000, 7_000),
    ("7–12 км", 7_000, 12_000),
)
SUBURBAN_BANDS = (("за МКАД до 20 км", 0, 20_000), ("за МКАД 60+ км", 20_000, 10**9))


@dataclass(frozen=True)
class Leg:
    id: int
    zone: str
    source: str
    straight_meters: float
    suburban_km: float
    yandex: int
    recorded: tuple[int, int]
    v4: int
    now: int


def load_legs() -> list[Leg]:
    _, path_factor = TRANSPORT_PARAMETERS[Transport.PUBLIC_TRANSPORT]
    legs = []
    with LEGS_FILE.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            origin = Coordinates(latitude=float(row["from_lat"]), longitude=float(row["from_lon"]))
            destination = Coordinates(latitude=float(row["to_lat"]), longitude=float(row["to_lon"]))
            straight = haversine_meters(origin, destination)
            path_km = round(straight * path_factor) / 1000
            _, v4, _ = effective_edge_metrics(
                origin,
                destination,
                Transport.PUBLIC_TRANSPORT,
                method=PREVIOUS_ROUTE_ESTIMATION_METHOD,
            )
            _, now, _ = effective_edge_metrics(origin, destination, Transport.PUBLIC_TRANSPORT)
            legs.append(
                Leg(
                    id=int(row["id"]),
                    zone=row["zone"],
                    source=row["source"],
                    straight_meters=straight,
                    suburban_km=path_km * (1 - share_inside_mkad(origin, destination)),
                    yandex=int(row["yandex_best_min"]),
                    recorded=(int(row["v4_min"]), int(row["v5_min"])),
                    v4=v4,
                    now=now,
                )
            )
    return legs


def band_line(name: str, legs: list[Leg]) -> str:
    v4_errors = [leg.v4 - leg.yandex for leg in legs]
    now_errors = [leg.now - leg.yandex for leg in legs]

    def error_text(errors: list[int]) -> str:
        mae = statistics.mean(abs(error) for error in errors)
        return f"{mae:.1f} / {statistics.mean(errors):+.1f}"

    def underestimated(errors: list[int]) -> int:
        return sum(error <= -10 for error in errors)

    return (
        f"| {name} | {len(legs)} | {statistics.mean(leg.yandex for leg in legs):.1f} "
        f"| {statistics.mean(leg.v4 for leg in legs):.1f} "
        f"| {statistics.mean(leg.now for leg in legs):.1f} "
        f"| {error_text(v4_errors)} | {error_text(now_errors)} "
        f"| {underestimated(v4_errors)} → {underestimated(now_errors)} |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Модель ОТ против Яндекс Карт")
    parser.add_argument("--legs", action="store_true", help="напечатать каждое плечо")
    args = parser.parse_args()

    legs = load_legs()
    drift = [leg.id for leg in legs if leg.recorded != (leg.v4, leg.now)]
    print(f"Плеч: {len(legs)}. Модель: {ROUTE_ESTIMATION_METHOD}.")
    if drift:
        print(f"Пересчёт разошёлся с записанным при калибровке у плеч: {drift}")
    print()
    print(
        "| Полоса | Плеч | Яндекс, мин | v4 | сейчас | Ошибка v4: MAE / смещ. "
        "| Ошибка сейчас: MAE / смещ. | Занижено ≥10 мин |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    city = [leg for leg in legs if leg.suburban_km <= SUBURBAN_BOARDING_MIN_KM]
    suburban = [leg for leg in legs if leg.suburban_km > SUBURBAN_BOARDING_MIN_KM]
    for bands, group in ((CITY_BANDS, city), (SUBURBAN_BANDS, suburban)):
        for name, low, high in bands:
            band = [leg for leg in group if low <= leg.straight_meters < high]
            if band:
                print(band_line(name, band))
    print(band_line("**город**", city))
    print(band_line("**за МКАД**", suburban))
    print(band_line("**все**", legs))
    plan = [leg for leg in legs if leg.source == "plan"]
    print()
    print(
        f"Плановые плечи ({len(plan)}): Яндекс {sum(leg.yandex for leg in plan)} мин, "
        f"v4 {sum(leg.v4 for leg in plan)}, сейчас {sum(leg.now for leg in plan)}."
    )
    if args.legs:
        print()
        for leg in legs:
            print(
                f"{leg.id:>3} {leg.zone:<11} {leg.source:<13} {leg.straight_meters:>7.0f} м  "
                f"Яндекс {leg.yandex:>3}  v4 {leg.v4:>3}  сейчас {leg.now:>3}"
            )


if __name__ == "__main__":
    main()
