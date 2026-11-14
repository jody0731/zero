"""ZERO's releases where they are published: the newest one's version, and a
release's archive for this machine fetched and checked.

A release is a tag, `v` and dotted numbers, with an archive for each machine:
ZERO's code, its packages and its own Python, `zero-<version>-<machine>.tar.gz`,
beside its sha256 (`….tar.gz.sha256`). Releases are found where a copy of ZERO
came from: an install's release.json names the place, a clone's origin on
GitHub does, and ZERO_RELEASES_URL names a mirror laid out as GitHub lays them
out (`<source>/releases/latest` leads to the newest release's tag page,
`<source>/releases/download/<tag>/<file>` is one of its files). The sum comes
from the same place: it finds a download cut short or damaged, not a release
replaced where it is published (Codex's installer checks no more).
"""

import hashlib
import os
import platform
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# A mirror of ZERO's releases, in place of where a copy came from
MIRROR_ENV = "ZERO_RELEASES_URL"
# A request's wait for the server's next bytes
TIMEOUT_SECONDS = 30
CHUNK_BYTES = 1 << 20
# A release's tag page, where `releases/latest` leads
TAG_PAGE = re.compile(r"/releases/tag/v(\d+(?:\.\d+)*)/?$")
VERSION = re.compile(r"\d+(?:\.\d+)*")
SHA256 = re.compile(r"[0-9a-f]{64}")
# A git remote on GitHub, by https or ssh
GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)


class ReleaseError(Exception):
    """A release couldn't be found, fetched or checked; the text says which."""


class NoArchive(ReleaseError):
    """The release has no archive for this machine."""


def number(version):
    """A version's numbers, to compare: (0, 7, 5, 3)."""
    return tuple(int(part) for part in version.split("."))


def machine():
    """This machine as archives name it: x86_64-linux."""
    return f"{platform.machine().lower()}-{sys.platform}"


def archive_name(version, machine_name=None):
    return f"zero-{version}-{machine_name or machine()}.tar.gz"


def source_from_origin(url):
    """A git remote's releases on GitHub (https://github.com/OWNER/REPO);
    None for a remote anywhere else."""
    match = GITHUB_REMOTE.match(url.strip())
    if match is None:
        return None
    return f"https://github.com/{match.group(1)}/{match.group(2)}"


def source(found=None):
    """Where releases come from: the mirror ZERO_RELEASES_URL names, else
    `found` (an install's release.json, a clone's origin); None for neither."""
    place = os.environ.get(MIRROR_ENV) or found
    return place.rstrip("/") if place else None


def opened(url):
    """A response for `url`, or ReleaseError saying why not."""
    try:
        return urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ReleaseError(f"{url} isn't there") from None
        raise ReleaseError(f"{url}: HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise ReleaseError(f"couldn't reach {url}: {reason}") from None


def newest(place):
    """The newest release's version, from where `<place>/releases/latest`
    leads."""
    url = f"{place}/releases/latest"
    with opened(url) as response:
        final = response.geturl()
    match = TAG_PAGE.search(final.split("?")[0])
    if match is None:
        raise ReleaseError(f"{url} led to {final}, not a release's tag")
    return match.group(1)


def file_url(place, version, name):
    return f"{place}/releases/download/v{version}/{name}"


def expected_sum(place, version, name):
    """The archive's sha256, as its release states it."""
    url = file_url(place, version, name) + ".sha256"
    with opened(url) as response:
        text = response.read(4096).decode("ascii", errors="replace")
    found = text.split()[0].lower() if text.split() else ""
    if not SHA256.fullmatch(found):
        raise ReleaseError(f"{url} holds no sha256")
    return found


def fetch(place, version, folder, out=None, machine_name=None):
    """The release's archive for this machine, into `folder`, its sum
    checked: its path. `out(text)`: says what is fetched. A release with no
    archive for this machine, a download cut short and a sum that differs
    raise ReleaseError, leaving nothing in `folder` but a `.part` file."""
    if not VERSION.fullmatch(version):
        raise ReleaseError(f"{version!r} isn't a version")
    name = archive_name(version, machine_name)
    try:
        expected = expected_sum(place, version, name)
    except ReleaseError as error:
        raise NoArchive(
            f"release {version} has no archive for this machine "
            f"({machine_name or machine()}): {error}") from None
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / (name + ".part")
    digest = hashlib.sha256()
    with opened(file_url(place, version, name)) as response:
        size = response.headers.get("Content-Length")
        if out:
            megabytes = f" ({int(size) / 1e6:.0f} MB)" if size else ""
            out(f"Fetching {name}{megabytes} from {place}.")
        got = 0
        with open(part, "wb") as stream:
            while True:
                try:
                    chunk = response.read(CHUNK_BYTES)
                except OSError as error:
                    raise ReleaseError(f"the download of {name} broke off: "
                                       f"{error}") from None
                if not chunk:
                    break
                stream.write(chunk)
                digest.update(chunk)
                got += len(chunk)
    if size and got != int(size):
        raise ReleaseError(f"the download of {name} stopped at {got} of "
                           f"{size} bytes")
    if digest.hexdigest() != expected:
        raise ReleaseError(f"{name}'s sha256 isn't the one its release states")
    path = folder / name
    os.replace(part, path)
    return path
