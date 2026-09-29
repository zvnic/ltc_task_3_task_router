from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.schemas import PlanningInput, PlanningResult


@dataclass(frozen=True)
class SolverConfig:
    time_limit_seconds: int
    random_seed: int | None = None


@runtime_checkable
class RouteSolver(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def formula(self) -> str: ...

    @property
    def approach(self) -> str: ...

    @property
    def advantages(self) -> tuple[str, ...]: ...

    @property
    def disadvantages(self) -> tuple[str, ...]: ...

    def solve(
        self,
        planning_input: PlanningInput,
        config: SolverConfig,
    ) -> PlanningResult: ...
