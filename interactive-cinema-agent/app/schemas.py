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
