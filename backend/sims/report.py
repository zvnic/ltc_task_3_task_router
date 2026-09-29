"""Сводка замеров модели: собирает JSON прогонов в таблицы «до / после».

Запуск внутри контейнера backend_tools:
    python -m sims.report > /artifacts/model_upgrade_tables.md

Скрипт ничего не считает заново и не ходит в БД: он читает готовые JSON прогонов из
/artifacts и печатает Markdown. Нужен, чтобы числа в отчёте совпадали с замерами, а не
переписывались руками. Отсутствующий файл не роняет прогон — секция помечается пропуском.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# файл прогона -> под каким заголовком колонка попадёт в таблицу
COMPARE_FILES = (("compare_v2.json", "до"), ("after_compare_v2.json", "после"))
FRAGILITY_FILES = (
    ("sim_vostok.json", "до"),
    ("after_fragility_buf0.json", "после, буфер 0"),
    ("after_fragility_buf10.json", "после, буфер 10"),
)
ALGOS_FILES = (
    ("sim_vostok.json", "до"),
    ("after_algos_lex.json", "после, lexicographic"),
    ("after_algos_cost.json", "после, cost"),
)

KPI_COLUMNS = (
    ("assigned", "заявок"),
    ("distance_km", "пробег, км"),
    ("travel_minutes", "в пути, мин"),
    ("used_engineers", "бригад"),
    ("makespan_minutes", "день, мин"),
    ("total_wait_minutes", "ожидание, мин"),
)
FRAGILITY_COLUMNS = (
    ("window_missed", "срыв окон"),
    ("routes_broken", "маршрутов сломано"),
    ("beyond_shift", "за смену"),
    ("delay_p90_min", "опоздание p90, мин"),
)
SCENARIO_COLUMNS = (
    ("срочных_назначено", "срочных назначено"),
    ("срочных_не_назначено", "срочных отказано"),
    ("обычных_назначено", "обычных назначено"),
    ("пробег_км", "пробег, км"),
    ("длина_дня_мин", "день, мин"),
    ("бригад_занято", "бригад"),
)


def _load(directory: Path, name: str) -> dict[str, Any] | None:
    path = directory / name
    if not path.is_file() or not path.stat().st_size:
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _missing(names: list[str]) -> str:
    return f"_Нет файлов прогона: {', '.join(names)}._"


def section_compare(directory: Path) -> str:
    """Эвристика вставки против OR-Tools по трём зонам: до и после правок."""
    loaded = [(title, _load(directory, name)) for name, title in COMPARE_FILES]
    present = [(title, data) for title, data in loaded if data is not None]
    if not present:
        return _missing([name for name, _ in COMPARE_FILES])

    zones = sorted({zone for _, data in present for zone in data})
    blocks: list[str] = []
    for zone in zones:
        variants = sorted(
            {name for _, data in present for name in data.get(zone, {}) if isinstance(
                data[zone].get(name), dict
            )}
        )
        header = ["вариант", "прогон", *(title for _, title in KPI_COLUMNS), "срыв окон, +25 %"]
        rows: list[list[str]] = []
        for variant in variants:
            for title, data in present:
                values = data.get(zone, {}).get(variant)
                if not isinstance(values, dict):
                    continue
                rows.append(
                    [
                        variant,
                        title,
                        *(_cell(values.get(key)) for key, _ in KPI_COLUMNS),
                        _cell(values.get("срыв_окон_при_дороге_+25%")),
                    ]
                )
        blocks.append(f"**{zone}**\n\n{_table(header, rows)}")
    return "\n\n".join(blocks)


def section_fragility(directory: Path) -> str:
    """Устойчивость опорного плана к замедлению: буфер 0 против буфера 10 минут."""
    loaded = [(title, _load(directory, name)) for name, title in FRAGILITY_FILES]
    present = [(title, data) for title, data in loaded if data is not None]
    if not present:
        return _missing([name for name, _ in FRAGILITY_FILES])

    labels: list[str] = []
    for _, data in present:
        for label in data.get("устойчивость", {}):
            if label not in labels:
                labels.append(label)

    header = ["сценарий", "прогон", *(title for _, title in FRAGILITY_COLUMNS)]
    rows: list[list[str]] = []
    for label in labels:
        for title, data in present:
            values = data.get("устойчивость", {}).get(label)
            if not isinstance(values, dict):
                continue
            rows.append(
                [label, title, *(_cell(values.get(key)) for key, _ in FRAGILITY_COLUMNS)]
            )

    plans_header = ["прогон", "алгоритм", *(title for _, title in KPI_COLUMNS)]
    plans: list[list[str]] = []
    for title, data in present:
        plan = data.get("опорный_план")
        if not isinstance(plan, dict):
            continue
        plans.append(
            [
                title,
                _cell(plan.get("алгоритм")),
                *(_cell(plan.get(key)) for key, _ in KPI_COLUMNS),
            ]
        )
    plans_table = _table(plans_header, plans)
    return f"{_table(header, rows)}\n\nОпорный план каждого прогона:\n\n{plans_table}"


def section_algos(directory: Path) -> str:
    """Режим целевой функции: lexicographic против cost на одних и тех же данных."""
    loaded = [(title, _load(directory, name)) for name, title in ALGOS_FILES]
    present = [(title, data) for title, data in loaded if data is not None]
    if not present:
        return _missing([name for name, _ in ALGOS_FILES])

    algorithms: list[str] = []
    for _, data in present:
        for algorithm in data.get("алгоритмы", {}):
            if algorithm not in algorithms:
                algorithms.append(algorithm)

    header = ["алгоритм", "прогон", *(title for _, title in KPI_COLUMNS), "валидатор"]
    rows: list[list[str]] = []
    for algorithm in algorithms:
        for title, data in present:
            values = data.get("алгоритмы", {}).get(algorithm)
            if not isinstance(values, dict):
                continue
            rows.append(
                [
                    algorithm,
                    title,
                    *(_cell(values.get(key)) for key, _ in KPI_COLUMNS),
                    _cell(values.get("валидатор")),
                ]
            )
    return _table(header, rows)


def section_scenarios(directory: Path) -> str:
    """Стресс по срочности: доля срочных заявок с узким окном."""
    data = _load(directory, "after_scenarios.json")
    if data is None:
        return _missing(["after_scenarios.json"])

    header = ["сценарий", "алгоритм", *(title for _, title in SCENARIO_COLUMNS), "валидатор"]
    rows: list[list[str]] = []
    for title, scenario in data.get("сценарии", {}).items():
        window = scenario.get("окно_срочных_мин", {})
        label = f"{title} (окно {window.get('мин')}–{window.get('макс')} мин)"
        for algorithm, values in scenario.get("алгоритмы", {}).items():
            rows.append(
                [
                    label,
                    algorithm,
                    *(_cell(values.get(key)) for key, _ in SCENARIO_COLUMNS),
                    _cell(values.get("валидатор")),
                ]
            )
    return _table(header, rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Сводные таблицы замеров модели")
    parser.add_argument("--dir", default="/artifacts", help="папка с JSON прогонов")
    args = parser.parse_args()
    directory = Path(args.dir)

    sections = (
        ("Вставка против OR-Tools по зонам", section_compare(directory)),
        ("Устойчивость к замедлению, Восток", section_fragility(directory)),
        ("Режим целевой функции, Восток", section_algos(directory)),
        ("Стресс по срочности, Восток", section_scenarios(directory)),
    )
    print("# Сводные таблицы замеров\n")
    for title, body in sections:
        print(f"## {title}\n\n{body}\n")


if __name__ == "__main__":
    main()
