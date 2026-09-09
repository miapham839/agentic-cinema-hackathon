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

1. As the type hint on a `FunctionTool`'s parameter (see `insert_script_extraction`
   and `insert_budget_extraction` in app/tools.py) — ADK's FunctionTool
   auto-generates a JSON schema from the type hint and forces the model's tool
   call arguments to match it.
2. As a 1:1 mirror of the ClickHouse table columns they get written to
   (script_nodes, script_edges, production_locations), minus the
   database-managed `version` / `updated_at` columns.

See docs/DESIGN_DECISIONS.md, section "Structured extraction: output_schema vs.
typed tool parameters" for why (1) was chosen over `LlmAgent.output_schema`.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ScriptNode(BaseModel):
    """One node (scene/beat) in the branching story graph. Maps 1:1 to a row
    in the ClickHouse `script_nodes` table."""

    node_id: str = Field(description="Stable unique identifier for this node, e.g. 'scene_1a'.")
    title: str = Field(description="Short human-readable scene title.")
    location_id: str = Field(
        description="Identifier of the location this scene is shot at. Must match a "
        "location_id present in the companion budget/locations document."
    )
    narrative_text: str = Field(description="The scene's narrative/script text as written.")
    characters_present: list[str] = Field(
        default_factory=list, description="Names of characters present in this scene."
    )
    state_modifiers: dict[str, str] = Field(
        default_factory=dict,
        description="Story-state variables this scene changes, as string key/value pairs "
        "(e.g. {'trust_level': '-1', 'has_weapon': 'true'}).",
    )
    shoot_days: Optional[int] = Field(
        default=None,
        description="Number of days needed to shoot this scene, ONLY if the document "
        "explicitly states or clearly implies a schedule for this scene. Leave unset "
        "(do not guess or estimate) if the document doesn't say.",
    )
    num_crew_required: Optional[int] = Field(
        default=None,
        description="Crew size needed for this scene, ONLY if the document explicitly "
        "states or clearly implies one. Leave unset (do not guess or estimate) if the "
        "document doesn't say.",
    )
    is_deleted: bool = Field(
        default=False,
        description="Soft-delete flag. Always False for a fresh document ingestion — "
        "never set this to True yourself; it's only ever set when a correction is "
        "specifically removing this node.",
    )


class ScriptEdge(BaseModel):
    """One directed choice/transition between two nodes. Maps 1:1 to a row in
    the ClickHouse `script_edges` table."""

    parent_node_id: str = Field(description="node_id this choice originates from.")
    child_node_id: str = Field(description="node_id this choice leads to.")
    choice_text: str = Field(description="The choice text presented to the viewer.")
    required_state: dict[str, str] = Field(
        default_factory=dict,
        description="Story-state conditions required for this choice to be available "
        "(empty dict if the choice is always available).",
    )
    is_deleted: bool = Field(
        default=False,
        description="Soft-delete flag. Always False for a fresh document ingestion — "
        "never set this to True yourself; it's only ever set when a correction is "
        "specifically removing this edge.",
    )


class ScriptExtraction(BaseModel):
    """Full structured extraction of a script PDF: every node and edge found."""

    nodes: list[ScriptNode] = Field(description="Every scene/node found in the script.")
    edges: list[ScriptEdge] = Field(description="Every choice/edge found in the script.")


class ProductionLocation(BaseModel):
    """One shooting location's cost/logistics data. Maps 1:1 to a row in the
    ClickHouse `production_locations` table."""

    location_id: str = Field(description="Stable unique identifier, e.g. 'loc_diner_01'.")
    location_name: str = Field(description="Human-readable location name.")
    daily_rate_usd: float = Field(description="Cost in USD to shoot at this location for one day.")
    company_move_penalty_usd: float = Field(
        description="One-time USD cost incurred when the production moves to this location."
    )
    requires_permit: bool = Field(description="Whether shooting here requires a permit.")
    max_cast_capacity: int = Field(description="Maximum number of cast members this location can hold.")
    total_shoot_days: Optional[int] = Field(
        default=None,
        description="Total number of days the production expects to shoot at this "
        "location, ONLY if the document explicitly states one. Leave unset (do not "
        "guess or estimate) if the document doesn't say.",
    )


class BudgetExtraction(BaseModel):
    """Full structured extraction of a production budget/locations PDF."""

    locations: list[ProductionLocation] = Field(description="Every location found in the document.")


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


class GraphCorrection(BaseModel):
    """A version-incrementing write to the story graph: one or more corrected
    nodes and/or edges, applied together by `apply_graph_correction` (see
    app/tools.py). A single-scene fix is `nodes=[one_node]`; a multi-part fix
    (e.g. merging two scenes) is `nodes=[node_a, node_b], edges=[...]` — one
    shape covers both, matching how `insert_script_extraction` already treats
    one edge and many edges identically."""

    suggestion_id: Optional[str] = Field(
        default=None,
        description="The suggestion this correction fulfills, if any. Set when executing "
        "an approved suggestion or a rollback; left unset for a direct, unprompted correction.",
    )
    nodes: list[ScriptNode] = Field(
        default_factory=list, description="Corrected/new nodes to write, each as a full ScriptNode."
    )
    edges: list[ScriptEdge] = Field(
        default_factory=list, description="Corrected/new edges to write, each as a full ScriptEdge."
    )


class SuggestionOption(BaseModel):
    """One of up to 3 concrete fix choices attached to a Suggestion. Carries
    the full GraphCorrection needed to execute it, so approving it doesn't
    need to re-derive the fix."""

    option_id: str = Field(description="Short stable identifier for this option within its suggestion, e.g. 'a'.")
    label: str = Field(description="Short label for a UI button, e.g. 'Merge into scene_3a'.")
    description: str = Field(description="One or two sentences explaining what this option does.")
    correction: GraphCorrection = Field(
        description="Exactly what apply_graph_correction should write if this option is chosen."
    )
    expected_versions: dict[str, int] = Field(
        default_factory=dict,
        description="INTERNAL — leave unset; record_suggestion computes this automatically "
        "from `correction` (never trust a model-supplied value here). Snapshot of the "
        "version each referenced node_id/edge_ref was at when this option was created — "
        "checked at approval time to detect whether anything changed underneath it since.",
    )
    stale: bool = Field(
        default=False,
        description="INTERNAL — leave unset (defaults to False); only ever set by cascade "
        "marking after another suggestion's correction lands, never by the proposing agent. "
        "A UI hint that this option may need re-review, not itself a safety gate.",
    )


class Suggestion(BaseModel):
    """A persisted, actionable finding from graph_auditor_agent or
    budget_agent. Written via `record_suggestion` (see app/tools.py) instead
    of only being stated in chat, so it can be listed and responded to later
    (see the suggestions REST API in app/app_utils/suggestions_api.py)."""

    category: str = Field(
        description="One of: dead_end, orphaned_choice, continuity_break, unreachable_node, "
        "budget_overrun, savings_suggestion, edit_request, rollback_request."
    )
    summary: str = Field(description="One-sentence summary of the finding, shown as the suggestion's title.")
    detail: str = Field(description="Fuller explanation of the finding, shown when expanded.")
    affected_node_ids: list[str] = Field(
        default_factory=list,
        description="INTERNAL — leave unset; record_suggestion recomputes this automatically as "
        "the union of node_ids referenced across every option's correction, so it can never "
        "drift from what the options actually contain.",
    )
    affected_edge_refs: list[str] = Field(
        default_factory=list,
        description="INTERNAL — leave unset; record_suggestion recomputes this automatically as "
        "the union of 'parent_node_id->child_node_id' refs across every option's correction.",
    )
    options: list[SuggestionOption] = Field(
        min_length=1,
        max_length=3,
        description="1 to 3 concrete ways to resolve this finding.",
    )
