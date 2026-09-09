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

"""Read-only story-graph API — a live view of `script_nodes`/`script_edges`
for a frontend graph visualization, separate from the suggestions queue
(app/app_utils/suggestions_api.py). Same house style as that file: plain
backend code, no LLM/agent turn involved, reading straight off the shared
ClickHouse write client (app/app_utils/clickhouse_client.py).

Two routes:
  - `GET /graph` — a one-off snapshot,
    `{"nodes": [...], "edges": [...], "locations": [...], "constraints": {...}|null}`.
  - `GET /graph/stream` — a Server-Sent-Events connection. Sends that same
    snapshot immediately on connect, then sends it again, in full, every
    time the graph actually changes — so a client just re-renders whatever
    arrives instead of computing its own diffs.

"Every time the graph actually changes" currently means exactly one thing:
apply_graph_correction (app/tools.py) succeeding, which right now only ever
happens from suggestions_api.py's approve branch — that's the only writer
to script_nodes/script_edges (see app/tools.py's module docstring). That
route calls `notify_graph_changed()`, below, right after a successful
write. If another write path is ever added, it needs to call this too, or
`GET /graph/stream` clients silently stop seeing updates.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from fastapi import Query
from fastapi.responses import StreamingResponse

from app.app_utils.clickhouse_client import get_clickhouse_write_client
from app.tools import PROJECT_ID

if TYPE_CHECKING:
    from fastapi import FastAPI

# One asyncio.Queue per currently-connected `/graph/stream` client — how a
# plain, synchronous ClickHouse write (from an unrelated request) gets its
# result pushed out to every open SSE connection. Session-local, in-process
# only: fine for one server process; wouldn't fan out across multiple
# instances without something like Redis pub/sub, out of scope here.
_subscribers: set[asyncio.Queue] = set()


def _rows_to_dicts(result) -> list[dict]:
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def _get_current_graph(client, project_id: str) -> dict:
    """Current production state — `script_nodes`/`script_edges` plus the
    costing tables they join against, all `FINAL` (so each row resolves to
    its latest version, not every physical version ReplacingMergeTree
    hasn't merged away yet), tombstoned (`is_deleted`) rows excluded.

    Locations and constraints ride along with the graph rather than sitting
    behind their own endpoint because a frontend can't render a scene's cost
    without them (a node only carries a location_id), and because the
    `/graph/stream` snapshot has to stay internally consistent — a graph
    push that changed a scene's location would otherwise arrive with stale
    prices attached."""
    nodes = client.query(
        "SELECT * FROM script_nodes FINAL WHERE project_id = %(pid)s AND is_deleted = false ORDER BY node_id",
        parameters={"pid": project_id},
    )
    edges = client.query(
        "SELECT * FROM script_edges FINAL WHERE project_id = %(pid)s AND is_deleted = false "
        "ORDER BY parent_node_id, child_node_id",
        parameters={"pid": project_id},
    )
    locations = client.query(
        "SELECT * FROM production_locations FINAL WHERE project_id = %(pid)s ORDER BY location_id",
        parameters={"pid": project_id},
    )
    # At most one row in this POC; ORDER BY/LIMIT keeps it deterministic if
    # an older version hasn't been merged away yet.
    constraints = client.query(
        "SELECT * FROM production_constraints FINAL WHERE project_id = %(pid)s "
        "ORDER BY updated_at DESC LIMIT 1",
        parameters={"pid": project_id},
    )
    constraint_rows = _rows_to_dicts(constraints)
    return {
        "nodes": _rows_to_dicts(nodes),
        "edges": _rows_to_dicts(edges),
        "locations": _rows_to_dicts(locations),
        "constraints": constraint_rows[0] if constraint_rows else None,
    }


async def notify_graph_changed() -> None:
    """Call once, right after any write that changes script_nodes/script_edges
    lands successfully. Re-reads the full current graph (one query) and
    pushes it to every connected `/graph/stream` client — a fresh read
    rather than trying to patch clients' state from just the
    node_versions/edge_versions apply_graph_correction returns, since that's
    a partial diff and `/graph`'s contract is always a full, consistent
    snapshot. A no-op if nobody's currently connected."""
    if not _subscribers:
        return
    client = get_clickhouse_write_client()
    graph = _get_current_graph(client, PROJECT_ID)
    for queue in list(_subscribers):
        queue.put_nowait(graph)


async def _graph_event_stream(project_id: str):
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.add(queue)
    try:
        client = get_clickhouse_write_client()
        yield f"data: {json.dumps(_get_current_graph(client, project_id), default=str)}\n\n"
        while True:
            # A short timeout, not a real deadline: just so a long quiet
            # stretch (nothing changing) still sends *something*, so
            # browsers/proxies don't decide the connection is dead and
            # close it out from under us.
            try:
                graph = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps(graph, default=str)}\n\n"
    finally:
        _subscribers.discard(queue)


async def attach_graph_routes(app: "FastAPI") -> None:
    """Registers the graph routes on `app`. Call once, from a FastAPI
    `lifespan`, the same way `attach_suggestions_routes` already is."""

    @app.get("/graph")
    async def get_graph(
        project_id: str = Query(PROJECT_ID, description="Defaults to '01' — this POC only tracks one production."),
    ):
        client = get_clickhouse_write_client()
        return _get_current_graph(client, project_id)

    @app.get("/graph/stream")
    async def stream_graph(
        project_id: str = Query(PROJECT_ID, description="Defaults to '01' — this POC only tracks one production."),
    ):
        return StreamingResponse(
            _graph_event_stream(project_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
