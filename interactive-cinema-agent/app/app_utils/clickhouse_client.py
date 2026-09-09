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

"""Shared ClickHouse write connection (see docs/HITL_SUGGESTIONS_PLAN.md,
MVP steps 5 and 7).

Was previously a set of private helpers inside app/tools.py, used only by
parser_agent's write tools. Promoted here because app/app_utils/
suggestions_api.py (the plain FastAPI routes for listing/responding to
suggestions — no LLM involved) needs the exact same connection and the
exact same "flip a suggestion's status" logic, and reaching into another
module's private (`_`-prefixed) helpers isn't clean.

This is a plain, unauthenticated-by-the-model connection — nothing here is
an ADK tool. app/tools.py's clickhouse_tools (the read-only MCP toolset)
still lives in app/tools.py; it's what agents actually call.
"""

import json
import os

CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "your-instance.clickhouse.cloud")
CLICKHOUSE_PORT = os.getenv("CLICKHOUSE_PORT", "8443")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "default")

_write_client = None


def get_clickhouse_write_client():
    """Lazily creates a single reusable clickhouse-connect client, shared by
    every writer in the app: parser_agent's ingestion tools, apply_graph_correction/
    record_suggestion (app/tools.py), and the suggestions REST routes
    (app/app_utils/suggestions_api.py)."""
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


def _read_current_suggestion(client, suggestion_id: str) -> dict | None:
    """Reads a suggestion row as a plain dict (FINAL, so the latest
    version), or None if suggestion_id doesn't exist. Shared by every
    function below that needs to read-modify-write one field of a
    suggestion without disturbing the rest."""
    result = client.query(
        "SELECT * FROM suggestions FINAL WHERE suggestion_id = %(sid)s",
        parameters={"sid": suggestion_id},
    )
    if not result.result_rows:
        return None
    return dict(zip(result.column_names, result.result_rows[0]))


def _write_new_suggestion_version(client, row: dict) -> None:
    """Inserts `row` as a new version of a suggestion — the same
    insert-a-new-version discipline as every other write in this app, never
    an ALTER ... UPDATE. `row` should be a dict previously read via
    `_read_current_suggestion`, with one or more fields changed; `version`
    gets bumped and `updated_at` dropped (so ClickHouse's DEFAULT now()
    stamps a fresh timestamp) here, not by the caller."""
    row = dict(row)
    row.pop("updated_at", None)
    row["version"] = row["version"] + 1
    client.insert("suggestions", [list(row.values())], column_names=list(row.keys()))


def mark_suggestion_status(client, suggestion_id: str, status: str) -> bool:
    """Writes a new version of an existing `suggestions` row with `status`
    changed, carrying every other field forward unchanged.

    Returns True if a matching suggestion was found and updated, False if
    suggestion_id doesn't exist (a no-op either way — nothing is written).
    Callers that don't care whether a match existed (e.g.
    apply_graph_correction's optional "mark this suggestion executed" step,
    where suggestion_id may not be tied to a real suggestion at all) can
    ignore the return value; callers that DO care (e.g. the respond API
    route, which should tell a caller "no such suggestion" rather than
    silently succeeding) should check it."""
    row = _read_current_suggestion(client, suggestion_id)
    if row is None:
        return False
    row["status"] = status
    _write_new_suggestion_version(client, row)
    return True


def mark_options_stale(client, suggestion_id: str, options: list[dict]) -> bool:
    """Writes a new version of a suggestion row with `options_json`
    replaced by `options` (a full list of every option, with the specific
    one(s) that need it already flagged `stale: True` by the caller) —
    `status` is carried forward unchanged, since cascade marking is a
    per-option UI hint, not a suggestion-wide status change (see
    docs/HITL_SUGGESTIONS_PLAN.md, "Step 8, revised"). Returns True if the
    suggestion existed and was updated, False otherwise."""
    row = _read_current_suggestion(client, suggestion_id)
    if row is None:
        return False
    row["options_json"] = json.dumps(options)
    _write_new_suggestion_version(client, row)
    return True
