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

"""Suggestions REST API (see docs/HITL_SUGGESTIONS_PLAN.md section 4 / MVP
steps 6-8) — lets a frontend (or Postman, for now) list and respond to
suggestions `record_suggestion` (app/tools.py) persisted, without going
through `adk web` chat.

MVP step 6 built the list route. Step 7 added the respond route's dismiss/
manual branch — a pure status-flip write, no agent turn involved, since
neither action needs any domain reasoning or a graph write. Step 8 (this
file, now) adds the approve branch, and it's a plain, synchronous status-flip
write too: no agent turn, no ADK session involved at all. Picking an option
from an already-recorded suggestion needs no LLM reasoning — the option's
full GraphCorrection was already decided back when graph_auditor_agent or
budget_agent called record_suggestion (see app/tools.py); approving it just
means (1) re-checking that option's expected_versions against current
reality (check_staleness) and (2) calling apply_graph_correction directly,
in-process, with that option's own correction. See docs/HITL_SUGGESTIONS_PLAN.md,
"Step 8, revised," for why the original design (resuming the exact ADK
session that raised the suggestion via `runner.run_async` + `state_delta`)
was replaced — the short version: a suggestion is meant to be actionable
from ANY session (or no session — a future non-chat frontend), not just the
one that happened to raise it, and there's no domain reasoning happening on
approval that would need an LLM in the loop at all. `custom` (a free-text
response instead of picking one of the offered options) stays a clear "not
implemented yet" 501 — the same scope cut as the free-text EDIT REQUEST mode
elsewhere in this MVP; both need the same kind of open-ended interpretation
this plan deliberately deferred.

Reads go straight through `clickhouse_connect` (the same shared write
client every other writer in this app uses, from
app/app_utils/clickhouse_client.py) rather than an agent tool — there's no
LLM in the loop for a route that's just backend code reading its own table,
so the MCP-toolset/write-tool read/write split (app/tools.py's module
docstring) doesn't apply here. No route lets a client CREATE a suggestion
directly; that only ever happens via record_suggestion, from inside a real
agent turn — same principle as chat messages never being inserted by a UI.
"""

# Lets this file write type hints like `"FastAPI"` (in quotes, further
# down) without actually importing FastAPI at module load time. Only
# matters together with the `if TYPE_CHECKING:` block below.
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, Optional

# Query is FastAPI's way of describing a URL query-string parameter (the
# ?key=value part of a URL); HTTPException is how a route reports an error
# (a non-200 status) instead of just returning normally.
from fastapi import HTTPException, Query

# BaseModel is Pydantic's way of describing the SHAPE of a JSON request
# body — FastAPI reads the incoming POST body, checks it matches this
# shape, and hands you a real Python object instead of a raw dict.
from pydantic import BaseModel

from app.app_utils.clickhouse_client import get_clickhouse_write_client, mark_suggestion_status
from app.app_utils.graph_api import notify_graph_changed
from app.schemas import GraphCorrection
from app.tools import apply_graph_correction, check_staleness

# TYPE_CHECKING is always False when the file actually runs — this import
# only happens for type-checking tools (and humans reading the code), never
# at real runtime. Keeps this file from needing the full FastAPI machinery
# just to describe what type `app` is.
if TYPE_CHECKING:
    from fastapi import FastAPI


class RespondRequest(BaseModel):
    """The JSON body POST /suggestions/{id}/respond expects, e.g.
    {"action": "dismiss"} or {"action": "approve", "option_id": "a"}.
    Literal[...] restricts `action` to exactly these 4 strings — FastAPI
    rejects anything else with a 422 error automatically, before this
    file's own code even runs."""

    action: Literal["dismiss", "manual", "approve", "custom"]
    option_id: Optional[str] = None  # required in practice for "approve" (step 8), unused for the other 3 actions
    custom_text: Optional[str] = None  # required in practice for "custom" (step 8), unused for the other 3 actions


def _suggestion_row_to_dict(column_names: tuple, values: tuple) -> dict:
    """Turns one `suggestions` row into a JSON-friendly dict — same
    zip(column_names, values) -> dict pattern used throughout app/tools.py
    for reshaping a clickhouse_connect query result. options_json is parsed
    back into a real list here so callers get structured options, not an
    escaped JSON string to parse themselves."""
    # zip pairs up column names with their values position-by-position;
    # dict(...) turns those pairs into a name -> value mapping, e.g.
    # {"suggestion_id": "sugg_abc", "status": "pending", ...}.
    row = dict(zip(column_names, values))
    # options_json is stored in ClickHouse as one big text blob (see
    # app/tools.py's record_suggestion). json.loads turns that text back
    # into a real Python list of option objects; .pop removes the raw
    # "options_json" key from the dict and returns its value in one step,
    # and the result gets stored under a new, friendlier "options" key.
    row["options"] = json.loads(row.pop("options_json"))
    return row


# This function is called once, at server startup, from app/fast_api_app.py's
# lifespan() — see that file's comments. It doesn't run the server itself;
# it just adds route(s) to the `app` object that's passed in.
async def attach_suggestions_routes(app: "FastAPI") -> None:
    """Registers the suggestions routes on `app`. Call once, from a FastAPI
    `lifespan`, the same way `attach_a2a_routes` is already called in
    app/fast_api_app.py.

    No `runner` (or any other ADK object) needed — every route here, approve
    included, is plain backend code: ClickHouse reads/writes and a direct
    Python call to apply_graph_correction, never an agent turn."""

    # @app.get("/suggestions") registers this function to run whenever a
    # GET request comes in for that path. Defining it INSIDE
    # attach_suggestions_routes (instead of at module level) is what lets
    # it "close over" `app` and `runner` from the enclosing function.
    @app.get("/suggestions")
    async def list_suggestions(
        # FastAPI reads these straight from the URL's query string, e.g.
        # GET /suggestions?project_id=01&status=pending. Query("01", ...)
        # means "default to '01' if the caller doesn't provide one"; the
        # `description` text shows up in the auto-generated /docs page.
        project_id: str = Query("01", description="Defaults to '01' — this POC only tracks one production."),
        status: Optional[str] = Query(
            None, description="Filter by status (e.g. 'pending'). Omit to list every status."
        ),
    ):
        client = get_clickhouse_write_client()

        # Building the WHERE clause piece by piece, with %(name)s
        # placeholders — clickhouse_connect fills these in safely from the
        # `params` dict below (this avoids ever pasting a raw value
        # straight into the SQL string). Same technique as _next_version
        # in app/tools.py.
        where = "project_id = %(project_id)s"
        params = {"project_id": project_id}
        if status:
            # Only add this half of the filter if the caller actually
            # passed a `status` — otherwise every status is returned.
            where += " AND status = %(status)s"
            params["status"] = status

        # FINAL forces ClickHouse to resolve each suggestion down to its
        # latest version (see mark_suggestion_status below — every status
        # change is a new row, never an edit in place). Newest-updated
        # suggestions come back first.
        result = client.query(
            f"SELECT * FROM suggestions FINAL WHERE {where} ORDER BY updated_at DESC",
            parameters=params,
        )
        # result.column_names / result.result_rows are what
        # clickhouse_connect hands back: column names once, then one tuple
        # of values per row. Turn each row into a dict via the helper above.
        suggestions = [_suggestion_row_to_dict(result.column_names, row) for row in result.result_rows]
        # FastAPI automatically turns a returned dict into a JSON HTTP
        # response — no manual serialization needed.
        return {"suggestions": suggestions}

    @app.post("/suggestions/supersede_open")
    async def supersede_open_suggestions(
        project_id: str = Query("01", description="Defaults to '01' — this POC only tracks one production."),
    ):
        """Retires every still-open suggestion, ahead of a fresh analysis.

        Called by the UI's "Re-run analysis" button immediately before it asks
        the agents to look again. The panel then shows the findings of ONE
        analysis pass instead of two overlapping ones.

        Retires all pending findings, not only the ones the graph has moved
        past. The agents re-derive the whole picture on a re-run, so a
        surviving pending card is just the previous pass's copy of a finding
        that is about to be raised again — that is exactly how duplicate
        cards appeared. A finding that is still real comes straight back.

        `superseded` rather than `dismissed`: nobody rejected these, they were
        replaced by a newer pass. The two read very differently in the panel's
        resolved list, and only `dismissed`/`manual` block a finding from
        being raised again (see _find_open_duplicate in app/tools.py).

        Anything the user already decided — executed, dismissed, manual — is
        left exactly as it is.
        """
        client = get_clickhouse_write_client()
        result = client.query(
            "SELECT suggestion_id FROM suggestions FINAL "
            "WHERE project_id = %(pid)s AND status = 'pending'",
            parameters={"pid": project_id},
        )

        superseded = []
        for (suggestion_id,) in result.result_rows:
            mark_suggestion_status(client, suggestion_id, "superseded")
            superseded.append(suggestion_id)

        return {"status": "success", "superseded": superseded}

    @app.post("/suggestions/{suggestion_id}/respond")
    async def respond_to_suggestion(suggestion_id: str, body: RespondRequest):
        client = get_clickhouse_write_client()

        if body.action in ("dismiss", "manual"):
            # "dismiss" (the action the caller sends) becomes "dismissed"
            # (the status word actually stored) — "manual" needs no such
            # translation, it's spelled the same either way.
            new_status = "dismissed" if body.action == "dismiss" else "manual"
            found = mark_suggestion_status(client, suggestion_id, new_status)
            if not found:
                # 404 Not Found — the standard HTTP status for "nothing
                # exists at the id you asked for". Silently returning
                # success here would be worse: the caller would have no
                # way to tell a typo'd suggestion_id from a real success.
                raise HTTPException(status_code=404, detail=f"No suggestion with id {suggestion_id!r}")
            return {"status": "success", "suggestion_id": suggestion_id, "new_status": new_status}

        if body.action == "custom":
            # A free-text response instead of picking one of the offered
            # options needs the same kind of open-ended interpretation as
            # the free-text EDIT REQUEST mode this MVP deliberately left
            # out — see docs/HITL_SUGGESTIONS_PLAN.md. 501, not 400: this
            # is a real, recognized action, just not supported yet.
            raise HTTPException(status_code=501, detail="action='custom' isn't implemented yet.")

        # Only "approve" reaches here (RespondRequest.action only allows
        # these 4 values, and dismiss/manual/custom are all handled above).
        if not body.option_id:
            raise HTTPException(status_code=400, detail="action='approve' requires option_id.")

        result = client.query(
            "SELECT options_json, status FROM suggestions FINAL WHERE suggestion_id = %(sid)s",
            parameters={"sid": suggestion_id},
        )
        if not result.result_rows:
            raise HTTPException(status_code=404, detail=f"No suggestion with id {suggestion_id!r}")
        options_json, current_status = result.result_rows[0]
        if current_status != "pending":
            # Blocks a double-click/accidental-retry from running
            # apply_graph_correction twice on the same suggestion.
            raise HTTPException(
                status_code=409,
                detail=f"Suggestion {suggestion_id!r} is already {current_status!r}, not pending.",
            )

        options = json.loads(options_json)
        option = next((opt for opt in options if opt["option_id"] == body.option_id), None)
        if option is None:
            raise HTTPException(
                status_code=404,
                detail=f"Suggestion {suggestion_id!r} has no option {body.option_id!r}.",
            )

        # The real safety gate (see check_staleness's docstring, app/tools.py):
        # re-checks THIS option's own expected_versions against current
        # reality right now. Independent of whether cascade marking already
        # flagged this option `stale` in options_json — that flag is only
        # ever a UI hint to review before clicking; this check is what
        # actually decides whether the write is safe to make.
        mismatches = check_staleness(client, option["expected_versions"])
        if mismatches:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This option is stale — the story graph changed underneath it since "
                    "it was proposed. Re-run the audit/analysis to get a fresh suggestion.",
                    "mismatches": mismatches,
                },
            )

        # The option's full correction was already decided back when
        # record_suggestion stored it — approving just replays it, exactly
        # as-is, with no LLM involved. apply_graph_correction itself marks
        # this suggestion 'executed' and cascade-marks any other pending
        # suggestion's overlapping options 'stale' (see app/tools.py); this
        # route doesn't duplicate either of those writes.
        correction = GraphCorrection.model_validate(option["correction"])
        outcome = apply_graph_correction(correction)
        if outcome["status"] != "success":
            raise HTTPException(status_code=500, detail=outcome.get("message", "apply_graph_correction failed"))

        # Pushes the new graph out to every connected GET /graph/stream
        # client (see app/app_utils/graph_api.py) — the only other write
        # path that changes script_nodes/script_edges, so this is the one
        # place that needs to call it.
        await notify_graph_changed()

        return {
            "status": "success",
            "suggestion_id": suggestion_id,
            "option_id": body.option_id,
            "node_versions": outcome["node_versions"],
            "edge_versions": outcome["edge_versions"],
        }
