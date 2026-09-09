"""Generates the demo script+budget pair: "Last Train".

Run with:
    uv run --with fpdf2 python mock_data/generate_demo_pdfs.py

Produces, in mock_data/:
  - demo_script.pdf
  - demo_budget.pdf

Upload both together on one message, against a cleared database.

Built for a live demo, so it is deliberately small — six short scenes and
five locations. Every scene earns its place; nothing is padded, because
each extra scene is more tokens and a longer wait in front of an audience.

WHAT IT IS DESIGNED TO TRIGGER

Two parser questions (app/agent.py, "WHEN TO ASK THE USER", rule 3 — the
ambiguous-location rule, in both of its directions):

  Q1, one script location matching two budget entries. The script only ever
  calls the place "Ashgrove". The budget prices "Ashgrove Depot" AND
  "Ashgrove Rail Yard", with deliberately interchangeable descriptions that
  differ only in price. Nothing in either document favours one, and the
  script never says "depot" or "yard" on its own, so there is no lexical
  tiebreaker to resolve it silently.

  Q2, two script locations that might be one place. "Marlow Street Bar" and
  "The Late Bar" share their physical detail down to the split in the
  leather and the burn mark on the sill, but sit in contradicting places —
  under the station arches versus out past the river bridge. Matching
  detail argues for one bar under two names; the conflicting geography
  argues for two.

  Detail and geography alone were not enough. The budget prices BOTH bars
  (The Late Bar has to stay priced, or the second savings suggestion has
  nothing to save), and a separate line item is strong evidence for "two
  places" — so the model resolved it silently and only asked now and then.
  Both documents now state the uncertainty outright instead of implying it:
  the script carries a production note saying the point is unsettled, and
  the budget carries a location manager's note saying the two may be one
  room double-listed, priced apart only so the shoot does not under-book.
  Neither note picks a side, so there is nothing to resolve without asking.

Rule 2 (a missing budget cap) is deliberately NOT used. It cannot coexist
with a real overrun: with no stated total, parser_agent computes a floor
from the sum of every location's day rate, and that floor is always at
least as large as any single branch, so nothing can ever exceed it. This
document states its cap outright instead.

THE STORY GRAPH, and why each scene is here

  the_last_call    Marlow Street Bar   start; forks into both branches
  the_platform     Platform 9          branch A
  the_signal_box   The Signal Box      branch A. Expensive, and written with
                                       no detail tied to a signal box, so
                                       relocating it is verifiably safe.
                                       DEAD END — no outgoing choice, and no
                                       ending language.
  down_the_line    Ashgrove            branch B. Carries Q1.
  last_train_out   Platform 9          branch B; a real ending
  the_late_bar     The Late Bar        UNREACHABLE — nothing chooses into it.
                                       Carries Q2.

That gives the auditor a dead end and an unreachable scene at the same
time, which is the point: the natural repair for both is the same single
edge, the_signal_box -> the_late_bar. Two findings, one shared edge, so
approving either one makes the other stale.

THE MONEY

An interactive film shoots EVERY branch — the viewer only ever sees one
path, but all of them get filmed — so what the production actually pays is
every location at least one scene sits at, booked once: day rate (x1 day,
since none states a shoot length) plus its move-in.

  Marlow Street Bar   900 + 150 = $1,050
  Platform 9        1,200 + 250 = $1,450
  The Signal Box    3,200 + 600 = $3,800
  Ashgrove          2,400 + 450 = $2,850   (whichever one gets picked)
  The Late Bar      1,600 + 300 = $1,900   (only if kept as its own place)
  ---------------------------------------
  Stated cap                    = $7,000

  answering Q2 "same place"  -> $9,150   over by $2,150 (31%)
  answering Q2 "two places"  -> $11,050  over by $4,050 (58%)

The individual branches are $6,150 (High-Risk) and $5,200 (Safe), and this
is deliberate: NEITHER of them exceeds the cap on its own. Comparing a
single branch against the budget would say everything is fine while the
production is a third over. That contrast is the point — it is why the
budget panel headlines the production total, and why budget_agent is told
to flag the overrun there and treat per-branch figures as a comparison
between storylines only.

TWO savings, and both are real under this model. A location only leaves the
bill when NOTHING is shot there any more, so every saving here is "empty a
location out":

  1. Move the Signal Box scene onto Platform 9, which the production already
     pays for. The Signal Box then hosts nothing and drops off the bill
     entirely — a $3,800 saving. Its scene is written with no signal-box
     detail, so the auditor can verify the move as plot-safe.
  2. Shoot the Late Bar scene at Marlow Street Bar — the script describes the
     same booth, same window, same dead jukebox. That empties The Late Bar,
     saving another $1,900.

Note "consolidate Platform 9 across both branches" is NOT a saving here, even
though it looks like one: the production books each location once, so a
location used by two branches is already only paid for once. That trap is
why the second saving above had to be a location that genuinely empties.

If Q2 was answered "two separate places", saving 1 alone leaves the
production at $7,250 — still $250 over — and saving 2 is needed to get
under. If it was answered "same place", saving 1 alone is enough.
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
# SCRIPT PDF — "Last Train"
# ==============================================================================

pdf = new_pdf("LAST TRAIN\nDraft 1")

body(
    pdf,
    "INT. MARLOW STREET BAR - NIGHT\n\n"
    "Under the station arches, IVY nurses a drink in the cracked leather "
    "booth by the window. The jukebox in the corner has been dead for "
    "years. TOMAS drops a ticket on the table between them.\n\n"
    "TOMAS\n"
    "Last one out tonight. After that we're both stuck here.\n\n"
    "IVY\n"
    "Then we'd better not miss it.\n\n"
    "She doesn't pick the ticket up yet.\n\n"
    "Do they head straight for the platform and wait it out in the open, or "
    "cut round to Ashgrove and pick the train up further down the line? "
    "Going to the platform leads into The Platform. Cutting round leads "
    "into Down The Line.",
)

heading(pdf, "THE PLATFORM")
body(
    pdf,
    "EXT. PLATFORM 9 - CONTINUOUS\n\n"
    "Rain comes in sideways under the canopy. Ivy watches the departure "
    "board flicker and settle on a time that keeps moving further away. "
    "Tomas keeps checking the far end of the platform.\n\n"
    "TOMAS\n"
    "Someone's already been down here asking after you.\n\n"
    "Whatever that means, they can't stand here in the open to work it out. "
    "They head for The Signal Box.",
)

heading(pdf, "THE SIGNAL BOX")
body(
    pdf,
    "INT. THE SIGNAL BOX - CONTINUOUS\n\n"
    "The door shuts behind them and the noise of the rain drops away. Ivy "
    "finally reads the ticket properly, and whatever is printed on it isn't "
    "what Tomas told her.\n\n"
    "IVY\n"
    "This isn't tonight. This is a week ago.\n\n"
    "TOMAS\n"
    "I know.\n\n"
    "He doesn't explain, and she doesn't ask again yet.",
)

heading(pdf, "DOWN THE LINE")
body(
    pdf,
    "EXT. ASHGROVE - NIGHT\n\n"
    "They come in past the fence line at Ashgrove, gravel underfoot, "
    "carriages parked up dark on either side. Ivy has the ticket out again, "
    "holding it up to what little light there is.\n\n"
    "IVY\n"
    "You've had this a week.\n\n"
    "TOMAS\n"
    "I've had it a week.\n\n"
    "Somewhere ahead of them a train they can still catch is being made "
    "ready. They keep moving, towards Last Train Out.",
)

heading(pdf, "LAST TRAIN OUT")
body(
    pdf,
    "EXT. PLATFORM 9 - LATER\n\n"
    "The train is there, doors open, and for once nothing is stopping "
    "either of them getting on it. Tomas holds out a hand for the ticket. "
    "Ivy looks at him, then at the open door.\n\n"
    "IVY\n"
    "You first.\n\n"
    "He gets on. She follows. Whatever they were running from, tonight it "
    "doesn't catch them - the sequence ends here.",
)

heading(pdf, "THE LATE BAR")
body(
    pdf,
    "INT. THE LATE BAR - NIGHT\n\n"
    "Out past the river bridge, a long way from the station, the same "
    "cracked leather booth sits by the same window, and the same dead "
    "jukebox holds up the corner. Down to the split in the leather and the "
    "burn mark on the sill, it is the room from the opening scene. "
    "MARGARET wipes down the counter and doesn't look up.\n\n"
    "MARGARET\n"
    "They were in here for years, those two. Then one night, nothing.\n\n"
    "She turns the lights off over the booth and leaves it in the dark.\n\n"
    "[Production note: the writer has this as a second bar across town. The "
    "location team think it reads as the same room as Marlow Street Bar. "
    "This has not been settled.]",
)

pdf.output(str(OUT_DIR / "demo_script.pdf"))


# ==============================================================================
# BUDGET / LOCATIONS PDF — states its cap outright (so a real overrun is
# possible) and prices two interchangeable Ashgrove sites (so the script's
# bare "Ashgrove" can't be resolved without asking).
# ==============================================================================

pdf = new_pdf("LAST TRAIN\nBudget & location notes")

body(
    pdf,
    "Production's capped this sequence at seven thousand dollars, all in. "
    "Five locations below, priced at what each one actually costs us per "
    "day.",
)

heading(pdf, "Marlow Street Bar")
body(
    pdf,
    "$900 a day, $150 to move in. No permit - the landlord's an old friend. "
    "Holds about twenty.",
)

heading(pdf, "Platform 9")
body(
    pdf,
    "$1,200 a day, $250 move-in. Live platform, so a permit's required. "
    "Room for thirty.",
)

heading(pdf, "The Signal Box")
body(
    pdf,
    "$3,200 a day, $600 move-in. Permit required, and the access fee is "
    "what it is. Tight inside - eight people at a push.",
)

heading(pdf, "The Late Bar")
body(
    pdf,
    "$1,600 a day, $300 move-in. Small room out past the river bridge, "
    "counter and a handful of booths. No permit needed. Holds about "
    "fifteen.\n\n"
    "LOCATION MANAGER'S NOTE - UNRESOLVED. I am not convinced this is a "
    "different room from Marlow Street Bar. The scout photos that came back "
    "under both names show the same cracked leather booth, the same window "
    "and the same dead jukebox in the corner. But the two were scouted by "
    "different people, under different names, and the addresses we were "
    "given do not agree with each other. I have priced it separately here "
    "so we do not under-book. Somebody has to confirm which it is before we "
    "book either one. If it is one room under two names we are paying twice "
    "for it, and if it is two rooms and we treat it as one we turn up "
    "without a location. I cannot settle this from the paperwork.",
)

heading(pdf, "Ashgrove Depot")
body(
    pdf,
    "$2,400 a day, $450 move-in. Disused rolling stock parked up behind a "
    "wire fence, gravel underfoot, floodlights on the gate. Permit "
    "required. Holds about twenty-five.",
)

heading(pdf, "Ashgrove Rail Yard")
body(
    pdf,
    "$2,500 a day, $470 move-in. Disused rolling stock parked up behind a "
    "wire fence, gravel underfoot, floodlights on the gate. Permit required "
    "here too. Holds about twenty-two.",
)

pdf.output(str(OUT_DIR / "demo_budget.pdf"))

print("Wrote demo_script.pdf and demo_budget.pdf to", OUT_DIR)
