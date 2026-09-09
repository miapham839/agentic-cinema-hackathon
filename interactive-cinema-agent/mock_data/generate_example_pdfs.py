"""Generates the two ALTERNATE example sets shipped with the frontend.

Run with:
    uv run --with fpdf2 python mock_data/generate_example_pdfs.py

Produces, in mock_data/examples/ (filenames match public/examples/ in the
UI repo exactly, so the output can be copied across unchanged):
  - diner-standoff-script.pdf / diner-standoff-budget.pdf
  - the-tip-script.pdf / the-tip-budget.pdf

"Last Train" (generate_demo_pdfs.py) stays the headline set: it exercises
the whole pipeline including both directions of the ambiguous-location
question. These two are deliberately lighter and each shows something Last
Train does NOT, so someone who has already run Last Train learns a new
thing from each rather than watching the same run twice.

WHAT EACH SET IS FOR

  The Diner Standoff  No questions at all, so it runs start to finish
                      without stopping. Its findings are a choice pointing
                      at a scene that was never written, a choice gated on
                      something that never happened on its own branch, and
                      three caps that are not money: crew size, filming
                      days, and hero locations. Those three checks exist in
                      budget_agent (see "If max_primary_locations is set"
                      onwards in its instruction) but never fire on Last
                      Train, whose script states no per-scene days or crew.

  The Tip             One question, and a different one: the budget states
                      caps for crew, days and locations but never a dollar
                      figure, so parser_agent has to ask for it (WHEN TO
                      ASK THE USER, rule 2). Everything downstream is then
                      measured against the number the user chose. Also the
                      only set with a three-way fork.

Neither set has a dead end, on purpose. Last Train already has one (and an
unreachable scene besides), so a dead end in all three would mean seeing
the same finding three times. Both of these carry a continuity break
instead, which is the auditor's fourth category and the one nothing else
exercises: a choice whose required_state names a key no upstream node on
that path ever sets (app/agent.py, the auditor's step 2).

That check only fires if the parser records the gate, so both scripts
write the condition the way a branching manuscript actually writes one
("Open only if the shot was fired") on its own line under the fork, and
app/agent.py now has a required_state bullet telling the parser to record
it and to reuse the state_modifiers key vocabulary so the two line up.

Neither set repeats Last Train's ambiguous-location question. Every
location is named the same way in both of its documents, so there is
nothing to ask about.

Note on the dev fixtures: generate_mock_pdfs.py and
generate_hitl_test_pdfs.py are unchanged and stay as they are. They exist
to stress parser_agent's rules and deliberately contain traps (including
ambiguity traps) that would make a poor first impression as an example.
This file is separate so the two purposes cannot drift into each other.
"""

from pathlib import Path

from fpdf import FPDF, XPos, YPos

OUT_DIR = Path(__file__).resolve().parent / "examples"
OUT_DIR.mkdir(exist_ok=True)

# fpdf2's multi_cell leaves the cursor at the end of the last line by
# default, so without new_x/new_y every call after the first runs out of
# horizontal room.
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
# SET A - "The Diner Standoff"
#
# THE PITCH: a draft that was abandoned halfway. One fork points at a scene
# the writer never got around to writing, and the branch everyone wants to
# shoot is the one that breaks every limit the studio set.
#
# THE STORY GRAPH
#
#   the_standoff        Route 66 Diner   start; forks two ways
#     |- blood_on_the_floor  Harbor Docks    forks two ways
#     |    |- the_old_warehouse  Old Warehouse   a real ending
#     |    '- the_getaway_gone_wrong             NEVER WRITTEN. The fork
#     |                                          names it, no such scene
#     |                                          exists anywhere in the
#     |                                          document. This is the
#     |                                          orphaned_choice finding.
#     '- an_uneasy_truce     Route 66 Diner   Leads on to the_old_warehouse,
#                                             but that choice is written
#                                             "Open only if the shot was
#                                             fired" and this is the branch
#                                             where the gun came DOWN. The
#                                             shot is fired in
#                                             blood_on_the_floor, which is
#                                             the other branch entirely, so
#                                             nothing on this path ever sets
#                                             it. This is the
#                                             continuity_break finding, and
#                                             it reads like a gate copied
#                                             across from the other fork.
#
# No parser question fires here, on purpose. All three locations are named
# identically in both documents, the budget states a dollar cap, and every
# scene is unmistakably a scene. It runs start to finish without stopping,
# which is the contrast with Last Train.
#
# Note the_old_warehouse now has two parents, one from each branch. That is
# deliberate and is not itself a finding: converging branches are normal in
# a CYOA manuscript. Only the gate on the second edge is wrong.
#
# THE MONEY. Same model as everywhere else: a production shoots every
# branch, so it pays for each location a scene sits at, booked once, at
# day rate (x1 day, since the memo prices a single day each) plus move-in.
#
#   Route 66 Diner   4,200 +   900 = $5,100
#   Harbor Docks     5,200 + 1,300 = $6,500
#   Old Warehouse    3,800 + 1,500 = $5,300
#   ----------------------------------------
#   Total                           $16,900
#   Stated cap                      $12,000   over by $4,900 (41%)
#
# THE SAVING, and there is deliberately only one. A location leaves the
# bill when nothing is shot there any more. the_old_warehouse is written
# with nothing warehouse-specific in it, only crates and containers, which
# the Harbor Docks scene right before it already establishes are there.
# Moving it onto the docks empties the Old Warehouse:
#
#   16,900 - 5,300 = $11,600, which is $400 UNDER the cap.
#
# That is the point of the numbers. One approval takes the production from
# over budget to under, so the approve-then-rerun loop has a visible payoff
# inside a short run. It also removes a company move, since the two scenes
# then sit at the same location back to back.
#
# THE THREE CAPS THAT ARE NOT MONEY. Every scene states its shoot days and
# its crew as a production note, which is what makes budget_agent's
# max_filming_days and max_total_crew checks fire. Last Train states
# neither, so those checks skip there and this is the only set that shows
# them.
#
#   Branch A  standoff -> blood -> warehouse
#             3 distinct locations  vs 2 hero locations   OVER
#             1 + 2 + 1 = 4 days    vs 3 filming days     OVER
#             max(8, 18, 9) = 18    vs 10 crew            OVER
#
#   Branch B  standoff -> truce
#             1 distinct location   vs 2                  fine
#             1 + 1 = 2 days        vs 2                  fine
#             max(8, 8) = 8         vs 10                 fine
#
# Crew is a headcount for the branch's most demanding scene, not a sum, so
# 18 is the figure that matters on branch A. The split is the readable
# insight: the branch with the gunshot in it breaks all three limits, and
# the cheap compliant branch is the one that dead-ends.
# ==============================================================================

pdf = new_pdf("THE DINER STANDOFF\nDraft 1 - branching sequence")

body(
    pdf,
    "INT. ROUTE 66 DINER - NIGHT\n\n"
    "The overhead sign buzzes and flickers through the window. SAM has a "
    "revolver leveled across the counter. JAMIE hasn't moved from the "
    "corner booth in what feels like a full minute, palms flat against the "
    "formica like she's holding the table down.\n\n"
    "SAM\n"
    "You shouldn't have come back here.\n\n"
    "JAMIE\n"
    "I didn't have a choice.\n\n"
    "Somewhere in the kitchen a fryer timer goes off and nobody moves to "
    "answer it. Everything in the room is waiting on Sam.\n\n"
    "[PRODUCTION NOTE: one shoot day. Eight crew, we keep it small in "
    "there.]\n\n"
    "Does he pull the trigger, or does he lower the gun and try to talk "
    "his way out of it? Pulling the trigger drops us straight into Blood "
    "on the Floor. Trying to talk it out instead lands us in An Uneasy "
    "Truce.",
)

heading(pdf, "BLOOD ON THE FLOOR")
body(
    pdf,
    "EXT. HARBOR DOCKS - NIGHT\n\n"
    "The shot is fired before anyone in the room can stop it, and it "
    "cracks through the diner like the building flinched. Sam is "
    "out the back within seconds, Jamie's weight over one shoulder, the "
    "gun still warm in his other hand, and he doesn't slow down until the "
    "docks swallow him and the sirens are just noise behind him.\n\n"
    "He wades in to his knees and lets the revolver go under, then puts his "
    "back against a stack of shipping containers and finally lets her down "
    "onto the wet concrete. The tide takes the gun somewhere nobody is "
    "going to find it. Whatever was between them before, it isn't there "
    "anymore.\n\n"
    "[PRODUCTION NOTE: two shoot days, this one is the whole reason the "
    "schedule is tight. Night exterior on open water, so we need the full "
    "unit out there, eighteen bodies including the marine safety officer. "
    "There is no version of this scene that runs lean.]\n\n"
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
    "them has shifted, and it isn't going back to what it was.\n\n"
    "[PRODUCTION NOTE: one shoot day, same eight crew as the opener. We "
    "are already standing in the diner, so this is the cheap half of the "
    "sequence.]\n\n"
    "They leave together a few minutes later, which puts us in The Old "
    "Warehouse. Open only if the shot was fired.",
)

heading(pdf, "THE OLD WAREHOUSE")
body(
    pdf,
    "INT. OLD WAREHOUSE - LATER\n\n"
    "Sam moves along a row of stacked crates without breaking stride, "
    "keeping the light off him, one hand out to steady himself against the "
    "cold steel as he goes. By the time the first responders reach the "
    "water he is three blocks away and hasn't looked back once.\n\n"
    "Nothing in here belongs to him and nothing in here is worth going "
    "back for. Whatever comes after tonight isn't part of this - the "
    "sequence ends here.\n\n"
    "[PRODUCTION NOTE: one shoot day, nine crew. Crates and a steady cam, "
    "that's the whole scene. Art department flagged that we are not tied "
    "to this building for anything, any covered space with stacked freight "
    "in it plays the same.]",
)

pdf.output(str(OUT_DIR / "diner-standoff-script.pdf"))


# ==============================================================================
# SET A BUDGET - a producer's memo, prose not a spec sheet. Numbers appear
# in mixed phrasing (word numbers, "$X/day" vs "$X a day") the way a real
# memo writes them, and the permit requirement is implied rather than being
# a Yes/No field, so parser_agent has to read them out of the prose.
#
# No total_shoot_days is stated for any location, on purpose: the memo says
# outright that it prices one day each while the schedule firms up. That
# keeps the cost arithmetic above exact, and it is also why the per-scene
# day counts in the script are allowed to disagree with it.
# ==============================================================================

pdf = new_pdf("THE DINER STANDOFF\nBudget & location notes")

body(
    pdf,
    "Quick rundown for the diner sequence (we've been tracking it "
    "internally under the code diner-standoff-01). Studio's given us a "
    "hard ceiling of twelve thousand dollars for this whole stretch, wants "
    "it wrapped inside three shooting days, and is capping the crew at ten "
    "bodies on set. They've also asked us to keep it to two hero locations "
    "if we can, since every extra company move eats into both the schedule "
    "and the number above.\n\n"
    "Rates below are per day. The schedule isn't locked yet, so I've "
    "priced a single day at each of them for now and we'll revise once the "
    "board is up.",
)

heading(pdf, "Route 66 Diner")
body(
    pdf,
    "Our cheapest anchor at $4,200 for a full day, and the move-in fee is "
    "$900. It's home base for most of the sequence. Tight space, tops out "
    "around 15 people comfortably, and yes, we need the standard shoot "
    "permit for it.",
)

heading(pdf, "Harbor Docks")
body(
    pdf,
    "The expensive one: $5,200 a day, plus a $1,300 hit every time the "
    "company physically relocates out there. Capacity's around 20, and "
    "it's public waterfront so a permit is required, no way around it.",
)

heading(pdf, "Old Warehouse")
body(
    pdf,
    "The wildcard. Daily rate's $3,800, move-in runs $1,500, and because "
    "it's privately owned we're actually permit-free there, which helps. "
    "Can hold up to 25 if we ever need the extra room.",
)

pdf.output(str(OUT_DIR / "diner-standoff-budget.pdf"))


# ==============================================================================
# SET B - "The Tip"
#
# THE PITCH: the documents are complete and internally consistent except
# for one thing. The producer wrote down a crew cap, a day cap and a
# location cap, and never wrote down a dollar figure. So the first thing
# that happens is that the app asks the user for one, and every number it
# reports afterwards is measured against the answer they gave.
#
# THE STORY GRAPH. Three-way fork out of the opening scene, which is the
# only set that has one:
#
#   the_stairwell_handoff   Port Authority Office   start; forks THREE ways
#     |- foreman_confrontation  Shipping Office
#     |    '- foreman_ending    Shipping Office     a real ending
#     |- warehouse_stakeout     Harbor Warehouse
#     |    '- the_ledger        Harbor Warehouse    Leads on to
#     |                                             foreman_confrontation,
#     |                                             gated "Open only if
#     |                                             Reyes has already
#     |                                             talked". Reyes talks in
#     |                                             foreman_confrontation
#     |                                             itself, on a different
#     |                                             branch, so nothing on
#     |                                             this path sets it. This
#     |                                             is the continuity_break
#     |                                             finding.
#     '- fish_market_inquiry    Harbor Fish Market  a real ending, the
#                                                   trail goes cold
#
# WHY THE QUESTION FIRES. parser_agent's rule 2 (WHEN TO ASK THE USER,
# app/agent.py) triggers when a budget document states no total_budget_usd.
# The memo below states three other caps in plain prose, so it is clearly a
# real budget document rather than an unreadable one, and the omission is
# specific rather than general. The rule tells the parser to compute a
# floor first and offer it as option (a), and that floor is day rates only:
#
#   2,200 + 2,600 + 4,100 + 3,400 = $12,300
#
# WHY THAT FLOOR IS INTERESTING RATHER THAN A FORMALITY. The floor ignores
# company moves. What the production actually pays does not:
#
#   Port Authority Office  2,200 + 400 = $2,600
#   Shipping Office        2,600 + 500 = $3,100
#   Harbor Warehouse       4,100 + 900 = $5,000
#   Harbor Fish Market     3,400 + 800 = $4,200
#   --------------------------------------------
#   Total                              $14,900
#
# So a user who accepts the offered $12,300 immediately sees a $2,600
# overrun (21%) rather than a clean bill, which is the honest result and a
# better demonstration than either. Typing their own number puts them in
# control of the outcome, and choosing to skip the cap turns the overrun
# check off entirely. All three answers lead somewhere different, which is
# the point of asking at all.
#
# THE SAVING. fish_market_inquiry is sluglined at the market so its
# identity is unambiguous, but nothing in the action depends on a market:
# she works the harbour front, nobody talks to her, she leaves. Moving it
# to the Harbor Warehouse exterior empties the Fish Market:
#
#   14,900 - 4,200 = $10,700, which is $1,600 under the offered floor.
#
# THE BRANCH THE GATE CREATES. handoff -> stakeout -> ledger -> foreman
# confrontation -> foreman ending touches 3 distinct locations against a cap
# of 3, and sums 1 + 2 + 1 + 1 = 5 days against a cap of 5. Both land exactly
# on the limit rather than over it, so the crew cap below is the only
# non-dollar finding and this set stays light.
#
# THE CREW CAP. warehouse_stakeout states a unit of twelve against the
# memo's cap of eight, so every branch through the stakeout is flagged and
# the other two are not. Crew is the largest single scene's headcount on a branch, not a
# sum, which is why one scene is enough to fail it.
#
# NO DEAD END HERE. the_ledger used to be one. It now continues, and what
# is wrong with it is the gate on the way out rather than the absence of
# one. See the module docstring for why both alternate sets moved off dead
# ends.
#
# ONE DELIBERATE GAP. foreman_ending states no shoot days, while every
# other scene does. budget_agent is told to still add up what it has and
# say the figure is a partial lower bound rather than treating the missing
# value as zero. This set is where that behaviour is visible.
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
    "[PRODUCTION NOTE: one shoot day, six crew. Two actors and a "
    "stairwell.]\n\n"
    "Vic has three ways she can play this. Going at the shipping foreman "
    "directly with what's on the manifest takes us into Foreman "
    "Confrontation. Sitting on the warehouse first to see who actually "
    "shows up takes us into Warehouse Stakeout. Working the harbour front "
    "for talk before she tips her hand anywhere takes us into Fish Market "
    "Inquiry.",
)

heading(pdf, "FOREMAN CONFRONTATION")
body(
    pdf,
    "INT. SHIPPING OFFICE - DAY\n\n"
    "Vic drops the manifest on REYES's desk without any preamble. He "
    "doesn't even look surprised, which tells her everything the paperwork "
    "didn't.\n\n"
    "REYES\n"
    "You're going to want to sit down for this part.\n\n"
    "By the time he's done, Reyes has talked, and he has told her more than "
    "he meant to. They both know it.\n\n"
    "[PRODUCTION NOTE: one shoot day, six crew.]\n\n"
    "However that lands, it lands here: this one runs straight on into "
    "Foreman Ending.",
)

heading(pdf, "FOREMAN ENDING")
body(
    pdf,
    "INT. SHIPPING OFFICE - CONTINUOUS\n\n"
    "Vic folds the manifest back up and pockets it. Reyes doesn't get up "
    "and doesn't stop her. Whatever happens between the two of them after "
    "tonight isn't part of this - the sequence ends with her walking out "
    "the same door she came in.",
)

heading(pdf, "WAREHOUSE STAKEOUT")
body(
    pdf,
    "EXT. HARBOR WAREHOUSE - NIGHT\n\n"
    "Vic settles into the car across the lot, engine off, watching the "
    "loading doors for anything that doesn't belong. Nothing moves for a "
    "long time. Then, close to dawn on the second night, a truck she "
    "doesn't recognise backs up to the doors.\n\n"
    "[PRODUCTION NOTE: two shoot days, we need the stakeout covered across "
    "a wet night and a clear one before it cuts together. Twelve crew, the "
    "biggest unit in the sequence, and that is after trimming it.]\n\n"
    "She waits until the lot is empty again, then goes in. Straight on "
    "into The Ledger.",
)

heading(pdf, "THE LEDGER")
body(
    pdf,
    "INT. HARBOR WAREHOUSE - LATER THAT NIGHT\n\n"
    "Vic slips through the side door and finds the ledger exactly where "
    "the manifest said it would be. She reads two pages standing up, and "
    "whatever is on the second one stops her.\n\n"
    "It's still open in her hands when she reaches for the phone. She doesn't "
    "call Hale. She calls the number written inside the back cover, and the "
    "voice that answers sends her straight back across the water.\n\n"
    "[PRODUCTION NOTE: one shoot day, seven crew.]\n\n"
    "That takes us into Foreman Confrontation. Open only if Reyes has "
    "already talked.",
)

heading(pdf, "FISH MARKET INQUIRY")
body(
    pdf,
    "EXT. HARBOR FISH MARKET - NIGHT\n\n"
    "Vic works the harbour front the long way round, manifest folded out "
    "of sight, asking the same three questions of anyone still out at this "
    "hour. Nobody is in a hurry to talk to her and nobody has to be.\n\n"
    "By the time one of them finally says something worth hearing, it is "
    "about a truck that left two hours ago. The trail is cold and she "
    "knows it. The sequence ends with Vic walking back to the car with "
    "nothing to show for the night.\n\n"
    "[PRODUCTION NOTE: one shoot day, five crew. She never goes inside "
    "anywhere and never handles a thing, so we are not tied to this "
    "address - any stretch of the harbour front after dark plays it.]",
)

pdf.output(str(OUT_DIR / "the-tip-script.pdf"))


# ==============================================================================
# SET B BUDGET - states a crew cap, a filming-day cap and a hero-location
# cap, and no dollar figure anywhere. That omission is the whole point of
# this document, so do not "fix" it by adding a total.
#
# Every location here is named exactly as the script's sluglines name it,
# and no location is priced that the script does not use. That is
# deliberate: it leaves parser_agent nothing to ask about under rule 3, so
# the missing cap is the only question this set raises.
# ==============================================================================

pdf = new_pdf("THE TIP\nBudget & location notes")

body(
    pdf,
    "Location rundown for The Tip sequence. Producer wants a hard line at "
    "eight crew on set, no more than five filming days across the whole "
    "shoot, and ideally no more than three hero locations if we can help "
    "it. Numbers below are what each location actually costs us per day; "
    "the board isn't built yet so treat them as single days for now.\n\n"
    "Finance still owes us the top-line figure for the sequence and I "
    "don't want to guess at it in writing, so there's no total in here "
    "yet. Chase them before this goes anywhere.",
)

heading(pdf, "Port Authority Office")
body(
    pdf,
    "$2,200 for the day and a $400 move-in. Small, tops out at about 12 "
    "people, and we need the standard permit for it.",
)

heading(pdf, "Shipping Office")
body(
    pdf,
    "$2,600 for a full day, plus a $500 move-in fee. Seats about 15 "
    "comfortably, and yes, a permit's required.",
)

heading(pdf, "Harbor Warehouse")
body(
    pdf,
    "$4,100 a day, with a $900 move-in charge. Capacity's around 25, and a "
    "permit's required.",
)

heading(pdf, "Harbor Fish Market")
body(
    pdf,
    "$3,400 a day, plus an $800 move-in fee. Can hold up to 20, and it's a "
    "working market so a permit's required there too.",
)

pdf.output(str(OUT_DIR / "the-tip-budget.pdf"))

print("Wrote 4 example PDFs to", OUT_DIR)
