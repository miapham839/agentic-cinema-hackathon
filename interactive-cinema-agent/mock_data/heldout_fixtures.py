# ruff: noqa
"""Held-out fixtures for measuring parser_agent's HITL rules honestly.

WHY A SEPARATE SET
------------------
The previous round of instruction fixes put fixture strings verbatim into
parser_agent's instruction ("INT. PRECINCT" / "INT. 14TH STREET STATION",
scene_ledger_call -> scene_rooftop_pursuit). Rules 3 and 4 then "started
working" — but that measured string matching, not generalization. There was no
way to tell the difference.

So three domains are now kept deliberately disjoint:

  instruction examples  ->  hospital   (Ward Six / St. Brendan's, scene_vigil...)
  dev fixtures          ->  noir       (mock_data/generate_hitl_test_pdfs.py)
  held-out (this file)  ->  period drama

Nothing here appears in the instruction, so a rule firing on these fixtures is
evidence the model generalized the principle rather than recognizing a string.
Keep it that way: if you ever paste one of these names into app/agent.py, this
fixture set is burned and needs replacing.

The text lives here as constants (not just as PDFs) because the eval harness
feeds text directly — eval datasets are text-parts-only, and we're measuring
rule-triggering, not PDF parsing.

Render the PDFs with:
    uv run --with fpdf2 python mock_data/heldout_fixtures.py
"""

from pathlib import Path

# ==============================================================================
# STORY GRAPH — "The Ashfield Inheritance"
#
#                          scene_reading_of_the_will
#                    ┌──────────────┴──────────────┐
#                    ▼                             ▼
#          scene_stables_confrontation      scene_cottage_visit
#            (2-day shoot, THE STABLES)       [TRAP 3a: "the cottage" -
#                    ▼                         two cottages are budgeted]
#          scene_tack_room                           ▼
#            (1-day shoot, THE STABLES)       scene_cottage_refusal
#            -> stables total = 2 + 1 = 3
#                    ▼
#          scene_long_gallery
#          [TRAP 3b: "The Long Gallery" vs
#           budgeted "Ashfield Manor" -
#           zero shared words]
#                    ▼
#          scene_burning_the_letter
#          [TRAP 4: trails off, no stated outcome]
#            ┌───────┴────────┐
#            ▼                ▼
#   scene_departure    scene_churchyard
#   (control ending)   (control ending)
#
# TRAP 5: stables scenes state 2 + 1 = 3 shoot days; budget states 2.
# TRAP 2: budget doc states crew/day/location caps but no total in pounds.
# ==============================================================================

SCRIPT_TEXT = """THE ASHFIELD INHERITANCE
Draft 1 - branching sequence

INT. ASHFIELD MANOR - LIBRARY - DAY

The solicitor reads the will aloud to a room that has not exhaled in several
minutes. ELEANOR stands by the window. Her brother THOMAS does not look at her
once. When the solicitor reaches the clause about the estate, Eleanor finally
turns around.

SOLICITOR
The whole of it. To the younger sister.

THOMAS
Read it again.

Eleanor can go straight out to the stables and have it out with Thomas where
nobody will overhear, or she can walk down to the cottage first and hear what
the tenants already know.

STABLES CONFRONTATION

EXT. THE STABLES - CONTINUOUS

Thomas is already saddling a horse he has no intention of riding. Two-day shoot
on this one - we need the argument covered across both an overcast morning and
the golden hour, or the cut won't hold together.

THOMAS
You knew. All that time sitting with him, you knew.

ELEANOR
I sat with him because nobody else would.

THE TACK ROOM

EXT. THE STABLES - LATER

Eleanor finds the ledgers exactly where her father said they would be, behind
the feed bins. One-day shoot for this one - we only need the one angle on the
ledgers coming out.

She reads standing up, in the cold, for a long time.

THE LONG GALLERY

INT. THE LONG GALLERY - LATER

Eleanor walks the length of it alone, past every portrait of every man who has
ever owned this house. One-day shoot here, but we need the full length of the
room in one unbroken take.

She stops at the last frame, which is still empty.

BURNING THE LETTER

INT. THE LONG GALLERY - CONTINUOUS

She holds the solicitor's letter over the candle without letting it catch, and
stands there long enough that the wax runs down onto the sill.

DEPARTURE

EXT. ASHFIELD MANOR - DRIVE - DAWN

The trunk is loaded before anyone else is awake. Eleanor does not look back at
the house, and whatever the letter said, it goes with her. The sequence ends
here.

CHURCHYARD

EXT. ST. ALDATE'S CHURCHYARD - DAY

She reads the letter aloud to a headstone, once, and then puts it away for
good. Whatever comes after this is not part of this sequence.

COTTAGE VISIT

INT. THE COTTAGE - DAY

Eleanor ducks under the low beam and finds the tenants already halfway through
packing. Nobody here is surprised to see her, which tells her more than the
will did.

COTTAGE REFUSAL

INT. THE COTTAGE - CONTINUOUS

She offers them the tenancy in writing and they refuse it in writing, and that
is the end of that. The sequence ends here.
"""

BUDGET_TEXT = """THE ASHFIELD INHERITANCE
Budget & location notes

Location rundown for the Ashfield sequence. The estate office wants no more
than six crew on the grounds at any time, no more than four filming days across
the whole shoot, and ideally no more than three hero locations. Costs below are
what each location actually runs us per day.

Ashfield Manor
2,400 for a full day, plus a 700 move-in fee. Holds about 20 comfortably, and
we need the standard filming permit from the estate.

The Stables
1,900 a day with a 400 move-in charge. We're budgeting two total shoot days out
there. Capacity is around 12, and a permit is required.

Tenant Cottage
900 a day, 200 to move in. Very tight - six people at most - and no permit
needed since it's on estate land.

Gamekeeper's Cottage
1,100 a day, 250 to move in. Holds eight, permit not required.

St. Aldate's Churchyard
1,500 a day plus a 600 move-in fee. Holds up to 30, and the parish requires a
permit in writing.
"""

DEGENERATE_TEXT = """WARDROBE CONTINUITY MEMO

Reminder that all period costume fittings move to the east wing workroom from
Thursday. Please route alteration requests through the costume supervisor
rather than the production office.

No script pages attached - this is the standing wardrobe note only.
"""

# ==============================================================================
# What each fixture is designed to trip. The harness asserts against these.
# ==============================================================================

EXPECTED_DIAGNOSTIC_CODES = {
    # Deterministic now (app/checks.py) — these must fire on every single run.
    "MISSING_BUDGET_CAP": "Budget doc states crew/day/location caps but no total.",
    "SHOOT_DAY_CONFLICT": "Stables scenes sum to 3 (2+1); budget states 2.",
}

EXPECTED_JUDGMENT_CALLS = {
    # Semantic — these are what actually measure generalization.
    "location_identity_long_gallery": (
        "'The Long Gallery' shares no words with the budgeted 'Ashfield Manor', but "
        "narrative continuity suggests it's a room inside it rather than a separate "
        "location. Zero-word-overlap merge case."
    ),
    "location_identity_cottage": (
        "'The Cottage' could be either 'Tenant Cottage' or 'Gamekeeper's Cottage'. "
        "One reference, two budgeted candidates."
    ),
    "story_graph_burning_the_letter": (
        "scene_burning_the_letter trails off with no stated outcome; both "
        "scene_departure and scene_churchyard plausibly follow, and the text never "
        "picks one. Critically NOT phrased as an authored 'she could X or Y' fork."
    ),
}


def _render_pdfs() -> None:
    from fpdf import FPDF, XPos, YPos

    out_dir = Path(__file__).resolve().parent
    next_line = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}

    for filename, text in (
        ("heldout_script.pdf", SCRIPT_TEXT),
        ("heldout_budget.pdf", BUDGET_TEXT),
        ("heldout_degenerate.pdf", DEGENERATE_TEXT),
    ):
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "", 11)
        for block in text.split("\n\n"):
            pdf.multi_cell(0, 6, block.strip(), **next_line)
            pdf.ln(3)
        pdf.output(str(out_dir / filename))
        print("wrote", out_dir / filename)


if __name__ == "__main__":
    _render_pdfs()
