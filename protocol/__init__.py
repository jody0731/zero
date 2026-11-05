"""The one door: the wire format the host and every client share. The
protocol's number and the code's build (version.py), every message a caller
may send with its fields and reply (messages.py), and the same as JSON Schema
(schema.py writes schema.json, which ships). The host and its clients import
it, never each other."""
