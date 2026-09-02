# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic pre-write checks over staged extractions.

Pure functions: no ClickHouse, no ToolContext, no LLM, no I/O. That makes every
rule here unit-testable for free (tests/unit/test_extraction_checks.py).

WHY THIS EXISTS
---------------
Three of parser_agent's five "when to ask the user" rules used to be prose in
its instruction, which meant the model had to notice them by reading:

  - RULE 1 (degenerate extraction)  ->  `len(nodes) == 0`
  - RULE 2 (missing budget cap)     ->  `total_budget_usd is None` + a sum
  - RULE 5 (shoot-day conflict)     ->  group-by-location, sum, compare

None of that needs a language model, and rule 5 in particular was silently
skipped in testing even when the conflict was unambiguous — it was the only
rule requiring a deliberate cross-document aggregation pass, which is exactly
the kind of step that gets dropped under a long instruction. Arithmetic and
joins are code's job; judging whether two location names mean the same place
(rules 3 and 4) is the model's job, and those stay in the instruction.

Every check returns Diagnostic objects carrying a ready-to-ask question, so
parser_agent's remaining job is to relay findings rather than hunt for them.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas import BudgetExtraction, ProductionConstraints, ScriptExtraction

# Diagnostic codes. Stable strings — the eval harness asserts on these, so
# renaming one is a breaking change to the tests.
EMPTY_SCRIPT_EXTRACTION = "EMPTY_SCRIPT_EXTRACTION"
EMPTY_BUDGET_EXTRACTION = "EMPTY_BUDGET_EXTRACTION"
MISSING_BUDGET_CAP = "MISSING_BUDGET_CAP"
SHOOT_DAY_CONFLICT = "SHOOT_DAY_CONFLICT"
ORPHANED_LOCATION_REF = "ORPHANED_LOCATION_REF"

# Severity semantics:
#   must_ask - parser_agent must surface this to the user before committing.
#   warning  - reported for visibility; does not by itself require a question.
MUST_ASK = "must_ask"
WARNING = "warning"


class Diagnostic(BaseModel):
    """One finding from a deterministic check, with the question to ask about it."""

    code: str = Field(description="Stable diagnostic code, e.g. SHOOT_DAY_CONFLICT.")
    severity: str = Field(description="'must_ask' or 'warning'.")
    message: str = Field(description="What was found, stated plainly.")
    suggested_question: str = Field(
        description="A concrete, ready-to-ask question grounded in the actual numbers/names."
    )
    data: dict = Field(
        default_factory=dict,
        description="Structured detail (ids, numbers) so a correction can be applied later.",
    )


# ==============================================================================
# RULE 1 — degenerate extraction
# ==============================================================================


def check_script_extraction(script: Optional[ScriptExtraction]) -> list[Diagnostic]:
    """A script document that yielded no scenes at all is almost always the wrong file."""
    if script is None or script.nodes:
        return []
    return [
        Diagnostic(
            code=EMPTY_SCRIPT_EXTRACTION,
            severity=MUST_ASK,
            message="A script document was provided but no scenes could be identified in it.",
            suggested_question=(
                "I couldn't find any scenes in this document — is this the right file, or is "
                "it maybe an outline or treatment rather than the full script?"
            ),
        )
    ]


def check_budget_extraction(budget: Optional[BudgetExtraction]) -> list[Diagnostic]:
    """A budget document that yielded no costed locations is almost always the wrong file."""
    if budget is None or budget.locations:
        return []
    return [
        Diagnostic(
            code=EMPTY_BUDGET_EXTRACTION,
            severity=MUST_ASK,
            message="A budget document was provided but no locations with costs could be identified.",
            suggested_question=(
                "I couldn't find any locations with costs in this document — is this the right "
                "file?"
            ),
        )
    ]


# ==============================================================================
# RULE 2 — missing budget cap (+ the floor arithmetic)
# ==============================================================================


def compute_budget_floor(budget: Optional[BudgetExtraction]) -> float:
    """Sum daily_rate_usd x total_shoot_days (or x1 when unstated) across locations.

    Deliberately mirrors budget_agent's own per-location cost formula so the
    placeholder offered here can't disagree with what budget_agent later computes.
    """
    if budget is None:
        return 0.0
    return float(
        sum(loc.daily_rate_usd * (loc.total_shoot_days or 1) for loc in budget.locations)
    )


def check_missing_budget_cap(
    budget: Optional[BudgetExtraction],
    constraints: Optional[ProductionConstraints],
) -> list[Diagnostic]:
    """total_budget_usd is the one constraint budget_agent cannot work without.

    The other three constraint fields are independently optional and never
    trigger a question when absent — only the cap does.
    """
    if budget is None:
        return []
    if constraints is not None and constraints.total_budget_usd is not None:
        return []

    floor = compute_budget_floor(budget)
    return [
        Diagnostic(
            code=MISSING_BUDGET_CAP,
            severity=MUST_ASK,
            message=(
                "The budget document states no overall total approved budget. budget_agent "
                "cannot check for overruns without one."
            ),
            suggested_question=(
                f"This document doesn't state a total approved budget, and budget_agent needs "
                f"one to check for overruns. I can: (a) use ${floor:,.0f} as a placeholder "
                f"(based on your locations' day rates and stated shoot days — a rough floor, "
                f"not a real estimate), (b) use the exact number if you tell me, or (c) skip "
                f"budget overrun checks for this project. Which would you like?"
            ),
            data={"computed_floor_usd": floor},
        )
    ]


# ==============================================================================
# RULE 5 — shoot-day conflict between the two documents
# ==============================================================================


def check_shoot_day_conflicts(
    script: Optional[ScriptExtraction],
    budget: Optional[BudgetExtraction],
) -> list[Diagnostic]:
    """Compare each location's stated total_shoot_days against its scenes' summed shoot_days.

    Only checks a location when the comparison is actually meaningful: EVERY
    scene at that location has shoot_days set AND the location states
    total_shoot_days. A partial sum from incomplete scene data is the normal
    optional-field gap, not a conflict, and must never be asked about.
    """
    if script is None or budget is None:
        return []

    scenes_by_location: dict[str, list[Optional[int]]] = {}
    for node in script.nodes:
        scenes_by_location.setdefault(node.location_id, []).append(node.shoot_days)

    diagnostics: list[Diagnostic] = []
    for loc in budget.locations:
        if loc.total_shoot_days is None:
            continue
        scene_days = scenes_by_location.get(loc.location_id)
        if not scene_days:
            continue
        if any(d is None for d in scene_days):
            # Incomplete scene-level data - not a conflict.
            continue

        script_total = sum(d for d in scene_days if d is not None)
        if script_total == loc.total_shoot_days:
            continue

        diagnostics.append(
            Diagnostic(
                code=SHOOT_DAY_CONFLICT,
                severity=MUST_ASK,
                message=(
                    f"Script scenes at '{loc.location_name}' sum to {script_total} shoot days, "
                    f"but the budget document states {loc.total_shoot_days}."
                ),
                suggested_question=(
                    f"Your script's scenes at '{loc.location_name}' add up to {script_total} "
                    f"shoot days, but the budget document states {loc.total_shoot_days} total "
                    f"shoot days for that location. Which is correct — {script_total}, "
                    f"{loc.total_shoot_days}, or a different number?"
                ),
                data={
                    "location_id": loc.location_id,
                    "location_name": loc.location_name,
                    "script_total": script_total,
                    "budget_total": loc.total_shoot_days,
                    "scene_count": len(scene_days),
                },
            )
        )
    return diagnostics


# ==============================================================================
# Join integrity — a scene pointing at a location that has no cost row
# ==============================================================================


def check_orphaned_locations(
    script: Optional[ScriptExtraction],
    budget: Optional[BudgetExtraction],
) -> list[Diagnostic]:
    """Flag location_ids used by scenes that have no matching production_locations row.

    budget_agent prices a branch from daily_rate_usd, so a scene pointing at a
    location with no cost row silently makes that branch uncostable.
    """
    if script is None or budget is None:
        return []

    costed = {loc.location_id for loc in budget.locations}
    orphaned: dict[str, list[str]] = {}
    for node in script.nodes:
        if node.location_id not in costed:
            orphaned.setdefault(node.location_id, []).append(node.node_id)

    known = ", ".join(sorted(costed)) or "(none)"
    return [
        Diagnostic(
            code=ORPHANED_LOCATION_REF,
            severity=MUST_ASK,
            message=(
                f"Scenes {', '.join(sorted(nodes))} use location_id '{loc_id}', which has no "
                f"cost row in the budget document."
            ),
            suggested_question=(
                f"The scene(s) {', '.join(sorted(nodes))} take place at '{loc_id}', which isn't "
                f"listed in the budget document (it lists: {known}). Is this a separate location "
                f"with no cost data yet, or should it map to one of the budgeted locations?"
            ),
            data={"location_id": loc_id, "node_ids": sorted(nodes), "known_locations": sorted(costed)},
        )
        for loc_id, nodes in sorted(orphaned.items())
    ]


# ==============================================================================
# Entry point
# ==============================================================================


def run_all_checks(
    script: Optional[ScriptExtraction] = None,
    budget: Optional[BudgetExtraction] = None,
    constraints: Optional[ProductionConstraints] = None,
) -> list[Diagnostic]:
    """Run every deterministic check that applies to what's currently staged.

    Cross-document checks (shoot-day conflicts, orphaned locations) no-op unless
    both documents are present, matching the rule that there's nothing to
    cross-check on a single-document turn.
    """
    return [
        *check_script_extraction(script),
        *check_budget_extraction(budget),
        *check_missing_budget_cap(budget, constraints),
        *check_shoot_day_conflicts(script, budget),
        *check_orphaned_locations(script, budget),
    ]
