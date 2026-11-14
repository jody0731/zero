"""The sandbox's service in ZERO's server (server/services.py): at the
server's start, the scratch folders an earlier server on the same home left
are removed. A server that stops removes its sessions' (sandbox.Scratch); one
killed (SIGKILL, a power cut) can't. One server runs on a home, and this runs
once it holds the home's lock, so every folder with the home's mark is then
a leftover; another home's server, or another ZERO, marks its own.
"""

import shutil

from server.services import Service
from sandbox.sandbox import scratch_left


class SandboxService(Service):
    name = "sandbox"

    def begin(self):
        for folder in scratch_left(self.runtime.paths.home):
            shutil.rmtree(folder, ignore_errors=True)
