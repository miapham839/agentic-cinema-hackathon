# How `parser_agent` works, and why

`parser_agent` reads uploaded script and budget documents, turns them into
structured rows, asks you about anything genuinely unclear, then writes to
ClickHouse. This explains each step and the reason behind each design choice.

Source files: [`app/agent.py`](../app/agent.py) (the instruction),
[`app/tools.py`](../app/tools.py) (the tools), [`app/checks.py`](../app/checks.py)
(the automatic checks), [`app/schemas.py`](../app/schemas.py) (the data shapes).

---

## The short version

The agent runs four steps, in order:

```
1. STAGE   ->  2. CHECK   ->  3. ASK        ->  4. COMMIT
   (draft)      (code finds    (one question    (write everything
                 problems)      per turn)        to the database)
```

Nothing reaches the database until step 4.

The important idea: **code does the counting, the model does the reading.**
Anything that is arithmetic or a lookup runs in Python. Only judgment calls
about what the words mean go to the model.

---

## Step 1 — Stage

"Staging" means the agent hands over its draft, but nothing is written yet.
Think of it as putting a document in an outbox, not the mailbox.

The agent calls one tool per document:

| Tool | What it takes |
|---|---|
| `stage_script_extraction` | every scene and every choice, in one call |
| `stage_budget_extraction` | every location, in one call |
| `stage_production_constraints` | the overall budget and limits |

Each tool saves the draft into **session state**. Session state is a small
storage area attached to your conversation. It survives between messages, so the
agent can put something down now and pick it up later.

If the budget document never states a total budget, the agent skips
`stage_production_constraints` entirely. It must not invent a number. Step 2
catches the gap.

### Why the agent does not write here

The old version wrote each document straight to the database as it finished.
That caused a real failure: one run wrote the scenes, then silently skipped the
budget and constraints writes. No error appeared. The three tables ended up
disagreeing with each other.

Staging plus a single commit makes that impossible. Either everything is
written, or nothing is.

---

## Step 2 — Check

The agent calls `check_staged`. This tool takes **no arguments**. It reads the
drafts from session state by itself.

The checks live in [`app/checks.py`](../app/checks.py) and run in Python, not in
the model. They are **deterministic** — same input, same answer, every single
time. There is no chance of the model "not noticing."

| Check | What it does | Example |
|---|---|---|
| `EMPTY_SCRIPT_EXTRACTION` | No scenes found | You uploaded a catering memo by mistake |
| `EMPTY_BUDGET_EXTRACTION` | No costed locations found | Same, for the budget file |
| `MISSING_BUDGET_CAP` | No total budget stated | Memo lists day rates but never a total |
| `SHOOT_DAY_CONFLICT` | The two documents disagree | Script scenes at the stables add to 3 days; budget says 2 |
| `ORPHANED_LOCATION_REF` | A scene has no cost data | A scene at "Port Authority Office" that the budget never lists |

Each result comes back with a question already written for you, in
`suggested_question`. The agent does not have to phrase it.

### Why these moved out of the instruction

They used to be written as English rules in the agent's instruction. They fired
unpredictably.

`SHOOT_DAY_CONFLICT` was the worst case. The script said 2 days at one scene and
1 day at another, both at the stables. The budget said 2 days total. That is 3
against 2 — an obvious mismatch. The agent skipped it silently, more than once.

The reason is simple. Every other rule is something you notice while reading.
This one is not. It needs you to stop, group scenes by location, add numbers,
then compare against a second document. That is a separate deliberate pass, and
it sat last in a five-rule list inside a very long instruction.

Adding numbers is not a judgment call. Python adds them the same way forever.

### The budget floor, calculated in code

When no total budget is stated, `compute_budget_floor` works out a rough figure:

```
for each location:  daily_rate_usd x total_shoot_days (or x1 if not stated)
```

For the held-out fixture that is `2400 + 1900x2 + 900 + 1100 + 1500`.

This deliberately copies `budget_agent`'s own cost formula. If the two
calculations disagreed, the placeholder offered here would not match the numbers
`budget_agent` reports later.

---

## Step 3 — Ask

Everything that needs your input goes into **one** `request_input` call. Not one
call per question.

`request_input` pauses the agent and waits for you. It sends:

- a `message` — the questions, in plain text
- a `response_schema` — one named slot per answer, so each reply maps back to
  the question it answers

Two of the five rules cannot be decided by code. These are the model's job,
because they need someone to read the prose and judge meaning.

### Judgment call A — is this the same place?

Two directions, both worth asking about.

**One name, several candidates.** A scene says "the cottage." The budget lists
"Tenant Cottage" and "Gamekeeper's Cottage." Which one?

**Two names, possibly one place.** A scene is headed `INT. THE LONG GALLERY`.
The budget lists "Ashfield Manor." Those names share no words at all. But the
Long Gallery may simply be a room inside the manor.

That second case is the one that gets missed. If you match on how similar names
look, you find nothing — there is no overlap to find. You have to judge by the
story instead: same characters, scene continuing from the last one, no line
describing a move to somewhere new.

Getting this wrong is expensive in both directions. Split one real location into
two, and its costs get divided across two rows. Merge two real locations into
one, and you lose a location's costs entirely.

### Judgment call B — where does this scene lead?

Ask when the text genuinely does not say.

**Not this:** the writer wrote a fork — "she could go to the stables, or she
could go to the cottage." That is a normal branching choice. Record both. Do not
ask.

**This:** a scene trails off with no outcome. She holds the letter over a candle
and stands there. Two later scenes are written. Either could follow. Nothing in
the text picks one.

The old version handled this badly. It did not just skip the question — it
invented an answer. It chained the two endings into a sequence, one after the
other, and wrote a connecting choice that appears nowhere in the script. The
instruction now names that exact mistake: if you are linking two scenes only
because both needed to connect to something, stop and ask.

### Why one call and not several

You see all the questions together and answer once. Several calls would mean
answering, waiting, answering again.

---

## Step 4 — Commit

`commit_staged` applies your answers and writes every table together.

Your answers arrive as **corrections** — small records, not rewritten documents.
There are five kinds:

| Correction | Used for |
|---|---|
| `set_total_budget` | You gave a budget figure |
| `skip_constraints` | You chose to skip overrun checks |
| `set_location_total_shoot_days` | You settled a shoot-day disagreement |
| `merge_locations` | You confirmed two names are one place |
| `set_node_edges` | You settled where a scene leads |

`merge_locations` does real work in Python: it repoints every scene from the old
location to the kept one, then removes the duplicate cost row.

### Why corrections instead of a corrected document

This is about **serialization** — turning data into text to send it over the
wire. A 40-scene script becomes thousands of words of JSON every time it moves.

The old approach sent the whole extraction twice: once to write, and again with
the fix applied. Large repeated payloads were involved in a real failure earlier
in this project, `MALFORMED_FUNCTION_CALL`, where the model produced a broken
tool call instead of a valid one.

Corrections keep it to one trip. "Set the stables to 3 days" is a handful of
words, whatever the script's size.

### The guard that makes order real

`require_checks_before_commit` in [`app/tools.py`](../app/tools.py) blocks
`commit_staged` unless `check_staged` has already run. It is a
`before_tool_callback` — code that runs before a tool and can cancel it.

Order used to be an instruction. Instructions get dropped. This one cannot be.

---

## Two decisions that are easy to get wrong

### The data shapes are not in the instruction

[`app/schemas.py`](../app/schemas.py) defines the shape of the data — which
fields exist, which are required. A **JSON schema** is that shape written out as
text the model can read.

The instruction used to paste those schemas in full. That was 8,725 characters,
about 2,181 tokens, 29% of the whole instruction. (A **token** is roughly
three-quarters of a word. It is the unit models are billed and limited by.)

It was pure duplication. ADK already sends the same schema automatically, in the
**tool declaration** — the description of a tool that goes with every request. I
checked the generated copy against the pasted one. Byte for byte identical.

One catch made this worth care. The automatic copy carries each field's
`description=` text, but not prose from the instruction. So rules like "derive
`location_id` as a lowercase snake_case slug" had to **move into**
`app/schemas.py`, not just be deleted.

[`tests/unit/test_tool_declarations.py`](../tests/unit/test_tool_declarations.py)
guards this. The ADK feature that generates the schema is marked experimental.
If it is ever turned off, the fallback path drops every field description
silently — no error, just an agent that quietly forgets how to build a
`location_id`.

### Staged drafts use ordinary session state, not `temp:`

ADK offers a `temp:` prefix for values meant to last one turn. It looks like the
obvious fit for a draft.

It would have broken this. ADK strips `temp:` keys before saving
(`_trim_temp_delta_state` in `sessions/base_session_service.py`). They live only
in memory.

`request_input` pauses and waits for you, which can mean minutes and a fresh
request. This project also swaps in `VertexAiSessionService` when
`GOOGLE_CLOUD_AGENT_ENGINE_ID` is set, which reloads the conversation from
storage. **Persisted** state means saved to storage and reloadable. `temp:` is
not persisted, so the drafts would have vanished the moment you answered.

The staged keys are ordinary ones. `commit_staged` clears them when it is done.

---

## How we know it works

### Fast tests, no cost

[`tests/unit/test_extraction_checks.py`](../tests/unit/test_extraction_checks.py)
covers the automatic checks. No model calls, no database. 21 tests.

It covers the negative cases too, which matter as much. If one scene at a
location has no shoot-day figure, the totals will not match — but that is
missing data, not a disagreement. Asking about it would be wrong.

### Measuring the model, honestly

[`tests/eval/test_hitl_rules.py`](../tests/eval/test_hitl_rules.py) runs the same
documents several times and reports how often each rule fires, like
`SHOOT_DAY_CONFLICT: 5/5`.

Run it with:

```bash
RUN_HITL_EVAL=1 uv run pytest tests/eval/test_hitl_rules.py -s
```

It costs real model calls, so it is off by default. It replaces the ClickHouse
client with a recorder, so measuring never writes to the real database.

Models do not give the same answer every time. One good manual run tells you
nothing about the next one. A rate does.

### Test data is kept separate from the instruction's examples

Three sets of names, deliberately with nothing in common:

| Used for | Domain | File |
|---|---|---|
| Examples in the instruction | hospital | `app/agent.py` |
| Manual walkthroughs | noir | `mock_data/generate_hitl_test_pdfs.py` |
| Measurement | period drama | `mock_data/heldout_fixtures.py` |

This fixes a real mistake. An earlier round put test names straight into the
instruction — `INT. PRECINCT`, `INT. 14TH STREET STATION`, `scene_ledger_call`.
The rules then appeared to start working. But there was no way to tell whether
the model had understood the idea or was just matching words it had been handed.
The result proved nothing.

[`tests/unit/test_fixture_isolation.py`](../tests/unit/test_fixture_isolation.py)
fails the build if measurement names appear in the instruction. The mistake is
easy to repeat, because the natural way to clarify an instruction is to reach
for the example that is currently failing — which is exactly the example under
test.

That file also checks the traps still work. The first harness run reported
`SHOOT_DAY_CONFLICT: 0/3` and looked like the refactor had failed. It had not.
The fixture put the two shoot-day notes at different locations, so no
disagreement existed to find. A trap that does not trap reads exactly like a
broken agent.

---

## What this bought

| | Before | After |
|---|---|---|
| Instruction size | ~7,454 tokens | ~2,091 tokens |
| Rules the model must remember | 5 | 2 |
| Rules that run identically every time | 0 | 3 |
| Write order | an instruction | enforced by code |
| Rule-firing measured as | one manual run | a rate over N runs |

The instruction is 72% smaller. The three rules that were arithmetic now cannot
be skipped, because nothing is deciding whether to run them. The two that need
real reading are the only two left in the prompt, and they are stated in terms
of the story rather than in terms of the test data.
