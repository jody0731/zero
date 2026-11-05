"""ZERO's host: the one process that runs every live session.

`zero` starts it as the transient unit zero-host (client/launcher.py), or `zero
host` runs it in the terminal; both come here, so the client's command never
loads the host's code (the layer check). It serves until idle for the
configured minutes, reading the user's config.toml again whenever it changes.
The evals' host is dev/evalkit/eval_host.py, so a ZERO
shipped without dev/ has none.
"""

import argparse
import sys
from pathlib import Path

if __name__ == "__main__":
    # Run as a script: ZERO's folder in place of this one, whose names
    # (sessions, jobs) would hide ZERO's own
    sys.path[0] = str(Path(__file__).resolve().parents[1])

import assembly  # noqa: E402
import settings  # noqa: E402
from host.runtime import Runtime  # noqa: E402
from host.server import AlreadyRunning, Host  # noqa: E402
from paths import Paths, permissions_file  # noqa: E402


def build_runtime(paths, make_session=None, config=None, limits=None,
                  make_consent=None, saved_permissions=permissions_file,
                  tick=2.0, **options):
    """ZERO's runtime, put together as the host runs it: the assembly's
    sessions, modes and parts. `make_session`: in place of the assembly's
    (tests, the evals); `limits` over the config's for the assembly's.
    `make_consent`, `saved_permissions` and `tick` go to the parts
    (assembly.host_parts); `options` to the Runtime."""
    config = config or (lambda: settings.DEFAULTS)
    if make_session is None:
        make_session = assembly.host_sessions(paths, config, limits)
    parts = assembly.host_parts(config, make_consent, saved_permissions, tick)
    return Runtime(paths, make_session, tuple(assembly.MODES), parts=parts,
                   config=config, **options)


def run(paths, model=None):
    """`model`: new sessions' model over config.toml's. A broken
    config.toml starts the host on the defaults, the error said."""
    config = settings.Config(mode=assembly.canonical)
    runtime = build_runtime(paths, config=config, model=model)
    Host(runtime, paths).serve()


def parser():
    parser = argparse.ArgumentParser(prog="zero host")
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
