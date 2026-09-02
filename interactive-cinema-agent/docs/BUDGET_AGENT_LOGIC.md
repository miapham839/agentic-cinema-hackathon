# `budget_agent` logic

What `budget_agent` actually computes and why. For the full prompt text, see
`app/agent.py`; for how this fits into the wider multi-agent pipeline, see
`DESIGN_DECISIONS.md`.

## Role

Read-only cost analysis. Reconstructs every branch of the story graph,
prices it out, checks it against the production's approved constraints, and
proposes plot-appropriate savings — verifying each one with
`graph_auditor_agent` before recommending it. Never writes to ClickHouse.

## Data it reads

| Table | Used for |
|---|---|
| `production_constraints` | The approved limits: `total_budget_usd` (always set, if a row exists), `max_primary_locations` / `max_filming_days` / `max_total_crew` (each independently optional) |
| `production_locations` | `daily_rate_usd`, `company_move_penalty_usd`, `total_shoot_days` (optional) per location |
| `script_nodes` | The scenes: `location_id`, `shoot_days` (optional), `num_crew_required` (optional) |
| `script_edges` | The choices connecting scenes — used to reconstruct branches |

All four tables are `ReplacingMergeTree` keyed on `version`; every query
must use `FINAL`. `project_id` exists on all four but is always `'01'` in
this single-project POC, so it's not filtered on.

The three new optional fields (`total_shoot_days`, `shoot_days`,
`num_crew_required`) only get populated when the source document explicitly
stated them — expect gaps, and never treat a missing value as zero.

## 1. Branch reconstruction & cost

A **branch** is one root-to-leaf path through the story graph
(`script_nodes` + `script_edges`). For every branch, cost is:

```
cost = Σ (per visited location: daily_rate_usd × total_shoot_days_or_1)
     + Σ (company_move_penalty_usd, once per consecutive location change)
```

- `total_shoot_days_or_1` — if the location states `total_shoot_days`, use
  it; otherwise assume 1 day (the same behavior as before this field
  existed).
- The move penalty applies every time consecutive scenes in the branch sit
  at a *different* `location_id` than the scene before it.

**Known simplification:** if two branches both use the same location, each
branch's cost calc uses that location's full `total_shoot_days`
independently — there's no data to say how those days actually split
across branches.

## 2. Constraint checks

Each check only runs if the relevant constraint is actually set, and only
flags a branch as an overrun if it's set — an unset constraint is silently
skipped, never treated as "0" or "unlimited" in a way that produces a false
flag.

| Constraint | Compared against | Rule |
|---|---|---|
| `total_budget_usd` | branch cost (above) | Always checked — this field is always present if a `production_constraints` row exists. |
| `max_primary_locations` | count of distinct `location_id` in the branch | Skipped if unset. |
| `max_filming_days` | **sum** of `shoot_days` across the branch's scenes | Skipped if unset. If some scenes in the branch have `shoot_days` and others don't, the sum is reported as a partial/lower-bound figure, not a hard total. If *no* scene in the branch has it, the check is skipped for that branch — missing data is never counted as 0 days. |
| `max_total_crew` | **largest single** `num_crew_required` among the branch's scenes | Skipped if unset. Deliberately a max, not a sum — crew is a concurrent headcount for the branch's most demanding scene, not a per-scene cumulative cost. Same partial-data handling as filming days. |

## 3. Savings suggestions

For any branch that overruns something, `budget_agent` proposes
plot-appropriate fixes — consolidating locations shared across branches,
reordering scenes to avoid repeat company moves, cutting a location from an
overrunning branch — optionally informed by `graph_auditor_agent`'s
earlier full-audit findings if one already ran this conversation.

**Mandatory verification:** before finalizing any suggestion that changes
the graph (move/merge/cut/reorder a scene), `budget_agent` calls
`graph_auditor_agent` with a specific, narrow question (e.g. *"Would moving
scene_2a from loc_docks_01 to loc_diner_01 create any logic or continuity
problems?"*) and only keeps the suggestion if the answer is clean — one
verification call per idea, never batched. Suggestions that don't touch the
graph (e.g. "shoot these on the same day to avoid a move penalty") skip
this step.

## Output

One structured entry per branch: total cost, which constraints it overruns
(and by how much — noting partial/lower-bound figures explicitly), and any
verified savings suggestions. No SQL, no raw query dumps, no step-by-step
arithmetic, no narration of what was queried in what order — conclusions
only.
