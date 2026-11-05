#!/bin/sh
# Z.E.R.O.'s installer, run from the clone it installs. It checks what ZERO
# needs, checks out the newest release, makes the clone's Python environment
# (.venv/), installs the requirements and writes the zero command. Run again,
# it does the same, which repairs an install. ZERO's home (~/.zero) is made on
# first use and never touched here.
#
#   install.sh [--bin DIR]    DIR: where the zero command goes (~/.local/bin)
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd -P)
BIN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --bin)
            if [ $# -lt 2 ]; then
                echo "install.sh: --bin needs a folder" >&2
                exit 2
            fi
            BIN=$2
            shift 2
            ;;
        -h | --help)
            sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "install.sh: unknown option $1 (see --help)" >&2
            exit 2
            ;;
    esac
done

# What ZERO needs, all of it said at once; the package names are Debian's and
# Ubuntu's
missing=""
command -v git >/dev/null 2>&1 || missing="$missing git"
command -v bwrap >/dev/null 2>&1 || missing="$missing bubblewrap"
if ! command -v python3 >/dev/null 2>&1; then
    missing="$missing python3"
elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "ZERO needs Python 3.11 or newer; python3 is $(python3 -V 2>&1)." >&2
    exit 1
elif ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
    missing="$missing python3-venv"
fi
if [ -n "$missing" ]; then
    echo "ZERO needs:$missing" >&2
    echo "On Debian or Ubuntu: sudo apt install$missing" >&2
    exit 1
fi

# The newest release: tags are v and dotted numbers
if [ -d "$ROOT/.git" ]; then
    latest=$(git -C "$ROOT" tag --list 'v[0-9]*' --sort=-v:refname | head -n 1)
    if [ -n "$latest" ]; then
        git -C "$ROOT" checkout --quiet --detach "$latest"
        echo "ZERO ${latest#v}"
    fi
fi

if [ ! -x "$ROOT/.venv/bin/python" ]; then
    python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r "$ROOT/requirements.txt"

if [ -n "$BIN" ]; then
    "$ROOT/.venv/bin/python" "$ROOT/apps/cli/main.py" install --bin "$BIN"
else
    "$ROOT/.venv/bin/python" "$ROOT/apps/cli/main.py" install
fi
