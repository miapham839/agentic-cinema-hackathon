# POC Testing Guide — Interactive Story & Budget Co-Pilot

This explains what was built, how to test it end to end using `adk web`, and
what you should expect to see. It assumes no prior ADK or ClickHouse
background — concepts are explained as they come up. For *why* things were
built this way, see `DESIGN_DECISIONS.md`.

---

## 1. What was built

Four ClickHouse tables already existed in your `default` database
(`script_nodes`, `script_edges`, `production_locations`,
`production_constraints` — see section 3 for their columns). On top of them,
this pass built a 3-agent pipeline with an LLM supervisor:

```
root_agent (LLM supervisor — routes, doesn't do the work itself,
            decides after EVERY hand-back whether to continue)
├── parser_agent          reads your uploaded PDF(s), writes structured rows to
│                         ClickHouse, then transfers back to root_agent
├── graph_auditor_agent   reads the story graph, flags logic problems,
│                         then transfers back to root_agent
└── budget_agent          reads costs, flags budget overruns, and proposes
                           savings ideas — consulting graph_auditor_agent
                           directly (as a tool, not a handoff) to verify each
                           idea is plot-consistent before finalizing it;
                           doesn't transfer anywhere
```

Every specialist reports back to `root_agent`, never directly to the next
specialist — `root_agent` decides, every time, whether to continue the
pipeline or stop, based on what the user actually asked for:
- **Uploaded a document, no other instruction:** full pipeline —
  `parser_agent` → `graph_auditor_agent` → `budget_agent`, automatically.
- **Uploaded a document but said "don't audit yet" (or similar):** stops
  after `parser_agent`.
- **Targeted request, no upload** (e.g. "just audit the graph," "check the
  budget"): transfers to only that one specialist and stops as soon as it
  reports back — it will NOT cascade into the other specialists just
  because that's what the full pipeline would normally do.
- **No specialist needed** (a general question about the tool): `root_agent`
  answers directly.

This is an LLM-driven supervisor, not a `SequentialAgent` (which always ran
all three in a fixed order) — see `DESIGN_DECISIONS.md` sections 1 and 11
for why: in short, a human-approval step is coming later and needs real
routing, `budget_agent` needed a way to actively double-check its ideas
rather than just reading the auditor's report passively, and a targeted
single-agent request needs the pipeline to *not* cascade unnecessarily.

**What each agent can touch:**
- `parser_agent` can only **write** (via a custom tool built on the
  `clickhouse-connect` Python client) — it has no read access.
- `graph_auditor_agent` and `budget_agent` can only **read** (via the
  `mcp-clickhouse` MCP server, in its default read-only mode) — neither can
  write anything.
- `budget_agent` additionally has `graph_auditor_agent` itself as a tool
  (`consult_graph_auditor`), so it can ask targeted verification questions
  and get a direct answer, as many times as it needs, without handing off
  control. See `DESIGN_DECISIONS.md` section 10.

## 2. What's in `mock_data/`

There are **three** sets of fixtures. They test different things, and they are
deliberately kept apart. Pick the right one for what you are doing.

| Set | Files | Use it for |
|---|---|---|
| Pipeline | `mock_script.pdf`, `mock_budget.pdf` | The full 3-agent run: parse, audit, budget |
| HITL dev | `hitl_script.pdf`, `hitl_budget.pdf`, `hitl_degenerate.pdf` | Watching `parser_agent` ask questions |
| Held-out | `heldout_script.pdf`, `heldout_budget.pdf`, `heldout_degenerate.pdf` | Measuring whether the rules actually work |

**Why three sets and not one.** The instruction in `app/agent.py` contains
example text to teach the model what an ambiguous location looks like. If a
test document uses those same words, the model can match the words instead of
understanding the idea — and the test proves nothing. This happened once
already: fixture names were pasted into the instruction, two rules appeared to
start working, and the result was meaningless. So the three vocabularies share
nothing: the instruction uses a hospital setting, the HITL dev files use noir,
and the held-out files use a period drama.

`tests/unit/test_fixture_isolation.py` fails the build if held-out names ever
appear in the instruction again.

### 2a. Pipeline fixtures — `mock_script.pdf` + `mock_budget.pdf`

A 4-scene branching script, "The Diner Standoff," and a producer's budget memo
for the same sequence. Regenerate with:

```bash
uv run --with fpdf2 python mock_data/generate_mock_pdfs.py
```

### 2b. HITL dev fixtures — `hitl_*.pdf`

"The Tip." Built to make `parser_agent` ask questions. Regenerate with:

```bash
uv run --with fpdf2 python mock_data/generate_hitl_test_pdfs.py
```

Upload `hitl_script.pdf` and `hitl_budget.pdf` **in the same message**. Some
checks compare the two documents against each other, so they do nothing if only
one is present.

`hitl_degenerate.pdf` is a crew catering memo with no scenes at all. Upload it
alone, as if it were the script.

Five problems are hidden in these documents on purpose:

| # | The problem | Found by |
|---|---|---|
| 1 | `hitl_degenerate.pdf` has no scenes | code |
| 2 | The budget memo states a crew cap and a location cap, but never a total in dollars | code |
| 3 | Two location names that may mean one place, and one name that may mean either of two places | the model |
| 4 | A scene ends with no stated outcome, and two later scenes could each follow | the model |
| 5 | Script scenes at Harbor Warehouse add to 3 shoot days; the budget says 2 | code |

"Found by code" means a Python function in `app/checks.py` finds it. Same input,
same answer, every time. "Found by the model" means it needs someone to read the
prose and judge what the words mean. Section 5 covers how reliable each is.

### 2c. Held-out fixtures — `heldout_*.pdf`

"The Ashfield Inheritance," a period drama. Used by the automated measurement in
`tests/eval/test_hitl_rules.py`. The text also lives as plain strings in
`mock_data/heldout_fixtures.py`, because the test harness feeds text rather than
PDFs.

Do not use these for casual manual testing, and do not copy their wording into
`app/agent.py`. Once their words are in the instruction, they stop measuring
anything.

```bash
uv run --with fpdf2 python mock_data/heldout_fixtures.py
```

**Neither document uses schema-shaped labels anywhere** — no "Location ID:",
"State Change:", "Characters Present:", or "CHOICE: ... leads to ...". This
is deliberate: real uploaded scripts and budget memos won't hand
`parser_agent` pre-labeled fields, so the mock data is written the way a
real document would be, to prove `parser_agent` can actually infer the
structured data rather than just copy labeled fields into a matching shape.
See `DESIGN_DECISIONS.md` section 8 for the full rationale.

These two documents were **written together on purpose**, with two
intentional problems baked into the narrative so you can watch the agents
actually catch something instead of just reporting "looks fine":

1. **A broken choice.** The opening scene's "pull the trigger" fork leads to
   "Blood on the Floor," which in turn offers a "turn back for Jamie" fork
   into a beat called "The Getaway Gone Wrong" — a beat that's referenced
   but never actually written anywhere in the document (the way an
   unfinished draft branch looks in real life). `graph_auditor_agent` should
   flag this as an orphaned/broken choice.
2. **An unmarked dead end.** "An Uneasy Truce" (reached by talking Sam down
   instead of shooting) has no further fork and nothing in its text says
   it's an ending — unlike "The Old Warehouse," which explicitly closes out
   the sequence in its own prose ("the sequence ends here"). `graph_auditor_agent`
   should flag "An Uneasy Truce" as a dead end, but *not* flag "The Old
   Warehouse."
3. **A budget overrun.** The budget memo states a roughly **$12,000** total
   cap, a 2-day shoot limit, a 10-person crew cap, and a 2-location target —
   in prose, not a labeled section. `parser_agent` extracts these into
   `production_constraints` (see section 3.1), and `budget_agent` reads them
   from there. The "pull the trigger" branch (diner → docks → warehouse)
   costs about **$18,000** and touches 3 distinct locations — over both the
   dollar cap and the location-count target. The "talk it out" branch
   (diner → diner, no move) costs about **$9,000** and touches 1 location —
   under both, but it's the branch with the dead end from #2. `budget_agent`
   should flag the overrun branch on both counts, and ideally connect the
   two findings (e.g. "the talk-it-out branch is cheaper and within the
   location target, but currently a dead end").

**Because nothing is explicitly labeled, expect some details to vary
between runs, and that's fine.** Exact `state_modifiers` key names (e.g.
`trust_level` vs. `tension`) are inferred, not stated in the document, so
they may differ run to run — don't treat that as a bug. What should stay
stable: the 4-scene structure, the broken-choice and dead-end findings, the
diner/docks/warehouse cost mapping, and the dollar/location overrun on the
"pull the trigger" branch. If any of *those* drift, that's worth
investigating.

## 3. The ClickHouse tables (for reference)

All three use the `ReplacingMergeTree` engine, keyed on a `version` column —
see `DESIGN_DECISIONS.md` section 5 for why that matters for reads.

```
script_nodes(node_id, project_id, title, location_id, narrative_text,
             characters_present Array(String),
             state_modifiers Map(String,String),
             shoot_days Nullable(UInt16),
             num_crew_required Nullable(UInt16),
             version, updated_at)

script_edges(parent_node_id, project_id, child_node_id, choice_text,
             required_state Map(String,String),
             version, updated_at)

production_locations(location_id, project_id, location_name, daily_rate_usd,
                      company_move_penalty_usd, requires_permit,
                      max_cast_capacity,
                      total_shoot_days Nullable(UInt16),
                      version, updated_at)

production_constraints(project_id, total_budget_usd,
                        max_filming_days Nullable(UInt32),
                        max_total_crew Nullable(UInt32),
                        max_primary_locations Nullable(UInt32),
                        version, updated_at)
```

`Nullable` means the column may be empty. `shoot_days`, `num_crew_required` and
`total_shoot_days` are filled in only when a document states them outright.
Most documents don't. Empty is normal, not a parsing failure.

`project_id` is on all four tables and is always `'01'`. This POC tracks one
production. It is set in `app/tools.py`, not by the model, so every table
agrees. Don't filter on it.

`parser_agent` writes every row with `version = 1` (this POC only ever does
the *initial* ingestion — see `DESIGN_DECISIONS.md` section 4 for what a
future "apply an approved fix" write path would need to do differently).

### 3.1 `production_constraints` is populated by `parser_agent`, if the document states it

`parser_agent` reads the constraints out of the budget memo's prose — no
labeled section to look for. With `mock_budget.pdf` that's the opening
paragraph mentioning "twelve thousand dollars," "two shooting days," "ten
bodies," and "two hero locations."

`total_budget_usd` is the one field that must be present. `budget_agent` has
nothing to compare spending against without it. The other three
(`max_filming_days`, `max_total_crew`, `max_primary_locations`) are each
optional and stay empty when the document doesn't state them. No question is
asked about those.

**If the document states no total budget, the agent asks you.** It does not
guess, and it no longer just skips the row silently. You get three options: a
placeholder figure it calculates, a number you type, or skip budget checks
entirely. See section 5.

`max_filming_days` and `max_total_crew` are now enforced, not just reported —
`budget_agent` checks them against per-scene `shoot_days` and
`num_crew_required` when those exist. See `BUDGET_AGENT_LOGIC.md`.
`max_primary_locations` is enforced as before.

To test a different constraint value without editing the PDF, overwrite the row
directly:
```sql
INSERT INTO production_constraints
    (project_id, total_budget_usd, max_filming_days, max_total_crew, max_primary_locations, version)
VALUES
    ('01', 20000.00, 2, 10, 2, 2);
```
(bump `version` above whatever `parser_agent` last wrote — `1`, unless you've
re-uploaded — so ClickHouse's `ReplacingMergeTree` treats it as the newer row).

## 4. How to test it

1. Start the dev UI:
   ```bash
   agents-cli playground
   ```
   (or `adk web` directly, if you're running it outside `agents-cli`).

2. Open the chat, and use the **file upload button** to attach both PDFs to
   a single message: `mock_data/mock_script.pdf` and
   `mock_data/mock_budget.pdf`. Add a short message like "Please process
   these production documents."

   You can also upload just the script or just the budget doc on its own —
   `parser_agent` handles either PDF type independently and only writes to
   the table(s) relevant to what it received. One caveat: the shoot-day check
   compares the two documents, so it can't run on a single-document upload.

   To watch `parser_agent` ask questions instead, upload `hitl_script.pdf` and
   `hitl_budget.pdf` (section 2b) in one message. To see it refuse a bad file,
   upload `hitl_degenerate.pdf` on its own.

3. Send it. `root_agent` transfers to `parser_agent`, which reports back to
   `root_agent`, which continues to `graph_auditor_agent`, which reports
   back, and `root_agent` continues to `budget_agent` — you don't need to
   prompt each step separately or re-send anything. This will take
   noticeably longer than a normal single-agent chat turn: it's at least
   three specialist LLM calls plus several `root_agent` routing calls in
   between, plus several tool calls in sequence, and `budget_agent` may call
   `graph_auditor_agent` one or more additional times to verify its
   suggestions on top of that.

4. To test that targeted requests don't cascade unnecessarily (this is the
   whole point of section 1's routing rules — see `DESIGN_DECISIONS.md`
   section 11), try these in a **fresh session**, after the mock data has
   already been ingested once via step 2-3 above:
   - `"Just audit the current story graph, don't check the budget."` —
     expect only `graph_auditor_agent` to run. You should NOT see
     `budget_agent`'s reply at all.
   - `"Is the story currently within budget?"` — expect only `budget_agent`
     to run (it may still consult `graph_auditor_agent` internally via its
     tool if it wants to verify a suggestion, but that's a nested tool call,
     not a second top-level specialist reply).
   If either of these produces all three agents' replies instead of just
   one, that's the bug this section 4 exists to catch — `root_agent` cascaded
   when it shouldn't have.

## 5. What to expect in the response

You'll see each agent's reply appear as it happens (ADK's dev UI shows each
sub-agent's turn as it runs, so you'll see three distinct "messages" — one
per agent — rather than a single merged block; that's expected, not a bug).
You may also see `budget_agent`'s turn include one or more nested tool calls
to `graph_auditor_agent` (shown as a tool invocation, not a separate chat
message) — that's the verification loop from section 1, not an error.

### 5.1 `parser_agent` — what a correct run looks like

`parser_agent` runs four steps in order. You can watch each one in the dev UI as
a tool call.

```
stage_script_extraction   ->  drafts the scenes and choices. Writes nothing.
stage_budget_extraction   ->  drafts the locations. Writes nothing.
stage_production_constraints  ->  only if the document states a total budget.
check_staged              ->  runs the code checks. Takes no arguments.
adk_request_input         ->  asks you, once, if anything needs deciding.
commit_staged             ->  writes every table together.
```

**Correct order matters more than the exact tool list.** `check_staged` must
come before `commit_staged`. If it doesn't, `commit_staged` is refused outright
by a guard in `app/tools.py` and nothing is written. A run that stages, checks,
asks, then commits is healthy.

**On a clean document** (`mock_script.pdf` + `mock_budget.pdf`), there is nothing
to ask about, so no `adk_request_input` appears. You get a short confirmation:
how many nodes, edges and locations were written, and whether a
`production_constraints` row was written. It should not restate the constraint
numbers — `budget_agent` reports those.

**On the HITL documents** (`hitl_script.pdf` + `hitl_budget.pdf`), it pauses and
asks. All questions arrive in **one** message, not one at a time. Real output
from a run, lightly trimmed:

```
Before writing the parsed documents to the database, please clarify
the following points:

1. Budget cap:
This document doesn't state a total approved budget, and budget_agent
needs one to check for overruns. I can: (a) use $16,000 as a placeholder
(based on your locations' day rates and stated shoot days — a rough floor,
not a real estimate), (b) use the exact number if you tell me, or (c) skip
budget overrun checks for this project. Which would you like?

2. Shoot days conflict:
Your script's scenes at 'Harbor Warehouse' add up to 3 shoot days, but the
budget document states 2 total shoot days for that location. Which is
correct — 3, 2, or a different number?
```

The `$16,000` is calculated in Python, not by the model. It is each location's
day rate multiplied by its stated shoot days, or by 1 when unstated. For the
HITL budget that is `2600 + 4100x2 + 3400 + 1800`. The same formula
`budget_agent` uses, on purpose, so the placeholder can't disagree with the
numbers reported later.

Answer in plain English. "Use the placeholder", "3 days is right", "they're the
same location" all work. The agent turns your answers into small correction
records and then commits.

**On `hitl_degenerate.pdf`** (the catering memo), it should ask whether you
uploaded the right file, and write nothing.

### 5.2 How reliable each check is

This is measured, not assumed. `tests/eval/test_hitl_rules.py` runs the same
documents several times and counts how often each one fires.

| Check | Reliability | Why |
|---|---|---|
| Missing budget cap | Every run | Python: is the field empty? |
| Shoot-day conflict | Every run | Python: add the numbers, compare |
| Empty document | Every run | Python: is the scene count zero? |
| Scene with no cost data | Every run | Python: is the location in the budget? |
| Ambiguous location identity | **Unreliable** | Needs the model to judge the prose |
| Ambiguous scene destination | **Unreliable** | Needs the model to judge the prose |

The three code checks fired on every measured run. The two judgment calls did
not: on held-out documents, the destination question appeared in 1 run out of 3,
and the location question in 0 out of 3.

So: if the agent misses an ambiguous location name, that matches current known
behavior. It is a real gap, not a broken install. If it misses the shoot-day
conflict or the budget cap, something is genuinely wrong — those cannot be
skipped, because no decision is involved in running them.

**From `graph_auditor_agent`:** a structured list of issues. On this mock
data, expect it to call out:
- the "turn back for Jamie" choice (from "Blood on the Floor") as
  broken/orphaned — it leads to "The Getaway Gone Wrong," which is never
  actually written anywhere in the document,
- "An Uneasy Truce" as a dead end (no outgoing choices, no ending signal),
- and it should *not* flag "The Old Warehouse," since its own text
  explicitly ends the sequence.

**From `budget_agent`:** a per-branch cost breakdown, referencing the
auditor's findings. On this mock data, expect it to:
- total the diner → docks → warehouse branch at roughly $18,000 and flag it
  as ~$6,000 over the ~$12,000 cap, and separately flag it for touching 3
  locations against a target of 2,
- total the diner → diner branch at roughly $9,000 and note it's under both,
- suggest concrete savings (e.g. consolidating the docks/warehouse scenes
  back to the diner location to cut a company-move penalty and bring the
  branch under the location target too), and ideally tie this back to the
  dead-end finding on "An Uneasy Truce",
- for any suggestion that changes the graph (like the consolidation above),
  a short tag like "(verified with graph_auditor_agent — no continuity
  issues)" confirming it checked the idea before recommending it — this is
  the mandatory verification step from `DESIGN_DECISIONS.md` section 10. If
  a suggestion has no such tag and clearly changes the graph, that's worth
  flagging as a regression — the instruction requires it.

If you upload a budget document with no constraints section (or the table is
otherwise empty), `budget_agent` should instead say plainly that no
constraints are configured and it's skipping the overrun checks — that's
expected, not a bug.

An earlier version of this pipeline (the original `SequentialAgent` version,
before `production_constraints` existed and before the supervisor/consult-
tool architecture) was run end-to-end against an earlier version of this
mock data during development, and produced findings matching the above
pattern: correctly identifying the overrun, the dead end, and the broken
choice, and cross-referencing the auditor's findings in its savings
suggestions. The core cost/logic findings should be unaffected by the
architecture changes since then — only *how the pipeline routes* and *where
the budget cap comes from* changed. The verification-tag behavior described
above has not yet been run end-to-end since the supervisor/`AgentTool`
change; if you hit anything that doesn't match this guide, that's the part
most likely to need a follow-up fix. Minor wording will vary between runs
since it's an LLM, but the substance should match.

## 6. Verifying the data landed in ClickHouse (optional)

If you want to check the raw rows rather than trusting the agents' summary,
remember the `FINAL` requirement from `DESIGN_DECISIONS.md` section 5:

```sql
SELECT * FROM script_nodes FINAL;
SELECT * FROM script_edges FINAL;
SELECT * FROM production_locations FINAL;
SELECT * FROM production_constraints FINAL;
```

Without `FINAL`, you may see stale/duplicate rows if you query right after
an insert, before ClickHouse's background merge has run.

## 7. Known limitations of this POC (by design, not bugs)

- **Human-in-the-loop covers ingestion only.** `parser_agent` pauses and asks
  before writing (section 5.1). `graph_auditor_agent` and `budget_agent` still
  only *suggest* fixes in their text reply — approving one of their suggestions
  does not write corrected rows anywhere.
- **The two judgment-call checks are unreliable.** Measured at 1/3 and 0/3 on
  held-out documents. See section 5.2. The code checks are unaffected.
- **`node_id`s are not stable across runs.** Parsing the same script twice can
  name the same scene differently (e.g. `scene_opening_manifest` one run,
  `scene_port_authority_office` the next). ClickHouse deduplicates on `node_id`,
  so the two are treated as different scenes and both survive — leaving a
  duplicate scene and stale edges in the graph. Re-parsing the same script into
  a database that already holds it is not safe yet.
- **Re-uploading the same script twice** will insert a second `version = 1`
  row per `node_id`/edge — since both are literally version 1, ClickHouse
  won't treat one as "newer," so which one survives a background merge is
  unspecified (though harmless here, since the content would be identical
  from the same source doc). This is a non-issue until the approved-fix flow
  exists and starts writing `version = 2`, `3`, etc.
- **No cross-run graph merging.** If you upload two *different* scripts in
  separate turns, both get written as `version = 1` for whatever `node_id`s
  the model invents — there's no dedup/matching against previously ingested
  scenes from a prior turn.
