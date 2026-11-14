"""ZERO's HTTP door: one small process on 127.0.0.1 for the web UI
(apps/web_ui/) and for MCP at /mcp, every request whole through ZERO's
server; systemd holds its port and starts it at the first knock, and it
leaves when idle (door.py, guard.py, systemd_units.py)."""
