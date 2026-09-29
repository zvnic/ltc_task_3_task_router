from dataclasses import dataclass
from typing import Any

from app.planning.baseline import build_baseline
from app.planning.contracts import RouteSolver, SolverConfig
from app.planning.insertion import build_insertion
from app.planning.objective import plan_objective_key
from app.planning.optimizer import build_optimized
from app.planning.validation import validate_plan
from app.schemas import PlanningInput, PlanningResult


@dataclass(frozen=True)
class BaselineSolver:
    id: str = "baseline_v1"
    title: str = "Последовательный baseline"
    version: str = "1"
    description: str = "Входной порядок, первый допустимый инженер, добавление только в конец."
    formula: str = "j* = min { j | feasible(i, j) }; Rj ← Rj ⊕ i"
    approach: str = (
        "Заявки идут в исходном порядке. Для каждой выбирается первый инженер, "
        "которому допустимо добавить визит только в конец маршрута."
    )
    advantages: tuple[str, ...] = (
        "Детерминированный и легко проверяемый результат",
        "Быстрый эталон для сравнения оптимизаторов",
    )
    disadvantages: tuple[str, ...] = (
        "Не переставляет ранее добавленные визиты",
        "Сильно зависит от входного порядка и обычно даёт лишний пробег",
    )

    def solve(
        self,
        planning_input: PlanningInput,
        _config: SolverConfig,
    ) -> PlanningResult:
        return build_baseline(planning_input)


@dataclass(frozen=True)
class OrToolsGlsSolver:
    id: str = "ortools_gls_v1"
    title: str = "OR-Tools Guided Local Search"
    version: str = "1"
    description: str = "VRPTW с ограничениями навыка, транспорта, окон и смен."
    formula: str = "min lex(Uаварии, Uподключения, Uобычные, Kинженеров, T, D)"
    approach: str = (
        "OR-Tools строит VRPTW-модель, начинает с параллельной дешёвой вставки "
        "и улучшает решение Guided Local Search в пределах лимита времени. Лимит "
        "делится на два прохода — с ценой дуги по минутам в пути и по метрам; "
        "в план идёт лучший по ключу цели."
    )
    advantages: tuple[str, ...] = (
        "Совместно оптимизирует назначение, порядок визитов и маршруты",
        "Учитывает жёсткие окна, смены, навыки, транспорт и оснащение",
    )
    disadvantages: tuple[str, ...] = (
        "Поиск ограничен временем и не доказывает глобальный оптимум",
        "Результат сложнее объяснить без сравнения метрик и ограничений",
    )

    def solve(
        self,
        planning_input: PlanningInput,
        config: SolverConfig,
    ) -> PlanningResult:
        result = build_optimized(
            planning_input,
            time_limit_seconds=config.time_limit_seconds,
        )
        if result.algorithm == "ortools":
            result.algorithm = self.id
        return result


@dataclass(frozen=True)
class InsertionLocalSearchSolver:
    id: str = "insertion_ls_v1"
    title: str = "Вставка и локальные переносы"
    version: str = "2"
    description: str = (
        "Сначала сложные заявки, вставка по приросту времени в пути, затем переносы отрезков, "
        "обмены визитами и развороты; отказы пробуются заново после каждого улучшения."
    )
    formula: str = (
        "p* = arg minp (ΔT, ΔD)(i, p); затем min lex(Uаварии, Uподключения, Uобычные, K, T, D)"
    )
    approach: str = (
        "Заявки сортируются по классу работ и дефициту подходящих инженеров и вставляются "
        "туда, где прирост времени в пути наименьший. Дальше соседство: перенос отрезка, обмен "
        "визитами, разворот отрезка внутри маршрута, выселение мешающего визита ради "
        "отказанной заявки и роспуск самой лёгкой бригады."
    )
    advantages: tuple[str, ...] = (
        "Понятная и воспроизводимая эвристика",
        "Обычно заметно лучше последовательного baseline при малом времени расчёта",
        "Возвращается к отказанным заявкам после каждого принятого улучшения",
    )
    disadvantages: tuple[str, ...] = (
        "Локальные переносы могут застрять в локальном минимуме",
        "Исследует меньше вариантов, чем Guided Local Search",
        "Стоимость хода растёт с длиной маршрутов: на сотнях заявок нужен инкрементальный пересчёт",
    )

    def solve(
        self,
        planning_input: PlanningInput,
        config: SolverConfig,
    ) -> PlanningResult:
        return build_insertion(
            planning_input,
            time_limit_seconds=config.time_limit_seconds,
        )


SOLVERS: dict[str, RouteSolver] = {
    solver.id: solver
    for solver in (
        BaselineSolver(),
        OrToolsGlsSolver(),
        InsertionLocalSearchSolver(),
    )
}


def algorithm_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": solver.id,
            "title": solver.title,
            "version": solver.version,
            "description": solver.description,
            "formula": solver.formula,
            "approach": solver.approach,
            "advantages": list(solver.advantages),
            "disadvantages": list(solver.disadvantages),
        }
        for solver in SOLVERS.values()
    ]


def run_solvers(
    planning_input: PlanningInput,
    algorithm_ids: list[str],
    time_limit_seconds: int,
) -> dict[str, PlanningResult]:
    unknown = sorted(set(algorithm_ids) - set(SOLVERS))
    if unknown:
        raise ValueError(f"unknown algorithms: {', '.join(unknown)}")
    config = SolverConfig(time_limit_seconds=time_limit_seconds)
    results: dict[str, PlanningResult] = {}
    for algorithm_id in algorithm_ids:
        result = SOLVERS[algorithm_id].solve(planning_input, config)
        violations = validate_plan(planning_input, result)
        if violations:
            raise ValueError(f"{algorithm_id} validator rejected result: {violations}")
        results[algorithm_id] = result
    return results


def select_best(results: dict[str, PlanningResult]) -> tuple[str, PlanningResult]:
    if not results:
        raise ValueError("at least one solver result is required")
    return min(
        results.items(),
        key=lambda item: (plan_objective_key(item[1].metrics), list(SOLVERS).index(item[0])),
    )
