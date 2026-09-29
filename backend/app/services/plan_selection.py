"""Shared rules for the active/published plan shown in UI and API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

# Working plans operators should see after run/replan. Pair baselines such as
# "baseline" and "replan_baseline" stay available for diff/compare only.
PUBLISHED_PLAN_KINDS: frozenset[str] = frozenset(
    {
        "optimized",
        "baseline_fallback",
        "replan_optimized",
        "replan_baseline_fallback",
    }
)

# Higher rank wins when created_at ties (e.g. pair insert in one transaction).
PUBLISHED_KIND_RANK: dict[str, int] = {
    "replan_optimized": 4,
    "replan_baseline_fallback": 3,
    "optimized": 2,
    "baseline_fallback": 1,
}

def is_published_plan_kind(kind: str | None) -> bool:
    return bool(kind) and kind in PUBLISHED_PLAN_KINDS


def _created_at_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        # Support both Z and offset ISO strings from API/JSON dumps.
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return datetime.min
    return datetime.min


def select_latest_published_plan[T: Mapping[str, Any]](plans: Sequence[T]) -> T | None:
    """Pick latest published plan by created_at desc, then kind rank, then id."""
    published = [plan for plan in plans if is_published_plan_kind(str(plan.get("kind") or ""))]
    if not published:
        return None

    def sort_key(plan: Mapping[str, Any]) -> tuple[datetime, int, str]:
        kind = str(plan.get("kind") or "")
        return (
            _created_at_value(plan.get("created_at")),
            PUBLISHED_KIND_RANK.get(kind, 0),
            str(plan.get("id") or ""),
        )

    return max(published, key=sort_key)
