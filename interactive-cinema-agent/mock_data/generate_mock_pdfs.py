"""Generates the two mock PDFs used to test the POC pipeline end to end.

Run with:
    uv run --with fpdf2 python mock_data/generate_mock_pdfs.py

Produces mock_data/mock_script.pdf and mock_data/mock_budget.pdf. See
docs/POC_TESTING_GUIDE.md for what to expect when you upload them via the ADK web
UI's file upload button.

Deliberately free-form: these read like an actual screenplay draft and a
producer's budget memo, NOT like a form that mirrors the ClickHouse schema.
There are no "Location ID:", "State Change:", or "Characters Present:"
labels anywhere — location identity, story-state changes, and who's in a
scene all have to be read out of prose the way parser_agent would have to
for a real uploaded document. See docs/DESIGN_DECISIONS.md section 8 for why.
"""

from pathlib import Path

from fpdf import FPDF, XPos, YPos

OUT_DIR = Path(__file__).resolve().parent

# fpdf2's multi_cell leaves the cursor at the end of the last line by default,
# not back at the left margin — without new_x/new_y every call after the
# first runs out of horizontal room. Force the classic "next line, left
# margin" behavior everywhere.
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
# SCRIPT PDF — "The Diner Standoff" (branching interactive script draft)
#
# Written as an actual screenplay excerpt in a CYOA-style manuscript: scene
# headings are just the beat's own name (the way a writer would title a
# section), sluglines and action carry location and character info, and
# choices are woven into the prose as narrative forks rather than a labeled
# "CHOICE: ... leads to ..." field. State changes are implied by what
# happens, never spelled out.
#
# Intentional inconsistencies for graph_auditor_agent to catch:
#   - The "turn back for Jamie" fork leads to a beat ("The Getaway Gone
#     Wrong") that is referenced but never actually written anywhere in the
#     document — a broken/orphaned choice, the way an unfinished draft
#     branch would look in real life.
#   - "An Uneasy Truce" has no further fork and no ending language — an
#     unintentional dead end, unlike "The Old Warehouse" which explicitly
#     closes out the sequence.
# ==============================================================================

pdf = new_pdf("THE DINER STANDOFF\nDraft 1 - branching sequence")

body(
    pdf,
    "INT. ROUTE 66 DINER - NIGHT\n\n"
    "The overhead sign buzzes and flickers through the window. SAM has a "
    "revolver leveled across the counter. JAMIE hasn't moved from the corner "
    "booth in what feels like a full minute, palms flat against the formica "
    "like she's holding the table down.\n\n"
    "SAM\n"
    "You shouldn't have come back here.\n\n"
    "JAMIE\n"
    "I didn't have a choice.\n\n"
    "Somewhere in the kitchen a fryer timer goes off and nobody moves to "
    "answer it. Everything in the room is waiting on Sam.\n\n"
    "Does he pull the trigger, or does he lower the gun and try to talk his "
    "way out of it? Pulling the trigger drops us straight into Blood on the "
    "Floor. Trying to talk it out instead lands us in An Uneasy Truce.",
)

heading(pdf, "BLOOD ON THE FLOOR")
body(
    pdf,
    "EXT. HARBOR DOCKS - CONTINUOUS\n\n"
    "The shot cracks through the diner before anyone can stop it. Sam is out "
    "the back within seconds, Jamie's weight over one shoulder, the gun "
    "still warm in his other hand, and he doesn't slow down until the docks "
    "swallow him and the sirens are just noise behind him.\n\n"
    "Whatever was between them before, it isn't there anymore.\n\n"
    "From here, does he make the clean getaway, or does something pull him "
    "back for Jamie one more time? Running gets us to The Old Warehouse. "
    "Turning back sends us into The Getaway Gone Wrong.",
)

heading(pdf, "AN UNEASY TRUCE")
body(
    pdf,
    "INT. ROUTE 66 DINER - NIGHT (CONT'D)\n\n"
    "The gun comes down an inch, then another, until it's resting flat on "
    "the counter instead of pointed at anything. Jamie lets out a breath "
    "she's clearly been holding since the cold open.\n\n"
    "JAMIE\n"
    "Okay. Okay, just - leave it there.\n\n"
    "Neither of them says what almost just happened. But something between "
    "them has shifted, and it isn't going back to what it was.",
)

heading(pdf, "THE OLD WAREHOUSE")
body(
    pdf,
    "INT. OLD WAREHOUSE - LATER\n\n"
    "Sam ducks between rows of shipping crates and doesn't come out again. "
    "By the time the first responders reach the docks he's three blocks "
    "away and hasn't looked back once. Whatever comes after this isn't part "
    "of tonight - the sequence ends here.",
)

pdf.output(str(OUT_DIR / "mock_script.pdf"))


# ==============================================================================
# BUDGET / LOCATIONS PDF — a producer's budget memo, not a spec sheet.
#
# Numbers and constraints are stated in prose, in varied phrasing, the way a
# real memo would write them (word numbers, mixed "$X/day" vs "$X a day"
# phrasing, permit requirement implied rather than a Yes/No field).
#
# Intentional overrun for budget_agent to catch: the "shoot first" branch
# (diner -> docks -> warehouse) costs ~$18,000 in daily rates + move
# penalties, against the ~$12,000 cap stated in the memo, and touches 3
# distinct locations against the memo's 2-location target. The "talk it out"
# branch (diner -> diner) costs ~$9,000 and uses 1 location, staying under
# both, but dead-ends per "An Uneasy Truce" above - a good case for
# budget_agent to cross-reference with graph_auditor_agent's findings.
# ==============================================================================

pdf = new_pdf("THE DINER STANDOFF\nBudget & location notes")

body(
    pdf,
    "Quick rundown for the diner sequence (we've been tracking it internally "
    "under the code diner-standoff-01). Studio's given us a hard ceiling of "
    "twelve thousand dollars for this whole stretch, wants it wrapped inside "
    "two shooting days, and is capping the crew at ten bodies on set. "
    "They've also asked us to keep it to two hero locations if we can - "
    "every extra company move eats into both the schedule and the number "
    "above.\n\n"
    "Here's what we're working with location-wise.",
)

heading(pdf, "Route 66 Diner")
body(
    pdf,
    "Our cheapest anchor - $4,500 for a full day. Since it's home base for "
    "most of the sequence we're only budgeting the $1,200 move-in fee once. "
    "Tight space, tops out around 15 people comfortably, and yes, we need "
    "the standard shoot permit for it.",
)

heading(pdf, "Harbor Docks")
body(
    pdf,
    "The pricier of the two exteriors: $6,200 a day, plus a $2,000 hit every "
    "time the company physically relocates out there. Capacity's around 20, "
    "and it's public waterfront so a permit is required, no way around it.",
)

heading(pdf, "Old Warehouse")
body(
    pdf,
    "The wildcard. Daily rate's a reasonable $3,800, move-in runs $1,500, "
    "and because it's privately owned we're actually permit-free there, "
    "which helps. Can hold up to 25 if we ever need the extra room.",
)

pdf.output(str(OUT_DIR / "mock_budget.pdf"))

print("Wrote mock_script.pdf and mock_budget.pdf to", OUT_DIR)
