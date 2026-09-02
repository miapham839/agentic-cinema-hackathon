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

"""Structured-extraction schemas for parser_agent.

These Pydantic models are the single source of truth for the shape of data
parser_agent must produce from uploaded PDFs. They are used two ways:

1. As the type hint on a `FunctionTool`'s parameter (see `stage_script_extraction`
   and `stage_budget_extraction` in app/tools.py) — ADK's FunctionTool
   auto-generates a JSON schema from the type hint and forces the model's tool
   call arguments to match it.
2. As a 1:1 mirror of the ClickHouse table columns they get written to
   (script_nodes, script_edges, production_locations), minus the
   database-managed `version` / `updated_at` columns.

See docs/DESIGN_DECISIONS.md, section "Structured extraction: output_schema vs.
typed tool parameters" for why (1) was chosen over `LlmAgent.output_schema`.

IMPORTANT — the `Field(description=...)` strings below are the ONLY per-field
guidance the model receives. parser_agent's instruction deliberately does NOT
restate these schemas: ADK's FunctionTool already binds a byte-identical copy
of `model_json_schema()` into the function declaration via
`parameters_json_schema`, so dumping them into the instruction as well sent the
same ~2,200 tokens twice on every call. What the auto-generated declaration
carries is exactly these `description=` strings — prose that lives only in the
instruction does NOT reach the model as field guidance. So per-field extraction
rules belong HERE, not in the instruction. See tests/unit/test_tool_declarations.py,
which pins that behavior against the experimental ADK flag that controls it.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ScriptNode(BaseModel):
    """One node (scene/beat) in the branching story graph. Maps 1:1 to a row
    in the ClickHouse `script_nodes` table."""

    node_id: str = Field(
        description="Stable unique identifier for this scene, e.g. 'scene_1a'. The document "
        "won't give you one — invent a short, stable, lowercase snake_case id derived from "
        "the scene's own title or content."
    )
    title: str = Field(
        description="Short human-readable scene title. Give every scene one even if the "
        "document has no heading for it — infer a title from the scene's own content."
    )
    location_id: str = Field(
        description="Identifier of the location this scene is shot at, as a short, stable, "
        "lowercase snake_case slug derived from the location's NAME as it appears in the text "
        "(e.g. a location named 'Fairmont Hotel' -> 'loc_fairmont_hotel'). CRITICAL: if the "
        "same place is mentioned in both the script and a companion budget/locations document, "
        "it MUST get the exact same location_id in both, or the two won't join correctly "
        "downstream. Match by name meaning, not exact string — a full proper name, a shortened "
        "form, and a generic reference to the same place all resolve to one location_id when "
        "they clearly refer to the same place."
    )
    narrative_text: str = Field(description="The scene's narrative/script text as written.")
    characters_present: list[str] = Field(
        default_factory=list, description="Names of characters present in this scene."
    )
    state_modifiers: dict[str, str] = Field(
        default_factory=dict,
        description="Story-state variables this scene changes, as string key/value pairs "
        "(e.g. {'trust_level': '-1', 'has_weapon': 'true'}). Capture what the narrative "
        "implies has changed even when nothing is explicitly labeled a 'state change' — infer "
        "a reasonable key and value from what actually happens. Use an empty dict only when a "
        "scene genuinely changes nothing about the story's state. This is inference from what "
        "is depicted, not invention of events that aren't there.",
    )
    shoot_days: Optional[int] = Field(
        default=None,
        description="Number of days needed to shoot this scene, ONLY if the document "
        "explicitly states or clearly implies a shoot length for this specific scene (a "
        "production sidebar note, a scheduling table, a line like '2-day shoot'). Most scripts "
        "won't say this per scene — leave unset when it doesn't. Do NOT estimate or infer a "
        "number from the scene's dramatic content; a wrong guess here is indistinguishable "
        "from real scheduling data downstream.",
    )
    num_crew_required: Optional[int] = Field(
        default=None,
        description="Crew size needed for this scene, ONLY if the document explicitly states "
        "or clearly implies one for this specific scene. Leave unset when it doesn't. Do NOT "
        "estimate from the scene's content — a tense or crowded scene is not automatically "
        "'more crew'.",
    )


class ScriptEdge(BaseModel):
    """One directed choice/transition between two nodes. Maps 1:1 to a row in
    the ClickHouse `script_edges` table."""

    parent_node_id: str = Field(description="node_id this choice originates from.")
    child_node_id: str = Field(
        description="node_id this choice leads to. If a choice leads to a scene that is "
        "referenced narratively but never actually written in the document, still record the "
        "edge exactly as implied — do not silently drop it, and do not invent the missing "
        "scene as a node. The auditor agent downstream is responsible for flagging that as a "
        "broken reference."
    )
    choice_text: str = Field(description="The choice text presented to the viewer.")
    required_state: dict[str, str] = Field(
        default_factory=dict,
        description="Story-state conditions required for this choice to be available "
        "(empty dict if the choice is always available).",
    )


class ScriptExtraction(BaseModel):
    """Full structured extraction of a script PDF: every node and edge found."""

    nodes: list[ScriptNode] = Field(description="Every scene/node found in the script.")
    edges: list[ScriptEdge] = Field(description="Every choice/edge found in the script.")


class ProductionLocation(BaseModel):
    """One shooting location's cost/logistics data. Maps 1:1 to a row in the
    ClickHouse `production_locations` table."""

    location_id: str = Field(
        description="Stable unique identifier, as a short, lowercase snake_case slug derived "
        "from this location's NAME (e.g. 'Fairmont Hotel' -> 'loc_fairmont_hotel'). MUST use "
        "the exact same slug the companion script document's scenes use for this same place, "
        "or the cost data won't join to the story graph."
    )
    location_name: str = Field(description="Human-readable location name, as written in the document.")
    daily_rate_usd: float = Field(description="Cost in USD to shoot at this location for one day.")
    company_move_penalty_usd: float = Field(
        description="One-time USD cost incurred when the production moves to this location."
    )
    requires_permit: bool = Field(description="Whether shooting here requires a permit.")
    max_cast_capacity: int = Field(description="Maximum number of cast members this location can hold.")
    total_shoot_days: Optional[int] = Field(
        default=None,
        description="Total number of days the production expects to shoot at this location, "
        "ONLY if the document explicitly states one. Leave unset if it doesn't — do NOT derive "
        "this from daily_rate_usd, from the number of scenes, or from anything else; it must "
        "come directly from the document's own words.",
    )


class BudgetExtraction(BaseModel):
    """Full structured extraction of a production budget/locations PDF."""

    locations: list[ProductionLocation] = Field(description="Every location found in the document.")


class Correction(BaseModel):
    """One user-approved correction to apply to the staged drafts before writing.

    A closed vocabulary, one entry per possible answer to a HITL question. This
    exists so resolving an ambiguity costs a few small records instead of
    re-sending an entire corrected extraction — the drafts are serialized once,
    at staging time, and never again.

    Only the fields relevant to `action` need to be set; leave the rest unset.
    """

    action: Literal[
        "set_total_budget",
        "skip_constraints",
        "set_location_total_shoot_days",
        "merge_locations",
        "set_node_edges",
    ] = Field(
        description=(
            "Which correction to apply. 'set_total_budget' resolves a missing budget cap "
            "(needs amount_usd). 'skip_constraints' drops the constraints row entirely when "
            "the user opts out of overrun checks. 'set_location_total_shoot_days' resolves a "
            "shoot-day conflict (needs location_id + days). 'merge_locations' folds one "
            "location into another when the user confirms they're the same place (needs "
            "location_id + into_location_id). 'set_node_edges' replaces one scene's outgoing "
            "edges when the user resolves an ambiguous destination (needs node_id + "
            "child_node_ids)."
        )
    )
    location_id: Optional[str] = Field(
        default=None, description="Location being corrected, or merged away from."
    )
    into_location_id: Optional[str] = Field(
        default=None, description="For 'merge_locations': the location_id to keep."
    )
    node_id: Optional[str] = Field(
        default=None, description="For 'set_node_edges': the scene whose edges are being replaced."
    )
    child_node_ids: Optional[list[str]] = Field(
        default=None,
        description="For 'set_node_edges': the full set of scenes this one now leads to.",
    )
    amount_usd: Optional[float] = Field(
        default=None, description="For 'set_total_budget': the approved total budget in USD."
    )
    days: Optional[int] = Field(
        default=None, description="For 'set_location_total_shoot_days': the corrected day count."
    )


class ProductionConstraints(BaseModel):
    """Overall approved budget/schedule/crew limits for the production. Maps
    1:1 to a row in the ClickHouse `production_constraints` table.

    Note: project_id is NOT part of this schema — it's hardcoded by
    insert_production_constraints (see app/tools.py) to match the same
    hardcoded project_id used for script_nodes/script_edges/production_locations,
    rather than left for the model to invent one per parse."""

    total_budget_usd: float = Field(
        description="Total approved production budget, in USD. Required — budget_agent "
        "cannot check for overruns without this."
    )
    max_filming_days: Optional[int] = Field(
        default=None,
        description="Maximum number of filming days approved, ONLY if the document "
        "states one. Leave unset if it doesn't — do not invent a number.",
    )
    max_total_crew: Optional[int] = Field(
        default=None,
        description="Maximum total crew size approved, ONLY if the document states "
        "one. Leave unset if it doesn't — do not invent a number.",
    )
    max_primary_locations: Optional[int] = Field(
        default=None,
        description="Maximum number of distinct primary shooting locations approved, "
        "ONLY if the document states one. Leave unset if it doesn't — do not invent a number.",
    )
