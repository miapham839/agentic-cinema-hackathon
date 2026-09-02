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

"""Keeps the held-out fixtures actually held out.

An earlier round of instruction fixes pasted fixture strings verbatim into
parser_agent's instruction — "INT. PRECINCT" / "INT. 14TH STREET STATION",
scene_ledger_call -> scene_rooftop_pursuit. Rules 3 and 4 then appeared to start
working, but the measurement could no longer tell "generalized the principle"
apart from "matched a string it was handed," so the result meant nothing.

That mistake is easy to repeat: the natural way to make an instruction clearer
is to reach for the example currently failing, which is precisely the example
under test. This test makes that fail loudly and immediately instead of one
eval cycle later.

See docs/DESIGN_DECISIONS.md section 14.
"""

import re

import pytest

from app.agent import parser_agent
from mock_data.heldout_fixtures import BUDGET_TEXT, DEGENERATE_TEXT, SCRIPT_TEXT

# Distinctive vocabulary from the held-out (period drama) fixtures. Generic
# words the domains legitimately share ("scene", "location", "budget") are
# excluded — only names that would constitute a giveaway are listed.
HELD_OUT_VOCABULARY = [
    "ashfield",
    "eleanor",
    "thomas",
    "long gallery",
    "tack room",
    "stables",
    "aldate",
    "gamekeeper",
    "tenant cottage",
    "solicitor",
    "inheritance",
]

# Names the instruction legitimately uses as teaching examples. These must stay
# out of every fixture, or the fixtures stop being a fair test.
INSTRUCTION_EXAMPLE_VOCABULARY = [
    "ward six",
    "brendan",
    "lakeside chapel",
    "scene_vigil",
    "scene_ambulance_bay",
    "scene_hearing",
]


def test_instruction_contains_no_held_out_fixture_vocabulary():
    instruction = parser_agent.instruction.lower()
    leaked = [term for term in HELD_OUT_VOCABULARY if term in instruction]
    assert not leaked, (
        f"parser_agent's instruction contains held-out fixture vocabulary: {leaked}. "
        "The eval in tests/eval/test_hitl_rules.py can no longer distinguish "
        "generalization from string matching. Either reword the instruction with a "
        "different example, or retire the held-out fixture set."
    )


def test_held_out_fixtures_contain_no_instruction_examples():
    corpus = (SCRIPT_TEXT + BUDGET_TEXT + DEGENERATE_TEXT).lower()
    leaked = [term for term in INSTRUCTION_EXAMPLE_VOCABULARY if term in corpus]
    assert not leaked, (
        f"The held-out fixtures contain instruction example vocabulary: {leaked}. "
        "The model would be recognizing its own examples rather than reasoning."
    )


def test_held_out_fixtures_actually_contain_a_shoot_day_conflict():
    """Guards the trap itself, not the agent.

    The first harness run reported SHOOT_DAY_CONFLICT 0/3 and looked like a
    regression. The real cause was this fixture: the two shoot-day notes sat at
    different locations, so no conflict existed to detect. A trap that doesn't
    trap is worse than no trap, because it reads as a failing agent.
    """
    sluglines = {}
    current = None
    for line in SCRIPT_TEXT.splitlines():
        stripped = line.strip()
        if re.match(r"^(INT|EXT)\.", stripped):
            current = stripped
        match = re.search(r"(?i)\b(one|two|three|four)-day shoot", stripped)
        if match and current:
            words = {"one": 1, "two": 2, "three": 3, "four": 4}
            sluglines.setdefault(current, []).append(words[match.group(1).lower()])

    stables_days = [
        days for slug, day_list in sluglines.items() if "STABLES" in slug for days in day_list
    ]
    assert len(stables_days) >= 2, (
        "The held-out script needs at least two scenes at THE STABLES carrying "
        "explicit shoot-day notes, or rule 5 has nothing to detect."
    )
    assert sum(stables_days) != 2, (
        f"Stables scenes sum to {sum(stables_days)}, which matches the budget "
        "document's stated 2 — no conflict for the check to find."
    )


def test_held_out_budget_states_no_total_cap():
    """Rule 2's trap: caps on crew/days/locations, but no overall total."""
    corpus = BUDGET_TEXT.lower()
    assert "crew" in corpus, "budget should still state a crew cap"
    for phrase in ("total budget", "total approved", "overall budget"):
        assert phrase not in corpus, (
            f"held-out budget states {phrase!r}; rule 2 needs the total to be absent"
        )
