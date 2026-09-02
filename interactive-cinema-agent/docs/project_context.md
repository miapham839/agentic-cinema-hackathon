Project Context: Interactive Story & Budget Co-Pilot (MVP)

1. Project Purpose & Main Features
   This project is an enterprise-grade AI co-pilot for filmmakers and narrative designers creating interactive, branching movies (e.g., Black Mirror: Bandersnatch). Writing branching narratives causes a combinatorial explosion of production costs and logical plot holes.

This tool solves that by using a multi-agent AI network connected to a ClickHouse database to provide real-time budget telemetry and logic auditing as the director builds the script.

Core MVP Features:

Automated Ingestion: Parses unstructured script PDFs and budget/location PDFs into a structured node-based graph. (later on, when UI is set up, will be visualized as an interactive flow chart)

Real-Time Logic Auditing: Automatically traverses the story graph to detect logic flaws, dead ends, orphaned choices, and continuity breaks.

Budget Optimization: Calculates the financial cost of different story branches and flags when a location change breaks the production budget.

Human-in-the-Loop Fixes: When the system detects a plot hole or budget overrun, it suggests a fix. If the user approves, the system automatically writes the corrected graph nodes to the database.

2. Technical Design & Multi-Agent Architecture
   The system uses a hub-and-spoke multi-agent architecture powered by Google ADK. We strictly separate deterministic data logic from LLM context to prevent context bloat. ClickHouse is the absolute source of truth.

(Note: For this MVP, there is no dedicated "Narrative/Writer" agent. All creative grounding comes from the user's uploaded PDFs.)

The Agent Team
Supervisor Agent (The Router): Sits at the center, interacts with the user, and routes sub-tasks to the correct worker agents via function calling. It manages the human-in-the-loop approval process.

Parser Agent: Takes the user's initial PDF uploads (script drafts and production budgets). It uses structured outputs to extract the text into our strict JSON schema and executes the initial INSERT (Version 1) directly into ClickHouse.

Graph Auditor Agent:

Reads: Uses recursive SQL queries to scan the ClickHouse graph for plot holes and broken state variables the raises suggested fixes to the users.

Writes: If a user approves a suggested fix to a plot hole, this agent executes the INSERT statement to append the corrected node (incrementing the version number) to the ClickHouse database.

Budget Agent: A purely analytical agent that queries ClickHouse to aggregate location costs, calculate branch totals, and warn the Supervisor if constraints are breached. It should collaborate with audit agent to produce plot-appropriate saving suggestions (consolidating locations across branches,...)

3. The Database Strategy: Append & Version (ClickHouse)
   ClickHouse is an OLAP database optimized for appends, not transactional UPDATE statements. To handle script edits natively, we treat every node edit as an immutable event.

We use ClickHouse's ReplacingMergeTree engine. When a node needs updating, the agent does not run an UPDATE. It INSERTs a new row with the same node_id but a higher version integer. ClickHouse automatically deduplicates and serves the highest version in the background.

4. Data Schemas & ClickHouse SQL Setup
   A. The Story Graph (Nodes & Edges)
   Agent JSON Output Schema (Target Extraction):

JSON
{
"nodes": [
{
"node_id": "scene_1a",
"title": "The Diner Standoff",
"location_id": "loc_diner_01",
"narrative_text": "Character A draws a weapon...",
"characters_present": ["Character A", "Character B"],
"state_modifiers": {"trust_level": "-1", "has_weapon": "true"}
}
],
"edges": [
{
"parent_node_id": "scene_1a",
"child_node_id": "scene_2b",
"choice_text": "Shoot first",
"required_state": {}
}
]
}

B. The Production & Budget Data
Agent JSON Output Schema (Target Extraction):

JSON
{
"locations": [
{
"location_id": "loc_diner_01",
"location_name": "Route 66 Diner",
"daily_rate_usd": 4500.00,
"company_move_penalty_usd": 1200.00,
"requires_permit": true,
"max_cast_capacity": 15
}
]
}
