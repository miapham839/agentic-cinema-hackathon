# BranchArchitect — Agent Backend

> **This is the backend only.** The user interface lives in a separate
> repository:
> **[agentic-cinema-hackathon-ui](https://github.com/miapham839/agentic-cinema-hackathon-ui)**.
> Read that repo for anything to do with the story graph view, the budget
> panels, the chat, or the suggestion cards. Nothing in this repo renders a
> pixel; it serves JSON and a server-sent event stream.

The multi-agent backend for BranchArchitect. It ingests a screenplay and a
production budget, builds a versioned story graph in ClickHouse, audits that
graph for logic problems, prices out the shoot, and serves all of it to the
frontend over HTTP.

Built on [Google ADK](https://adk.dev). Four agents: a supervisor that routes,
and three specialists that parse, audit and cost.

---

## Prerequisites

- **Python 3.11–3.13** and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**
- **A ClickHouse database.** [ClickHouse Cloud](https://clickhouse.com/cloud)
  works; the tables are listed under *Database setup* below.
- **Model access**, either Vertex AI (`gcloud auth application-default login`)
  or a Google AI Studio API key.

---

## Setup

```bash
uv sync
cp .env.example .env
```

Fill in `.env`. Every variable is documented inline there; the ones you cannot
skip are `GOOGLE_CLOUD_PROJECT` (or `GEMINI_API_KEY`) and the five
`CLICKHOUSE_*` values.

### Database setup

The app does not create its own tables. Seven tables and two materialized
views need to exist before the first run:

| Table | Engine | Holds |
|---|---|---|
| `script_nodes`, `script_edges` | `ReplacingMergeTree(version)`, `ORDER BY` the id | The current story graph. Always read with `FINAL`, since old versions are only collapsed by background merges. |
| `script_nodes_log`, `script_edges_log` | `MergeTree`, `version` in the `ORDER BY` | Append-only history. Nothing is ever collapsed here, which is what makes version listing and rollback possible. |
| `production_locations`, `production_constraints` | `ReplacingMergeTree(version)` | Per-location day rates and move penalties; the approved budget cap. |
| `suggestions` | `ReplacingMergeTree(version)` | Findings from the audit and budget agents, with their fix options. Every status change is a new versioned row. |

Two materialized views (`script_nodes_mv`, `script_edges_mv`) connect the log
tables to the current-state tables: `CREATE MATERIALIZED VIEW ... TO
script_nodes AS SELECT ... FROM script_nodes_log`. Every write inserts one row
into a log table, and ClickHouse propagates it in the same insert, so there is
exactly one write path into the graph.

---

## Run it

```bash
uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8000
```

**Wait about 20 seconds before the first request.** On startup the app spawns
the `mcp-clickhouse` MCP server as a subprocess, and a request that arrives
before it is ready will fail.

Then start the frontend (see its repo) and open `http://localhost:8080`.

Quick check that the backend is alive and can reach ClickHouse:

```bash
curl "http://127.0.0.1:8000/graph?project_id=01"
```

JSON means it works. A 500 usually means the `CLICKHOUSE_*` values are wrong.
Those are read once at import, so a typo bakes in a placeholder for the life
of the process; fix `.env` and restart rather than expecting a retry to
recover.

### Other ways to run it

```bash
uv run adk web          # ADK's own chat UI, no frontend needed
uv run pytest tests/    # integration tests (these call the model)
agents-cli eval run     # evaluate against tests/eval/datasets/
```

---

## Demo fixtures

`mock_data/generate_demo_pdfs.py` generates the "Last Train" screenplay and
budget used throughout the demo. They are deliberately built to trigger every
feature in one pass: two genuinely ambiguous locations that force a clarifying
question, a dead end and an unreachable scene that produce overlapping fixes,
and a shoot that comes in 58% over its cap.

```bash
uv run --with fpdf2 python mock_data/generate_demo_pdfs.py
```

The same two PDFs are committed to the frontend's `public/examples/`, so
regenerating them here means copying them over there too.

---

## Layout

```
app/
├── agent.py            The four agents and their instructions. Most of the
│                       system's behaviour is prompt text in this file.
├── tools.py            Every tool. The read/write split lives here: agents
│                       read through the read-only mcp-clickhouse MCP server,
│                       and writes go through narrow typed functions instead.
├── schemas.py          Pydantic shapes the tools accept.
├── fast_api_app.py     Builds the server: ADK's routes plus the ones below.
└── app_utils/
    ├── suggestions_api.py   List and respond to suggestions. Approving is a
    │                        plain function call, not an agent turn.
    ├── graph_api.py         Read-only graph, plus the SSE stream the panels
    │                        subscribe to.
    ├── project_api.py       Destructive project reset, deliberately not a tool.
    ├── clickhouse_client.py Shared write connection.
    └── a2a.py               Agent-to-Agent protocol routes.
```

**One thing worth knowing before you scale it.** Sessions, the SSE subscriber
list, and the A2A task store are all in-process. The app is correct on a
single instance and silently wrong on more than one: a graph update written by
one instance never reaches a browser subscribed to another. Run one instance,
or move those three to a shared backing store first.
