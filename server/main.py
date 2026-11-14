"""ZERO's server: the one process that runs every live session.

`zero` starts it as the transient unit zero-server (client/launcher.py), or `zero
server` runs it in the terminal; both come here, so the client's command never
loads the server's code (the layer check). It serves until idle for the
configured minutes, reading the user's config.toml again whenever it changes.
The evals' server is dev/evalkit/eval_host.py, so a ZERO
shipped without dev/ has none.
"""

import argparse
import sys
from pathlib import Path

if __name__ == "__main__":
    # Run as a script: ZERO's folder in place of this one, whose names
    # (sessions, jobs) would hide ZERO's own
    folder = str(Path(__file__).resolve().parents[1])
    # With -I (ZERO's own Pythons), which leaves this one out, before
    # the rest
    if sys.flags.safe_path:
        sys.path.insert(0, folder)
    else:
        sys.path[0] = folder

import assembly  # noqa: E402
import settings  # noqa: E402
from server.runtime import Runtime  # noqa: E402
from server.server import AlreadyRunning, Server  # noqa: E402
from paths import Paths, mcp_servers_file, permissions_file  # noqa: E402


def build_runtime(paths, make_session=None, config=None, limits=None,
                  make_consent=None, saved_permissions=permissions_file,
                  tick=2.0, mcp_servers=mcp_servers_file, **options):
    """ZERO's runtime, put together as the server runs it: the assembly's
    sessions, modes and services. `make_session`: in place of the assembly's
    (tests, the evals); `limits` over the config's for the assembly's.
    `make_consent`, `saved_permissions`, `tick` and `mcp_servers` go to the
    services (assembly.services); `options` to the Runtime."""
    config = config or (lambda: settings.DEFAULTS)
    if make_session is None:
        make_session = assembly.server_session_maker(paths, config, limits)
    services = assembly.services(config, make_consent, saved_permissions, tick,
                                 mcp_servers)
    return Runtime(paths, make_session, assembly.mode_rows(), services=services,
                   config=config, **options)


def run(paths, model=None):
    """`model`: new sessions' model over config.toml's. A broken
    config.toml starts the server on the defaults, the error said."""
    config = settings.Config(mode=assembly.canonical)
    runtime = build_runtime(paths, config=config, model=model)
    Server(runtime, paths).serve()


def parser():
    parser = argparse.ArgumentParser(prog="zero server")
    parser.add_argument("--model", help="new sessions' default model")
    return parser


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        run(Paths(), args.model)
    except AlreadyRunning as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
