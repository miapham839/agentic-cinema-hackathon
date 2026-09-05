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
"""

import os

from google.adk.tools import ToolContext
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

from app.schemas import BudgetExtraction, ProductionConstraints, ScriptExtraction

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


def insert_script_extraction(extraction: ScriptExtraction, tool_context: ToolContext) -> dict:
    """Writes a structured script extraction (nodes + edges) to ClickHouse.

    Call this exactly once, after you have extracted ALL nodes and edges from
    the uploaded script PDF into the ScriptExtraction shape. Every row is
    written as version 1 (this is the initial ingestion of the document).

    Args:
        extraction: The full set of nodes and edges parsed from the script PDF.

    Returns:
        dict with status, and counts of rows written to script_nodes / script_edges.
    """
    if not extraction.nodes and not extraction.edges:
        return {"status": "error", "message": "extraction has no nodes and no edges"}

    try:
        client = _get_clickhouse_write_client()

        if extraction.nodes:
            node_columns = [
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
            ]
            node_rows = [
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
                for n in extraction.nodes
            ]
            client.insert("script_nodes", node_rows, column_names=node_columns)

        if extraction.edges:
            edge_columns = [
                "parent_node_id",
                "project_id",
                "child_node_id",
                "choice_text",
                "required_state",
                "version",
            ]
            edge_rows = [
                [
                    e.parent_node_id,
                    PROJECT_ID,
                    e.child_node_id,
                    e.choice_text,
                    e.required_state,
                    INITIAL_VERSION,
                ]
                for e in extraction.edges
            ]
            client.insert("script_edges", edge_rows, column_names=edge_columns)

    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "success",
        "nodes_inserted": len(extraction.nodes),
        "edges_inserted": len(extraction.edges),
    }


def insert_budget_extraction(extraction: BudgetExtraction, tool_context: ToolContext) -> dict:
    """Writes a structured budget/locations extraction to ClickHouse.

    Call this exactly once, after you have extracted ALL locations from the
    uploaded budget PDF into the BudgetExtraction shape. Every row is written
    as version 1 (this is the initial ingestion of the document).

    Args:
        extraction: The full set of locations parsed from the budget PDF.

    Returns:
        dict with status, and count of rows written to production_locations.
    """
    if not extraction.locations:
        return {"status": "error", "message": "extraction has no locations"}

    try:
        client = _get_clickhouse_write_client()

        columns = [
            "location_id",
            "project_id",
            "location_name",
            "daily_rate_usd",
            "company_move_penalty_usd",
            "requires_permit",
            "max_cast_capacity",
            "total_shoot_days",
            "version",
        ]
        rows = [
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
            for loc in extraction.locations
        ]
        client.insert("production_locations", rows, column_names=columns)

    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {"status": "success", "locations_inserted": len(extraction.locations)}


def insert_production_constraints(constraints: ProductionConstraints, tool_context: ToolContext) -> dict:
    """Writes the production's overall budget/schedule/crew constraints to ClickHouse.

    Call this once, only if the uploaded budget document states an overall
    approved budget, filming-day limit, crew-size limit, or location-count
    limit for the production. Written as version 1 (this is the initial
    ingestion of the document).

    Args:
        constraints: The production constraints parsed from the budget PDF.

    Returns:
        dict with status, and confirmation of the row written to production_constraints.
    """
    try:
        client = _get_clickhouse_write_client()

        columns = [
            "project_id",
            "total_budget_usd",
            "max_filming_days",
            "max_total_crew",
            "max_primary_locations",
            "version",
        ]
        row = [
            PROJECT_ID,
            constraints.total_budget_usd,
            constraints.max_filming_days,
            constraints.max_total_crew,
            constraints.max_primary_locations,
            INITIAL_VERSION,
        ]
        client.insert("production_constraints", [row], column_names=columns)

    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {"status": "success", "project_id": PROJECT_ID}
