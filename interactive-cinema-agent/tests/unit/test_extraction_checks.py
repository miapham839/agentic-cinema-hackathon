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

"""Unit tests for app/checks.py — the deterministic HITL rules.

These are the rules that used to live as prose in parser_agent's instruction,
where they fired unreliably (rule 5 was silently skipped even on unambiguous
conflicts). Now that they're code, they're pinned here: fast, free, no API
calls, no ClickHouse.

Deliberately includes the negative cases, because over-asking is as much a
failure as under-asking — a partially-filled shoot_days set must NOT be
reported as a conflict.
"""

import pytest

from app.checks import (
    EMPTY_BUDGET_EXTRACTION,
    EMPTY_SCRIPT_EXTRACTION,
    MISSING_BUDGET_CAP,
    MUST_ASK,
    ORPHANED_LOCATION_REF,
    SHOOT_DAY_CONFLICT,
    check_budget_extraction,
    check_missing_budget_cap,
    check_orphaned_locations,
    check_script_extraction,
    check_shoot_day_conflicts,
    compute_budget_floor,
    run_all_checks,
)
from app.schemas import (
    BudgetExtraction,
    ProductionConstraints,
    ProductionLocation,
    ScriptEdge,
    ScriptExtraction,
    ScriptNode,
)


def node(node_id: str, location_id: str, shoot_days=None) -> ScriptNode:
    return ScriptNode(
        node_id=node_id,
        title=node_id.replace("_", " ").title(),
        location_id=location_id,
        narrative_text="...",
        shoot_days=shoot_days,
    )


def location(location_id: str, daily_rate=1000.0, total_shoot_days=None) -> ProductionLocation:
    return ProductionLocation(
        location_id=location_id,
        location_name=location_id.replace("loc_", "").replace("_", " ").title(),
        daily_rate_usd=daily_rate,
        company_move_penalty_usd=100.0,
        requires_permit=False,
        max_cast_capacity=10,
        total_shoot_days=total_shoot_days,
    )


def codes(diagnostics) -> list[str]:
    return [d.code for d in diagnostics]


# ==============================================================================
# RULE 1 — degenerate extraction
# ==============================================================================


def test_empty_script_is_flagged():
    assert codes(check_script_extraction(ScriptExtraction(nodes=[], edges=[]))) == [
        EMPTY_SCRIPT_EXTRACTION
    ]


def test_script_with_scenes_is_not_flagged():
    script = ScriptExtraction(nodes=[node("a", "loc_x")], edges=[])
    assert check_script_extraction(script) == []


def test_absent_script_document_is_not_flagged():
    """No script uploaded at all is not the same as an unreadable one."""
    assert check_script_extraction(None) == []


def test_empty_budget_is_flagged():
    assert codes(check_budget_extraction(BudgetExtraction(locations=[]))) == [
        EMPTY_BUDGET_EXTRACTION
    ]


def test_budget_with_locations_is_not_flagged():
    assert check_budget_extraction(BudgetExtraction(locations=[location("loc_x")])) == []


# ==============================================================================
# RULE 2 — missing budget cap + floor arithmetic
# ==============================================================================


def test_floor_multiplies_by_total_shoot_days_when_stated():
    budget = BudgetExtraction(
        locations=[
            location("loc_a", daily_rate=2600.0),  # no total_shoot_days -> x1
            location("loc_b", daily_rate=4100.0, total_shoot_days=2),  # -> x2
            location("loc_c", daily_rate=3400.0),  # -> x1
            location("loc_d", daily_rate=1800.0),  # -> x1
        ]
    )
    # 2600 + 8200 + 3400 + 1800
    assert compute_budget_floor(budget) == 16000.0


def test_floor_of_empty_budget_is_zero():
    assert compute_budget_floor(BudgetExtraction(locations=[])) == 0.0
    assert compute_budget_floor(None) == 0.0


def test_missing_cap_is_flagged_with_floor_in_the_question():
    budget = BudgetExtraction(locations=[location("loc_a", daily_rate=1500.0)])
    diags = check_missing_budget_cap(budget, None)
    assert codes(diags) == [MISSING_BUDGET_CAP]
    assert diags[0].severity == MUST_ASK
    assert diags[0].data["computed_floor_usd"] == 1500.0
    assert "1,500" in diags[0].suggested_question


def test_present_cap_is_not_flagged():
    budget = BudgetExtraction(locations=[location("loc_a")])
    constraints = ProductionConstraints(total_budget_usd=50000.0)
    assert check_missing_budget_cap(budget, constraints) == []


def test_other_constraints_absent_does_not_trigger_a_question():
    """Only the cap is mandatory; schedule/crew caps are independently optional."""
    budget = BudgetExtraction(locations=[location("loc_a")])
    constraints = ProductionConstraints(
        total_budget_usd=50000.0,
        max_filming_days=None,
        max_total_crew=None,
        max_primary_locations=None,
    )
    assert check_missing_budget_cap(budget, constraints) == []


def test_no_budget_document_means_no_cap_question():
    assert check_missing_budget_cap(None, None) == []


# ==============================================================================
# RULE 5 — shoot-day conflict
# ==============================================================================


def test_conflict_is_flagged_when_all_scenes_have_shoot_days():
    """The exact case that was silently missed: 2 + 1 = 3 vs a stated 2."""
    script = ScriptExtraction(
        nodes=[
            node("scene_stakeout", "loc_warehouse", shoot_days=2),
            node("scene_break_in", "loc_warehouse", shoot_days=1),
        ],
        edges=[],
    )
    budget = BudgetExtraction(locations=[location("loc_warehouse", total_shoot_days=2)])

    diags = check_shoot_day_conflicts(script, budget)
    assert codes(diags) == [SHOOT_DAY_CONFLICT]
    assert diags[0].data["script_total"] == 3
    assert diags[0].data["budget_total"] == 2
    assert diags[0].data["location_id"] == "loc_warehouse"


def test_matching_totals_are_not_flagged():
    script = ScriptExtraction(
        nodes=[
            node("a", "loc_warehouse", shoot_days=2),
            node("b", "loc_warehouse", shoot_days=1),
        ],
        edges=[],
    )
    budget = BudgetExtraction(locations=[location("loc_warehouse", total_shoot_days=3)])
    assert check_shoot_day_conflicts(script, budget) == []


def test_partial_scene_data_is_skipped_not_flagged():
    """A lower sum from incomplete data is the normal optional-field gap.

    This is the v1 regression: one scene missing shoot_days must NOT produce a
    conflict, even though 2 != 3.
    """
    script = ScriptExtraction(
        nodes=[
            node("a", "loc_warehouse", shoot_days=2),
            node("b", "loc_warehouse", shoot_days=None),
        ],
        edges=[],
    )
    budget = BudgetExtraction(locations=[location("loc_warehouse", total_shoot_days=3)])
    assert check_shoot_day_conflicts(script, budget) == []


def test_location_without_total_shoot_days_is_skipped():
    script = ScriptExtraction(nodes=[node("a", "loc_warehouse", shoot_days=2)], edges=[])
    budget = BudgetExtraction(locations=[location("loc_warehouse", total_shoot_days=None)])
    assert check_shoot_day_conflicts(script, budget) == []


def test_every_conflicting_location_is_reported_not_just_the_first():
    """Checking must not stop after the first location."""
    script = ScriptExtraction(
        nodes=[
            node("a", "loc_one", shoot_days=5),
            node("b", "loc_two", shoot_days=4),
        ],
        edges=[],
    )
    budget = BudgetExtraction(
        locations=[
            location("loc_one", total_shoot_days=1),
            location("loc_two", total_shoot_days=1),
        ]
    )
    diags = check_shoot_day_conflicts(script, budget)
    assert len(diags) == 2
    assert {d.data["location_id"] for d in diags} == {"loc_one", "loc_two"}


def test_single_document_turn_has_nothing_to_cross_check():
    script = ScriptExtraction(nodes=[node("a", "loc_x", shoot_days=2)], edges=[])
    assert check_shoot_day_conflicts(script, None) == []
    assert check_shoot_day_conflicts(None, BudgetExtraction(locations=[location("loc_x")])) == []


# ==============================================================================
# Join integrity
# ==============================================================================


def test_orphaned_location_reference_is_flagged():
    """A scene pointing at a location with no cost row makes its branch uncostable."""
    script = ScriptExtraction(
        nodes=[node("a", "loc_costed"), node("b", "loc_missing")], edges=[]
    )
    budget = BudgetExtraction(locations=[location("loc_costed")])

    diags = check_orphaned_locations(script, budget)
    assert codes(diags) == [ORPHANED_LOCATION_REF]
    assert diags[0].data["location_id"] == "loc_missing"
    assert diags[0].data["node_ids"] == ["b"]


def test_fully_joined_locations_produce_no_diagnostics():
    script = ScriptExtraction(nodes=[node("a", "loc_x")], edges=[])
    budget = BudgetExtraction(locations=[location("loc_x")])
    assert check_orphaned_locations(script, budget) == []


# ==============================================================================
# Entry point
# ==============================================================================


def test_run_all_checks_aggregates_every_rule():
    script = ScriptExtraction(
        nodes=[
            node("a", "loc_warehouse", shoot_days=3),
            node("b", "loc_nowhere", shoot_days=1),
        ],
        edges=[ScriptEdge(parent_node_id="a", child_node_id="b", choice_text="go")],
    )
    budget = BudgetExtraction(locations=[location("loc_warehouse", total_shoot_days=1)])

    found = set(codes(run_all_checks(script, budget, None)))
    assert MISSING_BUDGET_CAP in found
    assert SHOOT_DAY_CONFLICT in found
    assert ORPHANED_LOCATION_REF in found


def test_clean_extraction_produces_no_diagnostics():
    """The no-questions path: everything joins, totals agree, cap is stated."""
    script = ScriptExtraction(nodes=[node("a", "loc_x", shoot_days=2)], edges=[])
    budget = BudgetExtraction(locations=[location("loc_x", total_shoot_days=2)])
    constraints = ProductionConstraints(total_budget_usd=10000.0)
    assert run_all_checks(script, budget, constraints) == []
