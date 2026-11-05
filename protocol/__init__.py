"""The wire format the host and every client share: the protocol's number and
the code's build (version.py), and every verb a caller may send with its
fields (verbs.py). The host and its clients import it, never each other."""
