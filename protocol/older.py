"""The protocol's older versions: how a reply in the server's own protocol
reads in an older one, a step at a time (design rule 7: where two sides
change at different times, a layer translates and states its version).

Each step is one function, `STEPS[n]`, turning protocol n's reply to a
message (or one of its stream's events) into protocol n-1's; `reply_in`
walks a reply down to the protocol a request is written in, as Stripe walks
a reply back through its version changes. A request needs no step: what an
older protocol lacks (an id on a message that changes something) the server
reads as that protocol had it.

Protocol 5 (0.7.6.1): every request names its protocol; each message that
changes something carries an id; hello's modes are rows, each with what it
is and whether a key steps to it; replies typed, rows built from the store
in one place. Protocol 4's modes were names.
"""

from protocol import version


def hello_in_4(reply):
    """Protocol 4's hello: the modes by name."""
    modes = reply.get("modes")
    if isinstance(modes, list):
        reply = {**reply, "modes": [mode["name"] if isinstance(mode, dict)
                                    else mode for mode in modes]}
    return reply


def step_to_4(name, reply):
    if name == "server/hello":
        return hello_in_4(reply)
    return reply


# protocol n → the step that turns its replies into protocol n-1's
STEPS = {5: step_to_4}


def reply_in(protocol, name, reply):
    """`reply`, the server's own protocol's, as protocol `protocol` has it;
    as it is for the server's own protocol or a newer one."""
    for step in range(version.PROTOCOL, max(protocol, version.OLDEST), -1):
        reply = STEPS[step](name, reply)
    return reply


def answered_in(asked):
    """The protocol a reply goes out in: the request's, or the server's own
    for a newer one; None for one older than the oldest it reads."""
    if asked < version.OLDEST:
        return None
    return min(asked, version.PROTOCOL)
