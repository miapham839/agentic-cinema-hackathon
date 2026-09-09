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

import json
import uuid

from google.adk.tools import ToolContext
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

from app.app_utils.clickhouse_client import (
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    get_clickhouse_write_client,
    mark_options_stale,
    mark_suggestion_status,
)
from app.schemas import (
    BudgetExtraction,
    GraphCorrection,
    ProductionConstraints,
    ScriptExtraction,
    ScriptNode,
    Suggestion,
)


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
# row it writes gets version=1.
#
# script_nodes/script_edges are NOT written directly (see
# docs/HITL_SUGGESTIONS_PLAN.md section 1): this writes into script_nodes_log /
# script_edges_log instead — plain append-only MergeTree tables, version in
# ORDER BY, nothing ever collapses — and a materialized view on each propagates
# every insert into script_nodes / script_edges automatically. This keeps every
# write to the story graph, initial ingestion included, on one path, so a later
# correction tool (apply_graph_correction, same plan doc) can compute the next
# version number from one place. production_locations/production_constraints
# have no log table and are still written directly — history/rollback was only
# asked for on the story graph, not the budget tables.

INITIAL_VERSION = 1

# Hardcoded until multi-project support exists (see docs/DESIGN_DECISIONS.md
# section 7). parser_agent never sets this itself — it's injected here so
# every row across all four tables agrees, regardless of what the model does.
PROJECT_ID = "01"


def _current_version(client, table: str, key_columns: list[str], key_values: list) -> int:
    """Returns the highest `version` already on record for the row
    identified by key_columns/key_values in `table`, or 0 if no such row
    exists yet. Never trust FINAL here — max() over all physical rows for a
    key is correct regardless of whether a background merge has collapsed
    duplicates yet, which FINAL depends on having happened.

    Two uses: `_next_version` (below) is this + 1, for writing; the
    staleness check (`check_staleness`) compares this directly against a
    snapshot taken earlier, for reading. 0 has a real meaning in both — "no
    row exists (yet)" — not just an arbitrary default."""
    where = " AND ".join(f"{col} = %(v{i})s" for i, col in enumerate(key_columns))
    params = {f"v{i}": v for i, v in enumerate(key_values)}
    result = client.query(f"SELECT max(version) FROM {table} WHERE {where}", parameters=params)
    current_max = result.result_rows[0][0] if result.result_rows else None
    return current_max or 0


def _next_version(client, table: str, key_columns: list[str], key_values: list) -> int:
    """Returns 1 + the highest `version` already on record for the row
    identified by key_columns/key_values in `table`, or 1 if no such row
    exists yet. Used for every version-incrementing write (apply_graph_correction's
    node/edge writes, and suggestion status transitions)."""
    return _current_version(client, table, key_columns, key_values) + 1


def insert_script_extraction(extraction: ScriptExtraction, tool_context: ToolContext) -> dict:
    """Writes a structured script extraction (nodes + edges) to ClickHouse.

    Call this exactly once, after you have extracted ALL nodes and edges from
    the uploaded script PDF into the ScriptExtraction shape. Every row is
    written as version 1 (this is the initial ingestion of the document).

    Args:
        extraction: The full set of nodes and edges parsed from the script PDF.

    Returns:
        dict with status, and counts of rows written to script_nodes_log / script_edges_log
        (which propagate into script_nodes / script_edges via a materialized view).
    """
    if not extraction.nodes and not extraction.edges:
        return {"status": "error", "message": "extraction has no nodes and no edges"}

    try:
        client = get_clickhouse_write_client()

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
            client.insert("script_nodes_log", node_rows, column_names=node_columns)

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
            client.insert("script_edges_log", edge_rows, column_names=edge_columns)

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
        client = get_clickhouse_write_client()

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
        client = get_clickhouse_write_client()

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


# ==============================================================================
# CLICKHOUSE WRITE TOOLS — HITL suggestions/corrections (see
# docs/HITL_SUGGESTIONS_PLAN.md, "Step 8, revised"). apply_graph_correction
# is called two ways: by graph_auditor_agent as an ADK tool (Step 10's
# rollback mode, and any future mode where an agent genuinely needs to
# construct a correction from scratch), and directly, as a plain Python
# function call with tool_context=None, from suggestions_api.py's approve
# branch — approving an already-fully-specified suggestion option needs no
# LLM judgment, just a deterministic replay, so it skips the agent/session
# machinery entirely. tool_context is never read in this function's body
# either way, so both calling conventions work identically.
# ==============================================================================


def apply_graph_correction(correction: GraphCorrection) -> dict:
    """Writes a version-incremented correction to the story graph: one or
    more corrected/new nodes and/or edges. The only function in this whole
    app that can write to script_nodes_log/script_edges_log. Not currently
    registered as any agent's tool — suggestions_api.py's approve branch is
    the only caller, a direct Python call, no agent turn involved (see
    module comment above and docs/HITL_SUGGESTIONS_PLAN.md, "Step 8,
    revised"). Deliberately NOT added to graph_auditor_agent's tools: its
    instruction only covers 2 modes (FULL AUDIT, CONSULTATION), neither of
    which tells the model when to call this — an ungated write tool with no
    instruction scoping its use is a real risk, not just unused surface
    area. Re-add it there only alongside real instruction text (e.g. a
    future Step 10 ROLLBACK mode) that says exactly when the model should
    reach for it.

    Writes ONLY to script_nodes_log/script_edges_log, one row per node/edge,
    each independently versioned (current_max_version(key) + 1, or 1 for a
    brand-new node/edge). The script_nodes_mv/script_edges_mv materialized
    views propagate each row into script_nodes/script_edges automatically,
    as part of the same insert — no second write from this tool.

    If correction.suggestion_id is set: marks that suggestion 'executed'
    once every node/edge write succeeds, then cascade-marks every OTHER
    still-pending suggestion whose options reference one of the same
    node_ids/edge_refs — flagging just those specific options `stale`
    inside their own options_json (not the whole suggestion; see
    mark_overlapping_suggestions_stale below). This does NOT check whether
    THIS correction is itself stale before writing — that's the caller's
    job (see check_staleness below), done once, before calling this, using
    the option's own expected_versions; apply_graph_correction always
    just writes what it's given.

    Args:
        correction: One or more corrected/new nodes and/or edges to write,
            and optionally the suggestion_id this correction fulfills.

    Returns:
        dict with status and the new version written for each node_id / edge.
    """
    if not correction.nodes and not correction.edges:
        return {"status": "error", "message": "correction has no nodes and no edges"}

    try:
        client = get_clickhouse_write_client()
        node_versions = {}
        edge_versions = {}

        for n in correction.nodes:
            version = _next_version(client, "script_nodes_log", ["node_id"], [n.node_id])
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
                "is_deleted",
                "suggestion_id",
                "version",
            ]
            row = [
                n.node_id,
                PROJECT_ID,
                n.title,
                n.location_id,
                n.narrative_text,
                n.characters_present,
                n.state_modifiers,
                n.shoot_days,
                n.num_crew_required,
                n.is_deleted,
                correction.suggestion_id,
                version,
            ]
            client.insert("script_nodes_log", [row], column_names=node_columns)
            node_versions[n.node_id] = version

        for e in correction.edges:
            version = _next_version(
                client,
                "script_edges_log",
                ["parent_node_id", "child_node_id"],
                [e.parent_node_id, e.child_node_id],
            )
            edge_columns = [
                "parent_node_id",
                "project_id",
                "child_node_id",
                "choice_text",
                "required_state",
                "is_deleted",
                "suggestion_id",
                "version",
            ]
            row = [
                e.parent_node_id,
                PROJECT_ID,
                e.child_node_id,
                e.choice_text,
                e.required_state,
                e.is_deleted,
                correction.suggestion_id,
                version,
            ]
            client.insert("script_edges_log", [row], column_names=edge_columns)
            edge_versions[f"{e.parent_node_id}->{e.child_node_id}"] = version

        # Cascade marking runs for EVERY correction, not just ones fulfilling
        # a suggestion. A rollback has no suggestion behind it, and it moves
        # versions exactly like an approval does — leaving it out would let
        # pending suggestions keep showing fresh-looking options that
        # check_staleness would then refuse at the click.
        mark_overlapping_suggestions_stale(
            client,
            node_ids=list(node_versions.keys()),
            edge_refs=list(edge_versions.keys()),
            exclude_suggestion_id=correction.suggestion_id,
        )
        if correction.suggestion_id:
            mark_suggestion_status(client, correction.suggestion_id, "executed")

    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "success",
        "node_versions": node_versions,
        "edge_versions": edge_versions,
    }


def check_staleness(client, expected_versions: dict[str, int]) -> dict[str, int]:
    """Compares an option's `expected_versions` snapshot (node_id or
    'parent->child' edge ref -> version it was written against) to current
    reality. Returns {key: current_version} for every key that no longer
    matches — an empty dict means nothing's stale, safe to apply.

    Deliberately conservative: it can only tell whether the version number
    moved, not whether whatever changed actually matters to this specific
    correction (a narrative_text typo fix bumps the version exactly the
    same as a genuinely conflicting change would). See docs/HITL_SUGGESTIONS_PLAN.md,
    "Step 8, revised," for why that's an accepted trade-off, not a bug —
    the miss only ever goes in the safe direction (occasionally blocking a
    fix that was still fine), never the unsafe one (never applies a fix
    that's genuinely out of date)."""
    mismatches = {}
    for key, expected in expected_versions.items():
        if "->" in key:
            parent, child = key.split("->", 1)
            current = _current_version(client, "script_edges_log", ["parent_node_id", "child_node_id"], [parent, child])
        else:
            current = _current_version(client, "script_nodes_log", ["node_id"], [key])
        if current != expected:
            mismatches[key] = current
    return mismatches


def mark_overlapping_suggestions_stale(
    client, node_ids: list[str], edge_refs: list[str], exclude_suggestion_id: str
) -> list[str]:
    """After a correction lands, finds every OTHER still-pending suggestion
    whose affected_node_ids/affected_edge_refs overlap with what was just
    written, and flags just the SPECIFIC option(s) within it whose own
    expected_versions reference one of those keys as `stale: True` —
    inside that option's own entry in options_json, not as a change to the
    suggestion's `status` (a suggestion can have other, still-valid
    options; see docs/HITL_SUGGESTIONS_PLAN.md, "Step 8, revised").

    Purely advisory — a UI hint that an option may need re-review before
    someone clicks it. Never the actual safety gate: check_staleness,
    re-run fresh at approval time on whichever specific option was chosen,
    is what actually prevents a bad write, regardless of whether this
    function ran, or what it found.

    Uses affected_node_ids/affected_edge_refs (top-level, fast array
    columns) to find CANDIDATE suggestions via one `hasAny` query, then
    checks each candidate's options' own expected_versions precisely — the
    top-level columns narrow the search, expected_versions decides the
    actual per-option flag.

    Returns the suggestion_ids that got at least one option flagged."""
    if not node_ids and not edge_refs:
        return []

    result = client.query(
        """
        SELECT suggestion_id, options_json FROM suggestions FINAL
        WHERE status = 'pending' AND suggestion_id != %(exclude)s
          AND (hasAny(affected_node_ids, %(node_ids)s) OR hasAny(affected_edge_refs, %(edge_refs)s))
        """,
        parameters={"exclude": exclude_suggestion_id or "", "node_ids": node_ids, "edge_refs": edge_refs},
    )

    changed_keys = set(node_ids) | set(edge_refs)
    flagged = []
    for suggestion_id, options_json in result.result_rows:
        options = json.loads(options_json)
        touched = False
        for opt in options:
            if opt.get("stale"):
                continue
            if set(opt.get("expected_versions", {}).keys()) & changed_keys:
                opt["stale"] = True
                touched = True
        if touched:
            mark_options_stale(client, suggestion_id, options)
            flagged.append(suggestion_id)
    return flagged


# A suggestion is blocked as a repeat only against these statuses.
# 'superseded' and 'executed' are deliberately absent: a superseded finding
# was retired precisely so a fresh pass could raise it again, and an executed
# one describes a graph that has since changed.
_BLOCKING_STATUSES = ("pending", "dismissed", "manual")


def _find_open_duplicate(client, category: str, keys: set[str]) -> tuple[str, str] | None:
    """Finds an existing suggestion that is the same finding as this one.

    "Same finding" = same category, and it touches at least one of the same
    scenes or choices. Overlap rather than an exact set match on purpose: two
    passes over the same graph word their options differently and may offer
    two fixes one time and three the next, so an exact match would let obvious
    repeats straight through.

    Returns (suggestion_id, status) of the first match, or None.
    """
    if not keys:
        # Nothing to match on. A finding with no affected scene or choice
        # can't be compared, so let it through rather than guess.
        return None

    rows = client.query(
        "SELECT suggestion_id, status, affected_node_ids, affected_edge_refs "
        "FROM suggestions FINAL "
        "WHERE project_id = %(pid)s AND category = %(cat)s AND status IN %(st)s",
        parameters={"pid": PROJECT_ID, "cat": category, "st": _BLOCKING_STATUSES},
    ).result_rows

    for suggestion_id, status, node_ids, edge_refs in rows:
        if keys & (set(node_ids or []) | set(edge_refs or [])):
            return suggestion_id, status
    return None


def record_suggestion(suggestion: Suggestion, tool_context: ToolContext) -> dict:
    """Persists a finding as an actionable suggestion, instead of only
    stating it in chat text. Held by both graph_auditor_agent and
    budget_agent — either can propose a fix. Neither executes one itself:
    approval is handled entirely by deterministic backend code (see
    app/app_utils/suggestions_api.py's approve branch), regardless of which
    agent proposed the suggestion being approved.

    suggestion_id, proposing_agent, session_id, user_id, status,
    expected_versions (per option), and affected_node_ids/affected_edge_refs
    are all injected/computed here from context and from the options'
    own corrections, never left for the model to supply — same principle
    as PROJECT_ID/INITIAL_VERSION on the ingestion tools above, so a prompt
    bug can't spoof which agent proposed something, and so the staleness
    check and cascade marking can never drift from what a correction
    actually contains (see docs/HITL_SUGGESTIONS_PLAN.md, "Step 8, revised").
    user_id is captured alongside session_id for traceability (which
    conversation raised this) — approval itself no longer needs either;
    see the suggestions API's approve branch.

    Args:
        suggestion: The finding and its 1-3 concrete fix options.

    Returns:
        dict with status. 'success' plus the new suggestion_id when the
        finding was recorded. 'duplicate' when it is already open in the
        panel, or 'already_handled' when the user dismissed it or took it on
        themselves — both mean nothing was written, both are normal, and
        neither is an error to retry.
    """
    try:
        client = get_clickhouse_write_client()
        suggestion_id = str(uuid.uuid4())

        all_node_ids: set[str] = set()
        all_edge_refs: set[str] = set()

        for opt in suggestion.options:
            # Backfill here, in code, rather than relying on the model to
            # set this later at execution time — GraphCorrection.suggestion_id
            # can't be known until this ID exists, so the model has no way
            # to fill it in itself; doing it deterministically now means
            # every option's stored correction is already self-identifying
            # by the time it's read back out of options_json.
            opt.correction.suggestion_id = suggestion_id

            # expected_versions and affected_node_ids/affected_edge_refs
            # both derive from the SAME source — this option's own
            # correction — computed once, here, so the staleness check
            # (keyed on expected_versions) and cascade marking (keyed on
            # affected_node_ids/affected_edge_refs) can never disagree
            # about what this option actually touches.
            expected_versions: dict[str, int] = {}
            for n in opt.correction.nodes:
                expected_versions[n.node_id] = _current_version(client, "script_nodes_log", ["node_id"], [n.node_id])
                all_node_ids.add(n.node_id)
            for e in opt.correction.edges:
                edge_ref = f"{e.parent_node_id}->{e.child_node_id}"
                expected_versions[edge_ref] = _current_version(
                    client, "script_edges_log", ["parent_node_id", "child_node_id"], [e.parent_node_id, e.child_node_id]
                )
                all_edge_refs.add(edge_ref)
            opt.expected_versions = expected_versions
            opt.stale = False

        # Don't raise the same finding twice. An analysis re-run re-derives
        # the whole picture, so without this every re-run stacked another
        # copy of every finding still open in the panel. Also honours a
        # decision the user already made: a finding they dismissed or took
        # on themselves is not raised again.
        duplicate = _find_open_duplicate(client, suggestion.category, all_node_ids | all_edge_refs)
        if duplicate:
            existing_id, existing_status = duplicate
            if existing_status == "pending":
                return {
                    "status": "duplicate",
                    "suggestion_id": existing_id,
                    "message": (
                        "Already recorded and still open in the suggestions panel. "
                        "Nothing was written. This is normal on a re-run — do not "
                        "retry, and do not count it as a new finding."
                    ),
                }
            return {
                "status": "already_handled",
                "suggestion_id": existing_id,
                "message": (
                    f"The user already marked this finding {existing_status!r}, so it was "
                    "not raised again. Nothing was written. Do not retry. Mention it only "
                    "as a count of findings the user is already handling."
                ),
            }

        columns = [
            "suggestion_id",
            "project_id",
            "session_id",
            "user_id",
            "proposing_agent",
            "category",
            "summary",
            "detail",
            "affected_node_ids",
            "affected_edge_refs",
            "options_json",
            "status",
            "version",
        ]
        row = [
            suggestion_id,
            PROJECT_ID,
            tool_context.session.id,
            tool_context.user_id,
            tool_context.agent_name,
            suggestion.category,
            suggestion.summary,
            suggestion.detail,
            list(all_node_ids),
            list(all_edge_refs),
            json.dumps([opt.model_dump() for opt in suggestion.options]),
            "pending",
            INITIAL_VERSION,
        ]
        client.insert("suggestions", [row], column_names=columns)

    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {"status": "success", "suggestion_id": suggestion_id}


# ==============================================================================
# ROLLBACK — restore one scene to an earlier version, behind a human
# confirmation. See docs/HITL_SUGGESTIONS_PLAN.md, Step 10.
# ==============================================================================


def _location_name(client, location_id: str) -> str:
    """The human name of a location ("Platform 9"), falling back to the id.

    Everything the user reads should use this, not the raw location_id:
    'loc_the_signal_box' is a join key, and putting it in a confirmation
    prompt or a version list makes the user decode our schema to answer a
    question about their own film.
    """
    if not location_id:
        return "no location set"
    rows = client.query(
        "SELECT location_name FROM production_locations FINAL WHERE location_id = %(id)s",
        parameters={"id": location_id},
    ).result_rows
    return rows[0][0] if rows and rows[0][0] else location_id


def rollback_scene(
    node_id: str,
    target_version: int,
    expected_current_version: int,
    tool_context: ToolContext,
) -> dict:
    """Restores one scene to an earlier version of itself, after the user
    confirms it in chat.

    Nothing is rewound: the old row is written FORWARD as the next version,
    exactly like any other correction, so the full history stays intact and
    a rollback can itself be rolled back.

    This is the one write tool an agent holds, and it is safe to hold only
    because it cannot act alone. The first call never writes — it asks
    tool_context.request_confirmation() and returns, which pauses the whole
    turn until a human answers in the UI. ADK re-runs this function on
    resume with tool_context.tool_confirmation populated; only a confirmed
    one reaches apply_graph_correction. Compare apply_graph_correction
    itself, which stays unregistered precisely because it has no such gate.

    expected_current_version is the version YOU read before calling. It is
    checked twice — once before asking, once after the human confirms — so a
    scene that moved while the confirmation sat open is refused instead of
    silently overwriting whatever landed in between. This is the same
    conservative version check approving a suggestion goes through
    (check_staleness), applied to a rollback.

    Args:
        node_id: The scene to restore. Never changes; it is the stable key.
        target_version: The version to restore the scene's content from.
        expected_current_version: The scene's current version as you last
            read it, used to detect a change underneath you.

    Returns:
        dict with status: 'awaiting_confirmation' on the first call,
        'cancelled' if the user declined, 'error' if it cannot safely
        proceed, or 'success' with the new version written.
    """
    try:
        client = get_clickhouse_write_client()
        current = _current_version(client, "script_nodes_log", ["node_id"], [node_id])

        if current == 0:
            return {"status": "error", "message": f"No scene with node_id {node_id!r}."}
        if target_version < 1 or target_version >= current:
            return {
                "status": "error",
                "message": (
                    f"Scene {node_id!r} is at version {current}. Pick a target "
                    f"version between 1 and {current - 1}."
                ),
            }

        result = client.query(
            "SELECT title, location_id, narrative_text, characters_present, "
            "state_modifiers, shoot_days, num_crew_required, is_deleted "
            "FROM script_nodes_log WHERE node_id = %(id)s AND version = %(v)s",
            parameters={"id": node_id, "v": target_version},
        )
        if not result.result_rows:
            return {
                "status": "error",
                "message": f"Scene {node_id!r} has no version {target_version} on record.",
            }
        row = dict(zip(result.column_names, result.result_rows[0]))

        # First call: never writes. Describe exactly what would change, then
        # hand the decision to the user and stop.
        if not tool_context.tool_confirmation:
            if expected_current_version != current:
                return {
                    "status": "error",
                    "message": (
                        f"Scene {node_id!r} is actually at version {current}, not "
                        f"{expected_current_version}. Re-read it and call again."
                    ),
                }
            current_row = client.query(
                "SELECT title, location_id FROM script_nodes_log "
                "WHERE node_id = %(id)s AND version = %(v)s",
                parameters={"id": node_id, "v": current},
            )
            now = dict(zip(current_row.column_names, current_row.result_rows[0])) if current_row.result_rows else {}
            # Names, never ids: this text is read by a human deciding
            # whether to undo something, so it has to describe their film,
            # not our join keys.
            changes = []
            if now.get("title") != row["title"]:
                changes.append(f'the title back to "{row["title"]}"')
            if now.get("location_id") != row["location_id"]:
                changes.append(
                    f"the location back to {_location_name(client, row['location_id'])} "
                    f"(currently {_location_name(client, now.get('location_id', ''))})"
                )
            detail = " and ".join(changes) if changes else "the scene's details back to that version"

            tool_context.request_confirmation(
                hint=(
                    f'Restore "{now.get("title") or row["title"]}" from version {current} '
                    f"back to version {target_version}?\n\n"
                    f"This puts {detail}.\n\n"
                    f"It is saved as version {current + 1}, so nothing is lost — "
                    f"you can undo this the same way."
                ),
            )
            # Without this the model narrates the placeholder return below as
            # if it were the outcome.
            tool_context.actions.skip_summarization = True
            return {"status": "awaiting_confirmation", "node_id": node_id}

        if not tool_context.tool_confirmation.confirmed:
            return {"status": "cancelled", "message": "The user declined the rollback."}

        # Confirmed. Re-check: the scene may have moved while the
        # confirmation was open.
        if current != expected_current_version:
            return {
                "status": "error",
                "message": (
                    f"Scene {node_id!r} changed while you were confirming — it is "
                    f"now at version {current}, not {expected_current_version}. "
                    "Nothing was written. Re-read it and try again."
                ),
            }

        correction = GraphCorrection(
            nodes=[
                ScriptNode(
                    node_id=node_id,
                    title=row["title"],
                    location_id=row["location_id"],
                    narrative_text=row["narrative_text"],
                    characters_present=list(row["characters_present"] or []),
                    state_modifiers=dict(row["state_modifiers"] or {}),
                    shoot_days=row["shoot_days"],
                    num_crew_required=row["num_crew_required"],
                    is_deleted=bool(row["is_deleted"]),
                )
            ],
        )
        outcome = apply_graph_correction(correction)
        if outcome["status"] != "success":
            return outcome

        return {
            "status": "success",
            "node_id": node_id,
            "restored_from_version": target_version,
            "new_version": outcome["node_versions"].get(node_id),
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


def list_scene_versions(scene: str, tool_context: ToolContext) -> dict:
    """Lists every recorded version of one scene, oldest first.

    Read-only, and the natural first step before any rollback: it reports
    the version numbers rollback_scene will accept, what changed at each
    one, and where each came from. Nothing is ever deleted from
    script_nodes_log, so this is the scene's complete history.

    `scene` may be the node_id or the title the user actually said ("The
    Signal Box"), matched case-insensitively — the user won't know the id.
    If a title matches more than one scene, no history is returned and the
    candidates come back instead, so you can ask which they meant rather
    than guessing.

    Args:
        scene: node_id, or the scene's title as the user said it.

    Returns:
        dict with status and, on success, node_id, title, current_version
        and `versions` — each with version, title, location (the location's
        NAME, not its id), when, came_from and is_current. Everything meant
        for the user is already in plain words here; don't add ids to it.
    """
    try:
        client = get_clickhouse_write_client()

        matches = client.query(
            "SELECT node_id, title FROM script_nodes FINAL "
            "WHERE is_deleted = false AND (node_id = %(s)s OR lower(title) = lower(%(s)s))",
            parameters={"s": scene},
        ).result_rows

        if not matches:
            return {
                "status": "error",
                "message": (
                    f"No scene matches {scene!r}. Look it up in script_nodes FINAL "
                    "and try the exact title or node_id."
                ),
            }
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "message": f"{scene!r} matches more than one scene — ask which one they mean.",
                "candidates": [{"node_id": n, "title": t} for n, t in matches],
            }

        node_id, title = matches[0]
        rows = client.query(
            "SELECT version, title, location_id, updated_at, suggestion_id "
            "FROM script_nodes_log WHERE node_id = %(n)s ORDER BY version",
            parameters={"n": node_id},
        ).result_rows

        current_version = rows[-1][0] if rows else 0

        versions = []
        for version, v_title, location_id, updated_at, suggestion_id in rows:
            if suggestion_id:
                # The id itself is a UUID with no meaning to the user, and
                # the suggestion may well be gone from the panel by now.
                source = "a suggestion you approved"
            elif version == 1:
                source = "the original script"
            else:
                source = "a rollback you confirmed"
            versions.append(
                {
                    "version": version,
                    "title": v_title,
                    # location_name, not location_id: the user reads this.
                    "location": _location_name(client, location_id),
                    "when": updated_at.strftime("%-d %b %Y at %H:%M"),
                    "came_from": source,
                    "is_current": version == current_version,
                }
            )

        return {
            "status": "success",
            "node_id": node_id,
            "title": title,
            "current_version": current_version,
            "versions": versions,
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
