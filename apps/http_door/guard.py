"""The door's guard: every request is checked here before anything runs.

1. **Host** is the door's own (`127.0.0.1:PORT`, `localhost:PORT`): a page of
   another site whose name was pointed at this machine (DNS rebinding) names
   its own host, and is refused.
2. **Origin**, where a browser sends one, is the door's own page, and a
   browser's `Sec-Fetch-Site` says same-origin or none (the user's own
   navigation): another site's page reaches nothing.
3. **Who sends it.** A browser (it sends an Origin or `Sec-Fetch-*`) runs
   other sites' pages too, so it shows the cookie the door set when the user
   signed in with the one-use link `zero web` opened. Any other client is
   proven as the server's socket proves its own: the kernel names the
   connection's owner and process (tcp_peer.py), which must be the user's;
   where the kernel can't say, the app token as `Authorization: Bearer`. The
   process found goes on to the server (relayed_for_pid), which holds ZERO's
   own commands to what they may do there, as on the socket.

The cookie and the sign-in link are values derived from the app token (an
HMAC of it), never the token itself, so the browser's store and its history
never hold it; a new token (its file removed, the server started again) ends
every sign-in.
"""

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass

from apps.http_door import tcp_peer
from client.connection import read_token

# The names the door answers to; the port is the door's own
LOOPBACK_NAMES = ("127.0.0.1", "localhost")
# What a browser's Sec-Fetch-Site may say: the door's own page, or the
# user's own navigation (typed, a bookmark, a link from the terminal)
FETCH_SITES = ("same-origin", "none")
# How long a sign-in link opens the door, once
SIGN_IN_SECONDS = 120
# How long a browser stays signed in
COOKIE_SECONDS = 30 * 24 * 3600
# What each derived value is for, so one can't stand for the other
COOKIE_PURPOSE = "zero web ui cookie"
SIGN_IN_PURPOSE = "zero web ui sign-in"


class Refused(Exception):
    """A request the guard turns away: its HTTP status and why."""

    def __init__(self, status, why):
        super().__init__(why)
        self.status = status


@dataclass(frozen=True)
class Proof:
    """Who a request is from: `pid` the client's process where the kernel
    named it (None: proven by the cookie or the token alone); `browser`:
    a browser sent it."""

    pid: int | None
    browser: bool


def derived(token, purpose, text=""):
    """An HMAC of the token, for one purpose: what the browser holds."""
    return hmac.new(token.encode(), f"{purpose}\n{text}".encode(),
                    hashlib.sha256).hexdigest()


def same(given, expected):
    return (isinstance(given, str) and bool(given)
            and hmac.compare_digest(given.encode(), expected.encode()))


def sign_in_code(token, now=None, nonce=None):
    """A one-use sign-in code for the page's link: when it ends, a nonce,
    and their HMAC."""
    ends = int((now if now is not None else time.time()) + SIGN_IN_SECONDS)
    nonce = nonce or secrets.token_urlsafe(12)
    return f"{ends}.{nonce}.{derived(token, SIGN_IN_PURPOSE, f'{ends}.{nonce}')}"


def cookie_name(port):
    """The cookie's name: a browser keeps cookies by host, not port, so two
    homes' doors on one machine keep theirs apart."""
    return f"zero_web_{port}"


class Guard:
    """The checks, for the door on `port`. `token_file`: the app token's
    path; `make_token()` starts ZERO's server, which writes it, when a
    request needs it and it isn't there yet. `peer_of(conn)`: the kernel's
    word on a connection (tests give their own)."""

    def __init__(self, port, token_file, make_token=None,
                 peer_of=tcp_peer.peer_of):
        self.port = port
        self.token_file = token_file
        self.make_token = make_token
        self.peer_of = peer_of
        self.hosts = {f"{name}:{port}" for name in LOOPBACK_NAMES}
        self.origins = {f"http://{host}" for host in self.hosts}
        # Each sign-in code's nonce, kept until its code ends: used once
        self.used = {}

    def token(self):
        """The app token's text; made by the server when there is none."""
        found = read_token(self.token_file)
        if found is None and self.make_token is not None:
            self.make_token()
            found = read_token(self.token_file)
        if found is None:
            raise Refused(503, f"no app token at {self.token_file}")
        return found

    def place(self, headers):
        """Steps 1 and 2: the Host, and a browser's Origin and fetch site.
        Returns whether a browser sent it."""
        host = headers.get("Host")
        if host not in self.hosts:
            raise Refused(421, f"this is ZERO's door on 127.0.0.1:{self.port}"
                          f"; Host {host!r} isn't it")
        origin = headers.get("Origin")
        if origin is not None and origin not in self.origins:
            raise Refused(403, f"requests from {origin} aren't taken here")
        site = headers.get("Sec-Fetch-Site")
        if site is not None and site not in FETCH_SITES:
            raise Refused(403, f"a {site} request isn't taken here")
        return origin is not None or any(
            name.lower().startswith("sec-fetch-") for name in headers.keys())

    def who(self, conn, headers, peer=None):
        """Step 3: the request's Proof, or Refused. `peer`: the kernel's
        word on this connection, if already asked."""
        browser = self.place(headers)
        if peer is None:
            peer = self.peer_of(conn)
        if peer is not None and peer.uid != os.getuid():
            raise Refused(403, "this door is another user's")
        pid = peer.pid if peer is not None else None
        if browser:
            if self.signed_in(headers):
                return Proof(pid, True)
            raise Refused(401, "not signed in: open the page with zero web")
        bearer = bearer_of(headers)
        if bearer is not None:
            if not same(bearer, self.token()):
                raise Refused(401, "wrong token: it isn't the text of the "
                              f"token file ({self.token_file})")
            return Proof(pid, False)
        if peer is not None:
            return Proof(pid, False)
        raise Refused(401, "the kernel doesn't say who connects here, so a "
                      "request proves the user with Authorization: Bearer "
                      f"and the token file's text ({self.token_file})")

    def signed_in(self, headers):
        """Whether the browser shows the door's cookie (or the token)."""
        bearer = bearer_of(headers)
        if bearer is not None:
            return same(bearer, self.token())
        given = cookies(headers).get(cookie_name(self.port))
        return same(given, derived(self.token(), COOKIE_PURPOSE))

    def sign_in(self, code, now=None):
        """The cookie's value for a good sign-in code, used now; Refused for
        one that is wrong, used or old."""
        now = time.time() if now is None else now
        self.used = {nonce: ends for nonce, ends in self.used.items()
                     if ends >= now}
        try:
            ends_text, nonce, mac = code.split(".", 2)
            ends = int(ends_text)
        except (AttributeError, ValueError):
            raise Refused(400, "that sign-in link is broken") from None
        token = self.token()
        if not same(mac, derived(token, SIGN_IN_PURPOSE, f"{ends}.{nonce}")):
            raise Refused(403, "that sign-in link isn't this door's")
        if ends < now or ends > now + SIGN_IN_SECONDS + 5:
            raise Refused(403, "that sign-in link is old: open the page "
                          "again with zero web")
        if nonce in self.used:
            raise Refused(403, "that sign-in link was used: open the page "
                          "again with zero web")
        self.used[nonce] = ends
        return derived(token, COOKIE_PURPOSE)

    def set_cookie(self, value):
        """The Set-Cookie header's value: the page's alone, never sent by
        another site's request, unreadable by script."""
        return (f"{cookie_name(self.port)}={value}; Path=/; HttpOnly; "
                f"SameSite=Strict; Max-Age={COOKIE_SECONDS}")


def bearer_of(headers):
    value = headers.get("Authorization")
    if not value:
        return None
    kind, _, given = value.partition(" ")
    # Another scheme's header isn't the door's to read
    return given.strip() if kind.lower() == "bearer" else None


def cookies(headers):
    found = {}
    for header in headers.get_all("Cookie") or ():
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                found[name] = value
    return found
