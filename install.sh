#!/bin/sh
# Z.E.R.O.'s installer, run from the clone it installs. It checks what ZERO
# needs, checks out the newest release, makes the clone's Python environment
# (.venv/), installs the requirements, writes the zero command and tries what
# ZERO needs from the machine (zero doctor). Run again, it does the same, which
# repairs an install. ZERO's home (~/.zero) is made on
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

# The Python ZERO runs on: $ZERO_PYTHON, else a free-threaded one on the PATH
# (its sessions then run side by side on every core), else python3
PYTHON=${ZERO_PYTHON:-}
if [ -z "$PYTHON" ]; then
    for candidate in python3.16t python3.15t python3.14t python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON=$candidate
            break
        fi
    done
fi

# What ZERO needs, all of it said at once; the package names are Debian's and
# Ubuntu's
missing=""
command -v git >/dev/null 2>&1 || missing="$missing git"
command -v bwrap >/dev/null 2>&1 || missing="$missing bubblewrap"
if [ -z "$PYTHON" ] || ! command -v "$PYTHON" >/dev/null 2>&1; then
    missing="$missing python3"
elif ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "ZERO needs Python 3.11 or newer; $PYTHON is $("$PYTHON" -V 2>&1)." >&2
    exit 1
elif ! "$PYTHON" -c 'import ensurepip, venv' >/dev/null 2>&1; then
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

# The environment made again when it was made with another Python (a
# free-threaded one found since, or ZERO_PYTHON set)
# A Python's version and whether it is free-threaded
kind='import sys, sysconfig
print(sys.version_info[:2], sysconfig.get_config_var("Py_GIL_DISABLED"))'
want=$("$PYTHON" -c "$kind")
if [ -x "$ROOT/.venv/bin/python" ]; then
    have=$("$ROOT/.venv/bin/python" -c "$kind" 2>/dev/null || echo none)
    if [ "$have" != "$want" ]; then
        echo "ZERO's environment is made again with $PYTHON ($("$PYTHON" -V 2>&1))."
        rm -rf "$ROOT/.venv"
    fi
fi
if [ ! -x "$ROOT/.venv/bin/python" ]; then
    "$PYTHON" -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r "$ROOT/requirements.txt"

if [ -n "$BIN" ]; then
    "$ROOT/.venv/bin/python" "$ROOT/apps/cli/main.py" install --bin "$BIN"
else
    "$ROOT/.venv/bin/python" "$ROOT/apps/cli/main.py" install
fi

# What ZERO needs from this machine, tried; a need not met is said with its
# fix, and ZERO stays installed
"$ROOT/.venv/bin/python" "$ROOT/doctor.py" || :
