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

"""Tool definitions for the Interactive Story & Budget Co-Pilot.

Read/write split (see docs/DESIGN_DECISIONS.md for the full rationale):
  - Reads go through the official `mcp-clickhouse` MCP server (`clickhouse_tools`),
    which is read-only by default. graph_auditor_agent and budget_agent use this.
  - Writes go through a small set of typed, scoped Python tools built directly on
    `clickhouse_connect`, defined below. Only parser_agent uses these. This avoids
    turning on mcp-clickhouse's CLICKHOUSE_ALLOW_WRITE_ACCESS flag, which would grant
    every agent holding that toolset arbitrary DDL/DML via run_query.

Writes follow a stage -> check -> commit lifecycle (see that section below for
why), with the deterministic pre-write rules living in app/checks.py.
"""

import os

from google.adk.tools import ToolContext
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

from app.checks import (
    MUST_ASK,
    check_budget_extraction,
    check_script_extraction,
    run_all_checks,
)
from app.schemas import (
    BudgetExtraction,
    Correction,
    ProductionConstraints,
    ScriptEdge,
    ScriptExtraction,
)

# ==============================================================================
# CLICKHOUSE CONNECTION CONFIG (shared by read + write paths)
# ==============================================================================

CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "your-instance.clickhouse.cloud")
CLICKHOUSE_PORT = os.getenv("CLICKHOUSE_PORT", "8443")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "default")


# ==============================================================================
# CLICKHOUSE MCP TOOLS (read-only) — graph_auditor_agent, budget_agent
# ==============================================================================
# Official mcp-clickhouse package (Python) from https://github.com/ClickHouse/mcp-clickhouse
# CLICKHOUSE_ALLOW_WRITE_ACCESS is intentionally NOT set, so this toolset can only
# call list_databases / list_tables / run_query(SELECT ...).

clickhouse_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "--with", "mcp-clickhouse", "--python", "3.12", "mcp-clickhouse"],
            env={
                "CLICKHOUSE_HOST": CLICKHOUSE_HOST,
                "CLICKHOUSE_PORT": CLICKHOUSE_PORT,
                "CLICKHOUSE_USER": CLICKHOUSE_USER,
                "CLICKHOUSE_PASSWORD": CLICKHOUSE_PASSWORD,
                "CLICKHOUSE_SECURE": "true",
                "CLICKHOUSE_VERIFY": "true",
            },
        ),
        # ADK's default is 5.0s, which only barely covers a warm `uv run
        # --with mcp-clickhouse` startup (~2.8s measured: dependency
        # resolution + FastMCP boot + its background worker, before the MCP
        # handshake can even start) — a cold uv cache or any added latency
        # trips it. This is startup overhead only; the ClickHouse Cloud
        # connection itself responds in ~1s.
        timeout=30.0,
    ),
)


# ==============================================================================
# CLICKHOUSE WRITE TOOLS (custom, scoped) — parser_agent only
# ==============================================================================
# script_nodes, script_edges and production_locations all use ReplacingMergeTree
# keyed on a `version` column (see docs/DESIGN_DECISIONS.md, "Append & Version").
# parser_agent only ever performs the *initial* ingestion of a document, so every
# row it writes gets version=1. A later "apply approved fix" tool (not built in
# this POC — see docs/project_context.md's human-in-the-loop feature) would increment
# the version instead of hardcoding 1.

INITIAL_VERSION = 1

# Hardcoded until multi-project support exists (see docs/DESIGN_DECISIONS.md
# section 7). parser_agent never sets this itself — it's injected here so
# every row across all four tables agrees, regardless of what the model does.
PROJECT_ID = "01"

_write_client = None


def _get_clickhouse_write_client():
    """Lazily creates a single reusable clickhouse-connect client for writes."""
    global _write_client
    if _write_client is None:
        import clickhouse_connect

        _write_client = clickhouse_connect.get_client(
            host=CLICKHOUSE_HOST,
            port=int(CLICKHOUSE_PORT),
            username=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
            database=CLICKHOUSE_DATABASE,
            secure=True,
        )
    return _write_client


# ==============================================================================
# STAGE -> CHECK -> COMMIT LIFECYCLE — parser_agent only
# ==============================================================================
# parser_agent used to call three fire-and-forget insert_* tools directly. That
# had two problems seen in testing:
#
#   1. Nothing structurally prevented a partial write. In one run only
#      insert_script_extraction fired; the budget and constraints writes were
#      silently skipped, leaving the three tables inconsistent.
#   2. Resolving a HITL answer meant re-sending an entire corrected extraction,
#      so every large draft got serialized twice — the payload pattern already
#      implicated in a MALFORMED_FUNCTION_CALL failure.
#
# The lifecycle fixes both: drafts are staged once, deterministic checks run
# over them in code (app/checks.py), the model asks the user about whatever the
# checks found, and a single commit applies small correction records and writes
# every table together.
#
# NOTE ON STATE KEYS: these are deliberately NOT "temp:" prefixed. ADK strips
# temp-scoped keys from the persisted event delta (_trim_temp_delta_state in
# sessions/base_session_service.py) — they survive only in the in-memory
# session object. request_input pauses the invocation across a request
# boundary, and this project swaps in VertexAiSessionService when
# GOOGLE_CLOUD_AGENT_ENGINE_ID is set (see app/app_utils/services.py), so
# temp-scoped drafts would silently vanish on resume. commit_staged clears
# these explicitly instead.

STAGED_SCRIPT_KEY = "staged_script"
STAGED_BUDGET_KEY = "staged_budget"
STAGED_CONSTRAINTS_KEY = "staged_constraints"
CHECKS_RAN_KEY = "staged_checks_ran"


async def require_checks_before_commit(tool, args, tool_context: ToolContext):
    """before_tool_callback: refuses commit_staged until check_staged has run.

    Returning a dict short-circuits the tool call and hands that dict back to
    the model as the result, so this blocks the write rather than merely warning.

    Without this the ordering is only a prompt instruction, and prompt
    instructions are exactly what got dropped in testing: one run committed the
    script extraction while silently skipping the budget and constraints writes,
    leaving the three tables inconsistent with no error anywhere.
    """
    if tool.name != "commit_staged":
        return None
    if tool_context.state.get(CHECKS_RAN_KEY):
        return None
    return {
        "status": "error",
        "message": (
            "commit_staged blocked: call check_staged first and resolve any "
            "must_ask diagnostics with the user before writing."
        ),
    }


def _clear_staged(tool_context: ToolContext) -> None:
    """Drops staged drafts so a later turn can't accidentally re-commit them."""
    for key in (STAGED_SCRIPT_KEY, STAGED_BUDGET_KEY, STAGED_CONSTRAINTS_KEY, CHECKS_RAN_KEY):
        if key in tool_context.state:
            tool_context.state[key] = None


def _load_staged(tool_context: ToolContext):
    """Rehydrates the staged drafts from session state as typed models."""
    raw_script = tool_context.state.get(STAGED_SCRIPT_KEY)
    raw_budget = tool_context.state.get(STAGED_BUDGET_KEY)
    raw_constraints = tool_context.state.get(STAGED_CONSTRAINTS_KEY)
    return (
        ScriptExtraction(**raw_script) if raw_script else None,
        BudgetExtraction(**raw_budget) if raw_budget else None,
        ProductionConstraints(**raw_constraints) if raw_constraints else None,
    )


def stage_script_extraction(extraction: ScriptExtraction, tool_context: ToolContext) -> dict:
    """Stages a parsed script (all scenes + all choices) for writing. Does not write yet.

    Call this exactly once, after extracting ALL scenes and choices from the
    uploaded script document. Nothing reaches the database until commit_staged.

    Args:
        extraction: The full set of nodes and edges parsed from the script document.

    Returns:
        dict with counts and any diagnostics found in this document on its own.
    """
    tool_context.state[STAGED_SCRIPT_KEY] = extraction.model_dump()
    tool_context.state[CHECKS_RAN_KEY] = None  # staging invalidates any prior check

    diagnostics = check_script_extraction(extraction)
    return {
        "status": "staged",
        "nodes_staged": len(extraction.nodes),
        "edges_staged": len(extraction.edges),
        "diagnostics": [d.model_dump() for d in diagnostics],
    }


def stage_budget_extraction(extraction: BudgetExtraction, tool_context: ToolContext) -> dict:
    """Stages parsed budget locations for writing. Does not write yet.

    Call this exactly once, after extracting ALL locations from the uploaded
    budget document. Nothing reaches the database until commit_staged.

    Args:
        extraction: The full set of locations parsed from the budget document.

    Returns:
        dict with counts and any diagnostics found in this document on its own.
    """
    tool_context.state[STAGED_BUDGET_KEY] = extraction.model_dump()
    tool_context.state[CHECKS_RAN_KEY] = None

    diagnostics = check_budget_extraction(extraction)
    return {
        "status": "staged",
        "locations_staged": len(extraction.locations),
        "diagnostics": [d.model_dump() for d in diagnostics],
    }


def stage_production_constraints(
    constraints: ProductionConstraints, tool_context: ToolContext
) -> dict:
    """Stages the production's overall budget/schedule/crew limits. Does not write yet.

    Call this only when the budget document actually states an overall total
    approved budget. If it doesn't, skip this tool — check_staged will detect
    the missing cap and give you the question to ask, including a computed
    placeholder figure. Never invent a budget number to force this call.

    Args:
        constraints: The production constraints parsed from the budget document.

    Returns:
        dict confirming what was staged.
    """
    tool_context.state[STAGED_CONSTRAINTS_KEY] = constraints.model_dump()
    tool_context.state[CHECKS_RAN_KEY] = None
    return {"status": "staged", "total_budget_usd": constraints.total_budget_usd}


def check_staged(tool_context: ToolContext) -> dict:
    """Runs every deterministic pre-write check over what you have staged.

    Takes no arguments — it reads the staged drafts directly, so you never
    re-send them. Checks for: documents that yielded nothing, a missing total
    budget (with a computed placeholder figure), shoot-day totals that disagree
    between the script and budget documents, and scenes pointing at locations
    that have no cost data.

    Call this after staging and before commit_staged. Each returned diagnostic
    carries a ready-to-ask `suggested_question`.

    Returns:
        dict with a list of diagnostics; an empty list means nothing needs asking.
    """
    script, budget, constraints = _load_staged(tool_context)
    if script is None and budget is None:
        return {"status": "error", "message": "nothing staged yet — stage a document first"}

    diagnostics = run_all_checks(script, budget, constraints)
    tool_context.state[CHECKS_RAN_KEY] = True
    return {
        "status": "checked",
        "must_ask_count": sum(1 for d in diagnostics if d.severity == MUST_ASK),
        "diagnostics": [d.model_dump() for d in diagnostics],
    }


def _apply_corrections(corrections, script, budget, constraints):
    """Applies the user's answers to the staged drafts. Pure; returns new state."""
    skip_constraints = False

    for c in corrections:
        if c.action == "set_total_budget" and c.amount_usd is not None:
            if constraints is None:
                constraints = ProductionConstraints(total_budget_usd=c.amount_usd)
            else:
                constraints.total_budget_usd = c.amount_usd

        elif c.action == "skip_constraints":
            skip_constraints = True

        elif c.action == "set_location_total_shoot_days" and budget is not None:
            for loc in budget.locations:
                if loc.location_id == c.location_id:
                    loc.total_shoot_days = c.days

        elif c.action == "merge_locations" and c.location_id and c.into_location_id:
            # Repoint every scene, then drop the now-duplicate cost row.
            if script is not None:
                for n in script.nodes:
                    if n.location_id == c.location_id:
                        n.location_id = c.into_location_id
            if budget is not None:
                budget.locations = [
                    loc for loc in budget.locations if loc.location_id != c.location_id
                ]

        elif c.action == "set_node_edges" and script is not None and c.node_id:
            targets = c.child_node_ids or []
            kept = [e for e in script.edges if e.parent_node_id != c.node_id]
            existing = {e.child_node_id: e for e in script.edges if e.parent_node_id == c.node_id}
            for child in targets:
                kept.append(
                    existing.get(child)
                    or ScriptEdge(
                        parent_node_id=c.node_id,
                        child_node_id=child,
                        choice_text=f"Leads to {child}",
                    )
                )
            script.edges = kept

    if skip_constraints:
        constraints = None
    return script, budget, constraints


def commit_staged(corrections: list[Correction], tool_context: ToolContext) -> dict:
    """Applies the user's answers and writes everything staged to ClickHouse.

    This is the only tool that writes. It writes the script, budget and
    constraints together so the tables can't end up inconsistent. Every row is
    written as version 1 (initial ingestion of the document).

    Args:
        corrections: The user's resolutions to whatever check_staged reported.
            Pass an empty list when nothing needed correcting.

    Returns:
        dict with per-table row counts.
    """
    script, budget, constraints = _load_staged(tool_context)
    if script is None and budget is None:
        return {"status": "error", "message": "nothing staged to commit"}

    script, budget, constraints = _apply_corrections(corrections, script, budget, constraints)

    written = {"nodes": 0, "edges": 0, "locations": 0, "constraints": 0}
    try:
        client = _get_clickhouse_write_client()

        if script is not None and script.nodes:
            client.insert(
                "script_nodes",
                [
                    [
                        n.node_id,
                        PROJECT_ID,
                        n.title,
                        n.location_id,
                        n.narrative_text,
                        n.characters_present,
                        n.state_modifiers,
                        n.shoot_days,
                        n.num_crew_required,
                        INITIAL_VERSION,
                    ]
                    for n in script.nodes
                ],
                column_names=[
                    "node_id",
                    "project_id",
                    "title",
                    "location_id",
                    "narrative_text",
                    "characters_present",
                    "state_modifiers",
                    "shoot_days",
                    "num_crew_required",
                    "version",
                ],
            )
            written["nodes"] = len(script.nodes)

        if script is not None and script.edges:
            client.insert(
                "script_edges",
                [
                    [
                        e.parent_node_id,
                        PROJECT_ID,
                        e.child_node_id,
                        e.choice_text,
                        e.required_state,
                        INITIAL_VERSION,
                    ]
                    for e in script.edges
                ],
                column_names=[
                    "parent_node_id",
                    "project_id",
                    "child_node_id",
                    "choice_text",
                    "required_state",
                    "version",
                ],
            )
            written["edges"] = len(script.edges)

        if budget is not None and budget.locations:
            client.insert(
                "production_locations",
                [
                    [
                        loc.location_id,
                        PROJECT_ID,
                        loc.location_name,
                        loc.daily_rate_usd,
                        loc.company_move_penalty_usd,
                        1 if loc.requires_permit else 0,
                        loc.max_cast_capacity,
                        loc.total_shoot_days,
                        INITIAL_VERSION,
                    ]
                    for loc in budget.locations
                ],
                column_names=[
                    "location_id",
                    "project_id",
                    "location_name",
                    "daily_rate_usd",
                    "company_move_penalty_usd",
                    "requires_permit",
                    "max_cast_capacity",
                    "total_shoot_days",
                    "version",
                ],
            )
            written["locations"] = len(budget.locations)

        if constraints is not None:
            client.insert(
                "production_constraints",
                [
                    [
                        PROJECT_ID,
                        constraints.total_budget_usd,
                        constraints.max_filming_days,
                        constraints.max_total_crew,
                        constraints.max_primary_locations,
                        INITIAL_VERSION,
                    ]
                ],
                column_names=[
                    "project_id",
                    "total_budget_usd",
                    "max_filming_days",
                    "max_total_crew",
                    "max_primary_locations",
                    "version",
                ],
            )
            written["constraints"] = 1

    except Exception as e:
        return {"status": "error", "message": str(e), "partial_write": written}

    _clear_staged(tool_context)
    return {"status": "success", "project_id": PROJECT_ID, "written": written}
