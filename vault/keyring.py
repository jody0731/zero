"""The OS keyring, isolated so a locked desktop cannot hold a server thread."""

import hashlib
import subprocess
import sys
from pathlib import Path


class Keyring:
    def __init__(self, home):
        self.account = hashlib.sha256(str(home.resolve()).encode()).hexdigest()

    def exchange(self, operation, value=""):
        result = subprocess.run(
            [sys.executable, "-m", "vault.keyring", operation, self.account],
            input=value, capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parents[1],
        )
        if result.returncode:
            raise OSError("OS keyring unavailable")
        return result.stdout

    def get(self):
        return self.exchange("get") or None

    def set(self, value):
        self.exchange("set", value)


def main():
    try:
        import keyring

        backend = keyring.get_keyring()
        # Only an OS store, never a third-party plaintext fallback.
        supported = {
            "keyring.backends.SecretService", "keyring.backends.kwallet",
            "keyring.backends.macOS", "keyring.backends.Windows",
        }
        if type(backend).__module__ == "keyring.backends.chainer":
            backend = next((item for item in backend.backends
                            if type(item).__module__ in supported), None)
        if type(backend).__module__ not in supported:
            return 1
        operation, account = sys.argv[1:]
        if operation == "get":
            sys.stdout.write(backend.get_password("zero-vault", account) or "")
        elif operation == "set":
            backend.set_password("zero-vault", account, sys.stdin.read())
        else:
            return 1
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
