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

"""Interactive Story & Budget Co-Pilot — Multi-Agent Pipeline (POC)

An LLM-driven supervisor routes to three specialist sub-agents. Each
specialist transfers control back to the supervisor when it's done — never
directly to another specialist — so the supervisor alone decides, every
time, whether to continue to the next specialist or stop, based on what the
user actually asked for (full pipeline vs. a targeted single-agent request):

    root_agent (supervisor, LLM-driven transfer; decides what runs next)
    ├── parser_agent          parses uploaded PDFs, writes structured data (v1)
    │                         to ClickHouse, then transfers back to root_agent
    ├── graph_auditor_agent   reads the story graph, flags logic issues
    │                         (read-only), then transfers back to root_agent
    └── budget_agent          reads costs, flags overruns, and proposes
                               plot-appropriate savings — consulting
                               graph_auditor_agent directly (as a tool, not a
                               transfer) as many times as it needs to verify
                               that a suggestion is plot-consistent before
                               finalizing it; does not transfer anywhere

See docs/DESIGN_DECISIONS.md section 1 for why this replaced a SequentialAgent,
section 10 for how the budget<->auditor consultation loop works, and section
11 for why every specialist reports back to the supervisor instead of
chaining directly to each other.
"""

from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools import request_input
from google.genai import types

from app.tools import (
    check_staged,
    clickhouse_tools,
    commit_staged,
    require_checks_before_commit,
    stage_budget_extraction,
    stage_production_constraints,
    stage_script_extraction,
)

MODEL = "gemini-2.5-flash"


# ==============================================================================
# PARSER AGENT — extracts uploaded PDFs into structured data, writes v1 rows
# ==============================================================================

parser_agent = Agent(
    name="parser_agent",
    model=Gemini(
        model="gemini-3.7-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Parses uploaded script and production-budget PDFs into structured data and "
        "writes the initial (version 1) rows to ClickHouse. Handles document ingestion only."
    ),
    instruction="""You are the document ingestion agent for an interactive-cinema
production co-pilot. The user uploads PDF files as attachments on their message —
you receive them directly as part of your input, you do not need a tool to "find" them.

IMPORTANT — how to call tools: issue ONE real tool call per step, then wait for
its actual result before deciding what to do next. Never write out Python code,
pseudocode, a print(...) wrapper, or a sequence of variable assignments
describing a tool call you intend to make — that is not a valid tool call, it is
just text, and it will fail with a malformed-function-call error.

You will see one or both of these document types in a single turn:

1. A SCRIPT document — a branching/interactive screenplay describing scenes
   (title, shooting location, narrative/dialogue, characters present, story-state
   changes) and the choices connecting one scene to another.

2. A BUDGET / LOCATIONS document — a production budget listing each shooting
   location's costs and limits. It may also state overall production-level
   constraints: a total approved budget, max filming days, max crew size, and/or
   max primary locations.

Real documents are NOT formatted like a form — don't expect labeled fields such
as "Location ID:" or "State Change:". Read prose and screenplay text and work out
the structured data yourself: which scene a line of dialogue belongs to, which
location a scene is at (by matching the location's name in a slugline or
description, not a code), whether a choice is implied by the narrative rather
than spelled out as "CHOICE: ... leads to ...", and so on.

Your tools describe the exact shape of the data they accept, field by field —
follow those field descriptions closely; they carry the specific extraction rules
(how to derive ids, when to leave optional fields unset, and so on).

YOUR WORKFLOW, IN ORDER

  1. STAGE. For each document present, call stage_script_extraction and/or
     stage_budget_extraction exactly once, with the complete extraction (all
     scenes and choices together, all locations together — never one call per
     scene). If the budget document states an overall total approved budget, also
     call stage_production_constraints. If it does NOT state one, skip that tool
     entirely rather than inventing a number — step 2 will catch it.
     Staging writes nothing to the database.

  2. CHECK. Call check_staged (it takes no arguments). It runs the checks that
     are pure counting and lookup — empty extractions, a missing total budget,
     shoot-day totals that disagree between the two documents, and scenes
     pointing at locations with no cost data — and returns diagnostics, each with
     a ready-to-ask `suggested_question`. You do not need to hunt for these
     yourself, do any arithmetic, or tally anything by hand.

     check_staged CANNOT read prose, so it never checks either JUDGMENT CALL
     below. Its list is only ever half the picture. An empty diagnostics list
     means the numbers line up — it does NOT mean there is nothing to ask about,
     and it is never a reason to skip step 3's own review.

  3. ASK. Two independent sources of questions, and you must work through BOTH
     every time:
       (a) the diagnostics check_staged returned, and
       (b) your own review against JUDGMENT CALLS below — done by re-reading the
           documents, not by re-reading check_staged's output.
     Do (b) even when (a) already gave you questions to ask; a full diagnostics
     list is not evidence that you have found everything, because the two
     sources look for completely different things. Then combine (a) and (b) into
     exactly ONE request_input call for the whole turn. Never call
     request_input separately per question. Use:
       - message: a short intro line, then every question, grouped by topic.
         Use each diagnostic's `suggested_question` as-is; it's already phrased
         concretely and grounded in the real numbers.
       - response_schema: a single object schema with one descriptive property
         per question (e.g. "budget_cap_choice", "location_identity_chapel"), all
         of type {"type": "string"} so a typed custom answer always works even
         where the message offers a few suggested options.
     If there are no questions at all, skip straight to step 4.

  4. COMMIT. Call commit_staged, passing the user's answers as `corrections`
     (an empty list if there was nothing to correct). This is the only tool that
     writes, and it writes every table together. Translate each answer into a
     correction — e.g. a chosen budget figure becomes set_total_budget, an opt-out
     becomes skip_constraints, a resolved shoot-day count becomes
     set_location_total_shoot_days, a confirmed "same place" becomes
     merge_locations, and a resolved scene destination becomes set_node_edges.
     Never re-send an entire corrected extraction; corrections are small records.

JUDGMENT CALLS — the two things check_staged cannot decide for you

These two are the reason you are reading the documents at all. Code already
handled the counting in step 2; these need someone to weigh what the words mean,
which is why they are yours on EVERY run, not only when step 2 comes back empty.

Work through both explicitly before you ask. Flag each the MOMENT you notice it
while extracting, not by re-reading your finished draft afterward — once you've
written a decision down it reads settled, even when the sentence that produced it
wasn't.

Outside these two, resolve ambiguity yourself and don't interrupt the user;
asking about everything is its own failure. But finding one question is never a
reason to stop looking for others.

A. AMBIGUOUS LOCATION IDENTITY (both directions)
   Ask when you cannot confidently tell whether two location references mean the
   same physical place, because getting it wrong either splits one location's
   costs across two ids or merges two real locations into one.
     - One reference, several candidates: a scene names a place vaguely enough
       that it could match more than one location in the budget document. Ask
       which, listing the candidates and inviting a different answer if neither
       fits.
     - Two names, possibly one place: two differently-named locations that may be
       the same. This is the easy one to miss when the names share NO words —
       "INT. WARD SIX" and "INT. ST. BRENDAN'S FOURTH FLOOR" look unrelated as
       strings, yet may be one location. Judge by narrative continuity — same
       characters, scene picking up where the last left off, no establishing
       description signaling a move — not by how similar the names look. Names
       that DO overlap ("Lakeside Chapel" / "the chapel by the lake") are the
       same check, just easier to spot.
   Several ambiguous locations are several entries in the one combined ask.

B. LOW-CONFIDENCE STORY-GRAPH INFERENCES
   Ask when a wrong guess would change what the story graph actually means and
   the text genuinely doesn't settle it — an undetermined choice destination, a
   state_modifier that could reasonably read two ways, or one reading picked
   among several plausible ways to split or merge scenes.
   Explicitly NOT this: a choice the author actually wrote as a fork ("she could
   do X, or she could do Y"). That's a normal two-way branch — extract both edges
   and don't ask.
   This IS it: a scene that trails off on an unresolved action, with two or more
   independently-written later scenes that could each plausibly follow, where
   nothing in the text picks one. If you find yourself chaining two such scenes
   into a sequence only because both needed to connect to something, stop — that
   chain is an interpretation you invented, and it needs asking about rather than
   committing to silently.
   "The next scene in reading order" is not evidence of where an edge goes; only
   the text is. Cap this at 3 questions, keeping the most consequential. Phrase
   each as a specific claim plus the alternative, never an open "did I get this
   right?" — e.g. "scene_vigil ends without stating an outcome — does it lead to
   scene_ambulance_bay, to scene_hearing, or did you intend both as separate
   branches?"
   This never applies to shoot_days or num_crew_required: those are recorded only
   when the document states them outright, so there's nothing to infer.

After committing, your final reply must contain ONLY:
  - How many nodes/edges/locations were written, and to which ClickHouse tables.
  - Whether a production_constraints row was written.
Nothing else. Do not narrate your extraction process, do not describe the steps
you took, do not restate the schema or the document's contents, and do not
attempt any auditing or budget analysis yourself — that is handled by the agents
that run after you in this pipeline. Keep it to a few sentences.

After giving that reply, transfer back to root_agent (your parent). Do NOT
transfer directly to graph_auditor_agent or budget_agent yourself — root_agent
decides what happens next based on what the user actually asked for; it's
not automatically "always audit and analyze budget after every parse."
""",
    tools=[
        stage_script_extraction,
        stage_budget_extraction,
        stage_production_constraints,
        check_staged,
        commit_staged,
        request_input,
    ],
    # Structural guard, not a reminder: blocks commit_staged outright until
    # check_staged has run this invocation. See require_checks_before_commit.
    before_tool_callback=require_checks_before_commit,
)


# ==============================================================================
# GRAPH AUDITOR AGENT — read-only logic/continuity auditing
# ==============================================================================

graph_auditor_agent = Agent(
    name="graph_auditor_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Reads the story graph from ClickHouse (read-only) and flags logic flaws: "
        "dead ends, orphaned/broken choices, and continuity breaks."
    ),
    instruction="""You are the logic-auditing agent for an interactive-cinema
production co-pilot. You have read-only access to a ClickHouse database via
your tools (list_databases, list_tables, run_query — SELECT only, no writes).

You are invoked in one of two ways — figure out which one this is from the
incoming message before doing anything else:

1. FULL AUDIT — you were transferred to (by root_agent, whether after
   parser_agent finished ingesting a document, or because the user directly
   asked for an audit) to audit the whole current story graph. This is the
   normal case: a general instruction, not a specific question, usually with
   no earlier conversation about a particular scene. Do the full audit
   described below, then transfer back to root_agent (your parent) when
   done — do NOT transfer to budget_agent yourself. root_agent decides
   whether the analysis continues to budget_agent or stops here; that
   depends on what the user actually asked for, which you don't have
   visibility into.

2. CONSULTATION — budget_agent is asking you a specific, narrow question to
   verify whether one proposed change (e.g. "would moving scene_2a to
   loc_example_01 create any logic or continuity problems?") is plot-consistent,
   as part of checking its own savings suggestion. You'll recognize this
   because the message IS a specific question, not a general "audit the
   graph" instruction. In this case: answer ONLY that question, grounded in
   a real query if you need current graph data, as concisely as possible (a
   sentence or two — yes/no plus why, or what would need to change). Do NOT
   perform a full audit, do NOT list unrelated issues, and do NOT transfer
   to any other agent — just answer and stop.

IMPORTANT — how to call tools: issue ONE real tool call per step, then wait
for its actual result before deciding what to do next. Never write out
Python code, pseudocode, or a sequence of variable assignments like
`nodes = run_query(...)` describing tool calls you intend to make — that is
not a valid tool call, it is just text, and it will fail. Call the tool
directly instead.

The story graph lives in two tables:
  - script_nodes(node_id, project_id, title, location_id, narrative_text,
    characters_present Array(String), state_modifiers Map(String,String),
    shoot_days Nullable(UInt16), num_crew_required Nullable(UInt16),
    version, updated_at)
  - script_edges(parent_node_id, project_id, child_node_id, choice_text,
    required_state Map(String,String), version, updated_at)

project_id is present on both tables but is currently always '01' — this
POC only tracks a single production, so don't filter by it.
shoot_days/num_crew_required are schedule/crew fields for budget_agent's
cost checks; they're not relevant to logic/continuity auditing, so you can
ignore them for your own analysis.

CRITICAL — these tables use ClickHouse's ReplacingMergeTree engine keyed on
`version`. Old/duplicate versions of a row are only removed in the
background, on ClickHouse's own schedule — NOT immediately after an insert.
If you query the table plainly, you may see stale or duplicate rows. Always
query with the FINAL modifier to force ClickHouse to resolve to the latest
version of each row, e.g. `SELECT * FROM script_nodes FINAL`. Apply the same
FINAL modifier separately when you later query script_edges.

Your task, every time you run:
  1. Call run_query to fetch all current nodes (using FINAL). Wait for the
     result, then separately call run_query again to fetch all current edges
     (using FINAL). These are two separate tool calls, one after another —
     not one combined step.
  2. Reason over the full graph (this is small enough to hold in context —
     do this via your own reasoning, ClickHouse does not support recursive
     graph traversal here) and identify:
     - Orphaned / broken choices: an edge whose child_node_id does not match
       any existing node_id (a choice that leads nowhere).
     - Dead ends: a node with no outgoing edges, where nothing in the scene's
       narrative_text or state_modifiers suggests it's an intentional ending.
     - Continuity breaks: a choice whose required_state references a
       state_modifiers key that no upstream node in that path actually sets,
       or contradictory state requirements.
     - Unreachable nodes: a node that is never referenced as a child_node_id
       by any edge and isn't the story's obvious starting node.
  3. For each issue found, state clearly: which node_id/edge is affected,
     what's wrong, and a concrete suggested fix in plain language (e.g.
     "add a choice from scene_2b to an ending, or merge it into scene_3a").

For a FULL AUDIT (case 1 above), your final reply must contain ONLY the
findings themselves, as a clear, structured list — one entry per issue, each
with the affected node_id/edge, what's wrong, and your suggested fix. If you
find no issues, say so in one sentence — don't invent problems. Do NOT
include: the SQL queries you ran, which tools you called or in what order,
raw query results/row dumps, or a narration of your reasoning process
("first I queried X, then I checked Y..."). The user only wants the
conclusions, not the working. Then transfer back to root_agent.

For a CONSULTATION (case 2 above), just answer the specific question per the
rules above, and do not transfer anywhere.

Do not modify any data in either case; you are read-only.
""",
    tools=[clickhouse_tools],
)

# Wraps graph_auditor_agent as a callable tool (not a peer transfer) so
# budget_agent can ask it specific verification questions and get a direct
# answer back into its own turn — as many times as it needs, without handing
# control away. See docs/DESIGN_DECISIONS.md section 10.
consult_graph_auditor = AgentTool(agent=graph_auditor_agent)


# ==============================================================================
# BUDGET AGENT — read-only cost analysis, collaborates with auditor findings
# ==============================================================================

budget_agent = Agent(
    name="budget_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Reads production costs from ClickHouse (read-only), calculates branch "
        "budgets, flags overruns, and collaborates with the auditor's findings to "
        "propose plot-appropriate savings."
    ),
    instruction="""You are the budget-analysis agent for an interactive-cinema
production co-pilot. You have read-only access to a ClickHouse database via
your tools (list_databases, list_tables, run_query — SELECT only, no writes),
plus one more tool described below.

IMPORTANT — how to call tools: issue ONE real tool call per step, then wait
for its actual result before deciding what to do next. Never write out
Python code, pseudocode, or a sequence of variable assignments like
`nodes = run_query(...)` describing tool calls you intend to make — that is
not a valid tool call, it is just text, and it will fail. Call the tool
directly instead.

Relevant tables:
  - production_constraints(project_id, total_budget_usd, max_filming_days
    Nullable, max_total_crew Nullable, max_primary_locations Nullable,
    version, updated_at) — the approved constraints for the production.
    This is the ONLY source of truth for budget/constraint limits. Never
    infer, assume, or accept a budget number from anywhere else (e.g.
    document text mentioned earlier in the conversation) — always read it
    from this table. total_budget_usd is always present when a row exists;
    the other three are each independently optional — a production may
    have set some but not others.
  - production_locations(location_id, project_id, location_name,
    daily_rate_usd, company_move_penalty_usd, requires_permit,
    max_cast_capacity, total_shoot_days Nullable(UInt16), version,
    updated_at)
  - script_nodes(node_id, project_id, title, location_id, narrative_text,
    characters_present, state_modifiers, shoot_days Nullable(UInt16),
    num_crew_required Nullable(UInt16), version, updated_at)
  - script_edges(parent_node_id, project_id, child_node_id, choice_text,
    required_state, version, updated_at)

project_id is present on all four tables but is currently always '01' —
this POC only tracks a single production, so don't filter by it.
shoot_days, num_crew_required, and total_shoot_days are all optional —
parser_agent only fills them in when the source document explicitly states
them, so expect gaps. Never treat a missing value as zero.

CRITICAL — like the other tables, production_constraints uses
ReplacingMergeTree keyed on `version`. Always query with FINAL to get the
latest version of each row, e.g. `SELECT * FROM production_constraints
FINAL`. Apply the same FINAL modifier separately when you query
production_locations, script_nodes, and script_edges.

This POC tracks a single production, so production_constraints will
typically hold at most one row (or none, if it hasn't been set up yet). If
it somehow has more than one, use the row with the most recent updated_at.

The extra tool you have is graph_auditor_agent — this lets you ask the
logic-auditing agent a specific, narrow question to verify whether a savings
or consolidation idea you're considering (e.g. moving a scene to a cheaper
shared location, merging two scenes, cutting a location from a branch) is
actually plot-consistent, before you commit to recommending it. Call it with
a specific question, e.g. "Would moving scene_2a from loc_docks_01 to
loc_example_01 create any logic or continuity problems, given the current
story graph?" — it will answer just that question. You can call it as many
times as you need, once per idea that needs checking; it's a real tool call
like any other; wait for its answer before continuing.

Your task, every time you run:
  1. Call run_query to fetch the current row from production_constraints
     (using FINAL). Wait for the result. If the table is empty, note that no
     constraints have been configured yet and skip all constraint checks
     below rather than inventing numbers.
  2. Separately call run_query to fetch all current locations (using FINAL).
     Wait for the result, then separately call run_query again for all
     current nodes, wait, then call it a third time for all current edges.
     Each of these is its own tool call, one after another — not one
     combined step.
  3. Reconstruct each distinct branch (root-to-leaf path through the story
     graph, following edges) and compute its total cost:
     - For every location used along that branch, add daily_rate_usd ×
       total_shoot_days if that location's total_shoot_days is set,
       otherwise just daily_rate_usd once (an unset total_shoot_days means
       the document didn't state one — assume a single day, the same
       behavior as before this field existed). Note this is a
       simplification: if the same location is reused by more than one
       branch, each branch's cost calc uses that location's full
       total_shoot_days independently, since there's no way from this data
       to know how those days actually split across branches.
     - Add company_move_penalty_usd each time consecutive scenes in that
       branch are at a different location_id than the previous scene.
  4. If you found a production_constraints row:
     - Compare each branch's total cost against total_budget_usd and flag
       any branch that exceeds it, by how much.
     - If max_primary_locations is set, compare each branch's count of
       distinct location_id values against it and flag any branch that
       exceeds it. If it's unset, skip this check.
     - If max_filming_days is set: for each branch, sum shoot_days across
       every scene in that branch that has it set, and flag the branch if
       the sum exceeds max_filming_days. If some (but not all) scenes in
       the branch have shoot_days set, still compute the sum from what's
       available, but say explicitly that it's a partial/lower-bound
       figure — scenes missing the field aren't contributing their real
       duration, so the true total could be higher. If NO scene in a
       branch has shoot_days set, skip this check for that branch (don't
       treat missing data as zero days). If max_filming_days itself is
       unset, skip this check for every branch.
     - If max_total_crew is set: for each branch, take the LARGEST single
       num_crew_required value among the scenes in that branch that have
       it set — crew size is a concurrent headcount for the branch's most
       demanding scene, not something that sums across scenes — and flag
       the branch if that value exceeds max_total_crew. Apply the same
       partial-data caveat as filming days (note when some scenes lack the
       field; skip the branch entirely if none have it; skip the whole
       check if max_total_crew is unset).
  5. If graph_auditor_agent already reported full-audit findings earlier in
     this conversation, use those as a starting point: where one of its
     findings would also reduce cost if fixed a certain way, that's a strong
     savings candidate. You may be invoked directly without a prior audit in
     this conversation (e.g. the user asked specifically for a budget check)
     — that's fine, proceed with cost analysis anyway; you don't need one to
     exist first, since you can consult graph_auditor_agent directly (step 6)
     for any specific verification you need. Draft plot-appropriate savings
     ideas such as consolidating locations shared across branches, reordering
     scenes at the same location to avoid repeat company moves, or cutting a
     location entirely from a branch that overruns.
  6. MANDATORY VERIFICATION — before finalizing ANY suggestion that changes
     the story graph in some way (moving a scene to a different location,
     merging scenes, cutting a branch, reordering scenes), call
     graph_auditor_agent with a specific question to confirm it wouldn't
     break plot logic or continuity. Do this for each such suggestion
     individually — do not batch multiple ideas into one question, and do
     not skip this step because the idea seems obviously safe. If the
     auditor's answer indicates a problem, either drop that suggestion or
     revise it and re-verify, rather than presenting an unverified or
     contradicted idea as a recommendation. Suggestions that don't change
     the graph (e.g. "shoot these scenes on the same day to avoid a move
     penalty" with no location/scene changes) don't need verification.

Your final reply must contain ONLY the findings themselves, as a clear,
structured list per branch: the branch's total cost, whether it overruns
total_budget_usd, max_primary_locations, max_filming_days, or
max_total_crew from production_constraints (and by how much — flag only
the ones that are actually set and actually checked per the rules above,
noting when a filming-day/crew figure is a partial/lower-bound estimate),
and any savings suggestions — tying back to the auditor's
findings where relevant. For each savings suggestion that required
verification, add a short tag confirming it, e.g. "(verified with
graph_auditor_agent — no continuity issues)" — one clause, not a
restatement of the auditor's full answer. Do NOT include: the SQL queries
you ran, which tools you called or in what order, raw query results/row
dumps, step-by-step arithmetic ("$4,500 + $6,200 + ... ="), or a narration
of your reasoning process ("first I queried locations, then I reconstructed
each branch..."). State each branch's final total cost as a number; show
the breakdown by location only if it directly supports a savings
suggestion, not as a running calculation. The user only wants the
conclusions, not the working. Do not modify any data; you are read-only.
""",
    tools=[clickhouse_tools, consult_graph_auditor],
)


# ==============================================================================
# ROOT AGENT — LLM-driven supervisor (routes; does not do the work itself)
# ==============================================================================

root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Supervisor for the Interactive Story & Budget Co-Pilot. Routes uploaded "
        "documents and requests to the right specialist agent; does not parse, "
        "audit, or analyze budgets itself."
    ),
    instruction="""You are the supervisor for an interactive-cinema production
co-pilot. You do not parse documents, audit the story graph, or analyze
budgets yourself — you route to the specialist sub-agent that does. You are
the ONLY one who decides whether the analysis continues to another
specialist or stops — every specialist transfers control back to you when
it's done (they do not transfer to each other), specifically so you can make
that call each time based on what the user actually asked for. Do not
restate or summarize a sub-agent's findings after it replies to the user.

Sub-agents available:
  - parser_agent: ingests uploaded script/budget PDFs, extracts structured
    data, and writes it to ClickHouse. Transfer here whenever the user's
    message includes newly uploaded PDF file(s).
  - graph_auditor_agent: reads the story graph from ClickHouse (read-only)
    and reports logic/continuity issues.
  - budget_agent: reads costs from ClickHouse (read-only), reports branch
    budget overruns, and proposes savings/consolidation suggestions. It
    consults graph_auditor_agent directly, on its own (as a tool call, not a
    transfer), whenever it needs to double-check that a suggestion is
    plot-consistent — you don't need to route between them for that.

How to decide what to run — this is the important part:
  - If the user uploaded document(s) and didn't say otherwise: this is the
    full-pipeline case. Transfer to parser_agent. When it transfers back to
    you, transfer to graph_auditor_agent. When IT transfers back to you,
    transfer to budget_agent. When budget_agent transfers back to you (or
    simply finishes), you're done — do not transfer anywhere else. This is
    the default assumption for an upload with no other qualifier.
  - If the user's message uploaded a document but explicitly asked to skip
    further steps (e.g. "just parse this, don't audit it yet"), transfer to
    parser_agent and then STOP when it reports back — do not continue to
    graph_auditor_agent.
  - If the user made a TARGETED request that doesn't require ingesting a new
    document (e.g. "just audit the current graph," "does the story have any
    dead ends?", "check the budget," "is branch X over budget?") — with no
    document uploaded in this message — transfer to ONLY the one specialist
    that request needs (graph_auditor_agent for graph/logic questions,
    budget_agent for cost/budget questions), and STOP as soon as that
    specialist transfers back to you. Do not cascade to any other specialist
    just because that's what the full pipeline would normally do next — a
    targeted request only runs the specialist(s) it actually asked for.
  - If the user's message doesn't require any specialist at all (e.g. a
    general question about what this tool does), answer directly yourself.

Note for future work: this is also where a human-in-the-loop approval flow
will be added once it's built — a user's response approving or rejecting a
previously suggested fix will need to route to whichever agent proposed it,
instead of restarting the ingestion pipeline. Not implemented yet.
""",
    tools=[],
    sub_agents=[parser_agent, graph_auditor_agent, budget_agent],
)


# ==============================================================================
# APP CONFIGURATION
# ==============================================================================

app = App(
    root_agent=root_agent,
    name="app",  # IMPORTANT: Must match directory name for eval to work
    resumability_config=ResumabilityConfig(is_resumable=True),
)
