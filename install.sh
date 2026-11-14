#!/bin/sh
# Z.E.R.O.'s installer. It fetches the newest release for this machine (ZERO
# with its own Python inside) from GitHub, or from the mirror ZERO_RELEASES_URL
# names, checks its sha256, unpacks it and hands it to its own `zero install`:
# the release goes in ~/.local/share/zero, the zero command in ~/.local/bin,
# then `zero doctor` tries what ZERO needs from the machine. No Python, git or
# pip is needed. Run again, it installs the newest again, which repairs an
# install. ZERO's home (~/.zero) is made on first use and never touched here.
#
#   install.sh [--bin DIR] [--dir DIR] [--version VERSION]
#       --bin: where the zero command goes (~/.local/bin)
#       --dir: where releases go (~/.local/share/zero)
#       --version: a release other than the newest, as 0.7.5.3
set -eu

SOURCE=${ZERO_RELEASES_URL:-https://github.com/jody0731/zero}
SOURCE=${SOURCE%/}
BIN=""
DIR=""
VERSION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --bin | --dir | --version)
            if [ $# -lt 2 ]; then
                echo "install.sh: $1 needs a value" >&2
                exit 2
            fi
            case "$1" in
                --bin) BIN=$2 ;;
                --dir) DIR=$2 ;;
                --version) VERSION=$2 ;;
            esac
            shift 2
            ;;
        -h | --help)
            sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "install.sh: unknown option $1 (see --help)" >&2
            exit 2
            ;;
    esac
done

case "$(uname -s)" in
    Linux) MACHINE="$(uname -m)-linux" ;;
    *)
        echo "ZERO runs on Linux for now; this machine is $(uname -s)." >&2
        exit 1
        ;;
esac

# What the installer and ZERO need, all of it said at once; the package names
# are Debian's and Ubuntu's
missing=""
if command -v curl >/dev/null 2>&1; then
    FETCH=curl
elif command -v wget >/dev/null 2>&1; then
    FETCH=wget
else
    missing="$missing curl"
fi
command -v tar >/dev/null 2>&1 || missing="$missing tar"
command -v gzip >/dev/null 2>&1 || missing="$missing gzip"
if command -v sha256sum >/dev/null 2>&1; then
    SUM="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    SUM="shasum -a 256"
else
    missing="$missing coreutils"
fi
command -v bwrap >/dev/null 2>&1 || missing="$missing bubblewrap"
if [ -n "$missing" ]; then
    echo "ZERO needs:$missing" >&2
    echo "On Debian or Ubuntu: sudo apt install$missing" >&2
    exit 1
fi

# A file to a path
fetch() {
    if [ "$FETCH" = curl ]; then
        curl -fsSL --retry 2 -o "$2" "$1"
    else
        wget -q -O "$2" "$1"
    fi
}

# Where a URL leads, its redirects followed
lands() {
    if [ "$FETCH" = curl ]; then
        curl -fsSL -o /dev/null -w '%{url_effective}' "$1"
    else
        wget -q -O /dev/null --server-response "$1" 2>&1 |
            sed -n 's/^ *[Ll]ocation: *//p' | tail -n 1 | tr -d '\r'
    fi
}

# The newest release: where releases/latest leads, a tag page
if [ -z "$VERSION" ]; then
    if ! page=$(lands "$SOURCE/releases/latest"); then
        echo "ZERO's installer couldn't reach $SOURCE." >&2
        exit 1
    fi
    VERSION=${page##*/tag/v}
fi
case "$VERSION" in
    "" | *[!0-9.]*)
        echo "ZERO's installer found no release at $SOURCE ($VERSION)." >&2
        exit 1
        ;;
esac

NAME="zero-$VERSION-$MACHINE.tar.gz"
URL="$SOURCE/releases/download/v$VERSION/$NAME"
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT
trap 'exit 1' INT TERM
echo "Fetching $NAME from $SOURCE."
if ! fetch "$URL.sha256" "$SCRATCH/$NAME.sha256"; then
    echo "ZERO $VERSION has no archive for this machine ($MACHINE)." >&2
    exit 1
fi
if ! fetch "$URL" "$SCRATCH/$NAME"; then
    echo "The download of $NAME didn't finish; nothing was installed." >&2
    exit 1
fi
expected=$(cut -d ' ' -f 1 "$SCRATCH/$NAME.sha256")
actual=$(cd "$SCRATCH" && $SUM "$NAME" | cut -d ' ' -f 1)
if [ "$expected" != "$actual" ]; then
    echo "$NAME's sha256 isn't the one its release states; nothing was" \
        "installed." >&2
    exit 1
fi
mkdir "$SCRATCH/unpacked"
tar -xzf "$SCRATCH/$NAME" -C "$SCRATCH/unpacked"
RELEASE="$SCRATCH/unpacked/zero-$VERSION"
if [ ! -x "$RELEASE/bin/zero" ]; then
    echo "$NAME isn't a release archive of ZERO; nothing was installed." >&2
    exit 1
fi

# The release installs itself: copied into the install folder, tried, made
# current, the zero command written; then zero doctor
set -- install
if [ -n "$BIN" ]; then
    set -- "$@" --bin "$BIN"
fi
if [ -n "$DIR" ]; then
    set -- "$@" --dir "$DIR"
fi
"$RELEASE/bin/zero" "$@"
