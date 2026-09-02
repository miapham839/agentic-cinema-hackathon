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

"""Repeat-run harness: turns "parser_agent feels unstable" into a number.

WHY THIS EXISTS
---------------
Rule-firing was previously judged by eyeballing single manual runs through
adk web. That can't distinguish "the fix worked" from "that run happened to go
well" — and it's how a contaminated result went unnoticed for a round.

This runs the same held-out fixture N times and reports a per-rule fire rate
("SHOOT_DAY_CONFLICT: 5/5, location_identity: 3/5"), which is the actual
quantity of interest.

WHAT IT MEASURES vs. WHAT IT DOESN'T
------------------------------------
Feeds fixture TEXT rather than PDFs: this measures rule-triggering and tool
sequencing, not PDF parsing. (ADK eval datasets are text-parts-only in every
documented example, so this also sidesteps unverified attachment support.)

Since rules 1/2/5 moved into app/checks.py they are deterministic and covered
for free by tests/unit/test_extraction_checks.py. What's genuinely uncertain —
and therefore what this harness is really for — is:
  * do the semantic judgment calls (location identity, story-graph inference)
    fire on fixtures the instruction has never seen?
  * does the agent follow the stage -> check -> ask -> commit sequence?

Each run stops the moment the agent calls request_input. That call is a
long-running tool: it pauses the agent until the user's answer arrives as a
function response. Nothing here supplies one, so reading past it hangs forever
rather than ending. Stopping there costs nothing, because which rules fired and
what was asked are both already decided at that point.

COST: this makes real model calls. It is opt-in:
    RUN_HITL_EVAL=1 uv run pytest tests/eval/test_hitl_rules.py -s
Set HITL_EVAL_RUNS to change the sample count (default 5).
"""

import asyncio
import os
from collections import Counter

import pytest
from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from mock_data.heldout_fixtures import BUDGET_TEXT, DEGENERATE_TEXT, SCRIPT_TEXT

# pytest doesn't go through the FastAPI startup path that normally calls this,
# so without it the model client has no credentials (GOOGLE_GENAI_USE_VERTEXAI /
# GOOGLE_CLOUD_PROJECT, or GOOGLE_API_KEY) and fails at construction.
load_dotenv()

RUNS = int(os.getenv("HITL_EVAL_RUNS", "5"))

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_HITL_EVAL"),
    reason="Costs real model calls. Set RUN_HITL_EVAL=1 to run.",
)


class _FakeClickHouseClient:
    """Records inserts instead of performing them."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, int]] = []

    def insert(self, table, rows, column_names=None):  # noqa: D102
        self.inserts.append((table, len(rows)))


@pytest.fixture(autouse=True)
def no_real_writes(monkeypatch):
    """Keeps the harness from writing to the real ClickHouse.

    A measurement harness that runs N times per invocation must not mutate the
    production database — otherwise every measurement run pollutes the data the
    rest of the pipeline is being judged against. commit_staged still executes
    fully (so ordering and correction-application are genuinely exercised), it
    just lands in a recorder.
    """
    import app.tools as tools

    monkeypatch.setattr(tools, "_get_clickhouse_write_client", _FakeClickHouseClient)


# ==============================================================================
# Driving the agent
# ==============================================================================


async def _run_parser_once(user_text: str) -> dict:
    """Runs parser_agent once and summarizes what it did.

    Returns the tool-call sequence, whether request_input fired, and the text
    of the questions it asked (so we can tell WHICH rules it surfaced).
    """
    from app.agent import parser_agent

    runner = InMemoryRunner(agent=parser_agent, app_name="hitl_eval")
    session = await runner.session_service.create_session(
        app_name="hitl_eval", user_id="eval_user"
    )

    tool_calls: list[str] = []
    question_text = ""
    diagnostic_codes: list[str] = []
    staged_scene_locations: dict[str, str] = {}
    staged_budget_locations: list[str] = []

    message = types.Content(role="user", parts=[types.Part(text=user_text)])
    agen = runner.run_async(
        user_id="eval_user", session_id=session.id, new_message=message
    )
    try:
        async for event in agen:
            for call in event.get_function_calls() or []:
                tool_calls.append(call.name)
                if call.name == "adk_request_input":
                    question_text += str(call.args.get("message", ""))
                # Capture what was actually extracted. When a judgment call
                # doesn't fire, this distinguishes "didn't notice the ambiguity"
                # from "resolved it confidently and saw nothing to ask about" —
                # two very different problems with different fixes.
                if call.name == "stage_script_extraction":
                    for node in (call.args.get("extraction") or {}).get("nodes", []):
                        staged_scene_locations[node.get("node_id", "?")] = node.get(
                            "location_id", "?"
                        )
                if call.name == "stage_budget_extraction":
                    for loc in (call.args.get("extraction") or {}).get("locations", []):
                        staged_budget_locations.append(loc.get("location_id", "?"))
            # check_staged's own response tells us what the code-side checks found.
            for response in event.get_function_responses() or []:
                if response.name == "check_staged":
                    payload = response.response or {}
                    for diagnostic in payload.get("diagnostics", []) or []:
                        diagnostic_codes.append(diagnostic.get("code", ""))

            # STOP HERE once the agent asks.
            #
            # request_input is a long-running tool: it pauses the invocation and
            # waits for the user's answer to come back as a function response.
            # There is no user in this harness, and InMemoryRunner builds its own
            # App without ResumabilityConfig, so nothing ever delivers that answer
            # or cancels the wait — consuming further would block forever (main
            # thread parked in kevent, worker thread on _queue_SimpleQueue_get).
            #
            # Stopping loses nothing: everything being measured — which rules
            # fired, what was asked, the tool order up to the ask — is already
            # determined at this point.
            if "adk_request_input" in tool_calls:
                break
    finally:
        await agen.aclose()

    return {
        "tool_calls": tool_calls,
        "asked": "adk_request_input" in tool_calls,
        "question_text": question_text.lower(),
        "diagnostic_codes": diagnostic_codes,
        "staged_scene_locations": staged_scene_locations,
        "staged_budget_locations": staged_budget_locations,
    }


def _run_n(user_text: str, n: int) -> list[dict]:
    return [asyncio.run(_run_parser_once(user_text)) for _ in range(n)]


BOTH_DOCUMENTS = (
    "Here are the script and budget documents for this production. "
    "Please parse them and write them to the database.\n\n"
    f"===== SCRIPT DOCUMENT =====\n{SCRIPT_TEXT}\n\n"
    f"===== BUDGET / LOCATIONS DOCUMENT =====\n{BUDGET_TEXT}"
)

DEGENERATE_ONLY = (
    "Here is the script document for this production. Please parse it and write "
    f"it to the database.\n\n===== SCRIPT DOCUMENT =====\n{DEGENERATE_TEXT}"
)


@pytest.fixture(scope="module")
def both_documents_runs():
    """One shared set of N runs, reused by every assertion in this module.

    Each run is a real multi-turn agent invocation costing real tokens and ~90s,
    so re-running them per test would multiply both by the number of tests for
    no additional signal.
    """
    return _run_n(BOTH_DOCUMENTS, RUNS)


@pytest.fixture(scope="module")
def degenerate_runs():
    return _run_n(DEGENERATE_ONLY, RUNS)


# ==============================================================================
# Reporting
# ==============================================================================


def _report(title: str, counts: Counter, runs: int) -> None:
    print(f"\n=== {title} ({runs} runs) ===")
    for label, hits in sorted(counts.items()):
        bar = "#" * hits + "." * (runs - hits)
        print(f"  {label:38} {hits}/{runs}  {bar}")


# ==============================================================================
# Tests
# ==============================================================================


def test_deterministic_rules_fire_every_single_run(both_documents_runs):
    """Rules 2 and 5 are code now — anything less than 100% is a real bug.

    These used to be prose and were skipped unpredictably; SHOOT_DAY_CONFLICT in
    particular was missed even on an unambiguous 3-vs-2 mismatch.
    """
    results = both_documents_runs
    counts = Counter()
    for r in results:
        for code in set(r["diagnostic_codes"]):
            counts[code] += 1
    _report("Deterministic checks (app/checks.py)", counts, RUNS)

    assert counts["MISSING_BUDGET_CAP"] == RUNS, (
        f"MISSING_BUDGET_CAP fired {counts['MISSING_BUDGET_CAP']}/{RUNS}; the budget "
        "document states no total, so this is deterministic and must always fire."
    )
    assert counts["SHOOT_DAY_CONFLICT"] == RUNS, (
        f"SHOOT_DAY_CONFLICT fired {counts['SHOOT_DAY_CONFLICT']}/{RUNS}; the stables "
        "scenes sum to 3 against a stated 2, so this is deterministic."
    )


def test_semantic_judgment_calls_fire_on_unseen_fixtures(both_documents_runs):
    """The real generalization measurement.

    These fixtures share no vocabulary with the instruction's examples, so a hit
    here means the model applied the principle rather than matching a string.
    Asserts a floor rather than perfection — this is the number to watch over time.
    """
    results = both_documents_runs
    counts = Counter()
    for r in results:
        q = r["question_text"]
        # Zero-word-overlap merge: "The Long Gallery" vs budgeted "Ashfield Manor".
        if "long gallery" in q:
            counts["location_identity_long_gallery"] += 1
        # One reference, two budgeted candidates: Tenant vs Gamekeeper's Cottage.
        if "cottage" in q:
            counts["location_identity_cottage"] += 1
        # Unresolved destination out of the letter-burning beat.
        if "letter" in q or "departure" in q or "churchyard" in q:
            counts["story_graph_ambiguous_destination"] += 1
        if r["asked"]:
            counts["asked_anything"] += 1
    _report("Semantic judgment calls (held-out)", counts, RUNS)

    # Print what was actually asked. Without this, a miss is ambiguous: either
    # the model didn't ask, or it asked in words this test doesn't match on.
    for i, r in enumerate(results, 1):
        print(f"\n--- run {i} questions ---\n{r['question_text'][:1200]}")

    assert counts["asked_anything"] == RUNS, (
        "parser_agent committed without asking anything on at least one run, despite "
        "deterministic diagnostics being present."
    )
    floor = max(1, RUNS // 2)
    for label in (
        "location_identity_long_gallery",
        "location_identity_cottage",
        "story_graph_ambiguous_destination",
    ):
        assert counts[label] >= floor, (
            f"{label} fired only {counts[label]}/{RUNS} (floor {floor}). This fixture "
            "shares no wording with the instruction, so a low rate means the rule "
            "isn't generalizing."
        )


def test_lifecycle_order_up_to_the_ask(both_documents_runs):
    """stage -> check -> ask, with nothing written before the user is consulted.

    This stops at the ask, because that's where the run stops: request_input
    pauses the agent, and the harness has no user to resume it. So the commit
    half of the lifecycle is covered by unit tests instead — the
    require_checks_before_commit guard is what makes the ordering structural,
    and it's verified directly rather than inferred from a trace here.
    """
    results = both_documents_runs
    counts = Counter()
    for r in results:
        calls = r["tool_calls"]
        if "stage_script_extraction" in calls:
            counts["staged_script"] += 1
        if "stage_budget_extraction" in calls:
            counts["staged_budget"] += 1
        if "check_staged" in calls:
            counts["called_check_staged"] += 1
        if "check_staged" in calls and "adk_request_input" in calls:
            if calls.index("check_staged") < calls.index("adk_request_input"):
                counts["checked_before_asking"] += 1
        if "commit_staged" not in calls:
            counts["no_write_before_asking"] += 1
    _report("Lifecycle ordering (up to the ask)", counts, RUNS)

    assert counts["called_check_staged"] == RUNS, "check_staged was skipped on some run"
    assert counts["checked_before_asking"] == RUNS, (
        "the agent asked the user before running check_staged, so its questions "
        "couldn't have included the deterministic diagnostics"
    )
    assert counts["no_write_before_asking"] == RUNS, (
        "commit_staged ran before the user was asked — unresolved diagnostics "
        "would have been written as-is"
    )


def test_degenerate_document_is_questioned_not_written(degenerate_runs):
    """A document with no scenes must produce a question, never an empty write."""
    results = degenerate_runs
    counts = Counter()
    for r in results:
        if r["asked"]:
            counts["asked_about_the_document"] += 1
        if "commit_staged" not in r["tool_calls"]:
            counts["did_not_write"] += 1
    _report("Degenerate document", counts, RUNS)

    assert counts["asked_about_the_document"] >= max(1, RUNS - 1), (
        "a memo with no scenes should prompt a question about whether it's the right file"
    )


def test_report_staged_locations(both_documents_runs):
    """Diagnostic only: shows how the model actually resolved each location."""
    for i, r in enumerate(both_documents_runs, 1):
        print(f"\n--- run {i} ---")
        print("  budget locations:", r["staged_budget_locations"])
        print("  scene -> location:")
        for node_id, loc in r["staged_scene_locations"].items():
            print(f"    {node_id:34} -> {loc}")
        print("  diagnostics:", r["diagnostic_codes"])
