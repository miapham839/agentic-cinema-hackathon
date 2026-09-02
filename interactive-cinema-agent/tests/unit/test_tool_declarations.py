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

"""Guards the assumption that let us delete the schema dumps from the instruction.

parser_agent's instruction used to paste
`json.dumps(ScriptExtraction.model_json_schema())` (and two more) inline —
~2,200 tokens, 29% of the instruction, duplicating what ADK's FunctionTool
already binds into the function declaration via `parameters_json_schema`.

That deletion is only safe while two things hold:

  1. The nested model schema really is bound into the declaration.
  2. `Field(description=...)` strings survive into it — because those are now
     the ONLY per-field extraction guidance the model receives (see the module
     docstring in app/schemas.py).

Both depend on ADK's JSON_SCHEMA_FOR_FUNC_DECL feature flag, which is
EXPERIMENTAL and merely defaults on. The legacy code path rebuilds parameters
field-by-field and drops every description silently — no error, just an agent
that quietly stops knowing how to derive a location_id. These tests fail loudly
if that ever happens.
"""

import pytest
from google.adk.tools import FunctionTool

from app.tools import (
    commit_staged,
    stage_budget_extraction,
    stage_production_constraints,
    stage_script_extraction,
)


def params_of(fn) -> dict:
    declaration = FunctionTool(fn)._get_declaration()
    schema = declaration.parameters_json_schema
    assert schema is not None, (
        f"{fn.__name__} has no parameters_json_schema — the JSON_SCHEMA_FOR_FUNC_DECL "
        "feature flag is likely off, which also means field descriptions are being "
        "dropped. Re-enable it or restore the schema dumps in the instruction."
    )
    return schema


@pytest.mark.parametrize(
    "fn, model_name",
    [
        (stage_script_extraction, "ScriptExtraction"),
        (stage_budget_extraction, "BudgetExtraction"),
        (stage_production_constraints, "ProductionConstraints"),
    ],
)
def test_nested_model_schema_is_bound_into_the_declaration(fn, model_name):
    """The model must receive the full nested schema without us pasting it in."""
    schema = params_of(fn)
    assert model_name in schema.get("$defs", {}), (
        f"{model_name} missing from {fn.__name__}'s declaration $defs"
    )


@pytest.mark.parametrize(
    "model_name, field",
    [
        ("ScriptNode", "location_id"),
        ("ScriptNode", "node_id"),
        ("ScriptNode", "shoot_days"),
        ("ScriptNode", "state_modifiers"),
        ("ScriptEdge", "child_node_id"),
    ],
)
def test_script_field_descriptions_survive(model_name, field):
    """These carry extraction rules that exist nowhere else now."""
    defs = params_of(stage_script_extraction)["$defs"]
    description = defs[model_name]["properties"][field].get("description", "")
    assert description.strip(), (
        f"{model_name}.{field} lost its description in the bound declaration. "
        "That guidance is no longer in the instruction, so the model would be "
        "flying blind on this field."
    )


def test_location_id_guidance_reaches_the_model():
    """The join rule is the single most load-bearing field description."""
    defs = params_of(stage_script_extraction)["$defs"]
    description = defs["ScriptNode"]["properties"]["location_id"]["description"].lower()
    assert "snake_case" in description
    assert "same location_id" in description or "exact same" in description


def test_optional_scheduling_fields_keep_their_do_not_guess_rule():
    """shoot_days must stay 'only if stated' — a guess here corrupts budget math."""
    defs = params_of(stage_script_extraction)["$defs"]
    for field in ("shoot_days", "num_crew_required"):
        description = defs["ScriptNode"]["properties"][field]["description"].lower()
        assert "only if" in description
        assert "not estimate" in description or "do not" in description


def test_correction_vocabulary_is_bound_for_commit():
    """commit_staged's corrections are how HITL answers get applied."""
    schema = params_of(commit_staged)
    assert "Correction" in schema.get("$defs", {})
    action = schema["$defs"]["Correction"]["properties"]["action"]
    # Literal[...] should surface as an enum the model can choose from.
    enum_values = action.get("enum") or [
        c.get("const") for c in action.get("anyOf", []) if isinstance(c, dict)
    ]
    for expected in (
        "set_total_budget",
        "skip_constraints",
        "set_location_total_shoot_days",
        "merge_locations",
        "set_node_edges",
    ):
        assert expected in enum_values, f"{expected} missing from the Correction action enum"


def test_instruction_does_not_restate_the_schemas():
    """Regression guard: the dumps must not creep back in alongside the binding."""
    from app.agent import parser_agent

    instruction = parser_agent.instruction
    # A pasted json.dumps(model_json_schema()) is unmistakable.
    assert '"properties"' not in instruction, (
        "parser_agent's instruction appears to contain a pasted JSON schema again — "
        "ADK already binds these via parameters_json_schema, so this is ~2,200 "
        "duplicated tokens per call."
    )
