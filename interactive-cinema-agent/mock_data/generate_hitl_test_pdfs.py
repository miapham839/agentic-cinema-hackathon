"""Generates mock PDFs for stress-testing parser_agent's HITL rules.

STATUS: these are now the DEV fixtures — good for manual walkthroughs via
`adk web`. They must NOT be used to measure whether an instruction change
worked, because several of their identifiers were once pasted into
parser_agent's instruction (see docs/DESIGN_DECISIONS.md section 14). Use the
held-out set in mock_data/heldout_fixtures.py for measurement instead.

Also note rules 1, 2 and 5 are no longer prose in the instruction at all — they
are deterministic checks in app/checks.py and fire from code, so the traps below
for those three now verify plumbing rather than model judgment. Rules 3 and 4
(location identity, story-graph inference) are the only ones still decided by
the model. See docs/DESIGN_DECISIONS.md section 12.

Run with:
    uv run --with fpdf2 python mock_data/generate_hitl_test_pdfs.py

Produces, in mock_data/:
  - hitl_script.pdf / hitl_budget.pdf   — one script+budget pair, "The Tip,"
    with five deliberate traps, one per rule. Upload both in the SAME
    message to exercise the combined-ask flow end to end.
  - hitl_degenerate.pdf                 — an unrelated one-page memo, not a
    script at all. Upload it alone as a "script" to trigger rule 1.

This is v2 of this fixture. v1's traps for rules 3(direction 1), 4, and 5
turned out to be resolvable by legitimate inference (sequential context,
recognizing a normal branching-choice pattern) rather than genuine
ambiguity — the model wasn't wrong not to ask about them. This version
tightens each trap so there's no honest way to resolve it without asking:
shoot-day statements are unambiguous production notes (not diegetic prose
that could be read either way), the destination ambiguity isn't phrased as
a "she could X, or she could Y" choice (which is legitimately just a normal
fork, not a low-confidence case), and the location-name traps aren't
preceded by a scene that already establishes which place is meant.

See the module-level ASCII graph below for the full story shape, and each
scene's inline comment for exactly which rule it's there to trip (or, for
the two control scenes, to confirm nothing fires where nothing should).
"""

from pathlib import Path

from fpdf import FPDF, XPos, YPos

OUT_DIR = Path(__file__).resolve().parent

_NEXT_LINE = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}


def new_pdf(title):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 10, title, **_NEXT_LINE)
    pdf.ln(2)
    return pdf


def heading(pdf, text):
    pdf.set_font("Helvetica", "B", 12)
    pdf.ln(4)
    pdf.multi_cell(0, 8, text, **_NEXT_LINE)
    pdf.set_font("Helvetica", "", 11)


def body(pdf, text):
    pdf.multi_cell(0, 6, text, **_NEXT_LINE)


# ==============================================================================
# STORY GRAPH — "The Tip"
#
#                              the_tip (3-way choice)
#                    /                |                    \
#         foreman_confrontation  warehouse_stakeout    harbor_place_inquiry
#              |  [CONTROL:            |  [shoot_days=2,       |  [RULE 3a:
#              |   clean, no           |   explicit]           |   "the harbor
#              |   traps]              v                       |   place" —
#              v                  warehouse_break_in            |   matches 2
#         foreman_ending          |  [shoot_days=1,             |   budget
#         [CONTROL ending]        |   explicit]                 |   locations]
#                                 v                              v
#                            ledger_call                    (ends here)
#                            [RULE 4: trailing
#                             action, no explicit
#                             choice — 2 scenes
#                             below both plausibly
#                             follow it]
#                            /            \
#              rooftop_pursuit        captain_debrief
#              [CONTROL ending,       |  [sluglined
#               reuses Harbor          |   INT. PRECINCT]
#               Warehouse]             v
#                                  precinct_epilogue
#                                  [RULE 3b: sluglined
#                                   INT. 14TH STREET
#                                   STATION — suspected
#                                   same place as
#                                   captain_debrief's
#                                   "PRECINCT," but
#                                   budget only lists
#                                   "Precinct House"]
#
# RULE 5 (shoot-day conflict) lives at the Harbor Warehouse location:
# warehouse_stakeout (2) + warehouse_break_in (1) = 3 script days, vs.
# hitl_budget.pdf's stated total_shoot_days: 2 for Harbor Warehouse.
#
# RULE 2 (missing budget cap) is document-wide: hitl_budget.pdf states a
# crew cap, a filming-day cap, and a hero-location cap, but never an overall
# dollar figure.
#
# RULE 1 (degenerate extraction) is untouched from v1 — hitl_degenerate.pdf
# still has zero scenes in it.
# ==============================================================================

pdf = new_pdf("THE TIP\nDraft 2 - branching sequence")

body(
    pdf,
    "INT. PORT AUTHORITY OFFICE - NIGHT\n\n"
    "VIC meets DANA in the stairwell they always use when Dana has "
    "something she shouldn't. Dana presses a folded shipping manifest into "
    "Vic's hand before either of them says a word.\n\n"
    "DANA\n"
    "Whatever you do with this, it didn't come from me.\n\n"
    "VIC\n"
    "It never does.\n\n"
    "Vic has three ways she could play this: go confront the shipping "
    "foreman directly with what's on the manifest, stake out the warehouse "
    "first and see who actually shows up, or head straight for the harbor "
    "place to ask around before she tips her hand anywhere else.",
)

heading(pdf, "FOREMAN CONFRONTATION")
body(
    pdf,
    "INT. SHIPPING OFFICE - DAY\n\n"
    "Vic drops the manifest on Reyes's desk without any preamble. He "
    "doesn't even look surprised, which tells her everything the paperwork "
    "didn't.\n\n"
    "REYES\n"
    "You're going to want to sit down for this part.\n\n"
    "By the time he's done talking, Reyes has told her more than he "
    "meant to, and they both know it.",
)

heading(pdf, "FOREMAN ENDING")
body(
    pdf,
    "INT. SHIPPING OFFICE - CONTINUOUS\n\n"
    "Vic folds the manifest back up and pockets it. Whatever happens with "
    "Reyes from here isn't part of tonight - the sequence ends with her "
    "walking out the same door she came in.",
)

heading(pdf, "WAREHOUSE STAKEOUT")
body(
    pdf,
    "EXT. HARBOR WAREHOUSE - NIGHT\n\n"
    "Vic settles into the car across the lot, engine off, watching the "
    "loading doors for anything that doesn't belong. Full two-day shoot on "
    "this one - we need the exterior stakeout covered across both a rainy "
    "night and a clear one before we can cut it together.\n\n"
    "Nothing moves for a long time. Then, close to dawn on the second "
    "night, a truck she doesn't recognize backs up to the loading doors.",
)

heading(pdf, "WAREHOUSE BREAK-IN")
body(
    pdf,
    "INT. HARBOR WAREHOUSE - LATER THAT NIGHT\n\n"
    "Vic slips through the side door once the truck pulls out again. One-"
    "day shoot for this one - one night, one location, we get the ledger "
    "reveal in a single take or we don't get it at all.\n\n"
    "The ledger is exactly where the manifest said it would be.",
)

heading(pdf, "LEDGER CALL")
body(
    pdf,
    "INT. HARBOR WAREHOUSE - CONTINUOUS\n\n"
    "The ledger's still open in her hands when she finally reaches for the "
    "phone, thumb hovering over Hale's number without pressing down on it "
    "yet.",
)

heading(pdf, "ROOFTOP PURSUIT")
body(
    pdf,
    "EXT. HARBOR WAREHOUSE ROOF - CONTINUOUS\n\n"
    "The call barely rings twice before headlights swing across the lot "
    "below, and Vic is already moving, ledger tucked under one arm, taking "
    "the fire escape two rungs at a time. Whatever was on that truck, "
    "it isn't done with her yet.",
)

heading(pdf, "CAPTAIN DEBRIEF")
body(
    pdf,
    "INT. PRECINCT - LATER\n\n"
    "Vic sets the ledger down on Captain Hale's desk without a word, "
    "still catching her breath from the drive over.\n\n"
    "HALE\n"
    "Tell me this isn't what I think it is.\n\n"
    "Vic doesn't answer, which is answer enough.",
)

heading(pdf, "PRECINCT EPILOGUE")
body(
    pdf,
    "INT. 14TH STREET STATION - MOMENTS LATER\n\n"
    "Hale pages back through the ledger at his desk, same tired "
    "fluorescent lighting overhead as always, while Vic waits by the door "
    "for whatever comes next. Whatever that turns out to be isn't part of "
    "tonight - the sequence ends here.",
)

heading(pdf, "HARBOR PLACE INQUIRY")
body(
    pdf,
    "Vic skips the warehouse and the shipping office both and heads "
    "straight for the harbor place to ask around before she shows her "
    "hand anywhere else. Nobody there is in a hurry to talk, and by the "
    "time anyone does, the trail's already gone cold. The sequence ends "
    "with her walking back to the car with nothing to show for it.",
)

pdf.output(str(OUT_DIR / "hitl_script.pdf"))


# ==============================================================================
# BUDGET / LOCATIONS PDF — deliberately missing an overall dollar cap, and
# with Harbor Warehouse's total_shoot_days set to conflict with the script.
# ==============================================================================

pdf = new_pdf("THE TIP\nBudget & location notes")

body(
    pdf,
    "Location rundown for The Tip sequence. Producer wants a hard line "
    "at eight crew on set, no more than five filming days across the "
    "whole shoot, and ideally no more than three hero locations if we can "
    "help it. Numbers below are what each location actually costs.",
)

heading(pdf, "Shipping Office")
body(
    pdf,
    "$2,600 for a full day, plus a $500 move-in fee. Seats about 15 "
    "comfortably, and yes, we need the standard permit for it.",
)

heading(pdf, "Harbor Warehouse")
body(
    pdf,
    "$4,100 a day, with a $900 move-in charge. We're budgeting two total "
    "shoot days there. Capacity's around 25, and a permit's required.",
)

heading(pdf, "Harbor Fish Market")
body(
    pdf,
    "$3,400 a day, plus an $800 move-in fee. Can hold up to 20, and it's "
    "a working market so a permit's required there too.",
)

heading(pdf, "Precinct House")
body(
    pdf,
    "Our cheapest location by far - $1,800 a day, $300 move-in. Tops out "
    "around 18 people, and a permit's required to film there.",
)

pdf.output(str(OUT_DIR / "hitl_budget.pdf"))


# ==============================================================================
# DEGENERATE PDF — not a script at all (tests rule 1). Unchanged from v1.
# ==============================================================================

pdf = new_pdf("CREW CATERING MEMO")

body(
    pdf,
    "Reminder that craft services will switch to the north-lot tent "
    "starting Monday. Please route any dietary requests through the 2nd "
    "AD by end of week. Coffee will still be available at both the main "
    "tent and video village.\n\n"
    "No script pages attached - this is just the standing catering note "
    "for the crew list.",
)

pdf.output(str(OUT_DIR / "hitl_degenerate.pdf"))

print("Wrote hitl_script.pdf, hitl_budget.pdf, and hitl_degenerate.pdf to", OUT_DIR)
