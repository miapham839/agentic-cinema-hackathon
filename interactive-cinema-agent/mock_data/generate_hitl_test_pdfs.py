"""Generates a mock script+budget pair for testing parser_agent's
request_input rules 2 and 3 (app/agent.py, "WHEN TO ASK THE USER").

Run with:
    uv run --with fpdf2 python mock_data/generate_hitl_test_pdfs.py

Produces, in mock_data/:
  - hitl_script.pdf   - "Cold Storage," a short branching sequence
  - hitl_budget.pdf   - its budget/location memo

Upload both in the SAME message. Two deliberate traps:

  Rule 2 - missing budget cap. The budget memo prices three locations but
    never states an overall total budget. Expected computed floor (GENERAL
    RULE's formula - no location states a total_shoot_days, so each
    defaults to x1):
      Fisherman's Cannery    $3,300
      Cannery Row Storage    $3,450
      Midnight Diner         $2,100
      --------------------------------
      Floor                  $8,850

  Rule 3 - ambiguous location identity, both directions. v2: a first pass
    at this pair had a hidden tiebreaker in each trap, which is why
    Gemini 3.7 resolved both silently instead of asking. This version
    removes both tiebreakers:

    (a) the script's "the old cannery" is a plausible match for EITHER
        "Fisherman's Cannery" or "Cannery Row Storage" in the budget. v1
        named the second option "Bayfront Cold Storage" - the word
        "cannery" only appeared in one candidate's name, so a smart model
        had a free lexical tiebreaker and never needed to treat this as
        ambiguous. Now BOTH budget names contain "Cannery" and get
        identical waterfront/loading-door/permit descriptions differing
        only in price - there is no textual feature left that favors one
        over the other.

    (b) the script names two locations, "The Pancake Shack" and "Midnight
        Diner," with matching physical detail (cracked vinyl booth,
        buzzing neon sign) that argues FOR them being the same place under
        two names - but now each scene also gives a throwaway geographic
        anchor that CONFLICTS ("the north side of town" vs. "two blocks
        off the harbor"). v1 had only the matching detail and no
        counter-evidence, so a smart model confidently merged them. Now
        there's real signal on both sides - same physical description (a
        continuity slip would look exactly like this) but different
        stated geography (two genuinely different, similarly-described
        diners would also look exactly like this) - so neither "same
        place" nor "different places" is the safe default; only asking is.
        The budget still lists only "Midnight Diner," never "The Pancake
        Shack," so there's no way to silently sidestep the question by
        just not writing a location for one of them.

Rule 1 (degenerate extraction - a document with zero extractable scenes or
priced locations) deliberately isn't covered by this pair: it requires an
empty document, which can't coexist with the real scenes/locations rules 2
and 3 need to test against. Test rule 1 separately with an unrelated
one-page memo uploaded as a "script" or "budget."

Neither document uses schema-shaped labels (no "Location ID:", "CHOICE:
... leads to ...") - same house style as generate_mock_pdfs.py, so
parser_agent has to actually reason about the prose rather than
pattern-match a form. Both traps are genuine ambiguities, not something
resolvable by legitimate inference - if parser_agent silently resolves one
on its own instead of calling request_input, that's a real miss, not the
model being appropriately confident.
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
# SCRIPT PDF — "Cold Storage" (rule 3, both directions — see module docstring)
# ==============================================================================

pdf = new_pdf("COLD STORAGE\nDraft 1 - branching sequence")

body(
    pdf,
    "INT. THE PANCAKE SHACK - NIGHT\n\n"
    "NORA slides into the booth across from ELI, out on the north side of "
    "town past the old gas station - the same cracked vinyl seat, the "
    "same buzzing neon sign flickering red through the window behind "
    "him, same as always.\n\n"
    "ELI\n"
    "You said this was the last favor.\n\n"
    "NORA\n"
    "It is. After tonight, we're square.\n\n"
    "Eli doesn't answer right away. Outside, gulls are already circling "
    "the water a few blocks off - the old cannery, probably, picking "
    "through whatever the tide left behind.\n\n"
    "Does Nora go to the old cannery to finish this herself, or does she "
    "send Eli instead and stay behind? Going herself leads into "
    "Cannery Run. Sending Eli leads into Waiting It Out.",
)

heading(pdf, "CANNERY RUN")
body(
    pdf,
    "EXT. WATERFRONT - LATER\n\n"
    "Nora picks her way along the old cannery's loading doors, half of "
    "them rusted shut, gulls scattering off the pilings as she passes. "
    "Whatever's inside, she's not walking away from it clean.\n\n"
    "The sequence ends here for tonight - what's behind those doors is "
    "next episode's problem.",
)

heading(pdf, "WAITING IT OUT")
body(
    pdf,
    "INT. MIDNIGHT DINER - LATER\n\n"
    "Nora's back in a booth an hour later, two blocks off the harbor now "
    "instead of the north side of town - same cracked vinyl under her, "
    "same neon sign still buzzing outside the window, waiting on a text "
    "from Eli that hasn't come yet.\n\n"
    "The sequence ends here for tonight - whatever Eli finds out there "
    "is next episode's problem too.",
)

pdf.output(str(OUT_DIR / "hitl_script.pdf"))


# ==============================================================================
# BUDGET / LOCATIONS PDF — rule 2 (no total budget stated) and rule 3
# (Fisherman's Cannery / Cannery Row Storage both equally plausibly match
# "the old cannery" - both names contain "Cannery," both get the same
# waterfront/loading-door/permit description; only "Midnight Diner" is
# listed, not "The Pancake Shack")
# ==============================================================================

pdf = new_pdf("COLD STORAGE\nBudget & location notes")

body(
    pdf,
    "Location notes for the Cold Storage sequence. Keeping both old "
    "cannery buildings on hold for now - we'll lock which one we're "
    "actually using once we see the light test.",
)

heading(pdf, "Fisherman's Cannery")
body(
    pdf,
    "$3,300 a day, $600 move-in. Old fish-processing building right on "
    "the water, loading doors facing the pier, holds about 20. Permit's "
    "required.",
)

heading(pdf, "Cannery Row Storage")
body(
    pdf,
    "$3,450 a day, $650 move-in. Another old fish-processing building a "
    "few piers down, loading doors facing the water too, holds about 18. "
    "Permit's required here as well.",
)

heading(pdf, "Midnight Diner")
body(
    pdf,
    "$2,100 a day, $400 move-in. Small booth seating, tops out around "
    "12. No permit needed - it's privately owned.",
)

pdf.output(str(OUT_DIR / "hitl_budget.pdf"))

print("Wrote hitl_script.pdf and hitl_budget.pdf to", OUT_DIR)
