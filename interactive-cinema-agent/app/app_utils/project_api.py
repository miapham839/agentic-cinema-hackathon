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

"""Project reset — clears the current production so a different screenplay
can be ingested.

This POC tracks exactly one production (PROJECT_ID '01'), so "upload a
different script" and "start a new project" are the same action. Without
this, a second upload lands in one of two bad states: a graph holding two
unrelated stories at once, which makes graph_auditor_agent report a pile of
unreachable scenes that aren't really wrong; or — if the new script reuses
scene ids — rows written at version 1 against scenes already at a higher
version, which ReplacingMergeTree simply keeps, so the upload reports
success and silently changes nothing.

Deliberately NOT an agent tool. It destroys data and takes no judgement, so
it belongs behind an explicit button and a confirmation in the UI, not
behind a model deciding when to reach for it. Same reasoning that keeps
apply_graph_correction unregistered.

Kept out of graph_api.py because that module is the read-only view of the
production and should stay that way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.app_utils.clickhouse_client import get_clickhouse_write_client
from app.app_utils.graph_api import notify_graph_changed

if TYPE_CHECKING:
    from fastapi import FastAPI

# Order doesn't matter — each is truncated independently — but the log
# tables and their materialized-view targets have to go together, or the
# views would refill the targets from rows that no longer exist.
_PROJECT_TABLES = [
    "script_nodes",
    "script_nodes_log",
    "script_edges",
    "script_edges_log",
    "production_locations",
    "production_constraints",
    "suggestions",
]


async def attach_project_routes(app: "FastAPI") -> None:
    """Registers the project routes on `app`. Call once, from a FastAPI
    `lifespan`, the same way attach_graph_routes already is."""

    @app.post("/project/reset")
    async def reset_project():
        """Clears the whole production: story graph, its full version
        history, the costing tables and every suggestion.

        There is no undo. Version history exists to move a scene between
        versions of one production, not to recover a production you
        deliberately replaced.
        """
        client = get_clickhouse_write_client()
        cleared = []
        for table in _PROJECT_TABLES:
            client.command(f"TRUNCATE TABLE IF EXISTS {table}")
            cleared.append(table)

        # Push the now-empty graph so open clients fall back to the upload
        # view instead of showing scenes that no longer exist.
        await notify_graph_changed()
        return {"status": "success", "cleared": cleared}
