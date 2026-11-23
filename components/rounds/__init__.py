"""Long work in rounds.

A round is a turn in the same session whose context starts fresh from the
last round's handoff (DSH's goal rounds and `ralph`, with a check of our
own: done is a command the server runs, not ZERO's word). tool.py:
`start_rounds`, `end_round`, and the texts the model reads of rounds.
hooks.py: Rounds (a round's end: its handoff, the next round; the one note
when the work passes ZERO's estimate). service.py: what rounds are in the
server (their row, the next round's message, rounds/list and rounds/stop).
store.py: their table.

The estimate is ZERO's own yardstick (principle 3): nothing stops at
it, and no limit on a turn's spend is left (the standing amount went). No
settings of its own since then.
"""

SETTINGS = ()
