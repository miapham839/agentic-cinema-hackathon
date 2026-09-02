# Design Decisions

This document records the non-obvious technical choices made while building the
Interactive Story & Budget Co-Pilot POC pipeline, and why. `project_context.md`
describes the target product; this file explains where and why the POC
implementation deviates from it, and the ADK/ClickHouse mechanics behind each
choice.

---

## 1. LLM-driven supervisor `root_agent` (superseded a `SequentialAgent`)

**Decision:** `root_agent` is an `Agent` (LLM) with
`sub_agents=[parser_agent, graph_auditor_agent, budget_agent]`, using ADK's
default LLM-driven transfer — not a `SequentialAgent`.

**History:** the first version of this pipeline used a `SequentialAgent`
here instead, on the reasoning that requirement #4 fixed the flow to exactly
one path (upload → parse → audit → budget) with no decision to make, so an
LLM router would just add latency/cost/failure modes for zero benefit. That
reasoning held right up until two things changed:

1. **Human-in-the-loop is coming.** Once a user can approve/reject a
   suggested fix, the root agent needs to actually route between "ingest a
   new document" and "apply an approved fix" — a real decision a
   `SequentialAgent` can't make (it always runs the same fixed order).
   Building the supervisor as a real `Agent` now, before that feature
   exists, means the routing logic doesn't need to be re-architected later
   — just extended with a new case.
2. **Budget/audit collaboration needs to be interactive, not one-way.** The
   original pipeline let `budget_agent` *see* `graph_auditor_agent`'s
   findings (as prior conversation turns) but had no way for `budget_agent`
   to go back and ask a follow-up question — e.g. "is it actually safe to
   merge these two scenes?" A `SequentialAgent` has no mechanism for a later
   step to call back into an earlier one. See section 10 for how this is
   solved (via `AgentTool`, not via `sub_agents` transfer) — but that
   mechanism only matters once budget_agent's suggestions are genuinely
   collaborative rather than a one-shot summary read off the auditor's
   report, which is the whole reason to want an LLM-driven root vs. a fixed
   pipeline in the first place: the user explicitly wants suggestions to be
   *verified*, not just plausible-sounding, and verification is inherently
   an interactive process, not a fixed sequence.

**How the pipeline now actually runs, mechanically:** every specialist
transfers control back to `root_agent` (its parent) when it's done, and
`root_agent` decides after every single hop whether to continue to another
specialist or stop. This is NOT the same as having each specialist transfer
directly to the next one (peer-to-peer transfer, which ADK also supports by
default) — that was tried first and rejected once it became clear it can't
support a targeted request; see section 11 for the full mechanism and why.

**Trade-off accepted:** an LLM decides multiple extra times (whether to
continue, and to which specialist) compared to the deterministic version.
Each of those decisions is now genuinely load-bearing (this is what makes
the human-in-the-loop extension, the audit/budget collaboration, and
targeted single-agent requests possible), so this is no longer paying for a
decision that never varies — but it does reintroduce the failure mode the
original version avoided: the supervisor could, in principle, continue the
pipeline when it shouldn't, or stop too early. This is mitigated by making
the routing rules in `root_agent`'s instruction explicit and case-based
(full upload vs. explicit skip vs. targeted request vs. no specialist
needed) rather than a single vague "route appropriately."

---

## 2. Typed tool parameters (Pydantic) instead of `output_schema`

**Decision:** `parser_agent` has no `output_schema`. Structured extraction is
enforced by typing each write tool's parameter as a Pydantic model
(`insert_script_extraction(extraction: ScriptExtraction, ...)`,
`insert_budget_extraction(extraction: BudgetExtraction, ...)`), defined in
`app/schemas.py`.

**Why:** This mirrors Google's `document_processing.ipynb` tutorial pattern
(`GenerateContentConfig(response_schema=PydanticModel)`), but that parameter
only exists on the raw `google-genai` SDK — it does not apply to ADK's
`Agent()`. ADK's closest equivalent, `LlmAgent.output_schema`, constrains the
agent's *final text reply*, and historically has not been usable together
with `tools` (a schema-constrained agent can't also call tools in the same
turn in older ADK versions). Since parser_agent's whole job is to extract
data *and* call a tool with it, `output_schema` is the wrong mechanism here
regardless of whether the tools+output_schema restriction still holds in the
installed version.

`FunctionTool` auto-generates a full JSON schema from a Python/Pydantic type
hint on a tool's parameter (verified directly against the installed ADK
source — see `FunctionTool._get_declaration()`, which populates
`parameters_json_schema` from the Pydantic model, including nested `$defs`
for `ScriptNode`/`ScriptEdge`). Gemini's structured tool-calling then forces
the model's function-call arguments to conform to that schema. This gets the
same reliability guarantee as `response_schema`, without fighting ADK's
tool-calling loop.

**Trade-off accepted:** the model must extract the *entire* document in one
shot before making one tool call, rather than being able to interleave
partial writes. For a single-document, one-shot ingestion POC this is fine;
it's also what makes "call the tool exactly once" enforceable in the
instruction.

---

## 3. Read vs. write split: MCP for reads, `clickhouse_connect` for writes

**Decision:** `graph_auditor_agent` and `budget_agent` only get
`clickhouse_tools` (the official `mcp-clickhouse` MCP server, with
`CLICKHOUSE_ALLOW_WRITE_ACCESS` left unset — read-only). Only `parser_agent`
gets the custom `insert_script_extraction` / `insert_budget_extraction`
tools built directly on `clickhouse_connect`.

**Why:** `mcp-clickhouse`'s write mode is all-or-nothing: turning it on grants
arbitrary `INSERT`/`UPDATE`/`DDL` via `run_query` to *every* agent holding
that toolset, scoped only by whatever privileges the ClickHouse user itself
has. Since `graph_auditor_agent` and `budget_agent` never need to write
anything in this POC, giving them write-capable tools would be an
unnecessary privilege grant with no corresponding capability — a bug in one
of their prompts could result in an unintended `DELETE` or `ALTER`. The
custom tools are narrow by construction: they take a typed Pydantic
extraction, know exactly which 1–2 tables they write to and which columns,
and are the only write path in the whole agent graph. This was carried
forward unchanged from the earlier ClickHouse write-access work in this
project (see `CLICKHOUSE.md`).

---

## 4. `version=1` hardcoded for every parser_agent write

**Decision:** `INITIAL_VERSION = 1` in `app/tools.py`; every row
`parser_agent` writes uses that constant, not a computed "next version."

**Why:** `script_nodes`, `script_edges`, and `production_locations` all use
`ReplacingMergeTree` keyed on a `version` column — see section 5 below.
`parser_agent`'s only job in this POC is the *initial* ingestion of a
document (per `project_context.md`: "executes the initial INSERT (Version 1)
directly into ClickHouse"). There is no "compute the next version" logic
because there is no write path yet for the *second* kind of write described
in `project_context.md` — the human-in-the-loop approved-fix flow, where
graph_auditor_agent would insert a corrected node at `version = old + 1`.
Requirement #4 explicitly scopes this POC to "no human in the loop / asking
for human approval yet," so that write path (and the version-increment logic
it needs) was deliberately not built. Building it now would be a tool nobody
can reach yet, since no agent has a way to receive or act on user approval in
this pipeline.

**How to apply going forward:** When human-in-the-loop fixes are added,
`graph_auditor_agent` needs a *second*, separate write tool (not a reused
`insert_script_extraction`) that (a) computes `current_max_version(node_id) +
1` and (b) writes a single corrected node/edge, not a whole extraction. Keep
it a distinct tool rather than overloading the parser's tool, since the
auditor should not be able to perform a fresh bulk ingestion, and the parser
should not be able to apply incremental fixes.

---

## 5. `FINAL` is mandatory in every read-agent instruction

**Decision:** Both `graph_auditor_agent` and `budget_agent`'s instructions
explicitly and repeatedly tell the model to query with `FINAL` (e.g. `SELECT
* FROM script_nodes FINAL`), and explain why in-line.

**Why:** `ReplacingMergeTree` (the engine `agents-cli scaffold` chose for
these tables — see `SHOW CREATE TABLE` output) only deduplicates rows with
the same sorting key during background merges, on ClickHouse's own schedule.
A plain `SELECT` right after an insert can return stale or duplicate
versions of a row. This is silent and easy to miss in a demo (small
tables merge fast) but will produce confusing or duplicated audit/budget
output as soon as a node gets a second version and the merge hasn't run yet.
Since neither agent can be given a hardcoded query (they need to compose
their own `run_query` calls via the MCP tool, based on the graph shape they
discover), the only enforcement point available is the instruction text —
so both agents' instructions state the `FINAL` requirement as a "CRITICAL"
rule with a concrete example, not just prose.

---

## 6. Graph traversal happens in the LLM, not in SQL

**Decision:** `graph_auditor_agent` and `budget_agent` are instructed to
fetch the full set of current nodes/edges with one or two `SELECT ... FINAL`
queries, then reason over the graph themselves (dead ends, orphaned choices,
branch enumeration) — not via a recursive SQL query.

**Why:** `project_context.md` says the auditor "uses recursive SQL queries to
scan the ClickHouse graph." For a POC-sized graph (a handful of nodes), doing
graph traversal in SQL (recursive CTEs, or repeated self-joins) adds
significant query complexity for no accuracy benefit, and ClickHouse's
recursive-CTE support is less mature/ergonomic than Postgres's for this kind
of arbitrary-depth tree walk. Handing the auditor and budget agent the raw
node/edge lists and letting the LLM reason over the graph directly is simpler
to instruct correctly, easier to debug (the reasoning is visible in the
agent's own reply), and plenty fast at POC scale.

**How to apply going forward:** If the story graph grows large enough that
dumping every node/edge into the LLM's context becomes expensive or
inaccurate, that's the point to move traversal into SQL (or a Python helper
tool that does the graph walk and returns a precomputed branch list) instead
of raw `SELECT * ... FINAL` + LLM reasoning.

---

## 7. Budget constraints come from a dedicated `production_constraints` table

**Decision (superseded twice from the original version of this doc):** the
very first POC had `parser_agent` notice a stated budget figure in a
document's prose and mention it in its reply, with `budget_agent` scanning
earlier conversation turns for that number. Once a real
`production_constraints(project_id, total_budget_usd, max_filming_days,
max_total_crew, max_primary_locations, version, updated_at)` table was added
(same `ReplacingMergeTree`-keyed-on-`version` pattern as the other three
tables), that was replaced with `budget_agent` reading `total_budget_usd`
and `max_primary_locations` from the table via `run_query ... FINAL` — but
initially nothing wrote to that table at all; it had to be seeded manually
with a raw `INSERT`. The current version closes that gap: `parser_agent` now
extracts a `ProductionConstraints` from the budget document (when it states
one) and writes it via a fourth typed tool, `insert_production_constraints`
— the same "typed Pydantic tool parameter" pattern as its other two write
tools (see section 2). `budget_agent`'s side is unchanged — it's still
explicitly instructed never to accept a budget number from conversation
text, only from this table.

**Why the original conversation-text approach was replaced:**
piggybacking the constraint on `parser_agent`'s natural-language reply
worked, but was fragile by construction (see the trade-off originally
accepted below) and mixed two unrelated concerns — "what did this PDF say"
and "what's this project's approved budget" — into the same reply text
without a schema. A dedicated table is the correct source of truth: it's
queryable independent of any particular chat turn, survives across
sessions, and (now that parser_agent writes it) can still be set up by a
producer once, out of band, if a given upload doesn't happen to restate the
constraints.

**Constraints extraction is optional, unlike nodes/edges/locations:**
`insert_production_constraints` is only called when the budget document
actually states an overall constraint — parser_agent is explicitly told not
to invent values for constraints the document doesn't mention, and not to
call the tool at all if it states none. This is different from
`insert_script_extraction`/`insert_budget_extraction`, which are always
called once a script/budget document is present. The asymmetry is
intentional: a script document always has scenes, and a budget document
always has per-location costs, but not every budget document necessarily
restates the production's overall caps.

**Current limitation — no `project_id` concept elsewhere in the schema:**
`script_nodes`/`script_edges`/`production_locations` have no `project_id`
column, so there's no way to join a specific story graph to a specific
`production_constraints` row. Since this POC only ever handles one
production at a time, `budget_agent` is instructed to just take the single
row in the table (or the most recently updated one, if more than one
exists) rather than filtering by `project_id`. This is the same
single-project scoping this POC already has everywhere else (see section 4)
— it stops being sufficient the moment the system needs to track more than
one production concurrently, at which point `project_id` needs to be
threaded through the other three tables too, not just read out of
`production_constraints`.

**`max_filming_days` / `max_total_crew` are read but not enforced:**
`budget_agent` reports these two values for visibility, but doesn't check
anything against them — there's no per-scene filming-day or crew-size data
anywhere in `script_nodes`/`production_locations` to compare them to. Adding
that would mean extending `ScriptNode` (e.g. a `shoot_days` or
`crew_required` field) and `parser_agent`'s extraction — a bigger schema
change than "read one more table," out of scope for this pass.

**Original trade-off accepted (now resolved):** the conversation-text
approach meant budget_agent had nothing to compare against if the source
document didn't state a cap in prose, or parser_agent forgot to mention it.
That's no longer a concern — `production_constraints` is authoritative and
independent of any document's wording.

---

## 8. Mock data design

**Decision:** The mock script (`mock_data/mock_script.pdf`) and mock budget
(`mock_data/mock_budget.pdf`) were designed together so that:
- one narrative fork ("turn back for Jamie") leads to a beat that's
  referenced but never actually written anywhere in the document (an
  orphaned/broken choice),
- one beat ("An Uneasy Truce") has no further fork and no ending language
  (an unmarked dead end), contrasted with "The Old Warehouse," which
  explicitly closes out the sequence in its own prose, so the auditor has a
  clear signal *not* to flag it,
- the two branches have deliberately different costs: diner → docks →
  warehouse totals ~$18,000 against a ~$12,000 cap (an overrun), while
  diner → diner totals ~$9,000 (under cap, but on the branch with the dead
  end) — so a real run exercises overrun detection, logic-flaw detection,
  and the collaboration between the two findings, not just one in isolation.

**Why:** A "clean" mock document would only prove the happy path works. The
intentional inconsistencies are what make the audit/budget agents' output
worth reading in a demo — they're designed to force a concrete, checkable
finding out of each agent, per requirement #5.

**Revision — the documents were rewritten to be free-form, not
schema-shaped.** The first version of these mocks used explicit labeled
fields that mirrored the ClickHouse schema almost 1:1 — "Location ID:
loc_diner_01", "Characters Present: Sam, Jamie", "State Change: trust_level
= -1", "CHOICE 1: ... -> leads to SCENE 2A". That proved the tool-calling
and write path worked, but it didn't prove anything about `parser_agent`'s
actual job: real uploaded scripts and budget memos won't hand it
pre-labeled fields. The documents were rewritten as an actual screenplay
excerpt (sluglines, action, dialogue, narrative forks woven into prose) and
a producer's budget memo (numbers in varied prose phrasing — "twelve
thousand dollars," "$6,200 a day," "a reasonable $3,800" — rather than a
neat table), with no labels matching the schema anywhere. This forces
`parser_agent` to actually do the job it's meant to do: infer which scene a
line of dialogue belongs to, infer who's present from action/dialogue,
infer state changes from what a scene depicts rather than a stated flag,
and match a location mentioned by name across two documents to the same
`location_id` without being handed one. `parser_agent`'s own instruction
was updated alongside this (see `app/agent.py`) to explicitly permit this
kind of inference — the original instruction's "do not invent state
modifiers that are not in the text" was strict enough that a literal
reading could have made the model report an empty `state_modifiers` dict
for every scene once the labeled "State Change:" lines were gone. The
instruction now says inference of *implied* changes is expected and
required; only fabricating structure (scenes, choices, characters that
aren't there) is disallowed.

**Consequence — `state_modifiers` keys/values are no longer deterministic.**
Because nothing in the mock documents states a canonical key name like
`trust_level` anymore, the model invents its own reasonable key/value per
run (e.g. one run might produce `{"trust_level": "damaged"}`, another
`{"tension": "high"}` for the same scene). This is expected and is exactly
what free-form input should produce — don't treat a different exact key
name across two runs as a bug. What should stay stable across runs: the
node/edge structure (4 scenes, the broken choice, the dead end), the
location→cost mapping, and the numeric overrun. If those drift, that's a
real regression; if only the state_modifiers wording changes, it isn't.

Location NAMES ("Route 66 Diner," "Harbor Docks," "Old Warehouse") are
still stated identically across both documents, so `parser_agent` can match
them by name and derive the same `location_id` slug both times — but
there's no longer an explicit ID anywhere for it to just copy. See
`app/agent.py`'s parser_agent instruction for the exact rule on deriving and
reusing a consistent slug from a location's name.

---

## 9. `graph_auditor_agent`/`budget_agent` instructed to call one tool at a time

**Decision:** Both agents' instructions explicitly forbid "narrating" tool
calls as pseudocode (e.g. `nodes = run_query(...)`) and require issuing one
real tool call, waiting for its result, then deciding the next step —
spelled out as a numbered, one-call-per-step sequence rather than "query all
current nodes and edges" as a single combined instruction.

**Why:** During testing, `graph_auditor_agent` hit Gemini's
`UNEXPECTED_TOOL_CALL` finish reason — the model, instead of issuing a real
structured tool call, generated literal text that looked like Python:
```
databases = list_databases()
tables_in_db = list_tables(database='default')
nodes_query = "SELECT ... FROM script_nodes FINAL"
nodes_data = run_query(query=nodes_query)
...
```
This is a known Gemini/function-calling failure mode: when an instruction
lists several steps ("query nodes, then query edges") next to tool names
that read like Python function signatures (`run_query(query=...)`), the
model sometimes drafts the whole sequence as a script instead of emitting
one discrete tool call. `mcp-clickhouse`'s tool names (`list_databases`,
`list_tables`, `run_query`) are exactly this shape, so both instructions'
original "Query all current nodes and all current edges (using FINAL as
above)" line — combined with two FINAL example queries shown back-to-back —
was enough to trigger it. The fix was purely prompt-level: state the
one-call-per-step rule explicitly, and rewrite the multi-query steps as
separate numbered "call, wait, call again" instructions instead of one
combined "query everything" step.

---

## 10. `budget_agent` consults `graph_auditor_agent` via `AgentTool`, not `sub_agents` transfer

**Decision:** `graph_auditor_agent` is wired into `budget_agent` twice, in
two different ways, for two different purposes:
- As a **peer under `root_agent`'s `sub_agents`** (the normal case — receives
  a full-graph audit via transfer from `parser_agent`, then transfers to
  `budget_agent` when done). This is a one-way handoff: control moves to
  `graph_auditor_agent` and doesn't come back to whoever transferred it.
- As an **`AgentTool`** (`consult_graph_auditor = AgentTool(agent=graph_auditor_agent)`)
  in `budget_agent.tools`. This is a synchronous call: `budget_agent` asks a
  specific question, gets a direct answer back into its *own* turn, and can
  keep working — including calling it again for the next suggestion. Control
  never leaves `budget_agent`.

**Why not just use `sub_agents` transfer for this too:** transfer is a
hand-off, not a function call — once `budget_agent` transferred to
`graph_auditor_agent` to ask a question, it wouldn't get control back to
finish its own analysis; whatever `graph_auditor_agent` said would become
the end of that turn (or it would need to transfer *back*, which is control
flow the model has to get right every single time, for every question, with
no structural guarantee it will). `AgentTool` is the ADK primitive built
exactly for "call another agent like a function and use its answer" —
`budget_agent` stays in charge of its own turn regardless of how many
verification questions it asks.

**Why the same `graph_auditor_agent` instance can be reused both ways:**
ADK only allows an agent to have one `parent_agent` (`sub_agents=[...]`
assignment raises if the sub-agent already has a parent), but `AgentTool`
doesn't touch `parent_agent` at all — it spins up its own temporary
`Runner`/`InMemorySessionService` and invokes the wrapped agent as a
standalone call, whatever its existing parent tree looks like. So
`graph_auditor_agent` stays exactly what it already was — a peer of
`parser_agent`/`budget_agent` under `root_agent` — while also being
callable as a tool by `budget_agent`, with no duplicated agent definition to
keep in sync. The ADK docs technically discourage direct `AgentTool` usage
in favor of `mode='single_turn'` sub-agents for new inline tool-agents, but
that mode still requires a dedicated `parent_agent`, which would force a
second, duplicate `graph_auditor_agent` instance here — the DRY win of
reusing one instance outweighs following that discouragement for this
specific case.

**Consequence — each consultation call is a fresh, contextless session:**
because `AgentTool` runs the wrapped agent in a brand-new in-memory session,
`graph_auditor_agent` has no memory of its earlier full-audit pass (or
anything else in the main conversation) when `budget_agent` consults it —
it only sees the one question it's asked. This is actually the right
behavior here, not a limitation to work around: `graph_auditor_agent`'s
tools give it live, authoritative read access to ClickHouse, so it can
re-derive whatever graph state it needs to answer the specific question
correctly, rather than relying on a possibly-stale summary from its earlier
pass. The agent's own instruction explicitly distinguishes these two
invocation shapes ("FULL AUDIT" vs. "CONSULTATION") so it knows not to
perform a full audit or transfer onward when it's really just answering one
targeted question.

**Why verification is instructed as mandatory, not optional:** the whole
point of this mechanism is that a plausible-sounding savings suggestion
(cheaper, fewer locations) can still be wrong dramatically — e.g. proposing
to merge two scenes that are actually mutually exclusive branches, or move a
scene to a location where a required prop/character isn't present yet in
the story state. `budget_agent`'s instruction requires a verification call
for any suggestion that changes the graph, and requires dropping or
revising a suggestion the auditor flags as a problem, rather than
presenting it anyway. This is prompt-level enforcement (like the `FINAL`
requirement in section 5) — there's no code-level guarantee `budget_agent`
actually calls the tool before finalizing a suggestion, only the
instruction's explicit "MANDATORY VERIFICATION" step and the requirement to
tag verified suggestions in the final reply. If suggestion quality turns out
to need a harder guarantee than prompting, the next step would be a
structured intermediate step (e.g. `budget_agent` required to call a
"propose_suggestion" tool that itself calls the auditor server-side before
accepting the suggestion) rather than trusting the model to remember.

---

## 11. Specialists report back to `root_agent`, not to each other

**Decision:** every specialist's instruction ends with "transfer back to
root_agent" (or, for `budget_agent`, doesn't transfer at all). None of them
transfer directly to the next specialist. `root_agent` alone decides, after
every single hand-back, whether to continue and to whom.

**History:** the version of this pipeline right after the `SequentialAgent`
→ supervisor change (section 1) had each specialist transfer directly to
the next one — `parser_agent` → `graph_auditor_agent` → `budget_agent` —
with `root_agent` only making the very first routing decision. This worked
for the full-pipeline case, but broke a real requirement: it made
`graph_auditor_agent` unconditionally transfer to `budget_agent` after
*every* full audit, including one the user asked for on its own (e.g. "just
audit my current graph, don't check the budget"). `graph_auditor_agent` had
no way to know whether it had been reached via the automatic pipeline or a
standalone request — both look identical from inside a peer-to-peer
transfer chain, since by design the specialist doing the work never sees
who triggered it or why.

**Why routing back through the supervisor fixes this:** `root_agent` is the
only agent that ever sees the user's *original* message for the current
exchange, so it's the only one that can tell "the user uploaded a document"
apart from "the user asked for one specific thing." By making every
specialist report back to it instead of chaining onward, the "should this
continue?" decision moves to the one place that actually has the
information needed to make it. `root_agent`'s instruction now spells out
four cases explicitly: full pipeline (upload, no qualifier) → continue
through all three; upload with an explicit "don't audit yet" → stop after
`parser_agent`; a targeted request with no upload (e.g. "check the budget")
→ transfer to only that one specialist and stop when it reports back; no
specialist needed → answer directly. This means a request like "just audit
the current graph" now correctly runs only `graph_auditor_agent` and stops,
instead of always cascading into a budget analysis nobody asked for.

**Why this doesn't apply to `budget_agent`'s consultation calls:** those go
through `AgentTool` (section 10), not `sub_agents` transfer, and are
initiated by `budget_agent` itself mid-turn to answer a question it has —
not part of the "what runs next in the overall pipeline" decision at all.
`root_agent` is never involved in, or aware of, those calls.

**Trade-off accepted:** this adds one more supervisor round-trip per
specialist compared to direct peer chaining (specialist → root → next
specialist, instead of specialist → next specialist directly) — slightly
more latency and LLM calls for the full-pipeline case. This is the same
kind of trade-off accepted in section 1 (an LLM decision that never varies
for the full-pipeline case now costs something), but it's necessary here
specifically because the decision *does* vary once targeted requests are a
real input the supervisor has to handle, not just an edge case.

---

## 12. Deterministic HITL rules moved out of the prompt into `app/checks.py`

**Decision:** `parser_agent`'s five "when to ask the user" rules were split by
whether they actually need a language model. Three did not:

| Rule | Reduces to | Now lives in |
|---|---|---|
| 1 — degenerate extraction | `len(nodes) == 0` | `app/checks.py` |
| 2 — missing budget cap | `total_budget_usd is None` + a sum | `app/checks.py` |
| 5 — shoot-day conflict | group-by-location, sum, compare | `app/checks.py` |
| 3 — location identity | reading prose, judging meaning | instruction |
| 4 — story-graph inference | reading prose, judging meaning | instruction |

**Why:** as prose, these rules fired unpredictably. Rule 5 was the clearest
case — it was silently skipped even on an unambiguous mismatch (script scenes
summing to 3 against a stated 2), because it was the only rule requiring a
deliberate cross-document aggregation pass rather than something noticeable
while reading a single document. It was also last in a five-rule list inside a
~7,450-token instruction, which is exactly where instructions get dropped.

Arithmetic and joins are not a judgment call, and a check written in Python
runs identically every time. The two rules that genuinely require reading prose
stayed in the instruction, where they belong.

**Measured effect:** the instruction went from ~7,450 tokens to ~2,090 (-72%).
Roughly 2,200 of those tokens were the three
`json.dumps(model_json_schema())` blocks, which were pure duplication — ADK's
`FunctionTool` already binds a byte-identical copy of each schema into the
function declaration via `parameters_json_schema`. The rest came from rules
1/2/5 collapsing into "call `check_staged` and ask about what it returns."

**Consequence for `app/schemas.py`:** the auto-generated declaration carries
`Field(description=...)` but NOT instruction prose, so per-field extraction
rules (how to derive a `location_id`, when to leave `shoot_days` unset) now
live in the field descriptions rather than the instruction.
`tests/unit/test_tool_declarations.py` pins this, because the ADK feature flag
controlling it is experimental and its legacy fallback path strips every
description silently.

## 13. Writes follow a stage → check → commit lifecycle

**Decision (supersedes the three fire-and-forget `insert_*` tools):**
`parser_agent` now calls `stage_script_extraction` / `stage_budget_extraction` /
`stage_production_constraints` (which write nothing), then `check_staged`
(no arguments — it reads the staged drafts from session state), then
`commit_staged`, which applies the user's answers and writes every table
together.

**Why, problem by problem:**

- **Partial writes.** Previously each `insert_*` call was independent, and in
  testing one run committed the script extraction while silently skipping the
  budget and constraints writes, leaving the three tables inconsistent with no
  error raised. A single commit makes that impossible. A `before_tool_callback`
  (`require_checks_before_commit`) additionally refuses `commit_staged` until
  `check_staged` has run — a structural guard rather than another line of prompt
  the model can drop.
- **Double-serialized payloads.** Resolving a HITL answer used to mean
  re-sending an entire corrected extraction, so every large draft crossed the
  wire twice — the payload pattern already implicated in a
  `MALFORMED_FUNCTION_CALL` failure. Answers now apply as small `Correction`
  records (`set_total_budget`, `skip_constraints`,
  `set_location_total_shoot_days`, `merge_locations`, `set_node_edges`), so each
  extraction is serialized exactly once, at staging.

**State keys are deliberately not `temp:`-prefixed.** ADK strips temp-scoped
keys from the persisted event delta (`_trim_temp_delta_state` in
`sessions/base_session_service.py`); they survive only in the in-memory session
object. `request_input` pauses the invocation across a request boundary, and
this project swaps in `VertexAiSessionService` when
`GOOGLE_CLOUD_AGENT_ENGINE_ID` is set, so temp-scoped drafts would vanish on
resume. `commit_staged` clears the keys explicitly instead.

## 14. Instruction examples and test fixtures are kept in separate domains

**Decision:** three disjoint vocabularies —

- instruction examples: hospital (`app/agent.py`)
- dev fixtures: noir (`mock_data/generate_hitl_test_pdfs.py`)
- held-out measurement fixtures: period drama (`mock_data/heldout_fixtures.py`)

**Why:** an earlier round of instruction fixes put fixture strings *verbatim*
into the instruction — `INT. PRECINCT` / `INT. 14TH STREET STATION`,
`scene_ledger_call` → `scene_rooftop_pursuit`. Rules 3 and 4 then appeared to
start working, but the test could no longer distinguish "the model generalized
the principle" from "the model matched a string it had been handed." The result
was unusable as evidence.

`tests/eval/test_hitl_rules.py` measures against the held-out set only. If a
name from `mock_data/heldout_fixtures.py` ever appears in `app/agent.py`, that
fixture set is burned and needs replacing.

## 15. Rule-firing is measured as a rate, not eyeballed once

**Decision:** `tests/eval/test_hitl_rules.py` runs the same fixture N times
(default 5) and reports per-rule fire rates, e.g. `SHOOT_DAY_CONFLICT: 5/5`.
Opt-in via `RUN_HITL_EVAL=1` since it makes real model calls.

**Why:** single manual runs through `adk web` cannot separate "the fix worked"
from "that run went well," which is how a contaminated result survived a full
round of review. Deterministic rules should now be N/N by construction —
anything less is a real bug — while the semantic rules give the actual
generalization number, asserted against a floor rather than perfection.

The harness monkeypatches the ClickHouse client: a measurement harness that
runs N times must not mutate the production database it is measuring against.
It also feeds fixture *text* rather than PDFs, so it measures rule-triggering
and tool sequencing rather than PDF parsing.

**This immediately paid for itself:** its first run reported
`SHOOT_DAY_CONFLICT 0/3`, which looked like the refactor had failed. It hadn't
— the held-out fixture put the 2-day and 1-day scenes at *different* locations,
so no conflict existed to find. Eyeballing would have read that as a broken
rule and sent the fix in the wrong direction.
