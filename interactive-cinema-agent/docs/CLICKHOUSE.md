# ClickHouse MCP Integration Guide

## Overview

Your `interactive-cinema-agent` is configured to connect to ClickHouse Cloud using the **official `mcp-clickhouse` package** (Python).

- **Package:** `mcp-clickhouse` from https://github.com/ClickHouse/mcp-clickhouse
- **Language:** Python (no Node.js required)
- **Runs via:** `uv` (already installed)
- **Security:** Read-only by default

---

## How It's Configured

### 1. Dependencies (`pyproject.toml`)

Added `mcp` extra to enable MCP support:

```toml
dependencies = [
    "google-adk[gcp,otel-gcp,mcp]>=2.6.0,<3.0.0"
]
```

### 2. Credentials (`.env`)

ClickHouse credentials are stored in `.env` (lines 16-20):

```bash
CLICKHOUSE_HOST=your-instance.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=your_password_here
CLICKHOUSE_DATABASE=default
```

**⚠️ IMPORTANT:** This file is gitignored. Never commit credentials to git.

### 3. MCP Toolset (`app/tools.py`, lines 109-133)

The ClickHouse MCP server is configured as a toolset:

```python
import os
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# Load credentials from .env
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "your-instance.clickhouse.cloud")
CLICKHOUSE_PORT = os.getenv("CLICKHOUSE_PORT", "8443")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")

# Official mcp-clickhouse package
clickhouse_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "--with", "mcp-clickhouse", "--python", "3.10", "mcp-clickhouse"],
            env={
                "CLICKHOUSE_HOST": CLICKHOUSE_HOST,
                "CLICKHOUSE_PORT": CLICKHOUSE_PORT,
                "CLICKHOUSE_USER": CLICKHOUSE_USER,
                "CLICKHOUSE_PASSWORD": CLICKHOUSE_PASSWORD,
                "CLICKHOUSE_SECURE": "true",
                "CLICKHOUSE_VERIFY": "true",
            },
        ),
    ),
)
```

### 4. Agent Registration (`app/agent.py`, line 67)

The tools are registered to `subagent_1`:

```python
subagent_1 = Agent(
    name="subagent_1",
    tools=[
        subagent1_tool_placeholder,
        clickhouse_tools,  # ClickHouse MCP tools
    ],
)
```

---

## Available Tools

The agent has access to these ClickHouse tools:

### `run_query`
Execute SQL queries on your ClickHouse cluster.

**Example prompts:**
- "Run this query: SELECT * FROM movies LIMIT 10"
- "Count rows in the users table"
- "Show recent movie additions"

### `list_databases`
List all databases on the ClickHouse server.

**Example prompts:**
- "List all databases"
- "What databases are available?"

### `list_tables`
List tables in a database with optional details.

**Example prompts:**
- "List all tables"
- "Show tables in the cinema_data database"
- "What tables exist with their columns?"

---

## Your Next Steps

### Step 1: Add Your ClickHouse Credentials

Edit `.env` (lines 16-20) and replace the placeholders:

```bash
CLICKHOUSE_HOST=abc123.us-east-1.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=YourActualPassword123
CLICKHOUSE_DATABASE=cinema_data
```

### Step 2: Verify Setup

```bash
cd interactive-cinema-agent
uv run python test_clickhouse.py
```

**Expected output:**
```
✓ Step 1: Checking uv availability...
  ✅ uv found

✓ Step 2: Importing ClickHouse tools...
  ✅ ClickHouse toolset imported successfully

✓ Step 3: Loading agent...
  ✅ Agent loaded: subagent_1

✓ Step 4: Verifying agent can be initialized...
  ✅ Agent 'subagent_1' is ready!

✅ ALL TESTS PASSED!
```

This verifies your setup is correct. To test with real queries, continue to Step 3.

### Step 3: Try It Out

```bash
# Quick test
agents-cli run "List all databases"

# Interactive mode
agents-cli playground
```

Then try queries like:
- "Show me all tables"
- "Describe the structure of the movies table"
- "Query the first 10 rows from the users table"

### Step 4: Customize Your Agents

Edit `app/agent.py` to:

1. **Rename sub-agents** (change from `subagent_1` to descriptive names like `data_analyst`)
2. **Write descriptions** (used by root agent for delegation)
3. **Write instructions** (detailed guidance for each sub-agent)
4. **Add ClickHouse tools to other sub-agents** if needed:

```python
subagent_2 = Agent(
    name="subagent_2",
    tools=[
        subagent2_tool_placeholder,
        clickhouse_tools,  # Add ClickHouse access
    ],
)
```

### Step 5: Build Your Schema

Create your cinema data schema in ClickHouse Cloud and start querying!

---

## Customization Options

### Restrict Available Tools

In `app/tools.py` (line 132), add a tool filter:

```python
clickhouse_tools = McpToolset(
    connection_params=...,
    tool_filter=["run_query", "list_tables"],  # Only these tools
)
```

### Enable Write Access

By default, the server is read-only. To enable writes, add to `app/tools.py`:

```python
env={
    ...
    "CLICKHOUSE_ALLOW_WRITE_ACCESS": "true",  # Enable INSERT/UPDATE
    "CLICKHOUSE_ALLOW_DROP": "true",          # Enable DROP (dangerous!)
}
```

**⚠️ WARNING:** Only enable write access when necessary and with proper access controls.

### Production Deployment

For production, store credentials in GCP Secret Manager:

```bash
# Create secret
echo -n "your_password" | gcloud secrets create CLICKHOUSE_PASSWORD --data-file=-

# Deploy with secret
agents-cli deploy --secrets "CLICKHOUSE_PASSWORD=CLICKHOUSE_PASSWORD"
```

See `.adk-reference/DEPLOYMENT-GUIDE.md` for details.

---

## Troubleshooting

### "Connection refused" or "Authentication failed"
1. Verify credentials in `.env` are correct
2. Test manually: `curl https://your-host.clickhouse.cloud:8443/ping`
3. Check ClickHouse Cloud console to ensure instance is running

### "Package download slow"
First run downloads `mcp-clickhouse` package (30-60s). Subsequent runs are instant.

### "Import errors"
```bash
cd interactive-cinema-agent
uv sync --reinstall
```

### Agent doesn't use ClickHouse tools

Make instructions explicit in `app/agent.py`:

```python
instruction="""You are a data analyst with ClickHouse database access.

When asked about data, ALWAYS use ClickHouse tools:
- list_databases - See available databases
- list_tables - See available tables
- run_query - Execute SQL queries

Never make up data - always query the database."""
```

---

## Quick Reference

### Project Structure
```
interactive-cinema-agent/
├── .env                    # ← Add credentials here
├── app/
│   ├── tools.py           # Lines 109-133: clickhouse_tools config
│   └── agent.py           # Line 67: Tools registered to subagent_1
├── test_clickhouse.py     # Connection test script
└── pyproject.toml         # MCP dependency
```

### Commands
```bash
# Test connection
uv run python test_clickhouse.py

# Quick query
agents-cli run "List all databases"

# Interactive mode
agents-cli playground

# Run tests
uv run pytest tests/

# Deploy
agents-cli deploy
```

### Resources
- **Official Package:** https://github.com/ClickHouse/mcp-clickhouse
- **PyPI:** https://pypi.org/project/mcp-clickhouse/
- **ADK Docs:** https://adk.dev/
- **ADK Best Practices:** `.adk-reference/BEST-PRACTICES.md`

---

## Checklist

- [ ] Add ClickHouse credentials to `.env`
- [ ] Run `uv run python test_clickhouse.py`
- [ ] Test with `agents-cli run "List databases"`
- [ ] Customize sub-agent names and descriptions
- [ ] Build your cinema data schema in ClickHouse
- [ ] Test queries via `agents-cli playground`
- [ ] Run evaluation: `agents-cli eval run`
- [ ] Deploy when ready

---

**You're all set!** The ClickHouse MCP integration is configured and ready to use. Just add your credentials and start testing.
