"""Long work in rounds, and the budget every turn runs under.

A round is a turn in the same session whose context starts fresh from the
last round's handoff (DSH's goal rounds and `ralph`, with a check of our
own: done is a command the server runs, not ZERO's word). tool.py:
`start_rounds`, `end_round`, and the texts the model reads of rounds.
hooks.py: Budget (a turn's spend against the standing amount, and the
estimate past it, asked of the user in every mode, never the judge) and
Rounds (a round's end: its handoff, the next round). service.py: what
rounds are in the server (their row, the next round's message, rounds/list
and rounds/stop). store.py: their table.
"""

from settings import Setting

SETTINGS = (
    # What a turn may spend before the user is asked to let it go on, and
    # what an estimate for work in rounds may come to and start at once.
    # Agent time: a wait for the user's answer doesn't count
    Setting("standing_budget_minutes", "Limits",
            "Minutes a turn runs before asking", 120, "number"),
    # At the model's price (catalog.toml, models.toml); a model with no
    # price spends nothing
    Setting("standing_budget_dollars", "Limits",
            "Dollars a turn spends before asking", 2.0, "number"),
)
